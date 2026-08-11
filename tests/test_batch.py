"""Batch mode end to end (BATCH-1..10, TC-20, PERF-4, PERF-5, SEC-2).

Offline throughout. The only thing swapped out is the model call — every provider here
comes from `api.provider.fake` or wraps one, so nothing in this file opens a socket
(ENG-3).

The assertion this file exists for is **per-item isolation**. Known prior art tried the
synchronous version of this feature and it broke at two or three images; the failure mode
that replaces it is subtler and worse — a job that stops at item 40 because item 40 threw,
leaving 260 applications silently unprocessed. So the isolation tests do not check that a
failure is *handled*; they check that the other 299 items still finish, under a provider
that fails, under a provider that raises something nobody anticipated, and at full scale.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import logging as applog
from api.batch import manifest as manifest_mod
from api.batch.models import (
    BatchItem,
    ItemFailure,
    ItemState,
    JobState,
    job_cost,
    summarize,
    worst_first,
)
from api.batch.store import BatchStore, stored_name
from api.batch.worker import MAX_ATTEMPTS, ProviderBudget, WorkerPool, process
from api.config import Config
from api.main import create_app
from api.models import (
    Aggregate,
    Application,
    Cost,
    FieldName,
    FieldResult,
    Recommendation,
    Timings,
    Verdict,
    VerificationResult,
)
from api.provider.base import ExtractionRequest, ExtractionResponse, ProviderError
from api.provider.fake import FailingProvider, SpecBackedProvider
from api.routes import batch as batch_routes

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "assets" / "samples" / "old_tom.json"

GOOD_IMAGE = "old_tom_front.png"
POISON_IMAGE = "poison_front.png"


# --- fixtures and helpers -------------------------------------------------------------


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    """A config that never needs a key, so no test can reach the network."""
    base: dict[str, Any] = {
        "use_fake_provider": True,
        "storage_dir": str(tmp_path),
        "batch_workers": 4,
    }
    base.update(overrides)
    return Config(**base)


def _batch_is_mounted(app: Any) -> bool:
    for route in app.router.routes:
        if getattr(route, "original_router", None) is batch_routes.router:
            return True
        if str(getattr(route, "path", "")).startswith("/batch"):
            return True
    return False


def make_client(tmp_path: Path, provider: Any = None, **overrides: Any) -> TestClient:
    """The real app, with the batch router guaranteed to be mounted and reachable.

    `api/main.py` mounts an SPA catch-all last, and a catch-all registered ahead of the
    batch routes would swallow `GET /batch/{id}`. Anything added here is therefore moved
    to the front of the table. When main.py already mounts batch itself, this is a no-op
    and the app under test is exactly the shipped one.
    """
    app = create_app(config=make_config(tmp_path, **overrides), provider=provider)
    if not _batch_is_mounted(app):
        before = len(app.router.routes)
        app.include_router(batch_routes.router)
        added = app.router.routes[before:]
        del app.router.routes[before:]
        app.router.routes[0:0] = added
    return TestClient(app)


def checker_png(color: tuple[int, int, int] = (200, 200, 200), size: int = 240) -> bytes:
    """A small image with real high-frequency detail, so the quality gate passes it.

    A flat rectangle scores as hopeless and is pre-gated before any provider call, which
    would make every isolation test pass for the wrong reason.
    """
    height = int(size * 1.4)
    ys, xs = np.mgrid[0:height, 0:size]
    mask = ((xs // 6 + ys // 6) % 2).astype(np.uint8)
    channels = [(mask * value + 25).astype(np.uint8) for value in color]
    buffer = io.BytesIO()
    Image.fromarray(np.dstack(channels)).save(buffer, format="PNG")
    return buffer.getvalue()


GOOD_BYTES = checker_png((200, 200, 200))
POISON_BYTES = checker_png((200, 20, 20))


def old_tom(**overrides: Any) -> dict[str, Any]:
    raw = json.loads(SAMPLE.read_text())
    application = {k: v for k, v in raw.items() if not k.startswith("_")}
    application.update(overrides)
    return application


def manifest_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=manifest_mod.COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in manifest_mod.COLUMNS})
    return buffer.getvalue()


def row(front: str = GOOD_IMAGE, **overrides: Any) -> dict[str, Any]:
    entry = old_tom()
    entry["front_image"] = front
    entry.update(overrides)
    return entry


def post_batch(
    client: TestClient,
    rows: list[dict[str, Any]],
    *,
    images: dict[str, bytes] | None = None,
    archive: bytes | None = None,
    manifest_text: str | None = None,
    omit_manifest: bool = False,
) -> Any:
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    if not omit_manifest:
        text = manifest_text if manifest_text is not None else manifest_csv(rows)
        files.append(("manifest", ("manifest.csv", text.encode("utf-8"), "text/csv")))
    for name, data in (images if images is not None else {GOOD_IMAGE: GOOD_BYTES}).items():
        files.append(("files", (name, data, "image/png")))
    if archive is not None:
        files.append(("files", ("labels.zip", archive, "application/zip")))
    return client.post("/batch", files=files)


def zip_of(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def drain(client: TestClient, timeout: float = 60.0) -> None:
    pool: WorkerPool = client.app.state.batch_pool
    assert pool.drain(timeout), "the worker pool did not finish in time"


def wait_until(predicate: Any, timeout: float = 30.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(autouse=True)
def _quiet_logs() -> io.StringIO:
    stream = io.StringIO()
    applog.configure(stream=stream)
    return stream


# --- fake providers (all offline) -----------------------------------------------------


class PoisonProvider:
    """Real extraction for grey artwork; a provider outage for red artwork.

    Keying the failure off the pixels rather than a call counter is what makes the
    isolation tests deterministic: with six workers interleaving, "fail every tenth call"
    fails a different item on every run, and the assertion that the *other* items survived
    would be testing nothing in particular.
    """

    name = "fake:poison"

    def __init__(self, inner: Any, *, retryable: bool = False):
        self.inner = inner
        self.retryable = retryable
        self.failures = 0
        self._lock = threading.Lock()

    def _is_poison(self, data: bytes) -> bool:
        image = Image.open(io.BytesIO(data)).convert("RGB").resize((1, 1))
        red, green, _ = image.getpixel((0, 0))  # type: ignore[misc]
        return red > green + 40

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        if any(self._is_poison(image.data) for image in request.images):
            with self._lock:
                self.failures += 1
            raise ProviderError("Connection refused", retryable=self.retryable)
        return self.inner.extract(request)


class ExplodingProvider:
    """Raises something nobody planned for. Isolation must not depend on the type."""

    name = "fake:exploding"

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        raise RuntimeError("a bug nobody wrote a handler for")


class GatedProvider:
    """Lets the first `release_after` calls through, then holds until released.

    Turns "results are available while the job runs" from a race into an assertion.
    """

    name = "fake:gated"

    def __init__(self, inner: Any, release_after: int):
        self.inner = inner
        self.release_after = release_after
        self.gate = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        with self._lock:
            self.calls += 1
            call = self.calls
        if call > self.release_after:
            assert self.gate.wait(timeout=30), "gated provider was never released"
        return self.inner.extract(request)


def spec_provider() -> SpecBackedProvider:
    return SpecBackedProvider("tc01_old_tom_clean")


# --- the store (LP-147, LP-152, LP-158, BATCH-6) ---------------------------------------


def seed(store: BatchStore, count: int = 3) -> str:
    job = store.create_job(retention_hours=24)
    store.add_items(
        job.job_id,
        [
            (index + 2, Application.model_validate(old_tom()), [GOOD_IMAGE])
            for index in range(count)
        ],
    )
    return job.job_id


def test_a_job_and_its_items_survive_a_restart(tmp_path: Path) -> None:
    """BATCH-6, LP-158 — an agent who starts a batch before lunch comes back to it."""
    job_id = seed(BatchStore(tmp_path), count=5)

    reopened = BatchStore(tmp_path)
    assert reopened.get_job(job_id) is not None
    assert len(reopened.items(job_id)) == 5
    assert reopened.counts(job_id).queued == 5


def test_recovery_requeues_items_a_dead_process_left_processing(tmp_path: Path) -> None:
    """An item in `processing` with no process behind it is stranded forever otherwise."""
    store = BatchStore(tmp_path)
    job_id = seed(store)
    claimed = store.claim()
    assert claimed is not None

    reopened = BatchStore(tmp_path)
    assert reopened.recover() == 1
    assert reopened.counts(job_id).queued == 3


def test_two_workers_never_claim_the_same_item(tmp_path: Path) -> None:
    """Six workers selecting from one table is exactly where a double charge comes from."""
    store = BatchStore(tmp_path)
    seed(store, count=40)
    claimed: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        while True:
            item = store.claim()
            if item is None:
                return
            with lock:
                claimed.append(item.item_id)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(claimed) == 40
    assert len(set(claimed)) == 40


def test_a_claim_counts_an_attempt(tmp_path: Path) -> None:
    store = BatchStore(tmp_path)
    seed(store, count=1)
    first = store.claim()
    assert first is not None and first.attempts == 1
    store.requeue(first.item_id)
    second = store.claim()
    assert second is not None and second.attempts == 2


def test_the_job_is_marked_finished_when_the_last_item_lands(tmp_path: Path) -> None:
    store = BatchStore(tmp_path)
    job_id = seed(store, count=2)
    for _ in range(2):
        item = store.claim()
        assert item is not None
        store.complete(item.item_id, _result(Recommendation.READY_TO_APPROVE))
    job = store.get_job(job_id)
    assert job is not None and job.state is JobState.DONE


def test_retry_requeues_the_failed_and_leaves_the_finished_alone(tmp_path: Path) -> None:
    """BATCH-8, LP-157 — never reprocess the 290 that already cost money and minutes."""
    store = BatchStore(tmp_path)
    job_id = seed(store, count=3)

    done = store.claim()
    failed = store.claim()
    assert done is not None and failed is not None
    store.complete(done.item_id, _result(Recommendation.READY_TO_APPROVE))
    store.fail(failed.item_id, ItemFailure(code="provider_unavailable", message="x"))
    still_queued = store.claim()
    assert still_queued is not None
    store.complete(still_queued.item_id, _result(Recommendation.NEEDS_REVIEW))

    assert store.retry_failed(job_id) == 1

    states = {item.item_id: item.state for item in store.items(job_id)}
    assert states[done.item_id] is ItemState.DONE
    assert states[still_queued.item_id] is ItemState.DONE
    assert states[failed.item_id] is ItemState.QUEUED

    survivor = store.get_item(done.item_id)
    assert survivor is not None and survivor.result is not None


def test_retry_clears_the_failure_and_the_attempt_count(tmp_path: Path) -> None:
    store = BatchStore(tmp_path)
    job_id = seed(store, count=1)
    item = store.claim()
    assert item is not None
    store.fail(item.item_id, ItemFailure(code="provider_unavailable", message="x", attempts=3))

    store.retry_failed(job_id)
    reloaded = store.get_item(item.item_id)
    assert reloaded is not None
    assert reloaded.attempts == 0
    assert reloaded.failure is None


def test_images_are_stored_and_read_back_by_their_manifest_name(tmp_path: Path) -> None:
    store = BatchStore(tmp_path)
    job = store.create_job()
    store.save_image(job.job_id, "front.png", GOOD_BYTES)
    assert store.read_image(job.job_id, "front.png") == GOOD_BYTES
    assert store.read_image(job.job_id, "nothing.png") is None


def test_a_hostile_file_name_cannot_escape_the_job_directory(tmp_path: Path) -> None:
    """SEC-5 — an archive can name anything; the name it gets on disk is a digest."""
    store = BatchStore(tmp_path)
    job = store.create_job()
    store.save_image(job.job_id, "../../../etc/passwd", GOOD_BYTES)

    written = list((store.images_root / job.job_id).iterdir())
    assert [path.name for path in written] == [stored_name("passwd")]
    assert not (tmp_path.parent / "etc").exists()


# --- retention (SEC-2, LP-152) --------------------------------------------------------


def test_expired_batches_are_purged_with_their_artwork(tmp_path: Path) -> None:
    """A retained batch is 300 applications' worth of names sitting on a forgotten disk."""
    store = BatchStore(tmp_path)
    job = store.create_job(retention_hours=24, now=time.time() - 25 * 3600)
    store.add_items(job.job_id, [(2, Application.model_validate(old_tom()), [GOOD_IMAGE])])
    store.save_image(job.job_id, GOOD_IMAGE, GOOD_BYTES)

    assert store.purge_expired() == [job.job_id]
    assert store.get_job(job.job_id) is None
    assert store.items(job.job_id) == []
    assert not (store.images_root / job.job_id).exists()


