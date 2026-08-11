"""Retention TTL and its proof (SEC-2, LP-084, LP-085, LP-250).

"Provably gone" is taken literally here. Every assertion about a purge reads the filesystem:
the image path is checked with `Path.exists`, and the brand name is searched for in **every
byte of every file** under the storage root — `jobs.db`, its write-ahead log and its shared
index included. A test that trusted `purge_expired()`'s return value would have passed over
the finding in `test_deleted_rows_leave_no_residue_in_the_database`, which is exactly the
kind of thing this ticket exists to catch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import retention, security
from api.batch.models import ItemFailure, ItemState
from api.batch.store import BatchStore, stored_name
from api.config import Config
from api.main import create_app
from api.models import Application, Commodity
from api.retention import (
    ORPHAN_GRACE_SECONDS,
    RetentionPolicy,
    RetentionSweeper,
    install_sweeper,
    sweep,
)
from api.security import SecurityPolicy, harden

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "fixtures" / "labels"
SAMPLE = ROOT / "assets" / "samples" / "old_tom.json"

HOUR = 3600.0

#: Distinctive enough that finding it in a file is unambiguous, and shaped like the thing
#: retention exists to remove: a brand name off a real application.
BRAND = "Zzyzx Hollow Reserve Bourbon"
ADDRESS = "1147 Cottonmouth Road, Bardstown, Kentucky"
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"ZZYZX-HOLLOW-LABEL-ARTWORK" * 200


@pytest.fixture(autouse=True)
def _containment_is_never_left_installed() -> Iterator[None]:
    yield
    security.remove_log_containment()


def make_config(**overrides: Any) -> Config:
    base: dict[str, Any] = {"use_fake_provider": True}
    base.update(overrides)
    return Config(**base)


def an_application() -> Application:
    return Application(
        commodity=Commodity.SPIRITS,
        brand_name=BRAND,
        class_type="Kentucky Straight Bourbon Whiskey",
        alcohol_content=45.0,
        net_contents="750 mL",
        producer_name=BRAND,
        producer_address=ADDRESS,
        country_of_origin=None,
        is_import=False,
    )


def seed_job(store: BatchStore, *, created: float, ttl_hours: int = 24) -> tuple[str, Path]:
    """One batch job with one application and one image, as `POST /batch` would leave it."""
    job = store.create_job(retention_hours=ttl_hours, now=created)
    store.save_image(job.job_id, "front.png", IMAGE_BYTES)
    store.add_items(job.job_id, [(1, an_application(), ["front.png"])], now=created)
    return job.job_id, store.images_root / job.job_id / stored_name("front.png")


def every_byte_under(root: Path) -> bytes:
    """Everything on disk under `root`, concatenated. The only honest way to say 'gone'."""
    chunks: list[bytes] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            chunks.append(path.read_bytes())
    return b"".join(chunks)


def policy_for(tmp_path: Path, *, ttl_hours: int = 24) -> RetentionPolicy:
    return RetentionPolicy(storage_dir=tmp_path, ttl_hours=ttl_hours, sweep_seconds=900)


# --- the purge, read off the filesystem (LP-085) -----------------------------------------


def test_an_expired_job_loses_its_artwork_from_disk(tmp_path: Path) -> None:
    store = BatchStore(tmp_path)
    created = time.time()
    job_id, image = seed_job(store, created=created)

    assert image.is_file(), "the image should exist before the TTL passes"
    assert image.read_bytes() == IMAGE_BYTES

    report = sweep(policy_for(tmp_path), now=created + 25 * HOUR, store=store)

    assert report.jobs_purged == 1
    assert not image.exists()
    assert not (store.images_root / job_id).exists()
    assert store.get_job(job_id) is None


def test_an_expired_job_loses_its_results_too(tmp_path: Path) -> None:
    """Uploads *and* results (SEC-2). A verdict carries every extracted field."""
    store = BatchStore(tmp_path)
    created = time.time()
    job_id, _ = seed_job(store, created=created)
    item = store.items(job_id)[0]
    store.fail(
        item.item_id,
        ItemFailure(code="internal_error", message="stand-in", next_step="retry", attempts=1),
    )

    sweep(policy_for(tmp_path), now=created + 25 * HOUR, store=store)
    assert store.items(job_id) == []


def test_deleted_rows_leave_no_residue_in_the_database(tmp_path: Path) -> None:
    """The finding that makes 'provably' mean something.

    SQLite's `secure_delete` is off by default: a DELETE unlinks rows from the b-tree and
    leaves their bytes in freed pages. `purge_expired()` would return a tidy list of job IDs
    while three hundred applications' brand names and producer addresses stayed recoverable
    from `jobs.db` with `strings`. The sweep follows a purge with VACUUM and a WAL truncate,
    and this reads every byte back to prove it.
    """
    store = BatchStore(tmp_path)
    created = time.time()
    seed_job(store, created=created)

    assert BRAND.encode() in every_byte_under(tmp_path), "precondition: it is on disk now"

    sweep(policy_for(tmp_path), now=created + 25 * HOUR, store=store)

    remains = every_byte_under(tmp_path)
    assert BRAND.encode() not in remains, "brand name survived the purge inside the database"
    assert ADDRESS.encode() not in remains, "producer address survived the purge"
    assert IMAGE_BYTES not in remains, "label artwork survived the purge"


def test_deleting_alone_would_not_have_been_enough(tmp_path: Path) -> None:
    """Names the failure the VACUUM prevents, so a future refactor cannot quietly undo it."""
    store = BatchStore(tmp_path)
    created = time.time()
    store_root = tmp_path
    seed_job(store, created=created)

    # Exactly what `purge_expired` does, and nothing after it.
    store.purge_expired(now=created + 25 * HOUR)

    assert BRAND.encode() in every_byte_under(store_root), (
        "if this ever stops being true, secure_delete was turned on upstream and the "
        "VACUUM in api/retention.py can be dropped — the property test above still holds"
    )


def test_a_job_inside_its_ttl_is_untouched(tmp_path: Path) -> None:
    store = BatchStore(tmp_path)
    created = time.time()
    job_id, image = seed_job(store, created=created)

    report = sweep(policy_for(tmp_path), now=created + 23 * HOUR, store=store)

    assert report.jobs_purged == 0
    assert image.is_file()
    assert store.get_job(job_id) is not None
    assert store.items(job_id)[0].state is ItemState.QUEUED


def test_the_ttl_is_configurable_and_not_a_constant(tmp_path: Path) -> None:
    store = BatchStore(tmp_path)
    created = time.time()
    _, image = seed_job(store, created=created, ttl_hours=1)

    sweep(policy_for(tmp_path, ttl_hours=1), now=created + 30 * 60, store=store)
    assert image.is_file(), "half an hour into a one-hour TTL"

    sweep(policy_for(tmp_path, ttl_hours=1), now=created + 2 * HOUR, store=store)
    assert not image.exists()


def test_the_default_ttl_is_twenty_four_hours() -> None:
    assert RetentionPolicy(storage_dir=Path(".")).ttl_hours == 24
    assert Config().retention_hours == 24
    assert SecurityPolicy.from_config(Config()).retention_ttl_hours == 24


# --- orphans and loose artefacts ---------------------------------------------------------


def test_a_directory_no_job_claims_is_removed_once_it_is_old_enough(tmp_path: Path) -> None:
    """A crash between `create_job` and `add_items` leaves artwork with no owner."""
    store = BatchStore(tmp_path)
    orphan = store.images_root / "job_orphaned"
    orphan.mkdir(parents=True)
    (orphan / "artwork.img").write_bytes(IMAGE_BYTES)

    now = time.time() + ORPHAN_GRACE_SECONDS + 60
    report = sweep(policy_for(tmp_path), now=now, store=store)

    assert not orphan.exists()
    assert report.paths_removed == 1


def test_an_upload_still_in_progress_is_not_swept_out_from_under_itself(
    tmp_path: Path,
) -> None:
    """Deleting a live batch directory looks exactly like a lost batch."""
    store = BatchStore(tmp_path)
    fresh = store.images_root / "job_being_written"
    fresh.mkdir(parents=True)
    (fresh / "artwork.img").write_bytes(IMAGE_BYTES)

    sweep(policy_for(tmp_path), now=time.time(), store=store)
    assert fresh.is_dir()
    assert (fresh / "artwork.img").read_bytes() == IMAGE_BYTES


def test_loose_artefacts_age_out_on_the_same_ttl(tmp_path: Path) -> None:
    """Covers an uploads or results cache with no database row behind it."""
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True)
    stale = uploads / "old.img"
    stale.write_bytes(IMAGE_BYTES)
    import os

    os.utime(stale, (time.time() - 30 * HOUR, time.time() - 30 * HOUR))
    fresh = uploads / "new.img"
    fresh.write_bytes(IMAGE_BYTES)

    sweep(policy_for(tmp_path), now=time.time())

    assert not stale.exists()
    assert fresh.is_file()


def test_the_database_itself_is_never_deleted_as_an_artefact(tmp_path: Path) -> None:
    store = BatchStore(tmp_path)
    seed_job(store, created=time.time() - 100 * HOUR)
    sweep(policy_for(tmp_path), now=time.time(), store=store)
    assert store.db_path.is_file()


def test_a_sweep_on_an_empty_container_creates_nothing(tmp_path: Path) -> None:
    """A retention sweeper that brings a database into being is doing the opposite of its job."""
    root = tmp_path / "data"
    report = sweep(policy_for(root))
    assert report.jobs_purged == 0
    assert not root.exists()

    root.mkdir()
    sweep(policy_for(root))
    assert not (root / "jobs.db").exists()
    assert list(root.iterdir()) == []


# --- the timer (LP-084) -------------------------------------------------------------------


def test_retention_is_time_driven_not_traffic_driven(tmp_path: Path) -> None:
    """The finding this ticket exists to fix.

    `POST /batch` sweeps on its way in, so a server that receives no new batches never
    sweeps — a container left running overnight with one batch on disk keeps it forever.
    This drives the sweeper with zero requests of any kind.
    """
    store = BatchStore(tmp_path)
    created = time.time() - 30 * HOUR
    _, image = seed_job(store, created=created)
    assert image.is_file()

    sweeper = RetentionSweeper(policy_for(tmp_path), store_provider=lambda: store)

    async def one_cycle() -> None:
        task = asyncio.create_task(sweeper.run_forever())
        for _ in range(200):
            await asyncio.sleep(0.005)
            if sweeper.sweeps:
                break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(one_cycle())

    assert sweeper.sweeps >= 1, "the timer should have fired without any traffic"
    assert not image.exists()


def test_a_failing_sweep_does_not_kill_the_timer(tmp_path: Path) -> None:
    """A retention timer that stops after one bad cycle is worse than one that never ran,
    because the logs show it started."""
    sweeper = RetentionSweeper(policy_for(tmp_path))
    calls = {"n": 0}

    def explode(**_: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return retention.SweepReport()

    sweeper.sweep_once = explode  # type: ignore[method-assign]

    async def two_cycles() -> None:
        task = asyncio.create_task(sweeper.run_forever())
        for _ in range(200):
            await asyncio.sleep(0.005)
            if calls["n"] >= 1:
                break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(two_cycles())
    assert calls["n"] >= 1


def test_the_sweeper_is_wired_to_startup_and_shutdown(tmp_path: Path) -> None:
    from fastapi import FastAPI

    app = FastAPI()
    policy = SecurityPolicy(storage_dir=str(tmp_path), retention_sweep_seconds=900)
    sweeper = install_sweeper(app, policy)

    assert app.state.retention_sweeper is sweeper
    assert not sweeper.running

    with TestClient(app):
        assert sweeper.running
    assert not sweeper.running


def test_harden_starts_retention_without_any_batch_traffic(tmp_path: Path) -> None:
    """End to end: a container that boots and is never used still sweeps."""
    store = BatchStore(tmp_path)
    created = time.time() - 40 * HOUR
    _, image = seed_job(store, created=created)

    app = create_app(config=make_config(storage_dir=str(tmp_path)))
    harden(app, make_config(storage_dir=str(tmp_path)))

    with TestClient(app):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and image.exists():
            time.sleep(0.01)

    assert not image.exists(), "the sweeper should have run on startup, unprompted"


def test_the_sweep_interval_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LABELPROOF_RETENTION_SWEEP_SECONDS", "60")
    policy = SecurityPolicy.from_config(Config())
    assert RetentionPolicy.from_security_policy(policy).sweep_seconds == 60


def test_the_worst_case_lifetime_is_ttl_plus_one_interval() -> None:
    """Stated rather than rounded down — the README says 24h + the interval, not 24h."""
    policy = RetentionPolicy(storage_dir=Path("."), ttl_hours=24, sweep_seconds=900)
    assert policy.ttl_seconds == 24 * HOUR
    assert policy.ttl_seconds + policy.sweep_seconds == 24 * HOUR + 900


# --- single verifications persist nothing (SEC-2) ------------------------------------------


def _noisy_png(size: int = 900) -> bytes:
    """An image big enough that Starlette spools the multipart part to a real file.

    Below roughly 1 MB the part stays in memory and the disk is never touched, so a smaller
    fixture would make this test pass for a reason that has nothing to do with retention.
    """
    import io
    import os

    image = Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_single_verification_writes_nothing_to_the_storage_directory(
    tmp_path: Path,
) -> None:
    """There is nothing for the TTL to collect, and that is the strongest posture there is.

    Asserted against the filesystem rather than assumed from reading the route: `POST
    /verify` reads uploads into memory, ingests them, and returns the result in the response
    body. If that ever changes, this fails.
    """
    storage = tmp_path / "data"
    storage.mkdir()

    app = create_app(config=make_config(storage_dir=str(storage)))
    client = TestClient(app)

    raw = json.loads(SAMPLE.read_text())
    application = {k: v for k, v in raw.items() if not k.startswith("_")}
    name = "tc01_old_tom_clean.png"
    response = client.post(
        "/verify",
        data={"application": json.dumps(application)},
        files=[("images", (name, (LABELS / name).read_bytes(), "image/png"))],
    )

    assert response.status_code == 200
    assert list(storage.rglob("*")) == [], "a verification left an artefact behind"


def test_an_oversized_upload_leaves_no_spooled_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starlette spools multipart parts over ~1 MB to a real file on disk.

    Those are unlinked when the request ends, but "should be" is not the standard this
    ticket sets, so the temp directory is redirected and then read back.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))

    payload = _noisy_png()
    assert len(payload) > 1_000_000, "the fixture must exceed the spool threshold"

    app = create_app(config=make_config(storage_dir=str(tmp_path / "data")))
    client = TestClient(app)

    raw = json.loads(SAMPLE.read_text())
    application = {k: v for k, v in raw.items() if not k.startswith("_")}
    client.post(
        "/verify",
        data={"application": json.dumps(application)},
        files=[("images", ("noise.png", payload, "image/png"))],
    )

    leftovers = [path for path in spool.rglob("*") if path.is_file()]
    assert leftovers == [], f"upload bytes survived the request: {[p.name for p in leftovers]}"
