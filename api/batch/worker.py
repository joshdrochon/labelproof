"""The worker pool, and the two promises it exists to keep.

**One bad image fails one item, never the batch (BATCH-6).** This is the requirement most
easily lost, because it is lost by *omission* — one uncaught exception in a worker thread
and the pool quietly shrinks, or the whole job stops with 240 items unprocessed and no
explanation. So every item is processed inside a total barrier: `except Exception` catches
everything the pipeline can throw, converts it to a stored, plain-language failure on that
item, and the worker takes the next one. There is no exception class that can end a run,
and a test drives a provider that fails half the batch to prove it.

**Verify Now keeps priority (BATCH-9, PERF-5).** An agent working their queue must not
wait behind an importer dump. Batch calls take a slot from a shared budget and yield that
slot the moment interactive work appears; interactive work never queues behind batch at
all. The yield is bounded — a steady trickle of single verifications must not starve a
300-item job forever, so batch resumes after `MAX_YIELD_SECONDS` regardless.

Retries are bounded and then the item is *failed with a reason*, not retried forever. An
item that has failed three times is telling you something, and the agent needs to see it
rather than watch a counter climb.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

from api import errors
from api import logging as applog
from api.batch.models import (
    BatchItem,
    ItemFailure,
    ItemState,
)
from api.batch.store import BatchStore
from api.config import Config
from api.models import VerificationResult
from api.provider.base import (
    ExtractionProvider,
    ExtractionRequest,
    ExtractionResponse,
    ProviderError,
)
from api.verify import pregate_headline, prepare_images, unverified
from api.verify import verify as run_verification

#: How many times one item is attempted automatically before it is failed with a reason
#: (BATCH-8, LP-156). Three covers the transient case — a rate-limit blip, a dropped
#: connection — without turning a genuinely broken item into 300 seconds of retrying at
#: full token cost. An agent-initiated retry resets the counter and is unbounded by
#: comparison, because that one is a decision rather than a guess.
MAX_ATTEMPTS = 3

#: The longest a batch worker defers to interactive traffic before taking a slot anyway.
#: Without a ceiling, one agent verifying labels all afternoon would hold a 300-item job
#: at zero progress and nothing in the UI would explain why.
MAX_YIELD_SECONDS = 2.0

#: How long an idle worker waits for new work before retiring. Threads are respawned by
#: `WorkerPool.start()`, so retiring costs nothing and keeps a finished job from leaving
#: six threads spinning for the life of the process.
IDLE_GRACE_SECONDS = 0.05
IDLE_ROUNDS_BEFORE_EXIT = 4

ProviderFactory = Callable[[Sequence[str]], ExtractionProvider]


class ProviderBudget:
    """One provider budget shared by both modes, in which batch is the one that yields.

    Interactive work is never asked to acquire anything — `interactive()` only announces
    itself. That asymmetry *is* the priority rule: a queue an agent can be placed in is a
    queue an agent can wait in, and PERF-5 says they do not wait.
    """

    def __init__(self, batch_slots: int, *, max_yield_seconds: float = MAX_YIELD_SECONDS):
        self._slots = threading.BoundedSemaphore(max(1, batch_slots))
        self._lock = threading.Lock()
        self._interactive = 0
        self._idle = threading.Event()
        self._idle.set()
        self._max_yield = max_yield_seconds
        self.yields = 0
        """How many times a batch call stood aside. Observable so the priority rule can
        be asserted rather than assumed — a budget that never yields is indistinguishable
        from one that is not wired up."""

        self.interactive_seen = 0

    @property
    def interactive_in_flight(self) -> int:
        with self._lock:
            return self._interactive

    @contextmanager
    def interactive(self) -> Iterator[None]:
        """Mark a Verify Now request in flight. Acquires nothing and can never block."""
        with self._lock:
            self._interactive += 1
            self.interactive_seen += 1
            self._idle.clear()
        try:
            yield
        finally:
            with self._lock:
                self._interactive -= 1
                if self._interactive <= 0:
                    self._interactive = 0
                    self._idle.set()

    @contextmanager
    def batch_slot(self) -> Iterator[None]:
        """Take a batch slot, standing aside for interactive work first."""
        if not self._idle.is_set():
            self.yields += 1
            self._idle.wait(timeout=self._max_yield)
        self._slots.acquire()
        try:
            yield
        finally:
            self._slots.release()


class _BudgetedProvider:
    """The item's provider: resolved when it is first needed, and behind the shared budget.

    **Resolution is deferred, and that is not tidiness.** `verify_item` pre-gates the
    artwork before it extracts anything, so an image nobody could read never reaches a
    provider at all — and in sample mode resolving one anyway was actively wrong. The
    fixture provider fails closed on artwork it does not recognise (rightly), and that
    refusal landed on items the pre-gate had already answered honestly: a photograph too
    blurred to read came back as "this server is running in sample mode" instead of "this
    image could not be read", which is the tool blaming its own configuration for a defect
    in the picture. The costs are unchanged either way; only which sentence the agent gets.

    A provider that cannot be built is still a per-item failure with a readable reason, not
    a dead worker — it just leaves through `process`'s barrier now instead of a second one
    around construction, so it is logged like every other item failure.
    """

    def __init__(
        self,
        factory: ProviderFactory,
        filenames: Sequence[str],
        budget: ProviderBudget,
    ):
        self._factory = factory
        self._filenames = list(filenames)
        self._budget = budget
        self._inner: ExtractionProvider | None = None
        #: Whatever the resolved provider calls itself, once there is one. A plain
        #: attribute rather than a property because `ExtractionProvider` declares `name` as
        #: a settable variable, and a read-only property does not satisfy the protocol.
        self.name = "provider"

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        if self._inner is None:
            # Outside the slot: constructing a provider is not a call to it, and holding a
            # batch slot while doing so would shrink the budget for no reason.
            self._inner = self._factory(self._filenames)
            self.name = getattr(self._inner, "name", "provider")
        with self._budget.batch_slot():
            return self._inner.extract(request)


def verify_item(
    item: BatchItem,
    store: BatchStore,
    config: Config,
    provider: ExtractionProvider,
) -> VerificationResult:
    """Run one batch item through the same pipeline Verify Now uses.

    "The same pipeline" is literal on both halves: `api.verify.prepare_images` for
    ingest → quality → pre-gate, and `api.verify.verify` for extraction and comparison.
    Neither is re-implemented here, because the pre-gate is the requirement most likely to
    be lost by copy drift, and an importer dump is exactly where hopeless artwork arrives
    in quantity — a model call on an image nobody could read is the one cost in this
    product with a guaranteed zero return (the build spec, LP-321).
    """
    payloads: list[bytes] = []
    for name in item.images:
        data = store.read_image(item.job_id, name)
        if data is None:
            raise errors.ImageError(
                "The label image named in the manifest for this application was not "
                "found in the upload. Add the file to the upload and retry this item.",
                next_step="replace",
                code="image_missing",
            )
        payloads.append(data)

    if not payloads:
        raise errors.ImageError(
            "No label image was supplied for this application, so nothing could be "
            "checked. Add the artwork and retry this item.",
            next_step="replace",
            code="image_missing",
        )

    # A worker thread inherits no ContextVar, so the request ID that attributes every
    # interactive log line is empty here. Without these two the per-image lines from six
    # concurrent workers are indistinguishable from one another.
    prepared = prepare_images(
        payloads, config, job_id=item.job_id, item_id=item.item_id
    )

    if prepared.pregated:
        return unverified(
            item.application,
            headline=pregate_headline(prepared.reason or ""),
            per_field="Not checked — the image could not be read.",
        )

    return run_verification(
        item.application,
        prepared.usable,
        provider,
        unseen=prepared.skipped_reasons if prepared.partial else (),
    )


def _failure_for(exc: Exception, attempts: int) -> ItemFailure:
    """Turn whatever went wrong into a sentence an agent can act on (UX-6, OPS-5)."""
    if isinstance(exc, errors.LabelProofError):
        return ItemFailure(
            code=exc.code,
            message=exc.message,
            next_step=exc.next_step or "retry",
            attempts=attempts,
        )
    if isinstance(exc, ProviderError):
        return ItemFailure(
            code="provider_unavailable",
            message=(
                f"The label reading service could not be reached for this application "
                f"after {attempts} attempts. Nothing on it has been checked. Retry the "
                f"failed items once the service is back, or review this one by hand."
            ),
            next_step="retry",
            attempts=attempts,
        )
    return ItemFailure(
        code="internal_error",
        message=(
            "Something went wrong on our side while checking this application. Nothing "
            "on it has been checked and no application data has been changed. Retry the "
            "failed items, or review this one by hand."
        ),
        next_step="retry",
        attempts=attempts,
    )


def process(
    item: BatchItem,
    store: BatchStore,
    config: Config,
    provider: ExtractionProvider,
    *,
    max_attempts: int = MAX_ATTEMPTS,
) -> ItemState:
    """Process one item and record its outcome. Never raises — that is the whole point.

    Every exit from this function writes a terminal or requeued state for this item and
    nothing else. A caller looping over items can therefore never be interrupted by one
    of them, which is what per-item isolation means in practice (BATCH-6, TC-20).
    """
    try:
        result = verify_item(item, store, config, provider)
    except Exception as exc:  # noqa: BLE001 - per-item isolation: one bad item must not kill the batch (BATCH-6)
        retryable = isinstance(exc, ProviderError) and exc.retryable
        if retryable and item.attempts < max_attempts:
            store.requeue(item.item_id)
            applog.warn(
                "batch_item_retry",
                job_id=item.job_id,
                item_id=item.item_id,
                attempt=item.attempts,
                code="provider_unavailable",
            )
            return ItemState.QUEUED

        failure = _failure_for(exc, item.attempts)
        try:
            store.fail(item.item_id, failure)
        except Exception:  # noqa: BLE001 - the store is down; log and keep the worker alive (BATCH-6)
            applog.error(
                "batch_item_unrecorded",
                job_id=item.job_id,
                item_id=item.item_id,
                code=failure.code,
            )
        applog.warn(
            "batch_item_failed",
            job_id=item.job_id,
            item_id=item.item_id,
            attempt=item.attempts,
            code=failure.code,
        )
        return ItemState.FAILED

    try:
        store.complete(item.item_id, result)
    except Exception:  # noqa: BLE001 - the store is down; log and keep the worker alive (BATCH-6)
        applog.error("batch_item_unrecorded", job_id=item.job_id, item_id=item.item_id)
        return ItemState.FAILED

    applog.log(
        "batch_item_complete",
        job_id=item.job_id,
        item_id=item.item_id,
        recommendation=result.aggregate.recommendation.value,
        attempt=item.attempts,
    )
    return ItemState.DONE


class WorkerPool:
    """`Config.batch_workers` threads pulling from the store (LP-153, BATCH-9).

    Threads rather than processes: the work is a network call per item, so it is waiting,
    not computing, and a process pool would mean shipping image bytes across a pipe for
    no gain. Concurrency is configurable because the right number is a measured property
    of the provider's rate limits, not a constant anyone can reason out (pinned build decision).
    """

    def __init__(
        self,
        store: BatchStore,
        config: Config,
        provider_factory: ProviderFactory,
        *,
        budget: ProviderBudget | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ):
        self.store = store
        self.config = config
        self.provider_factory = provider_factory
        self.budget = budget or ProviderBudget(config.batch_workers)
        self.max_attempts = max_attempts
        self._lock = threading.Lock()
        self._live = 0
        self._stopping = False

    @property
    def workers(self) -> int:
        return max(1, self.config.batch_workers)

    @property
    def live(self) -> int:
        with self._lock:
            return self._live

    def start(self) -> None:
        """Make sure the pool is up to strength. Safe to call on every submission."""
        with self._lock:
            if self._stopping:
                return
            wanted = self.workers - self._live
            self._live += max(0, wanted)
        for _ in range(max(0, wanted)):
            threading.Thread(target=self._run, name="labelproof-batch", daemon=True).start()

    def _run(self) -> None:
        idle = 0
        try:
            while not self._stopping:
                item = self.store.claim()
                if item is None:
                    idle += 1
                    if idle >= IDLE_ROUNDS_BEFORE_EXIT:
                        return
                    time.sleep(IDLE_GRACE_SECONDS)
                    continue
                idle = 0
                self._process_one(item)
        finally:
            with self._lock:
                self._live -= 1

    def _process_one(self, item: BatchItem) -> None:
        """Run one item. Isolation covers provider resolution too — see `_BudgetedProvider`.

        A provider that cannot even be constructed — no key, sample mode that does not
        recognise the artwork — is a per-item failure with a readable reason, not a dead
        worker. That failure now leaves through `process`, which is where every other
        per-item failure leaves, so it is recorded and logged the same way.
        """
        provider = _BudgetedProvider(self.provider_factory, item.images, self.budget)
        process(item, self.store, self.config, provider, max_attempts=self.max_attempts)

    def drain(self, timeout: float = 60.0) -> bool:
        """Block until nothing is queued and no worker is running. Test-facing."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.live == 0 and not self.store.has_work():
                return True
            time.sleep(0.01)
        return False

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stopping = True
        deadline = time.monotonic() + timeout
        while self.live and time.monotonic() < deadline:
            time.sleep(0.01)
        self._stopping = False