def test_a_live_batch_is_not_purged(tmp_path: Path) -> None:
    store = BatchStore(tmp_path)
    job_id = seed(store)
    assert store.purge_expired() == []
    assert store.get_job(job_id) is not None


# --- per-item isolation (BATCH-6, TC-20) — the requirement most easily lost ------------


def _queued(store: BatchStore, job_id: str, images: list[str]) -> BatchItem:
    store.add_items(job_id, [(2, Application.model_validate(old_tom()), images)])
    item = store.claim()
    assert item is not None
    return item


def test_a_failing_provider_fails_one_item_and_not_the_run(tmp_path: Path) -> None:
    """BATCH-6 — the poisoned item fails; every other item still produces a verdict."""
    store = BatchStore(tmp_path)
    config = make_config(tmp_path)
    job = store.create_job()
    store.save_image(job.job_id, GOOD_IMAGE, GOOD_BYTES)
    store.save_image(job.job_id, POISON_IMAGE, POISON_BYTES)

    entries = [
        (index + 2, Application.model_validate(old_tom()),
         [POISON_IMAGE if index % 3 == 0 else GOOD_IMAGE])
        for index in range(12)
    ]
    store.add_items(job.job_id, entries)

    provider = PoisonProvider(spec_provider())
    while (item := store.claim()) is not None:
        process(item, store, config, provider)

    counts = store.counts(job.job_id)
    assert counts.done == 8
    assert counts.failed == 4
    assert provider.failures == 4

    for item in store.items(job.job_id, states=[ItemState.DONE]):
        assert item.result is not None
        assert item.result.aggregate.recommendation is Recommendation.READY_TO_APPROVE


