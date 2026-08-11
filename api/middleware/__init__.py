"""Security middleware for the public prototype URL (SEC-4, SEC-6, SEC-9).

`install_middleware` is called once, by `api.security.harden`, and adds four layers in the
order they must run. Starlette builds the stack so the **last** middleware added is the
**outermost**, which is why this list reads inside-out:

| Added | Position | Job |
|---|---|---|
| `StrictCORSMiddleware` | innermost | Refuse a foreign-origin write before the route runs |
| `RateLimitMiddleware`  | | Refuse a flood before any work happens |
| `ExceptionContainmentMiddleware` | | Catch everything; no traceback reaches stdout |
| `SecurityHeadersMiddleware` | outermost | Put the policy on every response, errors too |

**Headers are outermost, and containment sits just inside them.** That ordering was chosen
after getting it the other way round: with containment outside, its own 500 response is
written straight to the server and never passes through the header wrapper, so the one
response an attacker is most likely to be provoking would have been the only unhardened one
in the app. Containment still intercepts before Starlette's `ServerErrorMiddleware` — which
calls the registered handler and then re-raises into uvicorn's default traceback handler
(see `containment.py`) — because `ServerErrorMiddleware` is outside the entire user stack
either way. The headers layer does nothing but merge a fixed dict into a response start
message, so keeping it outside containment does not put an unguarded exception path above it
in any meaningful sense.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.middleware.containment import ExceptionContainmentMiddleware
from api.middleware.cors import StrictCORSMiddleware
from api.middleware.headers import SecurityHeadersMiddleware
from api.middleware.ratelimit import RateLimitMiddleware

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from api.security import SecurityPolicy


def install_middleware(app: FastAPI, policy: SecurityPolicy) -> None:
    """Add the four security layers to `app`. Call after the app's own middleware."""
    app.add_middleware(StrictCORSMiddleware, allowed_origins=policy.allowed_origins)
    app.add_middleware(
        RateLimitMiddleware,
        per_minute=policy.rate_limit_per_minute,
        ip_header=policy.client_ip_header,
    )
    app.add_middleware(ExceptionContainmentMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=policy.hsts)


__all__ = [
    "ExceptionContainmentMiddleware",
    "RateLimitMiddleware",
    "SecurityHeadersMiddleware",
    "StrictCORSMiddleware",
    "install_middleware",
]
