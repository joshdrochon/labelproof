"""Retention TTL — time-driven, and provable against the filesystem (SEC-2, LP-084).

Marcus asked for a document retention policy. The answer is an actual implemented one:
uploads and results are purged `LABELPROOF_RETENTION_HOURS` after they are created, 24 by
default, by a timer that does not care whether anyone is using the app.

**Why a timer and not a request hook.** `POST /batch` sweeps on its way in, which means a
server that receives no new batches never sweeps at all — a container left running overnight
with one batch on disk keeps it forever, and that is precisely the deployment the policy is
written for. The existing hook is harmless and stays; this adds the trigger that does not
depend on traffic.

**Why the sweep runs on a thread.** It does blocking SQLite and filesystem work. Doing that
on the event loop would put the 5-second gate at risk for whoever happens to be
mid-verification when the timer fires (PERF-1).

**Single verifications persist nothing.** `POST /verify` reads uploads into memory, ingests
them, and returns the result in the response body. There is no artefact for the TTL to
collect, which is a stronger posture than a 24-hour TTL rather than a gap in one — and
`test_retention.py` asserts that against the filesystem too, rather than assuming it.

Worst-case artefact lifetime is therefore `ttl_hours + sweep_seconds`, and the README says
so instead of claiming exactly 24 hours.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from api import logging as applog

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from api.batch.store import BatchStore
    from api.security import SecurityPolicy

#: Directories under the storage root this sweep is allowed to delete from. An explicit
#: list rather than "everything that is not the database", because a sweeper with a wide
#: blast radius pointed at a misconfigured `LABELPROOF_STORAGE_DIR` is its own incident.
MANAGED_SUBDIRS: tuple[str, ...] = ("batches", "uploads", "results")

#: Never removed, whatever their age. The database is state, not an artefact.
PROTECTED_NAMES: frozenset[str] = frozenset(
    {"jobs.db", "jobs.db-wal", "jobs.db-shm", "jobs.db-journal"}
)

#: How old an unreferenced `batches/<job_id>/` directory must be before it is treated as
#: orphaned. `POST /batch` creates the directory and the job row together, but a crash
#: between the two leaves a directory with no owner — and deleting one out from under a
#: live upload would look exactly like a lost batch, which is the failure a batch tool can
#: least afford.
ORPHAN_GRACE_SECONDS = 300.0

DEFAULT_SWEEP_SECONDS = 900


@dataclass(frozen=True)
class RetentionPolicy:
    """The retention rule, resolved. Everything the sweep needs and nothing else."""

    storage_dir: Path
    ttl_hours: int = 24
    sweep_seconds: int = DEFAULT_SWEEP_SECONDS

    @property
    def ttl_seconds(self) -> float:
        return max(0.0, float(self.ttl_hours) * 3600.0)

    @classmethod
    def from_security_policy(cls, policy: SecurityPolicy) -> RetentionPolicy:
        return cls(
            storage_dir=Path(policy.storage_dir),
            ttl_hours=policy.retention_ttl_hours,
            sweep_seconds=max(1, policy.retention_sweep_seconds),
        )


@dataclass
class SweepReport:
    """What one sweep actually removed. Returned so it can be logged and asserted."""

    jobs_purged: int = 0
    paths_removed: int = 0
    bytes_removed: int = 0
    removed: list[str] = field(default_factory=list)

    @property
    def anything(self) -> bool:
        return bool(self.jobs_purged or self.paths_removed)


# --- the sweep -----------------------------------------------------------------------------


def sweep(
    policy: RetentionPolicy,
    *,
    now: float | None = None,
    store: BatchStore | None = None,
) -> SweepReport:
    """Purge everything past its TTL. Blocking; call it off the event loop.

    Idempotent and safe to run against a storage directory that does not exist yet, which
    is the state on a fresh container that has never seen a batch.
    """
    moment = time.time() if now is None else now
    report = SweepReport()
    root = policy.storage_dir

    if not root.exists():
        return report

    resolved = _store_for(policy, store)
    if resolved is not None:
        report.jobs_purged = len(resolved.purge_expired(now=moment))

    _sweep_orphans(policy, resolved, moment, report)
    _sweep_loose_files(policy, moment, report)
    return report


def _store_for(policy: RetentionPolicy, store: BatchStore | None) -> BatchStore | None:
    """Reuse the app's store, or open the existing database — never create one.

    Constructing a `BatchStore` creates `jobs.db` and the `batches/` tree as a side effect.
    A retention sweeper that brings a database into being on a server which has never run a
    batch is a sweeper doing the opposite of its job.
    """
    if store is not None:
        return store
    if not (policy.storage_dir / "jobs.db").is_file():
        return None
    from api.batch.store import BatchStore as _BatchStore

    return _BatchStore(policy.storage_dir)


def _live_job_ids(store: BatchStore | None) -> set[str] | None:
    """Job IDs still in the database, or None when there is no database to ask."""
    if store is None:
        return None
    try:
        # The store exposes no job-listing API; borrowing its connection helper is cheaper
        # than opening a second one against the same WAL.
        with store._conn() as connection:
            rows = connection.execute("SELECT job_id FROM jobs").fetchall()
    except sqlite3.Error:
        return None
    return {str(row["job_id"]) for row in rows}


def _sweep_orphans(
    policy: RetentionPolicy,
    store: BatchStore | None,
    now: float,
    report: SweepReport,
) -> None:
    """Remove `batches/<job_id>/` directories no job row claims any more."""
    batches = policy.storage_dir / "batches"
    if not batches.is_dir():
        return

    live = _live_job_ids(store)
    for child in sorted(batches.iterdir()):
        if not child.is_dir():
            continue
        if live is not None and child.name in live:
            continue
        if _age(child, now) < ORPHAN_GRACE_SECONDS:
            continue
        _remove(child, report)


def _sweep_loose_files(policy: RetentionPolicy, now: float, report: SweepReport) -> None:
    """Age out anything else under the managed subdirectories.

    This is what covers a future upload or results cache without a database row behind it:
    the TTL applies to the artefact, not to the bookkeeping that happens to reference it.
    """
    ttl = policy.ttl_seconds
    for name in MANAGED_SUBDIRS:
        directory = policy.storage_dir / name
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if child.name in PROTECTED_NAMES:
                continue
            if _age(child, now) <= ttl:
                continue
            _remove(child, report)


def _age(path: Path, now: float) -> float:
    """Seconds since this path was last written, floored at zero.

    A directory's own mtime only moves when its entries change, so for a batch directory
    this is "when was the last image written into it" — which is the right clock for
    deciding whether an upload is still in progress.
    """
    try:
        newest = path.stat().st_mtime
        if path.is_dir():
            for entry in path.iterdir():
                with contextlib.suppress(OSError):
                    newest = max(newest, entry.stat().st_mtime)
    except OSError:
        return float("inf")
    return max(0.0, now - newest)


def _size_of(path: Path) -> int:
    if path.is_file():
        with contextlib.suppress(OSError):
            return path.stat().st_size
        return 0
    total = 0
    with contextlib.suppress(OSError):
        for entry in path.rglob("*"):
            if entry.is_file():
                with contextlib.suppress(OSError):
                    total += entry.stat().st_size
    return total


def _remove(path: Path, report: SweepReport) -> None:
    size = _size_of(path)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            path.unlink()
    if path.exists():
        return
    report.paths_removed += 1
    report.bytes_removed += size
    # The *name* is recorded, never the path — a filename can carry a brand name, and this
    # list exists for tests, not for a log line (SEC-4).
    report.removed.append(path.name)


# --- the timer -------------------------------------------------------------------------------


class RetentionSweeper:
    """Runs `sweep` on startup and then on a fixed interval, off the event loop.

    Owns no state beyond its task handle, so it is safe to construct in a test, drive one
    cycle by hand, and never start the loop at all.
    """

    def __init__(
        self,
        policy: RetentionPolicy,
        *,
        store_provider: object | None = None,
    ):
        self.policy = policy
        self._store_provider = store_provider
        self._task: asyncio.Task[None] | None = None
        self.sweeps = 0
        """How many cycles have completed. Observable so 'the timer is running' can be
        asserted rather than assumed — a sweeper nobody started looks identical to one
        that has simply found nothing to do."""

    def _store(self) -> BatchStore | None:
        provider = self._store_provider
        if provider is None:
            return None
        resolved = provider() if callable(provider) else provider
        return resolved  # type: ignore[return-value]

    def sweep_once(self, *, now: float | None = None) -> SweepReport:
        report = sweep(self.policy, now=now, store=self._store())
        self.sweeps += 1
        if report.anything:
            applog.log(
                "retention_purged",
                count=report.jobs_purged,
                bytes=report.bytes_removed,
                stage="retention",
            )
        return report

    async def _cycle(self) -> SweepReport:
        return await asyncio.to_thread(self.sweep_once)

    async def run_forever(self) -> None:
        """Sweep now, then every `sweep_seconds`, until cancelled.

        A failing sweep logs and waits for the next tick rather than killing the loop. A
        retention timer that stops after one bad cycle is worse than one that never ran,
        because the logs show it started.
        """
        while True:
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                applog.error(
                    "retention_sweep_failed",
                    code="internal_error",
                    reason_code=type(exc).__name__,
                )
            await asyncio.sleep(self.policy.sweep_seconds)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run_forever())
        applog.log(
            "retention_started",
            count=self.policy.ttl_hours,
            duration_ms=self.policy.sweep_seconds * 1000,
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()


def install_sweeper(app: FastAPI, policy: SecurityPolicy) -> RetentionSweeper:
    """Attach the sweeper to the app's startup and shutdown. Called by `security.harden`.

    The store is resolved lazily through `app.state` so the sweeper shares whatever
    `api/routes/batch.py` built — two `BatchStore` objects over one SQLite file would work,
    but sharing means the sweep sees the same WAL and the `VACUUM` contends with one fewer
    connection.
    """
    retention = RetentionPolicy.from_security_policy(policy)
    sweeper = RetentionSweeper(
        retention, store_provider=lambda: getattr(app.state, "batch_store", None)
    )
    app.state.retention_sweeper = sweeper

    async def _start() -> None:
        sweeper.start()

    async def _stop() -> None:
        await sweeper.stop()

    app.router.on_startup.append(_start)
    app.router.on_shutdown.append(_stop)
    return sweeper