def test_an_unanticipated_exception_still_only_costs_one_item(tmp_path: Path) -> None:
    """Isolation must not depend on the exception type — a RuntimeError is not special."""
    store = BatchStore(tmp_path)
    config = make_config(tmp_path)
    job = store.create_job()
    store.save_image(job.job_id, GOOD_IMAGE, GOOD_BYTES)
    item = _queued(store, job.job_id, [GOOD_IMAGE])

    state = process(item, store, config, ExplodingProvider())

    assert state is ItemState.FAILED
    failed = store.get_item(item.item_id)
    assert failed is not None and failed.failure is not None
    assert "went wrong on our side" in failed.failure.message


def test_a_failure_says_what_happened_and_what_to_do_next(tmp_path: Path) -> None:
    """UX-6 — a stored failure an agent cannot act on is a dead end in a 300-row table."""
    store = BatchStore(tmp_path)
    config = make_config(tmp_path)
    job = store.create_job()
    store.save_image(job.job_id, POISON_IMAGE, POISON_BYTES)
    item = _queued(store, job.job_id, [POISON_IMAGE])

    process(item, store, config, PoisonProvider(spec_provider()))

    failed = store.get_item(item.item_id)
    assert failed is not None and failed.failure is not None
    message = failed.failure.message
    assert "Nothing on it has been checked" in message
    assert "Retry" in message or "review this one by hand" in message
    assert "Traceback" not in message and "Exception" not in message


