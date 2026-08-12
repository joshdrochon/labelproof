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
#: `style-src 'self'` with **no** `'unsafe-inline'`, and that was checked in a browser
#: rather than reasoned about. An earlier version of this file relaxed it, on the theory
#: that the evidence overlay positions each highlight with a React inline `style` attribute
#: (`web/src/components/EvidenceOverlay.tsx`) and CSP3 blocks style attributes. That theory
#: was wrong: react-dom applies the `style` prop through `node.style.setProperty`, which is
#: a CSSOM mutation, and CSP governs style attributes *parsed from markup* and `<style>`
#: elements — not the CSSOM. Verified against the real built SPA under this exact policy:
#: an injected `<style>` element and an inline `<script>` were both refused
#: (`style-src-elem`, `script-src-elem`, so enforcement is real), while the evidence box
#: still computed to `left: 30.4px` and rendered over the brand name it cites.
#: Nothing in `web/src` needs the relaxation — two `style={{...}}` props, both in
#: EvidenceOverlay, both CSSOM. There is now no relaxation anywhere in this policy.
#:
#: `img-src` allows `blob:` because the app previews uploads through `URL.createObjectURL`,
#: and `data:` for inline icons. `worker-src blob:` covers the off-main-thread encode path.
#:
#: Known casualty: FastAPI's `/docs` loads Swagger UI from cdn.jsdelivr.net with an inline
#: bootstrap script, so it renders blank under this policy. That is the correct outcome —
#: NET-1 exists so an agency network admin can allowlist this app from one table, and a
#: developer convenience page is not worth a CDN entry on it.
CONTENT_SECURITY_POLICY: str = "; ".join(  # noqa: FLY002 - one directive per line, reviewably
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
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

    #: Requests per minute per client on `/verify` (SEC-9, the build spec default 30).
    rate_limit_per_minute: int = 30

    #: Header naming the real client, checked before the socket peer. **Empty by default.**
    #:
    #: An earlier version defaulted to `fly-client-ip`, reasoning that on Fly the socket peer
    #: is the proxy and keying on it would put every user in one bucket. That reasoning is
    #: right about Fly and catastrophic everywhere else: a trusted header the client supplies
    #: is a header the client can rotate, and rate limiting **fails open** — measured at
    #: 200 requests against a 3/min limit with a rotating header, 200 allowed, 0 refused.
    #:
    #: Failing open is the wrong direction for a control. So the safe default is the socket
    #: peer, which always works and can never be spoofed, and trusting a header is an
    #: explicit operator decision that logs a warning at startup (`_warn_about_identity`).
    #: Fly deployments set `LABELPROOF_CLIENT_IP_HEADER=fly-client-ip`, which is sound there
    #: because Fly's proxy overwrites the header on every request.
    client_ip_header: str = ""

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
        header = os.environ.get("LABELPROOF_CLIENT_IP_HEADER", "")
        origins = frozenset(
            origin.strip().rstrip("/").lower()
            for origin in os.environ.get("LABELPROOF_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        )
        return cls(
            rate_limit_per_minute=(
                config.rate_limit_per_minute if config is not None else 30
            ),
            client_ip_header=header.strip().lower(),
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
_installed_factory: Any = None
_original_excepthook: Any = None
_original_thread_excepthook: Any = None
_containment_lock = threading.Lock()


def _exception_name(exc_info: object) -> str:
    if isinstance(exc_info, tuple) and exc_info and isinstance(exc_info[0], type):
        return exc_info[0].__name__
    if isinstance(exc_info, BaseException):
        return type(exc_info).__name__
    return "Exception"


def _safe_arg(value: object, path: frozenset[int] = frozenset()) -> object:
    """Replace an exception passed as a log argument with its class name.

    This is the gap the traceback scrubbing alone did not close, and it is the most
    ordinary way in the world to write a log line:

        logger.error("extraction failed: %s", exc)

    No `exc_info`, so nothing about that record looks like a traceback — and `str(exc)` on a
    pydantic `ValidationError` is `input_value=...`, which on the extraction path is the
    label.

    **Containers are walked, not just the top level.** I claimed this was unconditional
    after handling `dict`, and it was not: a list or tuple of exceptions went through
    untouched, and

        logger.error("failures: %s", errors)

    with a list of per-item exceptions is an ordinary batch-worker line — `WorkerPool`
    collects exactly that, one per failed item, each carrying a brand name.

    **The recursion is bounded by cycle detection, not by a depth cap.** My first attempt
    capped it at four levels and returned the value untouched below that, which is a leak
    wearing a safety belt: `[exc, [exc, [exc, …]]]` printed in full past the bound. A depth
    cap has to fail open at the bottom to be useful, and failing open is what this function
    exists to prevent. Tracking the current path terminates on genuine cycles and never
    stops looking on the way down.
    """
    if isinstance(value, BaseException):
        return f"<{type(value).__name__}>"
    if not isinstance(value, (dict, list, tuple, set, frozenset)):
        return value

    marker = id(value)
    if marker in path:
        return "<cycle>"
    inner = path | {marker}

    if isinstance(value, dict):
        # `logger.info("%(what)s", {"what": exc})`. `LogRecord.__init__` unwraps a lone
        # mapping argument, so by the time anything else sees it the exception is nested one
        # level down — which is how that vector got past the first version.
        return {key: _safe_arg(item, inner) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_safe_arg(item, inner) for item in value)
    # Sets are rebuilt as lists: the scrubbed placeholders are strings and would collapse
    # two exceptions of the same class into one member, which is a lie about how many
    # things failed.
    return [_safe_arg(item, inner) for item in value]


def _safe_args(args: object) -> object:
    if isinstance(args, BaseException):
        return (f"<{type(args).__name__}>",)
    if isinstance(args, tuple):
        return tuple(_safe_arg(item) for item in args)
    if isinstance(args, dict):
        return {key: _safe_arg(item) for key, item in args.items()}
    return args


def install_log_containment() -> None:
    """Strip tracebacks and exception objects from every log record in this process.

    Self-healing rather than merely idempotent: if something else replaced the record
    factory after this ran — another library, another guard, a test — the containment is
    re-wrapped on top of whatever is there now. A control that can be silently switched off
    by an unrelated import is not a control.

    Records with no `exc_info` keep their message, which is what leaves uvicorn's startup
    and bind lines readable. What they do not keep is an exception object passed as an
    argument; see `_safe_arg`.
    """
    global _original_record_factory, _installed_factory
    global _original_excepthook, _original_thread_excepthook
    with _containment_lock:
        current = logging.getLogRecordFactory()
        if _installed_factory is not None and current is _installed_factory:
            return

        # First install captures the pristine factory. A re-install after a hijack wraps
        # whatever is now in place, and deliberately does NOT overwrite the original —
        # `remove_log_containment` must still be able to restore the process as it was.
        if _original_record_factory is None:
            _original_record_factory = current
        base = current

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            # LogRecord's positional contract:
            #   (name, level, pathname, lineno, msg, args, exc_info, func, sinfo)
            values = list(args)

            def field(index: int, name: str) -> object:
                return values[index] if len(values) > index else kwargs.get(name)

            def put(index: int, name: str, value: object) -> None:
                if len(values) > index:
                    values[index] = value
                else:
                    kwargs[name] = value

            exc_info = field(6, "exc_info")
            sinfo = field(8, "sinfo")

            if exc_info:
                # The traceback channel: message and all, because an exception log line
                # usually interpolates the exception it is reporting.
                put(4, "msg", _SCRUBBED)
                put(5, "args", (_exception_name(exc_info),))
                put(6, "exc_info", None)
                put(8, "sinfo", None)
            else:
                # Everything else keeps its message. A `stack_info=True` record is not an
                # exception report — it is someone asking "how did we get here" — so
                # destroying its message and naming an exception that never happened was
                # simply wrong. Drop the stack, keep the line.
                if sinfo:
                    put(8, "sinfo", None)
                msg = field(4, "msg")
                if isinstance(msg, BaseException):
                    put(4, "msg", f"<{type(msg).__name__}>")
                put(5, "args", _safe_args(field(5, "args")))

            record = base(*values, **kwargs)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return record

        logging.setLogRecordFactory(factory)
        _installed_factory = factory

        if _original_excepthook is None:
            _original_excepthook = sys.excepthook
            _original_thread_excepthook = threading.excepthook
        sys.excepthook = _scrubbed_excepthook
        threading.excepthook = _scrubbed_thread_excepthook


def remove_log_containment() -> None:
    """Put the process back exactly as it was. Used by tests and by nothing else."""
    global _original_record_factory, _installed_factory
    global _original_excepthook, _original_thread_excepthook
    with _containment_lock:
        if _original_record_factory is None:
            return
        logging.setLogRecordFactory(_original_record_factory)
        sys.excepthook = _original_excepthook
        threading.excepthook = _original_thread_excepthook
        _original_record_factory = None
        _installed_factory = None
        _original_excepthook = None
        _original_thread_excepthook = None


def containment_installed() -> bool:
    """Was containment ever installed in this process? (As distinct from still being on.)"""
    return _installed_factory is not None


def containment_active() -> bool:
    """Is containment actually in force *right now*?

    Reads the live record factory rather than a module flag. The flag version returned True
    after any library installed its own factory over the top, which is the worst possible
    answer from a function whose entire job is to tell you whether a security control is on.

    Called every sweep by `api.retention._watch_traceback_containment`. That caller is the
    point: without one, "self-healing" named a capability with no trigger, and a factory
    installed by any import after startup would have switched containment off for the life
    of the process with nothing to notice.
    """
    return _installed_factory is not None and logging.getLogRecordFactory() is _installed_factory


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

#: Headers whose value the *edge proxy* sets and overwrites, so a client cannot forge them.
#: Anything outside this set is client-supplied on at least one hop and trusting it turns
#: the rate limiter off for anyone who notices.
_OVERWRITTEN_BY_A_KNOWN_PROXY: frozenset[str] = frozenset({"fly-client-ip", "cf-connecting-ip"})


def _warn_about_identity(policy: SecurityPolicy) -> None:
    """Say out loud, at startup, when the rate limiter is trusting a header.

    A README paragraph is not where an operator discovers their rate limiter is off. This
    is a `WARNING` on stdout at boot, next to the bind line they are already reading.
    """
    header = policy.client_ip_header
    if not header:
        return
    applog.warn(
        "rate_limit_trusts_client_header",
        code=(
            "proxy_overwritten_header"
            if header in _OVERWRITTEN_BY_A_KNOWN_PROXY
            else "client_supplied_header"
        ),
        stage=header,
        kind="internal",
    )


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

    _warn_about_identity(policy)

    install_middleware(app, policy)

    from api.retention import install_sweeper

    install_sweeper(app, policy)

    app.state.security_policy = policy
    applog.log("security_installed", count=policy.rate_limit_per_minute)
    return policy
