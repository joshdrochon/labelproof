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

import asyncio
import csv
import io
import json
import threading
import time
import tracemalloc
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import logging as applog
from api import main as main_mod
from api.batch import manifest as manifest_mod
from api.batch import store as store_mod
from api.batch import worker as pool_mod
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


def make_config_api(**overrides: Any) -> Config:
    """A config with no storage directory, for the paths that never touch the store."""
    base: dict[str, Any] = {"use_fake_provider": True}
    base.update(overrides)
    return Config(**base)


def make_client(tmp_path: Path, provider: Any = None, **overrides: Any) -> TestClient:
    """The shipped app, exactly as `create_app` builds it.

    Nothing is added here on purpose. An earlier version of this helper mounted the batch
    router itself when it found it missing, which made every test in this file pass
    against an app whose batch endpoints were unreachable in production. The router is
    mounted by `api/main.py` or these tests fail, which is the point.
    """
    app = create_app(config=make_config(tmp_path, **overrides), provider=provider)
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


# --- wiring (LP-073) --------------------------------------------------------------------


def test_the_shipped_app_mounts_the_batch_router(tmp_path: Path) -> None:
    """Everything below this line reaches batch over HTTP, so it all depends on this line.

    Stated once and by name, because a diffuse failure across forty tests reads as "batch
    is broken" when the actual fault is one missing `include_router` in the app factory.
    """
    app = create_app(config=make_config(tmp_path))
    mounted = [
        route
        for route in app.router.routes
        if getattr(route, "original_router", None) is batch_routes.router
    ]
    assert mounted, "api/main.py does not mount api.routes.batch"


# --- upload memory (BATCH-2, PERF-4) ----------------------------------------------------
#
# A real dump is 300 applications and ~600 photographs, over a gigabyte. The route used to
# read every part into a list and check the total afterwards, so peak memory was a multiple
# of the upload and the container was OOM-killed — which kills six workers mid-item and
# takes per-item isolation down with them. Nothing bounded it except a 41 MB whole-request
# ceiling that also refused every real batch, so raising that ceiling and spooling to disk
# are one fix, not two.


