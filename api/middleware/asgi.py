"""Shared plumbing for the security middleware (LP-081, LP-082, LP-086).

These are pure-ASGI middleware rather than `BaseHTTPMiddleware` subclasses. The reason is
not style: `BaseHTTPMiddleware` runs the downstream app in a separate task connected by a
queue, which means an exception raised downstream surfaces in a different place than it was
raised and a rate-limit rejection still pays for a task spawn. The rate limiter must be able
to refuse a request before anything happens, and the containment layer must see exceptions
exactly where they leave the app, so both want the raw interface.

Everything here is small on purpose. A helper in the request path of every single request
against a 5-second gate should be readable in one screen.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any

from api import errors

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: Methods a browser will send cross-origin without a preflight and which change nothing
#: on the server. Anything outside this set carrying a foreign Origin is refused (LP-082).
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


def header(scope: Scope, name: str) -> str:
    """One request header, lower-cased name, decoded latin-1 as ASGI specifies."""
    wanted = name.lower().encode("latin-1")
    raw: Iterable[tuple[bytes, bytes]] = scope.get("headers") or ()
    for key, value in raw:
        if key == wanted:
            return value.decode("latin-1")
    return ""


def request_scheme(scope: Scope) -> str:
    """The scheme the *browser* used, not the one that reached this process.

    Fly terminates TLS at the edge, so `scope["scheme"]` is `http` on every request no
    matter what the user typed. `X-Forwarded-Proto` is what carries the truth there, and
    getting this wrong means HSTS is never emitted on the one deployment that needs it.
    """
    forwarded = header(scope, "x-forwarded-proto").split(",")[0].strip().lower()
    if forwarded in ("http", "https"):
        return forwarded
    scheme = str(scope.get("scheme") or "http").lower()
    return scheme if scheme in ("http", "https") else "http"


def request_origin(scope: Scope) -> str:
    """This request's own origin, as a browser would compute it. Lower-cased, no trailing /."""
    host = header(scope, "x-forwarded-host").split(",")[0].strip() or header(scope, "host")
    if not host:
        server = scope.get("server") or ("", 0)
        host = f"{server[0]}:{server[1]}" if server[1] else str(server[0])
    return f"{request_scheme(scope)}://{host}".strip().rstrip("/").lower()


def client_key(scope: Scope, ip_header: str) -> str:
    """Who to charge this request to. Socket peer unless an operator trusted a header.

    **`ip_header` is off by default, and turning it on is a security decision.** Whatever
    header it names is read from the request, so unless something between the client and
    this process *overwrites* that header on every request, the client controls it and can
    rotate it to get an unlimited number of buckets. Measured: 200 requests against a 3/min
    limit with a rotating header, 200 allowed, 0 refused. `api/security.py` logs a warning
    at startup whenever this is set, because a rate limiter that has silently failed open is
    worse than one that was never installed.

    Safe when the edge overwrites it: Fly (`fly-client-ip`), Cloudflare
    (`cf-connecting-ip`). **Not safe for `x-forwarded-for`**, which is the obvious thing to
    reach for behind nginx: it is an append-only chain, this function takes the leftmost
    entry, and the leftmost entry is whatever the client sent. Behind a proxy that appends,
    the value you want is the *rightmost trusted* hop, which this deliberately does not try
    to compute — getting that wrong is how header-based identity fails quietly.

    Even with an unspoofable header, per-IP limiting is per-*address*: anyone holding an
    IPv6 /64 (which is a single residential allocation) has 2^64 of them. This raises the
    cost of a flood; it does not make one impossible.
    """
    if ip_header:
        forwarded = header(scope, ip_header).split(",")[0].strip()
        if forwarded:
            return forwarded
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])
    return "unknown"


async def send_error(
    send: Send,
    error: errors.LabelProofError,
    *,
    status: int,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """Answer in the error taxonomy from inside a middleware.

    `status` is passed separately because two of the statuses this app needs — 429 and 403 —
    have no dedicated class in `api/errors.py`, and adding one there would mean editing a
    file this wave does not own. The *shape* is the taxonomy's, which is what the front end's
    single error renderer actually depends on (UX-6, OPS-5).
    """
    import json

    body = json.dumps(error.to_payload()).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("latin-1")),
    ]
    for key, value in (extra_headers or {}).items():
        headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))

    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
