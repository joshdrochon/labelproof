"""The job store — SQLite plus a TTL-swept image directory (the build spec, BATCH-6).

**Why a database at all.** A 300-item job runs for minutes. An in-memory dict loses it to
a deploy, a crash, or a machine restart, and the agent who kicked off the importer dump
before lunch comes back to nothing. SQLite is a file: the job survives a restart, and the
egress table stays empty because there is no broker to reach (NET-1).

**Why the queue lives in the same file as the results.** Claiming an item and recording
its outcome are then one transaction against one file. A separate queue would let the two
disagree — an item marked done in the queue with no result stored, or the reverse — and
"processed but nowhere to be found" is the failure a batch tool can least afford.

**Claiming is a transaction, not a read followed by a write.** `BEGIN IMMEDIATE` takes the
write lock before the row is chosen, so six workers cannot select the same item and verify
it six times. `RETURNING` would be shorter but pins a SQLite version; this does not.

**Image bytes live on the filesystem, not in a column.** 600 images of a few hundred KB
each is not what SQLite is for, and a directory is what the TTL sweep already knows how to
delete (SEC-2).

**An upload lands on disk before it lands anywhere else.** `staging()` hands the route a
scratch directory on the same filesystem as the store, so a 1.2 GB importer dump is spooled
rather than held, and the files an item actually needs are then *renamed* into the job
directory by `adopt_image` instead of being read out and written back. A rename is atomic
and free; reading 1.2 GB into memory to write it out again is neither.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from api.batch.models import (
    BatchItem,
    BatchJob,
    ItemFailure,
    ItemState,
    JobCounts,
    JobState,
    RowError,
)
from api.models import Application, VerificationResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    created_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL,
    expires_at      REAL NOT NULL,
    row_errors      TEXT NOT NULL DEFAULT '[]',
    unmatched_files TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS items (
    item_id     TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    row         INTEGER NOT NULL,
    state       TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    application TEXT NOT NULL,
    images      TEXT NOT NULL DEFAULT '[]',
    result      TEXT,
    failure     TEXT,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS items_by_job   ON items(job_id, row);
CREATE INDEX IF NOT EXISTS items_by_state ON items(state, created_at, row);
"""


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:16]}"


def new_item_id() -> str:
    return f"itm_{uuid.uuid4().hex[:16]}"


def is_expired(expires_at: float, *, now: float | None = None) -> bool:
    """Is this batch past its retention? One definition, imported rather than copied.

    ⚠️ **MERGE NOTE — delete this and import the canonical one.** `api/retention.py` on
    `wave/security` owns this predicate and its docstring says outright that read paths
    must import it rather than keep a copy, because "the failure mode of two copies is
    that the API keeps serving something the sweeper believes is gone and nobody notices
    until a reviewer asks". That module is not on `build/phase-1` yet, so importing it here
    would make this branch red, and committing red is not on the table. The resolution when
    the two branches meet is two lines: delete this function and
    `from api.retention import is_expired`. Semantics are identical by construction —
    `expires_at <= now`, matching `purge_expired` exactly — and
    `test_the_expiry_predicate_agrees_with_retentions` fails the moment they diverge.

    `claim()` cannot call this at all: it needs the comparison inside a SQL predicate so
    the check is part of the same `BEGIN IMMEDIATE` that picks the row. That copy is
    flagged in its own docstring.
    """
    return expires_at <= (time.time() if now is None else now)


def stored_name(supplied: str) -> str:
    """The on-disk name for an uploaded file.

    Derived from a hash rather than the uploaded name on purpose. An archive can contain
    `../../etc/passwd`, `CON`, a 300-character name, or two files differing only in case;
    a hex digest is none of those things, and nothing downstream needs the original name
    because the manifest already carries it (SEC-5).
    """
    key = Path(supplied.replace("\\", "/")).name.strip().lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".img"


