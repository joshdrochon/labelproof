"""Security middleware for the public prototype URL (SEC-4, SEC-6, SEC-9).

`install_middleware` is called once, by `api.security.harden`, and adds two layers in the
order they must run. Starlette builds the stack so the **last** middleware added is the
**outermost**, which is why this list reads inside-out:

| Added | Position | Job |
|---|---|---|
| `StrictCORSMiddleware` | innermost | Refuse a foreign-origin write before the route runs |
| `RateLimitMiddleware`  | | Refuse a flood before any work happens |
| `SecurityHeadersMiddleware` | outermost | Put the policy on every response, errors too |

**Headers are outermost** so a refusal is as hardened as a success: the responses an
attacker is most likely to be provoking are the error paths, and a header policy with holes
in them is a policy attackers read the error paths for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.middleware.cors import StrictCORSMiddleware
from api.middleware.headers import SecurityHeadersMiddleware
from api.middleware.ratelimit import RateLimitMiddleware

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from api.security import SecurityPolicy


def install_middleware(app: FastAPI, policy: SecurityPolicy) -> None:
    """Add the security layers to `app`. Call after the app's own middleware."""
    app.add_middleware(StrictCORSMiddleware, allowed_origins=policy.allowed_origins)
    app.add_middleware(
        RateLimitMiddleware,
        per_minute=policy.rate_limit_per_minute,
        ip_header=policy.client_ip_header,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=policy.hsts)


__all__ = [
    "RateLimitMiddleware",
    "SecurityHeadersMiddleware",
    "StrictCORSMiddleware",
    "install_middleware",
]
