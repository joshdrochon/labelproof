"""Structured logging with a no-content rule that is enforced, not merely stated.

    NO LABEL CONTENT IN LOGS. EVER.

SEC-4 says logs carry IDs, timings, token counts, and verdict summaries — never label
text, never extracted values, never image bytes. A comment saying so would be a
convention that erodes; instead the logger accepts only an allowlist of field names, and
anything else raises. There is no channel through which a brand name can reach a log line,
so the rule holds by construction rather than by discipline.

Tested by LP-086 and LP-251: run a verification, scan the logs, assert no label string
appears.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

#: Fields a log line may carry. Everything here is an identifier, a measurement, or a
#: category — nothing that could contain label text or PII.
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        # identity and correlation
        "request_id", "job_id", "item_id", "image_index", "fixture",
        # measurements
        "duration_ms", "stage", "bytes", "width", "height", "count",
        "input_tokens", "output_tokens", "cache_read_tokens", "usd",
        "blur", "exposure", "glare", "skew_deg", "confidence",
        # categories and outcomes
        "event", "kind", "code", "verdict", "recommendation", "field",
        "commodity", "model", "provider", "tier", "status", "attempt",
        "media_type", "quality", "reason_code", "ok",
    }
)


#: Every event this service emits, with the level it is emitted at and what it means
#: (LP-117, OPS-5). This is the log schema. The README renders it for operators and
#: `tests/test_logging.py` asserts both directions — an event in the code and not here
#: fails, and an event here that the README does not list fails.
#:
#: Levels carry a meaning an operator can page on:
#:   INFO     something happened, and it is what should happen
#:   WARNING  degraded but handled — the agent got an honest answer
#:   ERROR    a failure nobody chose; look at it
EVENTS: dict[str, tuple[int, str]] = {
    # lifecycle
    "app_started": (logging.INFO, "Process is up and serving."),
    "config_incomplete": (logging.WARNING, "A required setting is missing; /ready is red."),
    # request envelope (api/main.py middleware)
    "request_complete": (
        logging.INFO,
        "One HTTP request finished. Carries status and total duration.",
    ),
    "request_failed": (
        logging.INFO,
        "A request ended in the error taxonomy. Carries kind, code, status.",
    ),
    "unhandled_exception": (
        logging.ERROR,
        "Something broke that nobody anticipated. The agent got a sentence, not a trace.",
    ),
    # verification
    "image_scored": (logging.INFO, "Deterministic image-quality scores for one uploaded image."),
    "stage_complete": (
        logging.INFO,
        "One pipeline stage's duration (OPS-1). One line per stage per request.",
    ),
    "verify_complete": (logging.INFO, "A verification produced a recommendation."),
    "verify_pregated": (
        logging.INFO,
        "Images too poor to read; returned Unreadable with zero model calls.",
    ),
    "verify_over_budget": (
        logging.INFO,
        "The request budget expired; partial result returned as Needs review.",
    ),
    # provider
    "provider_call": (logging.INFO, "One model call, with its usage."),
    "provider_extract": (logging.INFO, "The whole extraction across every image."),
    "provider_retry": (logging.WARNING, "A provider call failed and is being retried."),
    "provider_unavailable": (logging.WARNING, "The provider could not be reached; answered 503."),
    "provider_bbox_dropped": (
        logging.WARNING,
        "An evidence box was unusable and was discarded rather than guessed.",
    ),
    "provider_typography_unusable": (
        logging.WARNING,
        "Typography signals could not be judged; the warning field fails closed.",
    ),
    "circuit_breaker": (
        logging.WARNING,
        "The provider circuit opened or closed. Opening is the warning; closing rides the "
        "same event.",
    ),
    # batch
    "batch_queued": (logging.INFO, "A batch job was accepted."),
    "batch_recovered": (
        logging.INFO,
        "Unfinished batch items were picked back up after a restart.",
    ),
    "batch_retry": (logging.INFO, "Failed items in a batch were requeued."),
    "batch_exported": (logging.INFO, "A batch result CSV was produced."),
    "batch_purged": (logging.INFO, "A batch job's data passed its TTL and was deleted."),
    "batch_item_complete": (logging.INFO, "One batch item reached a verdict."),
    "batch_item_failed": (logging.WARNING, "One batch item failed; the rest of the job continues."),
    "batch_item_retry": (logging.WARNING, "One batch item is being retried."),
    "batch_item_unrecorded": (
        logging.ERROR,
        "A batch item finished but its result could not be stored.",
    ),
}

class ContentInLogError(RuntimeError):
    """Raised when a caller tries to log a field that is not on the allowlist.

    This is deliberately loud. A rejected field is almost always someone about to log
    an extracted value, and SEC-4 makes that a compliance failure rather than a style
    issue.
    """


_request_id: ContextVar[str] = ContextVar("request_id", default="")

_logger = logging.getLogger("labelproof")


def configure(
    level: int = logging.INFO, stream: Any = None, *, guard_stdout: bool = True
) -> None:
    """Emit one JSON object per line to stdout. Fly captures stdout directly.

    `guard_stdout` also closes the one hole the allowlist cannot reach — see
    `install_stdout_guard`. It is a keyword argument rather than an environment variable
    on purpose: a compliance control that can be switched off by a misspelt env var in a
    deployment script is not a control.
    """
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.handlers = [handler]
    _logger.setLevel(level)
    _logger.propagate = False
    if guard_stdout:
        install_stdout_guard()


# --------------------------------------------------------------------------------------
# The traceback hole, and the plug for it
# --------------------------------------------------------------------------------------

#: Marker appended in place of a suppressed traceback, so an operator reading stdout can
#: tell "nothing went wrong" apart from "something went wrong and we are not printing it".
REDACTION_NOTE = "traceback withheld: SEC-4"

_original_factory: Any = None


def _safe_repr(value: object) -> object:
    """Replace an exception with its type name. Everything else passes through.

    An exception's `str()` is the leak. `pydantic.ValidationError` quotes the input that
    failed validation, and in this service that input is an extracted label field. The
    class name is a code identifier and can carry nothing from an upload.
    """
    if isinstance(value, BaseException):
        return f"<{type(value).__name__} redacted>"
    return value


def redact(record: logging.LogRecord) -> logging.LogRecord:
    """Strip every channel through which an exception's text reaches a handler.

    Mutates and returns the record. Exposed separately from the factory so the behaviour
    is testable without installing anything globally.
    """
    leaked: str | None = None

    if record.exc_info:
        exc = record.exc_info[1]
        kind = type(exc) if exc is not None else record.exc_info[0]
        leaked = getattr(kind, "__name__", "Exception")
        record.exc_info = None
    if record.exc_text:
        record.exc_text = None
        leaked = leaked or "Exception"
    if record.stack_info:
        record.stack_info = None

    if isinstance(record.msg, BaseException):
        leaked = type(record.msg).__name__
        record.msg = f"<{leaked} redacted>"
        record.args = None
    if isinstance(record.args, tuple):
        record.args = tuple(_safe_repr(arg) for arg in record.args)
    elif isinstance(record.args, dict):
        record.args = {key: _safe_repr(value) for key, value in record.args.items()}

    if leaked and isinstance(record.msg, str):
        # No '%' in the marker: `msg` may be a format string and `args` may be non-empty.
        record.msg = f"{record.msg} | exception={leaked} ({REDACTION_NOTE})"

    return record


def install_stdout_guard() -> None:
    """Stop any logger in the process from printing a traceback to stdout (SEC-4).

    The allowlist in this module governs *our* log calls and nothing else. uvicorn,
    asyncio, anyio and every library carry their own loggers and their own handlers, and
    the one thing they reliably write is a traceback — which contains the exception's
    message. That is not a theoretical leak here: the pipeline runs in
    `asyncio.to_thread`, a `pydantic.ValidationError` raised while validating an
    extraction quotes the label text that failed validation, and an exception escaping a
    worker thread is logged by the framework, not by us.

    The plug is a `LogRecord` factory rather than a filter on a handler, because:

      * uvicorn sets `propagate = False` on its loggers, so a filter on root never sees
        those records;
      * a filter added to every logger present at startup misses every logger and handler
        created afterwards;
      * the record factory runs for every record created anywhere in the process, before
        any handler can format it, and it is a documented stdlib hook.

    What is deliberately *not* done: foreign log messages are left intact. Redacting them
    wholesale would delete uvicorn's startup and access lines, which are what an ops team
    reads to know the service is alive. The residual gap is a third-party library that
    passes label text as a plain format argument — `logger.info("read %s", brand)`. No
    code path in this repository does that, and the README says so rather than claiming
    the guard is total.

    Idempotent; `uninstall_stdout_guard()` reverses it.
    """
    global _original_factory
    if _original_factory is not None:
        return
    _original_factory = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return redact(_original_factory(*args, **kwargs))

    logging.setLogRecordFactory(factory)


def uninstall_stdout_guard() -> None:
    """Restore the previous record factory. For tests and for local debugging."""
    global _original_factory
    if _original_factory is None:
        return
    logging.setLogRecordFactory(_original_factory)
    _original_factory = None


def new_request_id() -> str:
    rid = f"req_{uuid.uuid4().hex[:16]}"
    _request_id.set(rid)
    return rid


def current_request_id() -> str:
    return _request_id.get()


def set_request_id(rid: str) -> None:
    _request_id.set(rid)


def log(event: str, level: int = logging.INFO, /, **fields: object) -> None:
    """Emit one structured line.

    Raises ContentInLogError if any field is outside the allowlist — see the module
    docstring. Add genuinely safe fields to ALLOWED_FIELDS deliberately; do not work
    around this.

    `level` is positional-only on purpose. With it available by keyword, a caller
    forwarding `**fields` could supply `level` from a dict and change the severity of a
    line by accident — and the type checker cannot tell that apart from a real level, so
    it flags every forwarding call site instead.
    """
    rejected = sorted(set(fields) - ALLOWED_FIELDS)
    if rejected:
        raise ContentInLogError(
            f"Refusing to log field(s) {rejected}: not on the allowlist. Logs must "
            f"carry no label content or PII (SEC-4). If a field is genuinely safe, add "
            f"it to ALLOWED_FIELDS with a reason."
        )

    payload: dict[str, object] = {"event": event, "ts": round(time.time(), 3)}
    if rid := current_request_id():
        payload["request_id"] = rid
    payload.update(fields)
    _logger.log(level, json.dumps(payload, sort_keys=True, default=str))


def warn(event: str, **fields: object) -> None:
    log(event, logging.WARNING, **fields)


def error(event: str, **fields: object) -> None:
    log(event, logging.ERROR, **fields)


class stage:
    """Time a pipeline stage and log its duration (OPS-1).

    Usage:
        with stage("extract", image_index=0):
            ...
    """

    def __init__(self, name: str, **fields: object):
        self.name = name
        self.fields = fields
        self.duration_ms = 0

    def __enter__(self) -> stage:
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.duration_ms = int((time.perf_counter() - self._started) * 1000)
        log(
            "stage_complete",
            stage=self.name,
            duration_ms=self.duration_ms,
            ok=exc_type is None,
            **self.fields,
        )
