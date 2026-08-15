"""Readings taken while the agent was still typing (LP-346).

Extraction does not depend on the application. `api.verify.verify` passes exactly one
field of it to the model — `commodity`, which selects the rule text in the prompt — and
every other field is compared *after* the label has been read. So the six seconds spent
reading the label can be spent while the agent fills the form instead of after they press
the button, and pressing the button then costs the comparison alone, which measures 2ms.

That is the whole idea, and it changes nothing about what is checked. The same images go
to the same model with the same prompt and produce the same reading; only the moment
moves.

What it costs, stated plainly
-----------------------------

**A reading now outlives its request.** `POST /verify` reads uploads into memory and
returns everything in the response body, which is why `api/retention.py` can say single
verifications persist nothing and `test_retention.py` can assert it against the
filesystem. Holding a reading between two requests is a real change to that posture, so:

* **Nothing is written to disk.** This store is a dict in the process. The filesystem
  assertion in `test_retention.py` stays true, unchanged, and still passes.
* **It expires.** `TTL_SECONDS` is minutes, not hours — long enough to fill a form,
  short enough that a walked-away browser leaves nothing behind for long.
* **It is bounded.** `MAX_ENTRIES` caps how much artwork the process can be made to hold,
  because an endpoint that stores something is an endpoint someone can call in a loop.
  Oldest goes first.
* **It dies with the process.** No file, no database, nothing to sweep, nothing to
  recover. A restart is a purge.

The claim in the README therefore narrows from "a verification persists nothing" to
"nothing is written to disk, and a reading is dropped within minutes". That is weaker and
it is said out loud rather than left for someone to discover.

**A token is not a capability.** It names a reading of *these bytes* under *this
commodity*. `Prepared.matches` re-checks both against what the verify request actually
carried, so a token cannot be used to attach one label's reading to another label's
submission — which would be a false pass built out of a caching optimisation.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from api.models import Commodity, Extraction, ImageReport
from api.provider.base import ProviderUsage

#: How long a reading survives after it is taken.
#:
#: Long enough to type six fields without losing the head start, short enough that a
#: browser left open over lunch is not still holding label artwork. On expiry the verify
#: request extracts normally, so the ceiling on being wrong here is one slow check.
TTL_SECONDS: float = 600.0

#: How many readings the process will hold at once. Oldest evicted first.
#:
#: `/prepare` accepts an upload and does work, so it can be called in a loop. The rate
#: limiter bounds the calls; this bounds the memory, and the two are different questions —
#: 30 requests a minute for ten minutes is 300 readings, and each one holds its images.
MAX_ENTRIES: int = 64


def digest(payloads: Sequence[bytes]) -> str:
    """A name for exactly these bytes, in this order.

    The token is bound to this rather than to a session, so a reading can only ever be
    reused for the artwork it was taken from. Order is part of it: front-then-back and
    back-then-front are different submissions and the roles ride along with the index.
    """
    running = hashlib.sha256()
    for payload in payloads:
        running.update(hashlib.sha256(payload).digest())
    return running.hexdigest()


@dataclass(frozen=True)
class Prepared:
    """One label, already read, waiting for an application to compare it against."""

    token: str
    image_digest: str
    commodity: Commodity
    extractions: tuple[Extraction, ...]
    reports: tuple[ImageReport, ...]
    ingest_ms: int
    quality_ms: int
    extract_ms: int
    #: What the reading cost. Incurred at prepare time, reported at verify time — a
    #: verification that showed $0.00 because the call happened a minute earlier would
    #: understate the cost of every check the tool performs (OPS-4).
    usage: ProviderUsage
    #: Retake reasons for images the pre-gate refused while others were read. Carried
    #: because `verify` needs it to keep a field from being reported Missing on the
    #: strength of a photograph nobody looked at.
    unseen: tuple[str, ...]
    created: float = field(default_factory=time.monotonic)

    def matches(self, payloads: Sequence[bytes], commodity: Commodity) -> bool:
        """Is this reading actually a reading of what the caller just sent?

        Both halves matter. The digest stops one label's reading being attached to
        another's submission. The commodity stops a reading taken under the spirits rule
        text being used to answer a wine application — different rules travel in the
        prompt, so it is a different reading, not the same one relabelled.
        """
        return self.image_digest == digest(payloads) and self.commodity is commodity


class PreparedReadings:
    """The store. A dict, a lock, a clock, and no filesystem."""

    def __init__(
        self,
        *,
        ttl_seconds: float = TTL_SECONDS,
        max_entries: int = MAX_ENTRIES,
        clock: object = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, Prepared] = {}

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    def _expired(self, entry: Prepared, now: float) -> bool:
        return now - entry.created >= self._ttl

    def put(self, **fields: object) -> Prepared:
        """Store a reading and return it, token and all."""
        token = secrets.token_urlsafe(24)
        entry = Prepared(token=token, created=self._now(), **fields)  # type: ignore[arg-type]
        with self._lock:
            now = self._now()
            # Sweep on write. There is no background task for this and there does not need
            # to be: entries only appear here, so the only moment the store can grow is
            # the only moment it needs tidying.
            for key in [k for k, v in self._entries.items() if self._expired(v, now)]:
                del self._entries[key]
            while len(self._entries) >= self._max:
                oldest = min(self._entries.values(), key=lambda e: e.created)
                del self._entries[oldest.token]
            self._entries[token] = entry
        return entry

    def take(self, token: str, payloads: Sequence[bytes], commodity: Commodity) -> Prepared | None:
        """Claim a reading, or None — expired, unknown, or not of this label.

        **Single use.** A reading is removed whether or not it matched. It answers one
        submission; leaving it available afterwards would keep artwork in memory for a
        request that is already finished, and there is no flow that needs the same token
        twice.
        """
        with self._lock:
            entry = self._entries.pop(token, None)
        if entry is None:
            return None
        if self._expired(entry, self._now()):
            return None
        if not entry.matches(payloads, commodity):
            return None
        return entry

    def __len__(self) -> int:
        with self._lock:
            now = self._now()
            return sum(1 for e in self._entries.values() if not self._expired(e, now))
