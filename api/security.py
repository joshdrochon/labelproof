"""The security posture, in one place (SEC-4, SEC-6, SEC-9).

`harden(app, config)` is the whole front door. One call from the app factory installs the
rate limiter, the response headers, the strict same-origin CORS rule, the exception
containment layer, process-wide traceback containment, and the retention sweeper. One call
rather than six because the posture is a single thing a reviewer should be able to read end
to end, and because every extra wiring line in `api/main.py` is another chance to merge half
of it.

**Order is load-bearing.** Starlette builds the user middleware stack so the *last*
middleware added is the *outermost*, and `harden` is called after the app factory's own
middleware. That is what makes three things true:

* Security headers land on every response, including the ones an inner middleware
  short-circuits (the oversize-upload 413 never reaches a route).
* The rate limiter refuses a flood before any work happens.
* The containment middleware catches an exception **before** Starlette's
  `ServerErrorMiddleware`, which calls the registered handler and then re-raises
  unconditionally — and that re-raise is what puts a full traceback on stdout through
  uvicorn's default handler, carrying whatever label text the exception message holds.

That last one is the reason this module exists at all. `api/logging.py` allowlists field
names and raises on anything else, so nothing can be logged deliberately. Nothing about that
governs a traceback, which is why traceback containment is a second, separate layer here.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import types
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


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

    #: Replace tracebacks with a scrubbed line everywhere in the process (SEC-4).
    contain_tracebacks: bool = True

    #: Retention (SEC-2). `ttl_hours` is the policy; `sweep_seconds` is how often the timer
    #: checks, so the real worst-case artefact lifetime is the sum of the two.
    retention_ttl_hours: int = 24
    retention_sweep_seconds: int = 900
    storage_dir: str = "./.data"

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
            contain_tracebacks=not _env_flag("LABELPROOF_DEBUG_TRACEBACKS", False),
            retention_ttl_hours=(config.retention_hours if config is not None else 24),
            retention_sweep_seconds=_env_int("LABELPROOF_RETENTION_SWEEP_SECONDS", 900),
            storage_dir=(config.storage_dir if config is not None else "./.data"),
        )


# --- traceback containment ----------------------------------------------------------------
#
# `api/logging.py` makes it impossible to log label content on purpose. Nothing there governs
# an *accident*: a traceback formatted by uvicorn, by `threading.excepthook`, or by asyncio's
# default handler bypasses the allowlist entirely, and exception messages in this codebase
# carry label text for real — a pydantic `ValidationError` renders `input_value=...`, which
# on the extraction path is the label, and on `BatchStore.claim()` is the application's brand
# name and producer address raised on a worker thread.
#
# The hook used is `logging.setLogRecordFactory`, because it is the one place every logger
# and every handler in the process funnels through. Filters were the obvious alternative and
# do not work here: a filter attached to a `Logger` does not run for records propagated from
# a child, and uvicorn sets `propagate=False` on its own loggers — so a root filter would
# have missed precisely the leak being closed.

_SCRUBBED = "%s suppressed: traceback withheld (SEC-4)"

_original_record_factory: Any = None
_original_excepthook: Any = None
_original_thread_excepthook: Any = None
_containment_lock = threading.Lock()


def _exception_name(exc_info: object) -> str:
    if isinstance(exc_info, tuple) and exc_info and isinstance(exc_info[0], type):
        return exc_info[0].__name__
    if isinstance(exc_info, BaseException):
        return type(exc_info).__name__
    return "Exception"


def install_log_containment() -> None:
    """Strip tracebacks from every log record created anywhere in this process.

    Idempotent. The record keeps its logger, level and timestamp — only the traceback and
    the message that would have carried it are replaced, and the replacement names the
    exception class so a developer still knows what to reproduce. Log lines with no
    `exc_info` are untouched, which is what keeps uvicorn's startup and bind lines readable.
    """
    global _original_record_factory, _original_excepthook, _original_thread_excepthook
    with _containment_lock:
        if _original_record_factory is not None:
            return

        base = logging.getLogRecordFactory()
        _original_record_factory = base

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            # LogRecord's positional contract:
            #   (name, level, pathname, lineno, msg, args, exc_info, func, sinfo)
            values = list(args)
            exc_info = values[6] if len(values) > 6 else kwargs.get("exc_info")
            sinfo = values[8] if len(values) > 8 else kwargs.get("sinfo")
            if exc_info or sinfo:
                name = _exception_name(exc_info)
                if len(values) > 4:
                    values[4] = _SCRUBBED
                else:
                    kwargs["msg"] = _SCRUBBED
                if len(values) > 5:
                    values[5] = (name,)
                else:
                    kwargs["args"] = (name,)
                if len(values) > 6:
                    values[6] = None
                else:
                    kwargs["exc_info"] = None
                if len(values) > 8:
                    values[8] = None
                else:
                    kwargs["sinfo"] = None
            record = base(*values, **kwargs)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return record

        logging.setLogRecordFactory(factory)

        _original_excepthook = sys.excepthook
        _original_thread_excepthook = threading.excepthook
        sys.excepthook = _scrubbed_excepthook
        threading.excepthook = _scrubbed_thread_excepthook


def remove_log_containment() -> None:
    """Put the process back exactly as it was. Used by tests and by nothing else."""
    global _original_record_factory, _original_excepthook, _original_thread_excepthook
    with _containment_lock:
        if _original_record_factory is None:
            return
        logging.setLogRecordFactory(_original_record_factory)
        sys.excepthook = _original_excepthook
        threading.excepthook = _original_thread_excepthook
        _original_record_factory = None
        _original_excepthook = None
        _original_thread_excepthook = None


def containment_active() -> bool:
    return _original_record_factory is not None


def _scrubbed_excepthook(
    kind: type[BaseException], value: BaseException, tb: types.TracebackType | None
) -> None:
    """Uncaught on the main thread. `sys.excepthook` prints straight to stderr otherwise."""
    applog.error(
        "unhandled_exception", kind="internal", code="internal_error", reason_code=kind.__name__
    )


def _scrubbed_thread_excepthook(args: threading.ExceptHookArgs) -> None:
    """Uncaught on a worker thread — the batch pool's leak path, and stderr-direct too."""
    applog.error(
        "unhandled_thread_exception",
        kind="internal",
        code="internal_error",
        reason_code=args.exc_type.__name__,
    )


# --- the front door ------------------------------------------------------------------------


def harden(app: FastAPI, config: Config | None = None) -> SecurityPolicy:
    """Install the whole posture. Call once, from the app factory, after its own middleware.

    Returns the resolved policy so a caller (or a test) can assert what was applied rather
    than infer it. Everything installed here is inert until this runs, so a deployment that
    forgets the call is unhardened but never broken — which is the right failure direction
    for a wiring mistake.

    **Calling it twice is a no-op, deliberately.** Middleware added a second time is not
    harmless here: two rate limiters in the stack means every request spends two tokens, so
    the 30/min ceiling silently becomes 15 and the first thing to break is the demo. Once
    the app factory installs the posture, a caller that installs it again — a test, a
    fixture, a second wiring line someone added in good faith — gets the policy back
    unchanged rather than a quietly halved budget.
    """
    from api.middleware import install_middleware

    existing: SecurityPolicy | None = getattr(app.state, "security_policy", None)
    if existing is not None:
        return existing

    policy = SecurityPolicy.from_config(config)

    if policy.contain_tracebacks:
        install_log_containment()

    install_middleware(app, policy)

    from api.retention import install_sweeper

    install_sweeper(app, policy)

    app.state.security_policy = policy
    applog.log("security_installed", count=policy.rate_limit_per_minute)
    return policy
