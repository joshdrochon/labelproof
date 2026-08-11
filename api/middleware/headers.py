"""Security response headers on every response, without exception (SEC-6, LP-082).

Installed as the outermost user middleware, which is what lets it cover the responses that
never reach a route — the oversize-upload 413 the app factory short-circuits, a 429 from the
rate limiter, a 500 from the containment layer. A header policy with holes in the error
paths is a header policy an attacker reads the error paths for.

The header values themselves, and the reasoning behind each relaxation, live in
`api/security.py` so the whole posture reads in one place. This module is the mechanism.
"""

from __future__ import annotations

from api.middleware.asgi import (
    ASGIApp,
    Message,
    Receive,
    Scope,
    Send,
    request_scheme,
)
from api.security import (
    BASE_HEADERS,
    DEFAULT_CACHE_CONTROL,
    HSTS_VALUE,
    IMMUTABLE_CACHE_CONTROL,
    IMMUTABLE_PREFIX,
)


class SecurityHeadersMiddleware:
    """Adds the policy headers, and never overwrites one a route set deliberately."""

    def __init__(self, app: ASGIApp, *, hsts: bool = True):
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        additions = dict(BASE_HEADERS)

        # Over plaintext a browser must ignore HSTS (RFC 6797 §8.1), so sending it there
        # would only mislead whoever reads the response locally. LP-083 owns the
        # platform-side redirect; this is the app-side header it pairs with.
        if self.hsts and request_scheme(scope) == "https":
            additions["Strict-Transport-Security"] = HSTS_VALUE

        path = str(scope.get("path") or "/")
        additions["Cache-Control"] = (
            IMMUTABLE_CACHE_CONTROL
            if path.startswith(IMMUTABLE_PREFIX)
            else DEFAULT_CACHE_CONTROL
        )

        async def wrapped(message: Message) -> None:
            if message.get("type") == "http.response.start":
                _apply(message, additions)
            await send(message)

        await self.app(scope, receive, wrapped)


def _apply(message: Message, additions: dict[str, str]) -> None:
    """Merge headers into a `http.response.start` message.

    Existing values win. A route that set `Cache-Control` or `Content-Disposition` for the
    CSV export knew something this middleware does not, and silently overriding it is how a
    download turns into a blank page.
    """
    raw: list[tuple[bytes, bytes]] = list(message.get("headers") or [])
    present = {key.lower() for key, _ in raw}
    for name, value in additions.items():
        encoded = name.lower().encode("latin-1")
        if encoded in present:
            continue
        raw.append((encoded, value.encode("latin-1")))
    message["headers"] = raw