def multipart_stream(
    parts: Sequence[tuple[str, str, bytes | Iterator[bytes]]],
    boundary: str = "----labelproofprobe",
) -> Iterator[bytes]:
    """Yield a multipart body in pieces, never assembling it.

    A single shared chunk object is re-yielded for bulk content on purpose: the point of
    the measurement is what the *server* retains, so the generator must not put the upload
    in memory on the client's behalf.
    """
    for field, filename, content in parts:
        yield (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        if isinstance(content, bytes):
            yield content
        else:
            yield from content
        yield b"\r\n"
    yield f"--{boundary}--\r\n".encode()


def asgi_post(
    app: Any, path: str, body: Iterator[bytes], sent_bytes: list[int] | None = None
) -> tuple[int, Any]:
    """POST by driving the ASGI app directly, with a body that is never resident in full.

    TestClient cannot be used here. httpx does `b"".join(self.stream)` before it sends, so
    the whole upload lands in memory on the client side — which both hides whether the
    server did the same and makes any measurement meaningless. Driving the app by hand is
    also the only way to send a body with NO `Content-Length`: that is the shape a
    `Transfer-Encoding: chunked` upload arrives in, and it skips the whole-request ceiling
    in `api/main.py` entirely, so a cap has to hold without it.

    `sent_bytes`, if given, accumulates how much the client actually handed over. That is
    the number that matters for a disk bound: a refusal that arrives after the client has
    finished uploading has refused nothing.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"multipart/form-data; boundary=----labelproofprobe"),
            (b"transfer-encoding", b"chunked"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, Any]:
        try:
            chunk = next(body)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        if sent_bytes is not None:
            sent_bytes.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": True}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))

    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(payload) if payload else None


ONE_MB = b"\x89PNG\r\n\x1a\n" + b"q" * (1024 * 1024 - 8)


def megabytes(count: int) -> Iterator[bytes]:
    for _ in range(count):
        yield ONE_MB


def test_a_large_multi_part_upload_is_spooled_rather_than_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """120 MB of artwork across 15 parts must not cost 120 MB of RSS.

    Measured with tracemalloc around the whole request, so what is asserted is what the
    process retained while the route ran. Before the fix this tracked the upload almost
    exactly. Every part is named by the manifest, so this exercises the ACCEPT path —
    expansion, pairing, and 15 renames into the job directory — not just the refusal.

    The worker pool is held back for the duration. It starts before `POST /batch` returns,
    and a worker decoding an 8 MB image legitimately holds 8 MB — measured at 42 MB of
    perfectly correct verification noise, which would have set the ceiling here by
    accident and measured the wrong subsystem.
    """
    monkeypatch.setattr(WorkerPool, "start", lambda self: None)
    names = [f"f{n}.png" for n in range(15)]
    app = create_app(config=make_config(tmp_path), provider=spec_provider())
    body = multipart_stream(
        [("manifest", "manifest.csv", manifest_csv([row(front=n) for n in names]).encode())]
        + [("files", name, megabytes(8)) for name in names]
    )

    tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        status, payload = asgi_post(app, "/batch", body)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert status == 200, payload
    assert payload["accepted"] == 15
    assert peak < 24 * 1024 * 1024, (
        f"the route retained {peak / 1e6:.0f} MB while 120 MB was uploaded — the upload "
        f"is being held, not spooled"
    )


def test_a_single_huge_part_is_spooled_rather_than_held(tmp_path: Path) -> None:
    """One 64 MB part, which is the real shape: an agent uploads one `labels.zip`.

    The multi-part test above cannot catch per-part materialization — with 8 MB parts,
    reading each one whole still fits under any sane ceiling. Here the single part is
    larger than the ceiling, so `data = await upload.read()` fails outright and only
    genuine chunking passes.
    """
    app = create_app(config=make_config(tmp_path), provider=spec_provider())
    body = multipart_stream([("files", "one_big.png", megabytes(64))])

    tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        status, payload = asgi_post(app, "/batch", body)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Refused for being over the per-file cap — after it was streamed, which is the point.
    assert status == 400
    assert payload["error"]["code"] == "file_too_large"
    assert peak < 24 * 1024 * 1024, (
        f"the route retained {peak / 1e6:.0f} MB for a single 64 MB part — it is being "
        f"read whole, not chunked"
    )


def test_a_chunked_upload_is_cut_off_at_the_wire_not_after_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disk bound, and the only test here that measures the thing that matters.

    A `Transfer-Encoding: chunked` POST sends no `Content-Length`, so the header check in
    `api/main` is skipped by construction. Everything inside the route is downstream of
    Starlette's multipart parser, which drains the socket into temp files before the route
    function starts — so a cap enforced in `create_batch` fires only once the whole upload
    is already on the volume. Measured before `_WireLimit`: cap 1 MB, 200 MB sent, 200 MB
    written, refusal on the last byte. The volume holds `jobs.db`, so filling it takes
    every batch on the server with it.

    The assertion is therefore about how much the CLIENT got to send, not about the status
    code — a refusal after the upload finished has refused nothing.
    """
    monkeypatch.setattr(batch_routes, "MAX_TOTAL_BYTES", 4 * 1024 * 1024)
    app = create_app(config=make_config(tmp_path), provider=spec_provider())

    sent: list[int] = []
    status, payload = asgi_post(
        app, "/batch", multipart_stream([("files", "big.png", megabytes(256))]), sent
    )

    assert status == 400
    assert payload["error"]["code"] == "batch_too_large"
    assert payload["error"]["next_step"] == "reduce"

    ceiling = 4 * 1024 * 1024 + main_mod._BATCH_ENVELOPE_BYTES
    accepted = sum(sent)
    assert accepted <= ceiling + 2 * 1024 * 1024, (
        f"the client uploaded {accepted / 1e6:.0f} MB before being cut off, against a "
        f"{ceiling / 1e6:.0f} MB ceiling — the body is being taken in full and refused after"
    )
    assert accepted < 256 * 1024 * 1024


def test_the_wire_limit_leaves_verify_now_alone() -> None:
    """The tight single-verify ceiling still applies on its own path, over the wire."""
    app = create_app(config=make_config_api(max_image_bytes=1024 * 1024, max_images=2))
    sent: list[int] = []
    status, payload = asgi_post(
        app, "/verify", multipart_stream([("images", "big.png", megabytes(64))]), sent
    )
    assert status == 400
    assert payload["error"]["code"] == "file_too_large"
    assert "2 images of up to 1 MB each" in payload["error"]["message"]
    assert sum(sent) < 64 * 1024 * 1024


def longest_loop_block_ms(app: Any, path: str, body: Iterator[bytes]) -> tuple[int, float]:
    """Drive one request and report the longest stretch the event loop went unserved.

    A heartbeat coroutine does nothing but `await asyncio.sleep(0)` in a loop, which yields
    to anything else that is ready. The largest gap between two of its ticks is, by
    definition, the longest uninterrupted block of synchronous work in the process.
    """
    gaps: list[float] = []

    async def drive() -> int:
        running = True

        async def heartbeat() -> None:
            last = time.perf_counter()
            while running:
                await asyncio.sleep(0)
                now = time.perf_counter()
                gaps.append((now - last) * 1000)
                last = now

        async def receive() -> dict[str, Any]:
            try:
                return {"type": "http.request", "body": next(body), "more_body": True}
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"multipart/form-data; boundary=----labelproofprobe"),
                (b"transfer-encoding", b"chunked"),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }

        beat = asyncio.create_task(heartbeat())
        try:
            await app(scope, receive, send)
        finally:
            running = False
            await asyncio.sleep(0)
            beat.cancel()
        return int(next(m["status"] for m in sent if m["type"] == "http.response.start"))

    status = asyncio.run(drive())
    return status, max(gaps)


