"""Strict same-origin policy (SEC-6, LP-082).

This app serves its React SPA from the same origin as its API — one container, one URL
(pinned build decision) — so there is no legitimate cross-origin caller and "strict CORS" here means
genuinely strict, not permissive with an apology in a comment.

Three rules:

1. **No `Access-Control-Allow-Origin` is ever emitted** unless the origin is in
   `LABELPROOF_ALLOWED_ORIGINS`, which is empty by default. Wildcards are not accepted as
   a value; an allowlist that reads `*` is not an allowlist.
2. **A preflight from a disallowed origin is answered 403**, in the error taxonomy, rather
   than being handed to the router to 405 on.
3. **A non-safe method carrying a foreign `Origin` is refused 403** rather than executed.
   This is the rule that does real work. A browser blocks the *read* of a cross-origin
   `POST /verify` response, but without this the server has already ingested the images and
   spent the model call by the time the browser refuses to show the answer.

Requests with **no** `Origin` header are allowed through: curl, the deploy smoke test, and
anything server-to-server send none, and refusing them would break the ops path to protect
a browser that is not involved.

Development note: `npm run dev` proxies from `http://localhost:5173`, which is a foreign
origin to an API on `:8000`. That configuration needs
`LABELPROOF_ALLOWED_ORIGINS=http://localhost:5173`. It has no bearing on the deployed
single-origin container.
"""

from __future__ import annotations

from api import errors
from api.middleware.asgi import (
    SAFE_METHODS,
    ASGIApp,
    Message,
    Receive,
    Scope,
    Send,
    header,
    request_origin,
    send_error,
)

#: Echoed on an allowed preflight. Deliberately not `*` — a browser is told exactly what is
#: permitted, and anything else is a change someone has to make on purpose.
ALLOWED_METHODS = "GET, HEAD, POST, OPTIONS"
ALLOWED_HEADERS = "Content-Type, X-Request-ID"
PREFLIGHT_MAX_AGE = "600"


def cross_origin_refused() -> errors.UserError:
    return errors.UserError(
        "This tool only accepts requests from its own web page. Open the verification "
        "page and submit the label from there — nothing has been checked.",
        next_step="navigate",
        code="cross_origin_refused",
    )


class StrictCORSMiddleware:
    """Same-origin by default; an explicit allowlist is the only way out of it."""

    def __init__(self, app: ASGIApp, *, allowed_origins: frozenset[str] = frozenset()):
        self.app = app
        self.allowed_origins = frozenset(
            origin.strip().rstrip("/").lower()
            for origin in allowed_origins
            if origin.strip() and origin.strip() != "*"
        )

    def _permitted(self, scope: Scope, origin: str) -> bool:
        normalised = origin.strip().rstrip("/").lower()
        if not normalised:
            return True
        if normalised == request_origin(scope):
            return True
        return normalised in self.allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        origin = header(scope, "origin")
        method = str(scope.get("method") or "GET").upper()
        permitted = self._permitted(scope, origin)

        if method == "OPTIONS" and header(scope, "access-control-request-method"):
            if not permitted:
                await send_error(send, cross_origin_refused(), status=403)
                return
            await self._preflight(send, scope, origin)
            return

        if not permitted and method not in SAFE_METHODS:
            await send_error(send, cross_origin_refused(), status=403)
            return

        # A foreign origin on a safe method runs, but gets no ACAO header — so the browser
        # blocks the read, which is the default the platform already guarantees. The only
        # header added here is `Vary: Origin`, so a shared cache can never serve a response
        # shaped for one origin to another.
        allow = origin if (origin and permitted and origin.lower() != request_origin(scope)) else ""

        async def wrapped(message: Message) -> None:
            if message.get("type") == "http.response.start":
                raw: list[tuple[bytes, bytes]] = list(message.get("headers") or [])
                raw.append((b"vary", b"Origin"))
                if allow:
                    raw.append((b"access-control-allow-origin", allow.encode("latin-1")))
                message["headers"] = raw
            await send(message)

        await self.app(scope, receive, wrapped)

    async def _preflight(self, send: Send, scope: Scope, origin: str) -> None:
        """Answer the preflight directly. The router has nothing useful to say about one."""
        headers = [
            (b"content-length", b"0"),
            (b"vary", b"Origin"),
            (b"access-control-allow-methods", ALLOWED_METHODS.encode("latin-1")),
            (b"access-control-allow-headers", ALLOWED_HEADERS.encode("latin-1")),
            (b"access-control-max-age", PREFLIGHT_MAX_AGE.encode("latin-1")),
        ]
        if origin and origin.lower().rstrip("/") != request_origin(scope):
            headers.append(
                (b"access-control-allow-origin", origin.encode("latin-1"))
            )
        await send({"type": "http.response.start", "status": 204, "headers": headers})
        await send({"type": "http.response.body", "body": b""})
