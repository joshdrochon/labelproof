"""The security posture, in one place (SEC-4, SEC-6, SEC-9).

`harden(app, config)` is the whole front door. One call from the app factory installs the
rate limiter, the response headers, the strict same-origin CORS rule, the exception
containment layer, process-wide traceback containment, and the retention sweeper. One call
rather than six because the posture is a single thing a reviewer should be able to read end
to end, and because every extra wiring line in `api/main.py` is another chance to merge half
of it.

**Order is load-bearing.** Starlette builds the user middleware stack so the *last*
middleware added is the *outermost*, and `harden` is called after the app factory's own
middleware. That is what puts the security headers on every response, including the ones an
inner middleware short-circuits — the oversize-upload 413 never reaches a route, and a
response nobody routed is still a response an attacker can read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from api import logging as applog

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from fastapi import FastAPI

    from api.config import Config


# --- response headers ------------------------------------------------------------------

#: Content Security Policy for the single-page app FastAPI serves from its own origin.
#:
#: `script-src 'self'` is absolute: no inline script, no CDN, no `eval`. That is where XSS
#: lives and there is no relaxation of it anywhere in this file.
#:
#: `style-src` carries `'unsafe-inline'` and the reason is specific rather than habitual.
#: The evidence overlay positions each highlight box with a React inline `style` attribute
#: (`web/src/components/EvidenceOverlay.tsx`), and CSP3 blocks style *attributes* under a
#: bare `style-src 'self'`. The failure would be silent — every evidence box stacked at the
#: top-left of the label instead of over the text it cites — and a highlight pointing at the
#: wrong region is worse than no highlight in a product whose argument is honest evidence.
#: The residual risk is CSS injection, and React escapes every interpolated value while
#: `img-src` and `default-src 'none'` close the usual CSS exfiltration route.
#: `style-src-attr` was rejected as the narrower fix: browsers without it fall back to
#: `style-src`, so the breakage would depend on the reader's browser version.
#:
#: `img-src` allows `blob:` because the app previews uploads through `URL.createObjectURL`,
#: and `data:` for inline icons. `worker-src blob:` covers the off-main-thread encode path.
#:
#: Known casualty: FastAPI's `/docs` loads Swagger UI from cdn.jsdelivr.net with an inline
#: bootstrap script, so it renders blank under this policy. That is the correct outcome —
#: NET-1 exists so an agency network admin can allowlist this app from one table, and a
#: developer convenience page is not worth a CDN entry on it.
CONTENT_SECURITY_POLICY: str = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' blob: data:",
        "font-src 'self'",
        "connect-src 'self'",
        "media-src 'none'",
        "manifest-src 'self'",
        "worker-src 'self' blob:",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "upgrade-insecure-requests",
    )
)

#: Sent on every response regardless of scheme or path.
BASE_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    # Belt and braces with `frame-ancestors 'none'` for anything that predates CSP3.
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # A verdict URL should not travel to anywhere else in a Referer header.
    "Referrer-Policy": "no-referrer",
    # The app asks for no device capability. Denying them all makes that checkable.
    "Permissions-Policy": (
        "accelerometer=(), autoplay=(), camera=(), display-capture=(), "
        "encrypted-media=(), fullscreen=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), midi=(), payment=(), usb=()"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    # Search engines have no business indexing a verification result.
    "X-Robots-Tag": "noindex, nofollow",
}

#: One year, subdomains included. `preload` is deliberately absent: it is a commitment made
#: to browser vendors over a whole apex domain, and this prototype lives on a subdomain it
#: does not own. LP-083 owns the platform-side redirect; this is the app-side header.
HSTS_VALUE = "max-age=31536000; includeSubDomains"

#: Content-hashed by the bundler, so it is safe — and worth real money on the 5-second
#: gate — to let a browser keep it. Everything else is `no-store`, because a verdict body
#: carries extracted label text and an intermediary cache is retention nobody documented.
IMMUTABLE_PREFIX = "/assets/"
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
DEFAULT_CACHE_CONTROL = "no-store"


# --- policy ------------------------------------------------------------------------------


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SecurityPolicy:
    """Everything about the posture that an operator can move, and its defaults."""

    #: Requests per minute per client on `/verify` (SEC-9, BUILD.md §1 default 30).
    rate_limit_per_minute: int = 30

    #: Header naming the real client, checked before the socket peer.
    #:
    #: Defaults to Fly's, because on Fly every request arrives from the proxy and keying on
    #: the socket peer would put every user on earth in one 30/min bucket — which is the
    #: "throttled the demo into looking broken" failure, not abuse protection. Fly's proxy
    #: sets and overwrites this header, so on the deployment it is authoritative. Off Fly
    #: with no proxy it is spoofable; set this to "" there to key on the socket peer.
    client_ip_header: str = "fly-client-ip"

    #: Cross-origin browsers allowed to talk to this API. Empty means same-origin only,
    #: which is the shipping configuration — the SPA is served from this very origin.
    allowed_origins: frozenset[str] = frozenset()

    #: Emit HSTS on requests we can see are HTTPS.
    hsts: bool = True

    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: Config | None = None) -> SecurityPolicy:
        """Build the policy from validated config plus this module's own environment.

        The rate-limit ceiling, the retention TTL and the storage directory come from
        `api/config.py`, which already validates them. The handful of knobs that exist only
        for this module are read here rather than pushed into a file another wave owns.
        """
        header = os.environ.get("LABELPROOF_CLIENT_IP_HEADER")
        origins = frozenset(
            origin.strip().rstrip("/").lower()
            for origin in os.environ.get("LABELPROOF_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        )
        return cls(
            rate_limit_per_minute=(
                config.rate_limit_per_minute if config is not None else 30
            ),
            client_ip_header=(
                "fly-client-ip" if header is None else header.strip().lower()
            ),
            allowed_origins=origins,
            hsts=_env_flag("LABELPROOF_HSTS", True),
        )


# --- the front door ------------------------------------------------------------------------


def harden(app: FastAPI, config: Config | None = None) -> SecurityPolicy:
    """Install the whole posture. Call once, from the app factory, after its own middleware.

    Returns the resolved policy so a caller (or a test) can assert what was applied rather
    than infer it. Everything installed here is inert until this runs, so a deployment that
    forgets the call is unhardened but never broken — which is the right failure direction
    for a wiring mistake.
    """
    from api.middleware import install_middleware

    policy = SecurityPolicy.from_config(config)
    install_middleware(app, policy)

    app.state.security_policy = policy
    applog.log("security_installed", count=policy.rate_limit_per_minute)
    return policy