#: The longest the event loop may go unserved during one batch upload. Measured at 848 ms
#: before `_assemble` moved off the loop and 54 ms after, from the same 2.8 MB request.
#: 250 ms catches that regression with room for a contended CI box, and it is well inside
#: what a concurrent verification can absorb.
LOOP_BLOCK_CEILING_MS = 250


def test_a_zip_upload_does_not_stall_the_event_loop(tmp_path: Path) -> None:
    """PERF-5, BATCH-9 — the priority rule is worthless if the loop itself is dead.

    Zip expansion, CSV parsing, renames and SQLite are all blocking, and they used to run
    straight from `async def create_batch`. A 300-entry archive — 2.8 MB on the wire,
    1017x amplification — blocked the loop for 848 ms in one stretch, during which a
    concurrent `GET /health` got one sample in the whole second. `ProviderBudget` cannot
    help with this: it rations model slots, and a dead loop is not a slot problem. So the
    entire Verify-Now-keeps-priority invariant was defeated by a 2.8 MB upload.
    """
    entries = {f"f{n}.png": b"\x00" * (9 * 1024 * 1024) for n in range(200)}
    entries["manifest.csv"] = manifest_csv([row(front=name) for name in entries]).encode()
    archive = zip_of(entries)
    assert len(archive) < 4 * 1024 * 1024, "the point is that a small request does this"

    app = create_app(config=make_config(tmp_path), provider=spec_provider())
    status, worst = longest_loop_block_ms(
        app, "/batch", multipart_stream([("files", "labels.zip", archive)])
    )

    assert status in (200, 400)
    assert worst < LOOP_BLOCK_CEILING_MS, (
        f"the event loop went unserved for {worst:.0f} ms during a "
        f"{len(archive) / 1e6:.1f} MB upload — blocking work is running on it"
    )


def test_a_refused_upload_leaves_no_artwork_behind(tmp_path: Path) -> None:
    """SEC-2 — a rejected gigabyte must not sit in staging waiting for a sweep."""
    client = make_client(tmp_path)
    response = post_batch(client, [], manifest_text="not,a,manifest\n1,2,3\n")
    assert response.status_code == 400

    store: BatchStore = client.app.state.batch_store
    assert list(store.staging_root.iterdir()) == []


def test_accepted_artwork_is_moved_out_of_staging_not_copied(tmp_path: Path) -> None:
    """The staged file BECOMES the stored file — asserted on the inode, not on tidiness.

    The previous version of this test checked that staging was empty afterwards and that
    the image was readable. Both hold under a copy, because `staging()`'s `finally` deletes
    the directory either way, so the property in the test's name was unasserted and
    `source.replace(...)` -> `shutil.copyfile(...)` left the suite green. Only identity
    distinguishes a move from a copy, and identity is what bounds peak disk on a 1.2 GB
    dump.
    """
    client = make_client(tmp_path, provider=spec_provider())
    store_cls = BatchStore
    original = store_cls.adopt_image
    staged_inode: dict[str, int] = {}

    def spy(self: BatchStore, job_id: str, supplied_name: str, source: Path) -> None:
        staged_inode[supplied_name] = source.stat().st_ino
        original(self, job_id, supplied_name, source)

    store_cls.adopt_image = spy  # type: ignore[method-assign]
    try:
        job_id = post_batch(client, [row()]).json()["job_id"]
    finally:
        store_cls.adopt_image = original  # type: ignore[method-assign]

    store: BatchStore = client.app.state.batch_store
    stored = store.images_root / job_id / stored_name(GOOD_IMAGE)
    assert staged_inode[GOOD_IMAGE] == stored.stat().st_ino, (
        "the stored file is a different inode from the staged one — it was copied, not moved"
    )
    assert list(store.staging_root.iterdir()) == []
    assert store.read_image(job_id, GOOD_IMAGE) == GOOD_BYTES
    drain(client)