class BatchStore:
    """Every read and write the batch mode does. Safe to call from any worker thread."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "jobs.db"
        self.images_root = self.root / "batches"
        self.images_root.mkdir(parents=True, exist_ok=True)
        # Deliberately a sibling of `batches` and not a system temp dir: `adopt_image`
        # renames across it, and a rename only stays atomic and free within one filesystem.
        self.staging_root = self.root / "staging"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        # Serialises this process's writers so `BEGIN IMMEDIATE` contention stays inside
        # a lock we control rather than inside SQLite's busy-wait, which burns CPU across
        # every batch worker at once.
        self._write_lock = threading.Lock()
        self._migrate()

    # --- plumbing --------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._conn() as connection:
            connection.executescript(_SCHEMA)

    # --- writing ---------------------------------------------------------------------

    def create_job(
        self,
        *,
        row_errors: Sequence[RowError] = (),
        unmatched_files: Sequence[str] = (),
        retention_hours: int = 24,
        now: float | None = None,
        job_id: str | None = None,
    ) -> BatchJob:
        moment = time.time() if now is None else now
        job = BatchJob(
            job_id=job_id or new_job_id(),
            state=JobState.QUEUED,
            created_at=moment,
            expires_at=moment + retention_hours * 3600,
            row_errors=list(row_errors),
            unmatched_files=list(unmatched_files),
        )
        with self._write_lock, self._conn() as connection:
            connection.execute(
                "INSERT INTO jobs (job_id, state, created_at, expires_at, row_errors, "
                "unmatched_files) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job.job_id,
                    job.state.value,
                    job.created_at,
                    job.expires_at,
                    json.dumps([e.model_dump() for e in job.row_errors]),
                    json.dumps(job.unmatched_files),
                ),
            )
        (self.images_root / job.job_id).mkdir(parents=True, exist_ok=True)
        return job

    def add_items(
        self,
        job_id: str,
        entries: Sequence[tuple[int, Application, Sequence[str]]],
        *,
        now: float | None = None,
    ) -> list[str]:
        moment = time.time() if now is None else now
        ids = [new_item_id() for _ in entries]
        payload = [
            (
                item_id,
                job_id,
                row,
                ItemState.QUEUED.value,
                application.model_dump_json(),
                json.dumps(list(images)),
                moment,
            )
            for item_id, (row, application, images) in zip(ids, entries, strict=True)
        ]
        with self._write_lock, self._conn() as connection:
            connection.executemany(
                "INSERT INTO items (item_id, job_id, row, state, application, images, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
        return ids

    def claim(self, *, job_id: str | None = None, now: float | None = None) -> BatchItem | None:
        """Take the next queued item of a LIVE job, or None when there is nothing to do.

        The whole select-then-update runs inside `BEGIN IMMEDIATE`, which is what stops
        two workers from picking up the same application. Oldest job first, then manifest
        order, so a batch finishes roughly in the order the agent listed it and a second
        batch does not jump the first.

        **Expiry is part of "there is nothing to do".** This selected on `state` alone,
        which meant an expired job kept being *worked*: measured, three queued items of a
        job past its TTL all completed, spending tokens and writing freshly extracted brand
        names and label text to disk — for a job the API was, at the same moment, telling
        the caller had been deleted. Refusing to serve it and refusing to produce more of
        it are the same promise; stopping only the first is the API lying about the second.

        The predicate is `expires_at > now`, the exact complement of `purge_expired`'s
        `expires_at <= now`, so an item stops being claimable at the instant a sweep would
        take its job — never a moment later. See `is_expired` below on why this comparison
        is written twice and what to do about it.
        """
        moment = time.time() if now is None else now
        live = "AND job_id IN (SELECT job_id FROM jobs WHERE expires_at > ?)"
        with self._write_lock, self._conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if job_id is None:
                    row = connection.execute(
                        f"SELECT * FROM items WHERE state = ? {live} "
                        f"ORDER BY created_at, row LIMIT 1",
                        (ItemState.QUEUED.value, moment),
                    ).fetchone()
                else:
                    row = connection.execute(
                        f"SELECT * FROM items WHERE state = ? AND job_id = ? {live} "
                        f"ORDER BY row LIMIT 1",
                        (ItemState.QUEUED.value, job_id, moment),
                    ).fetchone()
                if row is None:
                    connection.execute("ROLLBACK")
                    return None

                attempts = int(row["attempts"]) + 1
                connection.execute(
                    "UPDATE items SET state = ?, attempts = ?, started_at = ? "
                    "WHERE item_id = ?",
                    (ItemState.PROCESSING.value, attempts, moment, row["item_id"]),
                )
                connection.execute(
                    "UPDATE jobs SET state = ?, started_at = COALESCE(started_at, ?) "
                    "WHERE job_id = ? AND state = ?",
                    (
                        JobState.PROCESSING.value,
                        moment,
                        row["job_id"],
                        JobState.QUEUED.value,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        item = _item_from_row(row)
        item.state = ItemState.PROCESSING
        item.attempts = attempts
        item.started_at = moment
        return item

    def complete(
        self, item_id: str, result: VerificationResult, *, now: float | None = None
    ) -> None:
        moment = time.time() if now is None else now
        with self._write_lock, self._conn() as connection:
            connection.execute(
                "UPDATE items SET state = ?, result = ?, failure = NULL, finished_at = ? "
                "WHERE item_id = ?",
                (ItemState.DONE.value, result.model_dump_json(), moment, item_id),
            )
            _settle_job(connection, _job_of(connection, item_id), moment)

    def fail(self, item_id: str, failure: ItemFailure, *, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        with self._write_lock, self._conn() as connection:
            connection.execute(
                "UPDATE items SET state = ?, failure = ?, finished_at = ? WHERE item_id = ?",
                (ItemState.FAILED.value, failure.model_dump_json(), moment, item_id),
            )
            _settle_job(connection, _job_of(connection, item_id), moment)

    def requeue(self, item_id: str) -> None:
        """Put an item back for another attempt. `attempts` is deliberately not reset."""
        with self._write_lock, self._conn() as connection:
            connection.execute(
                "UPDATE items SET state = ?, started_at = NULL WHERE item_id = ?",
                (ItemState.QUEUED.value, item_id),
            )

    def retry_failed(self, job_id: str, *, now: float | None = None) -> int:
        """Requeue the failed items and only the failed items (BATCH-8, LP-157).

        The `WHERE state = 'failed'` is the whole feature. Two hundred and ninety items
        already cost real money and real minutes; re-running them because three failed is
        the behaviour this endpoint exists to avoid. `attempts` resets because a retry an
        agent asked for is a fresh decision, not a continuation of the automatic ones.
        """
        moment = time.time() if now is None else now
        with self._write_lock, self._conn() as connection:
            cursor = connection.execute(
                "UPDATE items SET state = ?, attempts = 0, failure = NULL, "
                "started_at = NULL, finished_at = NULL WHERE job_id = ? AND state = ?",
                (ItemState.QUEUED.value, job_id, ItemState.FAILED.value),
            )
            requeued = cursor.rowcount or 0
            if requeued:
                connection.execute(
                    "UPDATE jobs SET state = ?, finished_at = NULL WHERE job_id = ?",
                    (JobState.PROCESSING.value, job_id),
                )
            else:
                _settle_job(connection, job_id, moment)
        return requeued

    def recover(self, *, now: float | None = None) -> int:
        """Requeue anything left mid-flight by a restart (BATCH-6, LP-158).

        An item in `processing` with no process behind it is stranded forever otherwise.
        Requeueing is safe because verification has no side effects outside this store —
        it reads images and writes a result, so doing it twice costs one extra call and
        changes nothing else.
        """
        with self._write_lock, self._conn() as connection:
            cursor = connection.execute(
                "UPDATE items SET state = ?, started_at = NULL WHERE state = ?",
                (ItemState.QUEUED.value, ItemState.PROCESSING.value),
            )
            return cursor.rowcount or 0

    # --- reading ---------------------------------------------------------------------

    def get_job(self, job_id: str) -> BatchJob | None:
        with self._conn() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _job_from_row(row) if row else None

    def get_item(self, item_id: str) -> BatchItem | None:
        with self._conn() as connection:
            row = connection.execute(
                "SELECT * FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
        return _item_from_row(row) if row else None

    def items(
        self,
        job_id: str,
        *,
        states: Sequence[ItemState] | None = None,
        limit: int | None = None,
    ) -> list[BatchItem]:
        query = "SELECT * FROM items WHERE job_id = ?"
        params: list[object] = [job_id]
        if states:
            query += f" AND state IN ({','.join('?' * len(states))})"
            params.extend(state.value for state in states)
        query += " ORDER BY row"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._conn() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_item_from_row(row) for row in rows]

    def counts(self, job_id: str) -> JobCounts:
        with self._conn() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS n FROM items WHERE job_id = ? GROUP BY state",
                (job_id,),
            ).fetchall()
        counts = JobCounts()
        for row in rows:
            setattr(counts, str(row["state"]), int(row["n"]))
            counts.total += int(row["n"])
        return counts

    def has_work(self, *, now: float | None = None) -> bool:
        """Is there anything `claim` would actually hand out?

        Must ask exactly the question `claim` answers, expiry included. When it did not,
        an expired job left three items `queued` that no worker would ever take, so
        `has_work` said yes forever and `drain` never returned — a pool that is finished
        looking identical to one that is stuck.
        """
        moment = time.time() if now is None else now
        with self._conn() as connection:
            row = connection.execute(
                "SELECT 1 FROM items WHERE state = ? "
                "AND job_id IN (SELECT job_id FROM jobs WHERE expires_at > ?) LIMIT 1",
                (ItemState.QUEUED.value, moment),
            ).fetchone()
        return row is not None

    # --- images ----------------------------------------------------------------------

    def save_image(self, job_id: str, supplied_name: str, data: bytes) -> None:
        directory = self.images_root / job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / stored_name(supplied_name)).write_bytes(data)

    def adopt_image(self, job_id: str, supplied_name: str, source: Path) -> None:
        """Move an already-staged file into the job directory.

        The upload path uses this rather than `save_image` because `save_image` takes
        `bytes`, and calling it means reading the file back into memory to write it out
        again — 600 photographs' worth, for no gain. `Path.replace` is a rename within one
        filesystem: atomic, constant-memory, and it frees the staging copy as it goes.
        """
        directory = self.images_root / job_id
        directory.mkdir(parents=True, exist_ok=True)
        source.replace(directory / stored_name(supplied_name))

    def read_image(self, job_id: str, supplied_name: str) -> bytes | None:
        path = self.images_root / job_id / stored_name(supplied_name)
        if not path.is_file():
            return None
        return path.read_bytes()

    # --- staging ---------------------------------------------------------------------

    @contextmanager
    def staging(self) -> Iterator[Path]:
        """A scratch directory for one upload, removed however this call ends.

        The route spools every incoming part in here before anything is parsed, so
        resident memory stays flat whatever the upload weighs. The `finally` matters as
        much as the directory: a refused upload — a bad zip, an unreadable manifest, a
        batch over the cap — must not leave a gigabyte of someone's label artwork behind
        on the way out (SEC-2).
        """
        directory = Path(tempfile.mkdtemp(prefix="up_", dir=self.staging_root))
        try:
            yield directory
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def purge_staging(self, *, older_than_seconds: float = 3600.0, now: float | None = None) -> int:
        """Drop staging directories a killed process could not clean up itself.

        `staging()` removes its own on every ordinary path, so this only ever finds the
        leavings of a SIGKILL or an OOM — which is exactly when a gigabyte of label
        artwork is most likely to be sitting there, and least likely to be noticed.

        Never raises: it is called from inside `purge_expired`, which the retention
        sweeper wraps in a `sqlite3.Error` handler and nothing wider. A filesystem hiccup
        here must not be able to abort a sweep that was about to delete expired jobs.
        """
        moment = time.time() if now is None else now
        removed = 0
        try:
            candidates = list(self.staging_root.glob("up_*"))
        except OSError:
            return 0
        for directory in candidates:
            try:
                if moment - directory.stat().st_mtime <= older_than_seconds:
                    continue
            except OSError:
                continue
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
        return removed

    # --- retention -------------------------------------------------------------------

    def purge_expired(self, *, now: float | None = None) -> list[str]:
        """Delete every job past its TTL, results and artwork alike (SEC-2, LP-152).

        Batch artefacts are exactly as ephemeral as a single verification's. A retained
        batch is 300 applications' worth of brand names and addresses sitting on a disk
        nobody is thinking about, which is the retention problem the security review
        names.

        **Abandoned staging goes with it**, and that placement is deliberate. The timed
        sweeper in `api/retention.py` walks `MANAGED_SUBDIRS = ("batches", "uploads",
        "results")` — it has never heard of `staging/`, so a directory a SIGKILL left
        behind would be swept only when a new `POST /batch` happened to arrive, which is
        exactly the traffic-dependence the timed sweeper exists to remove. Calling it from
        here means the sweeper picks it up for free, with no change to a file this branch
        does not own. If `"staging"` is later added to `MANAGED_SUBDIRS` this becomes
        harmlessly redundant rather than wrong.
        """
        moment = time.time() if now is None else now
        self.purge_staging(now=moment)
        with self._write_lock, self._conn() as connection:
            rows = connection.execute(
                "SELECT job_id FROM jobs WHERE expires_at <= ?", (moment,)
            ).fetchall()
            purged = [str(row["job_id"]) for row in rows]
            if purged:
                marks = ",".join("?" * len(purged))
                connection.execute(f"DELETE FROM items WHERE job_id IN ({marks})", purged)
                connection.execute(f"DELETE FROM jobs WHERE job_id IN ({marks})", purged)

        for job_id in purged:
            shutil.rmtree(self.images_root / job_id, ignore_errors=True)
        return purged


# --- row mapping ---------------------------------------------------------------------


def _job_of(connection: sqlite3.Connection, item_id: str) -> str:
    row = connection.execute(
        "SELECT job_id FROM items WHERE item_id = ?", (item_id,)
    ).fetchone()
    return str(row["job_id"]) if row else ""


def _settle_job(connection: sqlite3.Connection, job_id: str, now: float) -> None:
    """Mark the job finished once no item can still change state."""
    if not job_id:
        return
    row = connection.execute(
        "SELECT COUNT(*) AS pending FROM items WHERE job_id = ? AND state IN (?, ?)",
        (job_id, ItemState.QUEUED.value, ItemState.PROCESSING.value),
    ).fetchone()
    if row and int(row["pending"]) == 0:
        connection.execute(
            "UPDATE jobs SET state = ?, finished_at = ? WHERE job_id = ?",
            (JobState.DONE.value, now, job_id),
        )


def _job_from_row(row: sqlite3.Row) -> BatchJob:
    return BatchJob(
        job_id=str(row["job_id"]),
        state=JobState(str(row["state"])),
        created_at=float(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        expires_at=float(row["expires_at"]),
        row_errors=[RowError.model_validate(e) for e in json.loads(row["row_errors"])],
        unmatched_files=list(json.loads(row["unmatched_files"])),
    )


def _item_from_row(row: sqlite3.Row) -> BatchItem:
    return BatchItem(
        item_id=str(row["item_id"]),
        job_id=str(row["job_id"]),
        row=int(row["row"]),
        state=ItemState(str(row["state"])),
        attempts=int(row["attempts"]),
        application=Application.model_validate_json(row["application"]),
        images=list(json.loads(row["images"])),
        result=(
            VerificationResult.model_validate_json(row["result"]) if row["result"] else None
        ),
        failure=ItemFailure.model_validate_json(row["failure"]) if row["failure"] else None,
        created_at=float(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