def test_a_missing_image_fails_that_item_by_name_of_the_problem(tmp_path: Path) -> None:
    store = BatchStore(tmp_path)
    config = make_config(tmp_path)
    job = store.create_job()
    item = _queued(store, job.job_id, ["never_uploaded.png"])

    assert process(item, store, config, spec_provider()) is ItemState.FAILED
    failed = store.get_item(item.item_id)
    assert failed is not None and failed.failure is not None
    assert failed.failure.code == "image_missing"


def test_junk_bytes_fail_the_item_rather_than_the_batch(tmp_path: Path) -> None:
    """An importer dump contains junk. One junk file is one failed row."""
    store = BatchStore(tmp_path)
    config = make_config(tmp_path)
    job = store.create_job()
    store.save_image(job.job_id, "notes.png", b"this is not an image at all")
    item = _queued(store, job.job_id, ["notes.png"])

    assert process(item, store, config, spec_provider()) is ItemState.FAILED
    failed = store.get_item(item.item_id)
    assert failed is not None and failed.failure is not None
    assert failed.failure.message


def test_a_hopeless_image_is_an_unreadable_verdict_not_a_failed_item(tmp_path: Path) -> None:
    """LP-321 — the pre-gate spends nothing and can never produce a false pass."""
    store = BatchStore(tmp_path)
    config = make_config(tmp_path)
    job = store.create_job()
    blank = io.BytesIO()
    Image.new("RGB", (400, 560), (250, 248, 242)).save(blank, format="PNG")
    store.save_image(job.job_id, "blank.png", blank.getvalue())
    item = _queued(store, job.job_id, ["blank.png"])

    assert process(item, store, config, FailingProvider()) is ItemState.DONE

    done = store.get_item(item.item_id)
    assert done is not None and done.result is not None
    assert done.result.aggregate.recommendation is Recommendation.NEEDS_REVIEW
    assert {field.verdict for field in done.result.fields} == {Verdict.UNREADABLE}


# --- bounded retries (BATCH-8, LP-156) ------------------------------------------------


def test_a_retryable_outage_is_retried_and_then_failed_with_a_reason(tmp_path: Path) -> None:
    """Bounded, then honest. A counter that climbs forever tells the agent nothing."""
    store = BatchStore(tmp_path)
    config = make_config(tmp_path)
    job = store.create_job()
    store.save_image(job.job_id, GOOD_IMAGE, GOOD_BYTES)
    store.add_items(job.job_id, [(2, Application.model_validate(old_tom()), [GOOD_IMAGE])])

    provider = FailingProvider(retryable=True)
    states: list[ItemState] = []
    while (item := store.claim()) is not None:
        states.append(process(item, store, config, provider))

    assert states == [ItemState.QUEUED] * (MAX_ATTEMPTS - 1) + [ItemState.FAILED]
    failed = store.items(job.job_id)[0]
    assert failed.attempts == MAX_ATTEMPTS
    assert failed.failure is not None
    assert str(MAX_ATTEMPTS) in failed.failure.message


def test_a_non_retryable_failure_is_not_retried(tmp_path: Path) -> None:
    """Retrying something that cannot succeed spends 300 items' worth of budget on air."""
    store = BatchStore(tmp_path)
    config = make_config(tmp_path)
    job = store.create_job()
    store.save_image(job.job_id, GOOD_IMAGE, GOOD_BYTES)
    item = _queued(store, job.job_id, [GOOD_IMAGE])

    assert process(item, store, config, FailingProvider(retryable=False)) is ItemState.FAILED
    assert store.counts(job.job_id).failed == 1


# --- the shared provider budget (BATCH-9, PERF-5) -------------------------------------


def test_verify_now_never_queues_behind_batch_work() -> None:
    """PERF-5 — an agent working their queue must not wait for an importer dump."""
    budget = ProviderBudget(batch_slots=2)
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with budget.batch_slot():
            held.set()
            release.wait(timeout=10)

    workers = [threading.Thread(target=hold) for _ in range(2)]
    for worker in workers:
        worker.start()
    assert held.wait(timeout=5)

    started = time.monotonic()
    with budget.interactive():
        elapsed = time.monotonic() - started
    assert elapsed < 0.25, "Verify Now waited on a batch slot"

    release.set()
    for worker in workers:
        worker.join(timeout=10)


def test_batch_stands_aside_while_verify_now_is_in_flight() -> None:
    budget = ProviderBudget(batch_slots=4, max_yield_seconds=5.0)
    entered = threading.Event()
    done = threading.Event()

    def batch_call() -> None:
        with budget.batch_slot():
            entered.set()
        done.set()

    with budget.interactive():
        thread = threading.Thread(target=batch_call)
        thread.start()
        assert not entered.wait(timeout=0.2), "batch took a slot while Verify Now was live"

    assert done.wait(timeout=5)
    thread.join(timeout=5)
    assert budget.yields == 1


def test_batch_resumes_even_if_verify_now_never_stops() -> None:
    """Yielding is bounded. A steady trickle of singles must not stall a 300-item job."""
    budget = ProviderBudget(batch_slots=2, max_yield_seconds=0.05)
    with budget.interactive():
        started = time.monotonic()
        with budget.batch_slot():
            elapsed = time.monotonic() - started
    assert 0.04 <= elapsed < 2.0


class SlowProvider:
    """A provider with a real cost per call, so saturation is something to measure."""

    name = "fake:slow"

    def __init__(self, inner: Any, delay: float = 0.05):
        self.inner = inner
        self.delay = delay

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        time.sleep(self.delay)
        return self.inner.extract(request)