def test_staging_a_killed_process_left_behind_is_swept(tmp_path: Path) -> None:
    """`staging()` cleans up after itself on every ordinary path; this is for SIGKILL.

    Which is precisely when a gigabyte of someone's label artwork is most likely to be
    sitting there and least likely to be noticed.
    """
    store = BatchStore(tmp_path)
    abandoned = store.staging_root / "up_deadprocess"
    abandoned.mkdir()
    (abandoned / "0000001").write_bytes(GOOD_BYTES)

    assert store.purge_staging(now=time.time()) == 0, "a fresh upload was swept mid-flight"
    assert store.purge_staging(now=time.time() + 7200) == 1
    assert not abandoned.exists()


def test_the_ttl_sweep_reaches_staging_too(tmp_path: Path) -> None:
    """`purge_expired` is the entry point the timed sweeper calls, so staging hangs off it.

    `api/retention.py`'s sweeper walks MANAGED_SUBDIRS = ("batches", "uploads", "results")
    and has never heard of `staging/`. Without this, a directory left by a SIGKILL — the
    case where a gigabyte of label artwork is most likely to be sitting there — would be
    collected only when a new POST /batch arrived, which is precisely the traffic
    dependence the timed sweeper exists to remove (SEC-2).
    """
    store = BatchStore(tmp_path)
    abandoned = store.staging_root / "up_deadprocess"
    abandoned.mkdir()
    (abandoned / "0000001").write_bytes(GOOD_BYTES)

    store.purge_expired(now=time.time() + 7200)
    assert not abandoned.exists(), "the TTL sweep walked past an abandoned staging directory"


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


def expire(client: TestClient, job_id: str) -> None:
    """Push a job's TTL into the past without sweeping it, which is the real situation.

    Purging is driven by POST /batch. A server that takes one dump and goes quiet has
    exactly this on disk: a job past its life that nothing has come along to delete.
    """
    store: BatchStore = client.app.state.batch_store
    # Reaches into the store on purpose: there is no public setter for `expires_at` and
    # there should not be one. Only the clock moves a job past its life in production.
    with store._conn() as connection:
        connection.execute(
            "UPDATE jobs SET expires_at = ? WHERE job_id = ?", (time.time() - 60, job_id)
        )


def test_an_expired_batch_is_not_served_even_before_it_is_swept(tmp_path: Path) -> None:
    """SEC-2 — the promise is about what we hand back, not only about what we store.

    Between expiry and the next upload there is no sweep, and without an expiry check on
    the read paths the API went on serving the whole job: status, items, and an export
    carrying 300 applications' brand names and extracted label text — while the 'not
    found' message on the very same endpoint told the caller it had been deleted hours
    ago. Retaining data past a promise is a bug; answering with it while denying you have
    it is a false statement to a government user.
    """
    client = make_client(tmp_path, provider=spec_provider())
    job_id = post_batch(client, [row()]).json()["job_id"]
    drain(client)
    assert client.get(f"/batch/{job_id}").status_code == 200

    expire(client, job_id)

    for path in (f"/batch/{job_id}", f"/batch/{job_id}/export.csv"):
        response = client.get(path)
        assert response.status_code == 400, path
        assert response.json()["error"]["code"] == "batch_not_found", path

    retry = client.post(f"/batch/{job_id}/retry")
    assert retry.status_code == 400
    assert retry.json()["error"]["code"] == "batch_not_found"


def test_an_expired_export_leaks_no_label_text(tmp_path: Path) -> None:
    """The export is the leak that matters: every extracted field of every application."""
    client = make_client(tmp_path, provider=spec_provider())
    job_id = post_batch(client, [row()]).json()["job_id"]
    drain(client)
    assert "OLD TOM" in client.get(f"/batch/{job_id}/export.csv").text

    expire(client, job_id)
    body = client.get(f"/batch/{job_id}/export.csv").text
    assert "OLD TOM" not in body
    assert "Bardstown" not in body


