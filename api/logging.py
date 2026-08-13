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
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_creation_tokens", "usd",
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
    "unhandled_thread_exception": (
        logging.ERROR,
        "A worker thread died uncaught. The batch pool's leak path, and stderr-direct "
        "otherwise.",
    ),
    # verification
    "image_scored": (logging.INFO, "Deterministic image-quality scores for one uploaded image."),
    "stage_complete": (
        logging.INFO,
        "One pipeline stage's duration (OPS-1). One line per stage per request.",
    ),
    "verification_cost": (
        logging.INFO,
        "Tokens and dollars for one verification (OPS-4).",
    ),
    "cost_model_unknown": (
        logging.WARNING,
        "A verification ran on a model with no entry in the price list; cost was "
        "estimated at the most expensive known tier.",
    ),
    "verify_complete": (logging.INFO, "A verification produced a recommendation."),
    "adjudication": (
        logging.INFO,
        "Tier 3 saw at least one gray row. Carries how many were considered, how many "
        "were judged and how many changed, so the trigger rate is a number rather than "
        "an impression (LP-221).",
    ),
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
    "provider_price_unknown": (
        logging.WARNING,
        "The adapter priced a call at the unknown-model tier; the cost line is an "
        "over-estimate, not a quote.",
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
    # security posture (api/security.py, api/middleware/ratelimit.py)
    "security_installed": (
        logging.INFO,
        "The security middleware stack is installed. Carries the rate-limit ceiling.",
    ),
    "rate_limited": (
        logging.WARNING,
        "A request was refused with 429. Carries the lane and a correlation ID.",
    ),
    "rate_limit_trusts_client_header": (
        logging.WARNING,
        "The rate limiter is identifying clients by a header. If no proxy overwrites "
        "that header, the limiter can be bypassed.",
    ),
    "log_containment_reasserted": (
        logging.WARNING,
        "Something replaced the log record factory and traceback containment was "
        "reinstalled (SEC-4).",
    ),
    # retention (api/retention.py)
    "retention_started": (
        logging.INFO,
        "The retention sweeper is running. Carries the TTL and the sweep interval.",
    ),
    "retention_purged": (
        logging.INFO,
        "A sweep deleted expired data. Carries jobs removed and bytes reclaimed.",
    ),
    "retention_purge_failed": (
        logging.WARNING,
        "A sweep could not delete expired data; it stays on disk until the next sweep.",
    ),
    "retention_compaction_incomplete": (
        logging.WARNING,
        "The database was not compacted, so deleted content may remain in unused pages.",
    ),
    "retention_state_unwritable": (
        logging.WARNING,
        "The retention bookkeeping file could not be written; the next sweep redoes work.",
    ),
    "retention_sweep_failed": (
        logging.ERROR,
        "A whole retention cycle raised. The loop survives, but data is outliving its TTL.",
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


def configure(level: int = logging.INFO, stream: Any = None) -> None:
    """Emit one JSON object per line to stdout. Fly captures stdout directly."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.handlers = [handler]
    _logger.setLevel(level)
    _logger.propagate = False


# --------------------------------------------------------------------------------------
# The traceback hole, and the half of the plug that lives here
# --------------------------------------------------------------------------------------
#
# The allowlist above governs `applog.log()` and nothing else. uvicorn, asyncio, anyio and
# every library carry their own loggers and their own handlers, and a traceback printed by
# one of them carries the exception's message — which on this pipeline can be label text
# (a `pydantic.ValidationError` renders the input that failed validation).
#
# **Process-wide containment lives in `api/security.py`** (`install_log_containment`,
# installed by `harden()`): a `logging.setLogRecordFactory` hook that strips `exc_info` and
# `stack_info` from every record created anywhere in the process, plus scrubbed
# `sys.excepthook` and `threading.excepthook`. There is deliberately **one** record factory
# in this process. Two independent factories look like belt and braces and are a bug: each
# captures the other as "the original", so whichever is uninstalled first silently disables
# the other and leaves `containment_active()` reporting true.
#
# What lives here is the piece that hook does not cover, exported so it can be called from
# inside it.


#: Marker left in place of a redacted exception, so a reader can tell "nothing went wrong"
#: apart from "something went wrong and we are not printing it".
REDACTION_NOTE = "redacted: SEC-4"


def _safe_repr(value: object) -> object:
    """Replace an exception with its type name. Everything else passes through.

    An exception's `str()` is the leak. `pydantic.ValidationError` quotes the input that
    failed validation, and in this service that input is an extracted label field. The
    class name is a code identifier and can carry nothing from an upload.
    """
    if isinstance(value, BaseException):
        return f"<{type(value).__name__} {REDACTION_NOTE}>"
    return value


def scrub_exception_arguments(record: logging.LogRecord) -> logging.LogRecord:
    """Redact exception objects passed as a record's message or format arguments.

    Mutates and returns the record.

    This is the channel `exc_info` stripping does not reach. Stripping the traceback
    handles `logger.error("failed", exc_info=True)`; it does nothing for the other two
    natural spellings, where the exception *is* the payload:

        logger.error("call failed: %s", exc)       # args
        logger.error(exc)                          # msg

    Both render the exception's `str()` straight to stdout with no traceback involved.

    Deliberately narrow: it touches only `BaseException` instances, so ordinary log
    messages — uvicorn's startup and access lines, which are the signal an ops team reads —
    pass through byte-identical. It does not touch `exc_info`; that belongs to the
    containment factory in `api/security.py`, and doing it in two places is how two
    factories start fighting.

    Call it unconditionally from that factory, not only on the `exc_info` branch — the
    whole point is the records that have no `exc_info` to test.
    """
    if isinstance(record.msg, BaseException):
        record.msg = _safe_repr(record.msg)
        record.args = None
    if isinstance(record.args, tuple):
        record.args = tuple(_safe_repr(arg) for arg in record.args)
    elif isinstance(record.args, dict):
        record.args = {key: _safe_repr(value) for key, value in record.args.items()}
    return record


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

    `level` is positional-only (the `/`) on purpose. As a normal parameter it competed
    with `**fields` for the name "level": a caller writing `stage("extract",
    level="debug")` type-checked and then failed at runtime, and the only way to make
    `**dict[str, object]` assignable at all was a `# type: ignore` that silenced exactly
    that hazard. Positional-only, `level` cannot be bound by keyword, `**fields` needs no
    suppression, and a stray `level=` lands in `fields` where the allowlist rejects it
    loudly.
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