def test_verify_now_stays_fast_while_a_batch_is_running(tmp_path: Path) -> None:
    """PERF-5, LP-165 — the agent working their queue does not feel the importer dump.

    Runs over HTTP with the priority middleware installed, which is the one line
    `create_app` needs for the shared budget to mean anything.
    """
    app = create_app(
        config=make_config(tmp_path, batch_workers=4),
        provider=SlowProvider(spec_provider(), delay=0.05),
    )
    batch_routes.install_verify_priority(app)
    before = len(app.router.routes)
    app.include_router(batch_routes.router)
    added = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = added
    client = TestClient(app)

    job_id = post_batch(client, [row() for _ in range(24)]).json()["job_id"]
    wait_until(
        lambda: client.get(f"/batch/{job_id}").json()["counts"]["processing"] > 0,
        what="the batch to saturate its workers",
    )

    latencies: list[float] = []
    for _ in range(5):
        started = time.monotonic()
        response = client.post(
            "/verify",
            files=[("images", (GOOD_IMAGE, GOOD_BYTES, "image/png"))],
            data={"application": json.dumps(old_tom())},
        )
        latencies.append(time.monotonic() - started)
        assert response.status_code == 200

    drain(client)

    budget: ProviderBudget = app.state.provider_budget
    assert budget.interactive_seen >= 5, "the priority middleware never marked a request"
    assert budget.yields > 0, "batch never stood aside for Verify Now"
    assert max(latencies) < 2.0, f"Verify Now waited on batch work: {max(latencies):.2f}s"


# --- summary and ordering (LP-161, UX-10) ---------------------------------------------


def _result(
    recommendation: Recommendation,
    *,
    worst_field: FieldName = FieldName.BRAND_NAME,
    worst_verdict: Verdict = Verdict.MATCH,
) -> VerificationResult:
    fields = [
        FieldResult(
            field=name,
            verdict=worst_verdict if name is worst_field else Verdict.MATCH,
            extracted="x",
            expected="x",
            confidence=0.9,
            rationale="",
        )
        for name in FieldName
    ]
    return VerificationResult(
        request_id="req_test",
        aggregate=Aggregate(recommendation=recommendation, rationale="", driving_field=worst_field),
        fields=fields,
        images=[],
        timings_ms=Timings(),
        cost=Cost(input_tokens=100, output_tokens=10, usd=0.01),
    )


def _item(
    item_id: str,
    row_number: int,
    state: ItemState,
    result: VerificationResult | None = None,
) -> BatchItem:
    return BatchItem(
        item_id=item_id,
        job_id="job_test",
        row=row_number,
        state=state,
        application=Application.model_validate(old_tom()),
        result=result,
        failure=(
            ItemFailure(code="provider_unavailable", message="x")
            if state is ItemState.FAILED
            else None
        ),
    )


def test_worst_first_leads_with_return_for_correction() -> None:
    items = [
        _item("clean", 2, ItemState.DONE, _result(Recommendation.READY_TO_APPROVE)),
        _item("review", 3, ItemState.DONE,
              _result(Recommendation.NEEDS_REVIEW, worst_verdict=Verdict.MISMATCH)),
        _item("correct", 4, ItemState.DONE,
              _result(Recommendation.RETURN_FOR_CORRECTION,
                      worst_field=FieldName.GOVERNMENT_WARNING, worst_verdict=Verdict.MISSING)),
    ]
    assert [item.item_id for item in worst_first(items)] == ["correct", "review", "clean"]


def test_a_failed_item_outranks_one_that_only_needs_review() -> None:
    """"We could not check this" is an action; "needs your eyes" is a reading task."""
    items = [
        _item("review", 2, ItemState.DONE,
              _result(Recommendation.NEEDS_REVIEW, worst_verdict=Verdict.MISMATCH)),
        _item("failed", 3, ItemState.FAILED),
    ]
    assert [item.item_id for item in worst_first(items)] == ["failed", "review"]


def test_the_warning_statement_ranks_first_among_equals() -> None:
    """The field ranking is `triage_order`'s, not a second copy of it (MATCH-10, WARN-6)."""
    items = [
        _item("brand", 2, ItemState.DONE,
              _result(Recommendation.NEEDS_REVIEW, worst_field=FieldName.BRAND_NAME,
                      worst_verdict=Verdict.MISMATCH)),
        _item("warning", 3, ItemState.DONE,
              _result(Recommendation.NEEDS_REVIEW, worst_field=FieldName.GOVERNMENT_WARNING,
                      worst_verdict=Verdict.MISMATCH)),
    ]
    assert [item.item_id for item in worst_first(items)] == ["warning", "brand"]


def test_unfinished_items_sort_last() -> None:
    items = [
        _item("queued", 2, ItemState.QUEUED),
        _item("clean", 3, ItemState.DONE, _result(Recommendation.READY_TO_APPROVE)),
    ]
    assert [item.item_id for item in worst_first(items)] == ["clean", "queued"]


def test_a_failed_item_is_never_counted_as_an_outcome() -> None:
    """A batch that reports 300 checked while 60 produced nothing is the failure mode."""
    items = [
        _item("clean", 2, ItemState.DONE, _result(Recommendation.READY_TO_APPROVE)),
        _item("failed", 3, ItemState.FAILED),
    ]
    summary = summarize(items)
    assert summary.by_recommendation["failed"] == 1
    assert summary.by_recommendation[Recommendation.READY_TO_APPROVE.value] == 1
    assert sum(summary.by_recommendation.values()) == 2