def test_an_expired_batch_is_not_processed_either(tmp_path: Path) -> None:
    """Refusing to serve it and refusing to produce more of it are the same promise.

    `claim()` selected on `state` alone, so past the 24-hour mark the workers carried on:
    tokens spent, and freshly extracted brand names and label text written to disk, for a
    job the API was simultaneously telling the caller had been deleted. Stopping only the
    read path fixed the visible half and left the tool still manufacturing the data.
    """
    store = BatchStore(tmp_path)
    config = make_config(tmp_path)
    job = store.create_job(retention_hours=24)
    store.save_image(job.job_id, GOOD_IMAGE, GOOD_BYTES)
    store.add_items(
        job.job_id,
        [(n + 2, Application.model_validate(old_tom()), [GOOD_IMAGE]) for n in range(3)],
    )
    assert store.counts(job.job_id).queued == 3

    with store._conn() as connection:
        connection.execute(
            "UPDATE jobs SET expires_at = ? WHERE job_id = ?", (time.time() - 60, job.job_id)
        )

    pool = WorkerPool(store, config, lambda names: spec_provider())
    pool.start()
    assert pool.drain(timeout=30)

    counts = store.counts(job.job_id)
    assert counts.queued == 3, "an expired job was still being worked"
    assert counts.done == 0 and counts.failed == 0
    assert all(item.result is None for item in store.items(job.job_id))


def test_the_expiry_predicate_agrees_with_retentions(tmp_path: Path) -> None:
    """One definition or none. Skips until `api/retention.py` lands on this branch.

    `api.retention.is_expired` is the canonical predicate and its own docstring forbids
    copies. It is not on `build/phase-1` yet, so `api.batch.store.is_expired` carries the
    definition with a merge note. This is the tripwire: the moment both exist, a
    disagreement fails here rather than surfacing as the API serving something the sweeper
    thinks is gone.
    """
    retention = pytest.importorskip("api.retention")
    now = time.time()
    for offset in (-3600.0, -1.0, 0.0, 1.0, 3600.0):
        assert store_mod.is_expired(now + offset, now=now) == retention.is_expired(
            now + offset, now=now
        ), f"predicates disagree at offset {offset}"


def test_an_expired_batch_answers_the_same_way_as_one_that_never_existed(
    tmp_path: Path,
) -> None:
    """Same fact from the agent's seat: the batch is gone and a new one is needed."""
    client = make_client(tmp_path, provider=spec_provider())
    job_id = post_batch(client, [row()]).json()["job_id"]
    drain(client)
    expire(client, job_id)

    expired = client.get(f"/batch/{job_id}").json()["error"]
    missing = client.get("/batch/job_never_existed").json()["error"]
    assert expired == missing
    assert "24 hours" in expired["message"]


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

    Runs over HTTP against the shipped app. The priority middleware is installed by
    `create_app` and not by this test: a test that wires up the thing it is measuring can
    pass while the deployed process has no priority rule at all.
    """
    client = make_client(
        tmp_path,
        provider=SlowProvider(spec_provider(), delay=0.05),
        batch_workers=4,
    )
    app = client.app

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


def _priced_item(
    *,
    row: int,
    usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
) -> BatchItem:
    """A finished item carrying an explicit Cost, for the aggregation tests below."""
    result = _result(Recommendation.READY_TO_APPROVE)
    result.cost = Cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_write,
        usd=usd,
    )
    return _item(f"item{row}", row, ItemState.DONE, result)


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


def zip_with_corrupt_entry() -> bytes:
    """A zip whose central directory is fine and whose compressed stream is not.

    This is what a 1.2 GB dump copied off a flaky share looks like: the archive opens, the
    listing is right, and one entry blows up on decompression.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(GOOD_IMAGE, GOOD_BYTES)
    raw = bytearray(buffer.getvalue())
    middle = len(raw) // 2
    for offset in range(middle, middle + 40):
        raw[offset] ^= 0xFF
    return bytes(raw)


