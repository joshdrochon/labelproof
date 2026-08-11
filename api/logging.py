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