def test_the_summary_headline_names_how_many_need_attention() -> None:
    items = [
        _item("clean", 2, ItemState.DONE, _result(Recommendation.READY_TO_APPROVE)),
        _item("failed", 3, ItemState.FAILED),
        _item("review", 4, ItemState.DONE,
              _result(Recommendation.NEEDS_REVIEW, worst_verdict=Verdict.MISMATCH)),
    ]
    assert summarize(items).headline.startswith("2 applications need your attention")


def test_batch_cost_is_the_sum_of_its_items(tmp_path: Path) -> None:
    """OPS-4, LP-166 — what the job spent, from the numbers the pipeline already returned."""
    items = [
        _item(f"i{n}", n, ItemState.DONE, _result(Recommendation.READY_TO_APPROVE))
        for n in range(2, 5)
    ]
    cost = job_cost(items)
    assert cost.input_tokens == 300
    assert cost.output_tokens == 30
    assert cost.usd == pytest.approx(0.03)


# --- HTTP: upload (BATCH-3, LP-150, LP-151) -------------------------------------------


def test_the_manifest_template_downloads(tmp_path: Path) -> None:
    response = make_client(tmp_path).get("/batch/manifest-template.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.text.splitlines()[0] == ",".join(manifest_mod.COLUMNS)


def test_posting_a_manifest_queues_the_work_and_returns_a_job_id(tmp_path: Path) -> None:
    client = make_client(tmp_path, provider=spec_provider())
    response = post_batch(client, [row(), row()])

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"].startswith("job_")
    assert body["accepted"] == 2
    assert body["row_errors"] == []
    assert "2 applications queued" in body["message"]


def test_a_zip_is_accepted_in_place_of_selecting_every_file(tmp_path: Path) -> None:
    """LP-150 — the archive path, with the manifest inside it."""
    client = make_client(tmp_path, provider=spec_provider())
    archive = zip_of(
        {
            "labels/manifest.csv": manifest_csv([row()]).encode("utf-8"),
            "labels/" + GOOD_IMAGE: GOOD_BYTES,
        }
    )
    response = client.post(
        "/batch", files=[("files", ("labels.zip", archive, "application/zip"))]
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1

    drain(client)
    status = client.get(f"/batch/{response.json()['job_id']}").json()
    assert status["counts"]["done"] == 1


def test_an_archive_entry_cannot_write_outside_the_job_directory(tmp_path: Path) -> None:
    """SEC-5 — the name in a zip is attacker-controlled and is reduced to its last part."""
    client = make_client(tmp_path, provider=spec_provider())
    archive = zip_of({"../../" + GOOD_IMAGE: GOOD_BYTES})
    response = post_batch(client, [row()], images={}, archive=archive)

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert not (tmp_path.parent / GOOD_IMAGE).exists()


def test_a_corrupt_archive_is_answered_in_plain_language(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = post_batch(client, [row()], images={}, archive=b"not a zip file")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["kind"] == "user"
    assert "zip" in error["message"]
    assert error["next_step"]


def test_bad_rows_are_reported_by_number_and_the_rest_still_run(tmp_path: Path) -> None:
    """TC-20 — mixed manifest: clean rows queue, malformed rows come back numbered."""
    client = make_client(tmp_path, provider=spec_provider())
    rows = [row(), row(commodity="cider"), row(), row(alcohol_content="strong")]
    response = post_batch(client, rows)

    body = response.json()
    assert body["accepted"] == 2
    assert sorted({error["row"] for error in body["row_errors"]}) == [3, 5]
    assert "row 3, 5" in body["message"]

    drain(client)
    assert client.get(f"/batch/{body['job_id']}").json()["counts"]["done"] == 2


def test_uploaded_files_nobody_referenced_are_reported_by_name(tmp_path: Path) -> None:
    client = make_client(tmp_path, provider=spec_provider())
    response = post_batch(
        client,
        [row()],
        images={GOOD_IMAGE: GOOD_BYTES, "stray.png": GOOD_BYTES},
    )
    body = response.json()
    assert body["unmatched_files"] == ["stray.png"]
    assert "stray.png" in body["message"]


def test_a_row_naming_an_image_nobody_uploaded_is_skipped_with_its_number(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path, provider=spec_provider())
    response = post_batch(client, [row(), row(front="missing.png")])
    body = response.json()
    assert body["accepted"] == 1
    assert body["row_errors"][0]["row"] == 3
    assert "missing.png" in body["row_errors"][0]["message"]


def test_an_upload_with_no_manifest_says_what_to_send(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = post_batch(client, [], omit_manifest=True)
    assert response.status_code == 400
    assert "manifest" in response.json()["error"]["message"]


def test_a_manifest_with_nothing_queueable_is_refused_with_the_first_problem(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    response = post_batch(client, [row(commodity="cider")])
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "row 2" in message
    assert "nothing has been checked" in message


def test_an_unreadable_manifest_is_a_user_error_not_a_crash(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = post_batch(client, [], manifest_text="not,a,manifest\n1,2,3\n")
    assert response.status_code == 400
    assert "template" in response.json()["error"]["message"]


# --- HTTP: progressive results (BATCH-5, BATCH-4) -------------------------------------


def test_finished_items_are_readable_while_the_job_is_still_running(
    tmp_path: Path,
) -> None:
    """BATCH-5 — the whole reason this beats processing them one at a time."""
    provider = GatedProvider(spec_provider(), release_after=2)
    client = make_client(tmp_path, provider=provider, batch_workers=2)
    job_id = post_batch(client, [row() for _ in range(8)]).json()["job_id"]

    wait_until(
        lambda: client.get(f"/batch/{job_id}").json()["counts"]["done"] >= 2,
        what="the first two items to finish",
    )
    mid = client.get(f"/batch/{job_id}").json()
    assert mid["counts"]["done"] >= 2
    assert mid["counts"]["done"] + mid["counts"]["failed"] < mid["counts"]["total"]
    assert mid["state"] == JobState.PROCESSING.value
    assert len(mid["items"]) == mid["counts"]["done"] + mid["counts"]["failed"]
    assert mid["items"][0]["result"]["aggregate"]["recommendation"]
    assert "still running" in mid["message"]

    provider.gate.set()
    drain(client)

    final = client.get(f"/batch/{job_id}").json()
    assert final["counts"]["done"] == 8
    assert final["state"] == JobState.DONE.value
    assert "Finished" in final["message"]


def test_status_reports_counts_by_state(tmp_path: Path) -> None:
    """BATCH-4 — a ten-minute job needs observable progress."""
    client = make_client(tmp_path, provider=spec_provider())
    job_id = post_batch(client, [row() for _ in range(3)]).json()["job_id"]
    drain(client)

    counts = client.get(f"/batch/{job_id}").json()["counts"]
    assert counts == {"total": 3, "queued": 0, "processing": 0, "done": 3, "failed": 0}


def test_status_for_an_unknown_batch_explains_retention(tmp_path: Path) -> None:
    response = make_client(tmp_path).get("/batch/job_does_not_exist")
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "24 hours" in message
    assert "start a new batch" in message.lower()


def test_pending_items_are_hidden_unless_asked_for(tmp_path: Path) -> None:
    provider = GatedProvider(spec_provider(), release_after=0)
    client = make_client(tmp_path, provider=provider, batch_workers=1)
    job_id = post_batch(client, [row() for _ in range(3)]).json()["job_id"]

    body = client.get(f"/batch/{job_id}").json()
    assert body["items"] == []
    with_pending = client.get(f"/batch/{job_id}?include_pending=true").json()
    assert len(with_pending["items"]) == 3

    provider.gate.set()
    drain(client)


# --- HTTP: retry (BATCH-8) ------------------------------------------------------------


def test_retry_reruns_only_the_failed_items(tmp_path: Path) -> None:
    """LP-157 — the 290 that succeeded are not touched, and the endpoint says so."""
    provider = PoisonProvider(spec_provider())
    client = make_client(tmp_path, provider=provider)
    rows = [row(), row(front=POISON_IMAGE), row()]
    job_id = post_batch(
        client, rows, images={GOOD_IMAGE: GOOD_BYTES, POISON_IMAGE: POISON_BYTES}
    ).json()["job_id"]
    drain(client)

    before = client.get(f"/batch/{job_id}").json()
    assert before["counts"]["done"] == 2
    assert before["counts"]["failed"] == 1
    assert provider.failures == 1

    client.app.state.provider = spec_provider()
    client.app.state.batch_pool.provider_factory = lambda names: spec_provider()

    retried = client.post(f"/batch/{job_id}/retry").json()
    assert "Retrying 1 failed application" in retried["message"]
    assert "2 already checked were left alone" in retried["message"]

    drain(client)
    after = client.get(f"/batch/{job_id}").json()
    assert after["counts"]["done"] == 3
    assert after["counts"]["failed"] == 0
    assert provider.failures == 1, "a succeeded item was reprocessed"


def test_retrying_a_batch_with_no_failures_says_so(tmp_path: Path) -> None:
    client = make_client(tmp_path, provider=spec_provider())
    job_id = post_batch(client, [row()]).json()["job_id"]
    drain(client)

    body = client.post(f"/batch/{job_id}/retry").json()
    assert "no failed applications" in body["message"]
    assert body["counts"]["done"] == 1


# --- HTTP: export (BATCH-7, LP-162) ---------------------------------------------------


def test_export_carries_one_row_per_item_in_triage_order(tmp_path: Path) -> None:
    provider = PoisonProvider(spec_provider())
    client = make_client(tmp_path, provider=provider)
    rows = [row(), row(front=POISON_IMAGE), row()]
    job_id = post_batch(
        client, rows, images={GOOD_IMAGE: GOOD_BYTES, POISON_IMAGE: POISON_BYTES}
    ).json()["job_id"]
    drain(client)

    response = client.get(f"/batch/{job_id}/export.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert f"labelproof-{job_id}.csv" in response.headers["content-disposition"]

    rows_out = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows_out) == 3
    assert rows_out[0]["state"] == ItemState.FAILED.value
    assert "verdict_government_warning" in rows_out[0]
    assert rows_out[1]["recommendation"] == Recommendation.READY_TO_APPROVE.value
    assert rows_out[1]["verdict_government_warning"] == Verdict.MATCH.value


def test_export_carries_the_findings_not_only_the_verdicts(tmp_path: Path) -> None:
    """BATCH-7 — a label can match the application and still be non-compliant (TC-10)."""
    client = make_client(tmp_path, provider=SpecBackedProvider("tc10_non_standard_fill"))
    job_id = post_batch(
        client, [row(net_contents="733 mL")]
    ).json()["job_id"]
    drain(client)

    rows_out = list(csv.DictReader(io.StringIO(client.get(f"/batch/{job_id}/export.csv").text)))
    assert rows_out[0]["findings"]
    assert "net_contents" in rows_out[0]["findings"]


def test_a_failed_item_exports_its_reason(tmp_path: Path) -> None:
    client = make_client(tmp_path, provider=FailingProvider(retryable=False))
    job_id = post_batch(client, [row()]).json()["job_id"]
    drain(client)

    rows_out = list(csv.DictReader(io.StringIO(client.get(f"/batch/{job_id}/export.csv").text)))
    assert rows_out[0]["state"] == ItemState.FAILED.value
    assert "Nothing on it has been checked" in rows_out[0]["rationale"]


# --- logging (SEC-4) ------------------------------------------------------------------


def test_running_a_batch_puts_no_label_text_in_the_logs(tmp_path: Path) -> None:
    client = make_client(tmp_path, provider=spec_provider())
    # After `create_app`, which installs its own handler.
    stream = io.StringIO()
    applog.configure(stream=stream)

    job_id = post_batch(client, [row(), row(front="missing.png")]).json()["job_id"]
    drain(client)
    client.get(f"/batch/{job_id}/export.csv")

    logs = stream.getvalue()
    assert logs
    for secret in ("OLD TOM", "Bardstown", "Bourbon", GOOD_IMAGE, "missing.png"):
        assert secret not in logs


# --- scale (PERF-4, BATCH-2, BATCH-5, BATCH-6, TC-20) ---------------------------------


@pytest.mark.tc("TC-20")
def test_three_hundred_applications_isolate_failures_and_stay_readable_throughout(
    tmp_path: Path,
) -> None:
    """The batch this feature exists for, at the size Sarah described.

    Three things are asserted together because they are the same requirement in practice:
    every one of the 300 reaches a terminal state, the 30 poisoned items take only
    themselves down, and the finished ones are readable while the rest are still running.
    A 300-item job that is only inspectable at the end is a ten-minute dead spinner.
    """
    provider = PoisonProvider(GatedProvider(spec_provider(), release_after=200))
    gated: GatedProvider = provider.inner
    client = make_client(tmp_path, provider=provider, batch_workers=6)

    rows = [
        row(front=POISON_IMAGE if index % 10 == 0 else GOOD_IMAGE) for index in range(300)
    ]
    started = time.monotonic()
    accepted = post_batch(
        client, rows, images={GOOD_IMAGE: GOOD_BYTES, POISON_IMAGE: POISON_BYTES}
    ).json()
    assert accepted["accepted"] == 300
    assert accepted["row_errors"] == []

    # Progressive availability, asserted rather than raced: the gate holds the tail of the
    # batch open while the finished head is fetched over HTTP.
    job_id = accepted["job_id"]
    wait_until(
        lambda: client.get(f"/batch/{job_id}").json()["counts"]["done"] >= 100,
        timeout=120,
        what="the first hundred items to finish",
    )
    mid = client.get(f"/batch/{job_id}").json()
    assert mid["counts"]["total"] == 300
    assert 0 < mid["counts"]["done"] + mid["counts"]["failed"] < 300
    assert mid["items"], "no finished item was readable while the job ran"
    assert all(item["result"] or item["failure"] for item in mid["items"])
    assert mid["summary"]["worst_first"]

    gated.gate.set()
    drain(client, timeout=180)
    elapsed = time.monotonic() - started

    final = client.get(f"/batch/{job_id}?limit=1000").json()
    assert final["counts"]["total"] == 300
    assert final["counts"]["done"] == 270
    assert final["counts"]["failed"] == 30
    assert final["counts"]["queued"] == 0 and final["counts"]["processing"] == 0
    assert final["state"] == JobState.DONE.value
    assert provider.failures == 30

    # Isolation: the 270 unpoisoned items produced real verdicts, not a degraded shrug.
    approved = [
        item
        for item in final["items"]
        if (item["result"] or {}).get("aggregate", {}).get("recommendation")
        == Recommendation.READY_TO_APPROVE.value
    ]
    assert len(approved) == 270

    # Every failure is one an agent can act on.
    failures = [item for item in final["items"] if item["state"] == ItemState.FAILED.value]
    assert len(failures) == 30
    for item in failures:
        assert item["failure"]["message"].endswith(".")
        assert item["failure"]["next_step"]

    # Worst-first ordering is precomputed over the whole batch, failures ahead of clean.
    order = final["summary"]["worst_first"]
    assert len(order) == 300
    ranks = {item_id: index for index, item_id in enumerate(order)}
    assert max(ranks[item["item_id"]] for item in failures) < min(
        ranks[item["item_id"]] for item in approved
    )

    export = list(csv.DictReader(io.StringIO(client.get(f"/batch/{job_id}/export.csv").text)))
    assert len(export) == 300

    # Not the ten-minute gate itself — the fake provider is instant — but a floor that
    # catches the regression that matters: workers serialising instead of running.
    assert elapsed < 120, f"300 fixture-backed items took {elapsed:.1f}s"
