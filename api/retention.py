"""Retention TTL — time-driven, and provable against the filesystem (SEC-2, LP-084).

Marcus asked for a document retention policy. The answer is an actual implemented one:
uploads and results are purged `LABELPROOF_RETENTION_HOURS` after they are created, 24 by
default, by a timer that does not care whether anyone is using the app.

**Why a timer and not a request hook.** `POST /batch` sweeps on its way in, which means a
server that receives no new batches never sweeps at all — a container left running overnight
with one batch on disk keeps it forever, and that is precisely the deployment the policy is
written for. The existing hook is harmless and stays; this adds the trigger that does not
depend on traffic.

**Why the sweep runs on a thread.** It does blocking SQLite work, including a `VACUUM`.
Doing that on the event loop would put the 5-second gate at risk for whoever happens to be
mid-verification when the timer fires (PERF-1).

**Why `VACUUM`.** This is the part that makes "provably gone" mean something. SQLite's
default `secure_delete` is off, so a `DELETE` unlinks rows from the b-tree and leaves their
bytes in freed pages inside `jobs.db`. After a purge returns a tidy list of job IDs, three
hundred applications' brand names and producer addresses are still sitting in that file and
`strings` will find them. `VACUUM` rebuilds the file without the free pages, and
`wal_checkpoint(TRUNCATE)` makes sure the residue does not survive in the write-ahead log
either. `tests/test_retention.py` asserts the property by reading every byte of every file
under the storage root — not by trusting the return value.

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
import json
import shutil
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    {"jobs.db", "jobs.db-wal", "jobs.db-shm", "jobs.db-journal", ".compaction-state"}
)

#: How old an unreferenced `batches/<job_id>/` directory must be before it is treated as
#: orphaned. `POST /batch` creates the directory and the job row together, but a crash
#: between the two leaves a directory with no owner — and deleting one out from under a
#: live upload would look exactly like a lost batch, which is the failure a batch tool can
#: least afford.
ORPHAN_GRACE_SECONDS = 300.0

DEFAULT_SWEEP_SECONDS = 900


def is_expired(expires_at: float, *, now: float | None = None) -> bool:
    """The single definition of "this batch is past its retention".

    **Read paths must use this, not a copy of it.** `GET /batch/{id}` and
    `GET /batch/{id}/export.csv` in `api/routes/batch.py` currently serve a job — brand
    names, producer addresses, extracted values and all — for as long as the row survives,
    which is until a sweep removes it. That is at least `ttl + sweep_seconds` by design, and
    was forever in the shipped app because nothing called `harden`. Meanwhile the 404 copy
    tells the agent "Batches and their images are deleted 24 hours after they are started."

    Deleting on a timer and refusing to serve are two different guarantees and the second is
    the one the user was promised. They belong to different files, so the predicate lives
    here and is imported, because the failure mode of two copies is that the API keeps
    serving something the sweeper believes is gone and nobody notices until a reviewer asks.

    Matches `BatchStore.purge_expired`'s `expires_at <= now` exactly, deliberately: an
    artefact is expired the instant the sweep would take it, never a moment later.
    """
    moment = time.time() if now is None else now
    return expires_at <= moment


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
    compacted: bool = False
    purge_failed: bool = False
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
        # WRITE AHEAD. The obligation is recorded *before* the DELETE, not after it.
        # Recording it afterwards left a window — measured — in which the rows were gone and
        # nothing remembered they had been: ENOSPC (and retention runs precisely when the
        # disk is full), a read-only remount, or a SIGKILL between the commit and the marker
        # all ended with `compacted=True` over a database still full of brand names.
        note_compaction_owed(resolved.db_path)

        # A purge that loses the write lock to a batch worker raises
        # `sqlite3.OperationalError: database is locked`. Letting that propagate aborted the
        # WHOLE sweep — orphan directories and loose files included — so a running batch
        # switched retention off entirely for as long as it ran.
        try:
            purged = resolved.purge_expired(now=moment)
            report.jobs_purged = len(purged)
        except sqlite3.Error as exc:
            report.purge_failed = True
            applog.warn(
                "retention_purge_failed",
                code="sqlite_busy",
                reason_code=type(exc).__name__,
            )

        # Compaction is attempted on EVERY sweep, not only on one that purged something.
        # Gating it on `if purged:` was a hole with a permanent consequence: a compaction
        # that lost the lock left the brand names in freed pages, and by the next sweep
        # there was nothing left to purge, so it never retried and the data survived for the
        # life of the container. `_compact` is a no-op when the file is already known clean,
        # so the unconditional call costs two `stat` calls.
        report.compacted = _compact(resolved.db_path)

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


#: Where the last verified-clean state of the database is recorded. A file rather than a
#: variable because the obligation has to outlive the process.
COMPACTION_STATE = ".compaction-state"

#: Seconds the compaction will wait for the database lock before giving up and warning.
#: Deliberately short: this runs on a timer, so a contended sweep should stand aside for the
#: batch worker rather than hold it up, and the obligation survives in the marker either way.
COMPACTION_LOCK_WAIT = 2.0


def _state_path(db_path: Path) -> Path:
    return db_path.with_name(COMPACTION_STATE)


def _fingerprint(db_path: Path) -> list[int]:
    """Size and mtime of the database and its sidecars.

    **This is what makes the obligation independent of whoever did the deleting**, which is
    the whole reason it exists. The previous design recorded the obligation at delete time,
    which is only sound if every delete path remembers to — and one did not:
    `api/routes/batch.py` calls `purge_expired()` directly on its way into `POST /batch`,
    wrote no marker, and the sweep that followed reported `compacted=True` over a database
    from which `strings` still yielded every brand name and producer address. Permanently,
    with no attacker, no crash and no contention.

    A delete cannot avoid writing to the database or its write-ahead log, so a fingerprint
    that differs from the last verified-clean one means *something* changed and the file is
    not known clean. Inserts move it too, so this compacts more often than strictly
    necessary — which is the correct direction to be wrong in, and costs a VACUUM of a
    prototype-sized database on a fifteen-minute timer.
    """
    values: list[int] = []
    for suffix in ("", "-wal", "-shm"):
        path = db_path if not suffix else db_path.with_name(db_path.name + suffix)
        try:
            stat = path.stat()
            values.extend((stat.st_size, stat.st_mtime_ns))
        except OSError:
            values.extend((-1, -1))
    return values


def _read_clean_fingerprint(db_path: Path) -> list[int] | None:
    try:
        raw = json.loads(_state_path(db_path).read_text())
    except (OSError, ValueError):
        return None
    fingerprint = raw.get("clean_fingerprint") if isinstance(raw, dict) else None
    if isinstance(fingerprint, list) and all(isinstance(v, int) for v in fingerprint):
        return fingerprint
    return None


def _write_clean_fingerprint(db_path: Path, fingerprint: list[int] | None) -> bool:
    """Record (or clear) the last verified-clean state. Returns whether the write landed."""
    try:
        payload = json.dumps({"clean_fingerprint": fingerprint})
        _state_path(db_path).write_text(payload)
        return True
    except OSError:
        return False


def note_compaction_owed(db_path: Path) -> None:
    """Declare, before deleting anything, that the file is about to stop being clean.

    Call this **ahead of** the DELETE. It is belt to the fingerprint's braces: the
    fingerprint alone would catch the change afterwards, but a caller that knows it is about
    to delete should say so, so that a compaction which then fails is reported as a failure
    rather than as a routine miss.

    A failed write is logged, not swallowed. The previous version wrapped this in
    `contextlib.suppress(OSError)`, which meant the one condition most likely to break it —
    a full disk, which is exactly when retention runs — broke it in silence.
    """
    if not _write_clean_fingerprint(db_path, None):
        applog.warn("retention_state_unwritable", code="io_error", stage="retention")


def compaction_owed(db_path: Path) -> bool:
    """Might this database hold deleted content in pages nobody is using?

    True unless the file is byte-for-byte the one a compaction last verified clean. Two
    earlier answers to this question were both wrong:

    * `PRAGMA freelist_count > 0` — counts *wholly freed pages*, so a small batch's deleted
      rows sit inside pages still in use and it reads 0 while the brand names are there.
      Measured 0 at n=2 and n=5.
    * a marker written by the deleter — sound only if every delete path remembers, and
      `api/routes/batch.py` does not.

    Deriving it from the file removes both failure modes: there is no delete path, present
    or future, that can change this database without changing its fingerprint.
    """
    if not db_path.exists():
        return False
    clean = _read_clean_fingerprint(db_path)
    return clean is None or clean != _fingerprint(db_path)


def _wal_bytes(db_path: Path) -> int:
    wal = db_path.with_name(db_path.name + "-wal")
    with contextlib.suppress(OSError):
        if wal.is_file():
            return wal.stat().st_size
    return 0


def _compact(db_path: Path) -> bool:
    """`VACUUM` then truncate the WAL. Returns whether the file is **verified** clean.

    Two things this deliberately does not do.

    It does not trust `PRAGMA wal_checkpoint(TRUNCATE)`. That pragma **returns**
    `(busy, log, checkpointed)` — it does not raise — so the first version reported success
    after a measured `(1, 17, 0)`: busy, zero pages checkpointed, brand names still in
    `jobs.db-wal`, and `SweepReport.compacted` saying True. A control that reports it did
    the thing while not doing it is worse than no control, because it ends the
    investigation. The `busy` flag is now read and believed.

    It does not clear the obligation on hope. The clean fingerprint is recorded only when
    the VACUUM did not raise, the checkpoint reported not-busy, and the WAL is measurably
    empty. If a batch worker holds the write lock, none of that happens, the file stays
    un-clean, and the next sweep retries — which is the retry the old `if purged:` gate
    could never reach, because by then there was nothing left to purge.
    """
    if not compaction_owed(db_path):
        return True

    # A short lock wait, on purpose. This runs on a timer that comes round again in fifteen
    # minutes, so sitting on a lock that a batch worker needs buys nothing and costs the
    # thing the lock is for. Fail fast, warn, retry next tick.
    try:
        connection = sqlite3.connect(db_path, timeout=COMPACTION_LOCK_WAIT, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout={int(COMPACTION_LOCK_WAIT * 1000)}")
    except sqlite3.Error:
        return False

    vacuumed = False
    checkpointed = False
    try:
        connection.execute("VACUUM")
        vacuumed = True
    except sqlite3.Error:
        vacuumed = False
    try:
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # (busy, log_pages, checkpointed_pages). busy != 0 means a reader or writer stopped
        # it and the log was NOT reset, whatever the absence of an exception suggests.
        checkpointed = bool(row) and int(row[0]) == 0
    except sqlite3.Error:
        checkpointed = False
    finally:
        connection.close()

    if vacuumed and checkpointed and _wal_bytes(db_path) == 0:
        # Fingerprinted *after* the rebuild, because the rebuild is what changed the file.
        if _write_clean_fingerprint(db_path, _fingerprint(db_path)):
            return True
        # The database is clean; we simply cannot record that it is. Say so rather than
        # claiming success, and the next sweep will compact a clean file, which is wasteful
        # and harmless — the failure this whole mechanism exists to avoid is the other one.
        applog.warn("retention_state_unwritable", code="io_error", stage="retention")
        return False

    applog.warn("retention_compaction_incomplete", code="sqlite_busy", stage="retention")
    return False


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


def _watch_traceback_containment() -> None:
    """Re-assert the SEC-4 log guard if something has replaced it since startup.

    `api/security.py` can re-wrap itself, but nothing was ever calling it, so "self-healing"
    described a capability with no trigger: any library installing its own
    `logging.setLogRecordFactory` after startup switched traceback containment off for the
    life of the process and nothing noticed. `wave/observability` shipped exactly such a
    factory, so this is not hypothetical.

    The sweeper is the only thing in this app that runs on a timer, which makes it the only
    place a periodic check can live. It costs one identity comparison every fifteen minutes,
    and the alternative is a security control that can be silently disabled by an import.
    """
    from api import security

    if security.containment_installed() and not security.containment_active():
        applog.warn("log_containment_reasserted", code="internal_error", stage="containment")
        security.install_log_containment()


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
        _watch_traceback_containment()
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
            except Exception as exc:  # noqa: BLE001 - a sweep failure must not kill the loop
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

    # The lifespan context is WRAPPED rather than appending to `router.on_startup`.
    # Starlette ignores `on_startup`/`on_shutdown` entirely when an app supplies a
    # `lifespan=` context — so the append version worked today and would have switched
    # retention off, silently and with no test failing, the moment anyone modernised
    # `create_app` to the lifespan idiom. Wrapping covers both, because the handler-running
    # default is itself just a lifespan context.
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scope_app: FastAPI) -> AsyncIterator[Any]:
        async with inner(scope_app) as state:
            sweeper.start()
            try:
                yield state
            finally:
                await sweeper.stop()

    app.router.lifespan_context = lifespan
    return sweeper