def zip_with_unsupported_compression() -> bytes:
    """compress_type 99 — WinZip AES. `zipfile` raises NotImplementedError on read."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(GOOD_IMAGE, GOOD_BYTES)
    raw = bytearray(buffer.getvalue())
    raw[8:10] = (99).to_bytes(2, "little")
    central = raw.rfind(b"PK\x01\x02")
    raw[central + 10 : central + 12] = (99).to_bytes(2, "little")
    return bytes(raw)


def zip_with_truncated_entry() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(GOOD_IMAGE, GOOD_BYTES)
    raw = buffer.getvalue()
    central = raw.rfind(b"PK\x01\x02")
    return raw[: central - 60] + raw[central:]


@pytest.mark.parametrize(
    "archive",
    [
        pytest.param(zip_with_corrupt_entry(), id="corrupt-deflate"),
        pytest.param(zip_with_unsupported_compression(), id="unsupported-compression"),
        pytest.param(zip_with_truncated_entry(), id="truncated-entry"),
    ],
)
def test_an_unreadable_archive_entry_is_a_user_error_not_a_500(
    tmp_path: Path, archive: bytes
) -> None:
    """The archive opens and the listing is fine — it is decompression that fails.

    Every one of these left as a 500 saying "something went wrong on our side" with
    next_step=retry: advice that is wrong, infinitely repeatable, and hides the one fact
    that would let the agent act — that a named file in their archive is damaged. It also
    contradicts the rule stated at the top of `api/main.py`, that no path out of this app
    emits a framework default.
    """
    client = make_client(tmp_path)
    response = post_batch(client, [row()], images={}, archive=archive)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["kind"] == "user"
    assert error["code"] == "unreadable_archive_entry"
    assert error["next_step"] == "replace"
    assert GOOD_IMAGE in error["message"], "the agent is not told which file is bad"
    assert "went wrong on our side" not in error["message"]


def test_an_over_cap_batch_inside_an_archive_is_still_a_size_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry-read guard must not swallow our own refusals into "damaged archive"."""
    monkeypatch.setattr(batch_routes, "MAX_TOTAL_BYTES", 1024)
    client = make_client(tmp_path)
    response = post_batch(
        client, [row()], images={}, archive=zip_of({GOOD_IMAGE: GOOD_BYTES})
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "batch_too_large"


def test_two_files_with_one_name_are_refused_rather_than_silently_resolved(
    tmp_path: Path,
) -> None:
    """A DAM export laid out `front/x.png` + `back/x.png` used to store whichever was last.

    Names are reduced to their last segment, so the two collide. The manifest addresses
    images by name, which makes it genuinely ambiguous which one a row means — and the old
    `files[clean] = path` picked silently, reported nothing in `unmatched_files`, and
    showed the agent a verdict about a picture they did not send. Verified before the fix:
    front=GOOD, back=POISON, POISON stored, no warning anywhere.
    """
    client = make_client(tmp_path, provider=spec_provider())
    archive = zip_of(
        {
            "front/" + GOOD_IMAGE: GOOD_BYTES,
            "back/" + GOOD_IMAGE: POISON_BYTES,
            "manifest.csv": manifest_csv([row()]).encode("utf-8"),
        }
    )
    response = client.post(
        "/batch", files=[("files", ("labels.zip", archive, "application/zip"))]
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "duplicate_file_name"
    assert GOOD_IMAGE in error["message"]
    assert "nothing has been checked" in error["message"].lower()


def test_one_image_named_by_two_rows_is_still_fine(tmp_path: Path) -> None:
    """The complement: sharing an image across rows is normal and must keep working."""
    client = make_client(tmp_path, provider=spec_provider())
    response = post_batch(client, [row(), row()])
    assert response.status_code == 200
    assert response.json()["accepted"] == 2
    drain(client)


def test_an_oversized_archive_entry_is_refused_before_it_is_decompressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing zip-bomb check: the declared size, read before any decompression.

    `unpack`'s read bound was described as the check that stops a bomb. It is not —
    CPython's `ZipExtFile` never returns more than the declared `file_size`, so this
    comparison is what actually refuses one. Untested until now, which is how the wrong
    story survived in the docstring.

    The assertion has to be that `unpack` was never called. Checking only the status code
    passes with the declared-size check deleted, because the `+1` read bound then produces
    `cap + 1` bytes and `add`'s own size check refuses that instead — same response, after
    writing every entry to disk. Across a 4000-entry archive that is the whole difference
    between refusing a bomb and expanding one.
    """
    unpacked: list[str] = []
    original = batch_routes._Landing.unpack

    def spy(self: Any, archive: Any, entry: Any, limit: int) -> Path:
        unpacked.append(entry.filename)
        return original(self, archive, entry, limit)

    monkeypatch.setattr(batch_routes._Landing, "unpack", spy)

    client = make_client(tmp_path, max_image_bytes=64 * 1024)
    archive = zip_of({GOOD_IMAGE: b"\x00" * (256 * 1024)})
    response = post_batch(client, [row()], images={}, archive=archive)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "file_too_large"
    assert error["next_step"] == "resize"
    assert GOOD_IMAGE in error["message"]
    assert unpacked == [], "the oversized entry was decompressed before being refused"


def test_too_many_selected_files_says_so_and_points_at_the_zip(tmp_path: Path) -> None:
    """Starlette caps a multipart body at 1000 parts, below our own MAX_FILES of 4000.

    So for multi-select that limit always binds first, and FastAPI wraps it into a bare
    400. An agent who ctrl-A'd 1200 label images was told "That address is not part of
    this tool. Go back to the verification page and try again" — no number, no limit, and
    the wrong category of problem entirely.
    """
    client = make_client(tmp_path)
    files: list[tuple[str, tuple[str, bytes, str]]] = [
        ("manifest", ("manifest.csv", manifest_csv([row()]).encode(), "text/csv"))
    ]
    files.extend(("files", (f"f{n}.png", b"x", "image/png")) for n in range(1500))

    response = client.post("/batch", files=files)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "too_many_files"
    assert "1000" in error["message"]
    assert "zip" in error["message"]
    assert "Go back to the verification page" not in error["message"]


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


def test_every_image_line_says_which_item_it_belongs_to(tmp_path: Path) -> None:
    """Six workers interleaving, `image_index` only ever 0 or 1 — attribution or nothing.

    These lines exist to answer one question: which application was pre-gated, and on
    which defect. A worker thread inherits no ContextVar, so the request ID that
    attributes every interactive line is empty here; without job_id and item_id the whole
    stream is unreadable and the lines are pure noise (OPS-1).
    """
    client = make_client(tmp_path, provider=spec_provider())
    stream = io.StringIO()
    applog.configure(stream=stream)

    job_id = post_batch(client, [row(), row()]).json()["job_id"]
    drain(client)

    scored = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line and json.loads(line).get("event") == "image_scored"
    ]
    assert len(scored) == 2
    assert {entry["job_id"] for entry in scored} == {job_id}
    assert len({entry["item_id"] for entry in scored}) == 2, "two items, two item_ids"


def test_the_interactive_path_still_attributes_by_request_id(tmp_path: Path) -> None:
    """The complement: Verify Now needs no `owner`, because the ContextVar carries it."""
    client = make_client(tmp_path, provider=spec_provider())
    stream = io.StringIO()
    applog.configure(stream=stream)

    response = client.post(
        "/verify",
        files=[("images", (GOOD_IMAGE, GOOD_BYTES, "image/png"))],
        data={"application": json.dumps(old_tom())},
    )
    assert response.status_code == 200

    scored = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line and json.loads(line).get("event") == "image_scored"
    ]
    assert scored and all(
        entry["request_id"] == response.headers["X-Request-ID"] for entry in scored
    )
    # Present and null rather than absent, so one query shape reads both modes.
    assert all(entry["job_id"] is None and entry["item_id"] is None for entry in scored)


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


# --------------------------------------------------------------------------------------
# Cost accounting, found by a real 22-application batch on the deployed URL
# --------------------------------------------------------------------------------------


def test_a_batch_reports_dollars_and_not_just_tokens() -> None:
    """A live 22-application batch reported 40,507 input tokens and **$0.00**.

    Pricing was applied in `api/routes/verify.py` after `api.verify.verify` returned, so
    only requests arriving through that route were ever priced. The batch worker calls
    the function directly. Every item came back with real token counts and `usd = 0.0`,
    which does not read as "unknown" — it reads as free, on the one number OPS-4 exists
    to report and the one a cost analysis is built from.

    Asserted on the aggregate rather than on the pricing helper, because the helper was
    always correct. What was wrong was which callers reached it.
    """
    items = [
        _priced_item(row=1, usd=0.031, input_tokens=1849, cache_read=4351),
        _priced_item(row=2, usd=0.029, input_tokens=1849, cache_read=4351),
    ]
    total = job_cost(items)

    assert total.usd == pytest.approx(0.060)
    assert total.input_tokens == 3698


def test_the_job_total_counts_cached_tokens_too() -> None:
    """Both cache counters are billed, and both were being dropped.

    A cached read costs a tenth of an input token and writing an entry costs 1.25x one,
    and neither is inside `input_tokens`. The live batch read 4,351 cached tokens on
    every one of 22 items and the job total said `cache_read_tokens: 0`. Dropping them
    does not make the total conservative; it prices those tokens at nothing.
    """
    total = job_cost(
        [
            _priced_item(row=1, usd=0.01, input_tokens=100, cache_read=4351, cache_write=200),
            _priced_item(row=2, usd=0.01, input_tokens=100, cache_read=4351, cache_write=0),
        ]
    )

    assert total.cache_read_tokens == 8702
    assert total.cache_creation_tokens == 200


def test_an_unfinished_item_contributes_nothing() -> None:
    """Queued and failed items have no result, and must not be counted as free work."""
    finished = _priced_item(row=1, usd=0.03, input_tokens=1849, cache_read=4351)
    pending = _item("pending", 2, ItemState.QUEUED, None)

    assert job_cost([finished, pending]) == job_cost([finished])


# --------------------------------------------------------------------------------------
# CSV export is opened in a spreadsheet, and printed (BATCH-7)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "=cmd|'/c calc'!A0",
        "+1+1",
        "-2+3",
        "@SUM(1:1)",
        "\tleading tab",
        "\rleading return",
    ],
)
def test_a_cell_a_spreadsheet_would_execute_is_neutralised(hostile: str) -> None:
    """`csv.writer` quotes; quoting is not this protection.

    Quoting keeps a value in one cell. Excel still EVALUATES `=cmd|'/c calc'!A0` when the
    file is opened, and four columns of this export carry text nobody here controls:
    `brand_name` and `class_type` come straight from an uploaded manifest, and `findings`
    and `rationale` quote label text the model read off the artwork. A brand that prints a
    leading `=` is enough — no attacker required.

    This is the export the PRD says gets printed and handed upward, so the consequence
    lands in a case file.
    """
    assert batch_routes._csv_safe(hostile).startswith("'")


@pytest.mark.parametrize("ordinary", ["Old Tom Distillery", "750 mL", "45% Alc./Vol.", ""])
def test_ordinary_values_are_left_exactly_as_they_are(ordinary: str) -> None:
    """The guard must not put an apostrophe on every cell in the file."""
    assert batch_routes._csv_safe(ordinary) == ordinary


def test_the_export_route_applies_the_guard() -> None:
    """End to end over HTTP, because the sanitiser existing is not the same as it running.

    A helper defined and not called is how the confidence floor survived this whole build.
    """
    config = Config(use_fake_provider=True)
    app = create_app(config=config, provider=SpecBackedProvider("tc01_old_tom_clean"))
    client = TestClient(app)

    manifest = (
        "commodity,brand_name,class_type,alcohol_content,net_contents,producer_name,"
        "producer_address,country_of_origin,is_import,front_image,back_image\r\n"
        "spirits,=cmd|'/c calc'!A0,Whiskey,45,750 mL,Someone,Somewhere,,false,"
        "tc01_old_tom_clean.png,\r\n"
    )
    image = (ROOT / "fixtures" / "labels" / "tc01_old_tom_clean.png").read_bytes()
    accepted = client.post(
        "/batch",
        files=[
            ("manifest", ("m.csv", manifest.encode(), "text/csv")),
            ("files", ("tc01_old_tom_clean.png", image, "image/png")),
        ],
    )
    assert accepted.status_code == 200, accepted.text
    job_id = accepted.json()["job_id"]

    body = client.get(f"/batch/{job_id}/export.csv").text
    assert "=cmd" in body, "the value should still be readable, just not executable"
    assert ",=cmd" not in body and body.count("'=cmd") == 1, body[:400]


# --- cold-start wiring (LP-334) -------------------------------------------------------


def test_a_cold_start_under_load_builds_exactly_one_store_and_one_pool(
    tmp_path: Path,
) -> None:
    """LP-334. `get_store` and `get_pool` were check-then-assign, and they are reached
    from a THREAD POOL — both are sync dependencies, so Starlette hands each to a worker
    thread and two requests arriving together on a cold machine ran them concurrently.

    The losing thread's objects were not merely wasted. Two stores each ran `recover()`,
    so a job left `processing` by a dead process could be requeued twice; two pools meant
    two sets of worker threads against one SQLite file and two independent
    `ProviderBudget`s, quietly doubling the concurrency ceiling the pool exists to
    enforce; and the orphaned pool kept its threads with nothing to shut them down.

    Counting constructions rather than asserting `is` identity: identity only shows the
    survivor, and the bug is the object that was built and thrown away.
    """
    built: list[str] = []
    real_store = store_mod.BatchStore
    real_pool = pool_mod.WorkerPool

    class CountingStore(real_store):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append("store")
            super().__init__(*args, **kwargs)

    class CountingPool(real_pool):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append("pool")
            super().__init__(*args, **kwargs)

    app = create_app(config=make_config(tmp_path))
    request = SimpleNamespace(app=app)

    with (
        mock.patch.object(batch_routes, "BatchStore", CountingStore),
        mock.patch.object(batch_routes, "WorkerPool", CountingPool),
    ):
        # A barrier, so the threads are genuinely inside the window rather than merely
        # started at roughly the same time. Without it this test passes on a bug.
        gate = threading.Barrier(8)

        def cold_hit() -> None:
            gate.wait()
            batch_routes.get_pool(cast(Any, request))

        threads = [threading.Thread(target=cold_hit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

    assert [t.is_alive() for t in threads] == [False] * 8, (
        "a wiring thread never finished — the lock is likely being taken twice on one "
        "thread. `_WIRING` is a Lock, not an RLock, and `get_pool` must resolve the "
        "store BEFORE it acquires."
    )
    assert built.count("store") == 1, f"built {built.count('store')} stores: {built}"
    assert built.count("pool") == 1, f"built {built.count('pool')} pools: {built}"
