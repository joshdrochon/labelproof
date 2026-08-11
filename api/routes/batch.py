"""`POST /batch` and friends — the importer dump as a job (BUILD.md §3).

The route owns the upload boundary and the job's lifecycle; everything else lives in
`api.batch`. Four things here are deliberate.

**The upload returns immediately.** A 300-item batch is minutes of work. This endpoint
parses the manifest, stores the artwork, queues the items, and answers with a job ID —
under a second, no held connection. Everything else is fetched afterwards.

**A partly-wrong manifest is still a batch.** Rows that parse are queued; rows that do
not come back numbered, with the column named. Three bad rows out of 300 must not send an
agent back to processing them one at a time (TC-20).

**Nothing is held in memory.** A real batch is 300 applications and roughly 600
photographs — over a gigabyte. Every part is copied to staging a chunk at a time, the
per-file and per-job caps are checked on the chunk that crosses them, and the files an
item needs are renamed into the job directory rather than read out and written back. The
first version built `list[(name, bytes)]` and checked the total afterwards, which made
peak memory a multiple of the upload.

**What bounds memory here is not what bounds disk.** By the time this module runs, the
body is already on disk: FastAPI resolves `list[UploadFile]` as a dependency, so
Starlette's multipart parser has drained the socket into spooled temp files before the
route function starts. The caps below therefore run against local files, not against the
wire, and they bound *residency* only. Disk is bounded upstream by `api.main._WireLimit`,
which counts bytes in `receive`. Each part's temp file is closed as soon as it has been
staged, so the two copies do not both peak.

**Archive contents are hostile.** A zip can name `../../etc/passwd`, expand a kilobyte
into a gigabyte, or carry ten thousand entries. Names are reduced to their last segment,
every entry is size-capped before it is read *and* bounded again as it is decompressed —
the declared size is the attacker's number — and the entry count is capped too (SEC-5).

**The reader never waits on the writer.** `GET /batch/{id}` reads whatever has landed —
finished items are readable while the rest of the job runs, which is the entire point of
BATCH-5 and the only thing that beats one-at-a-time end to end.
"""

from __future__ import annotations

import contextlib
import csv
import io
import time
import zipfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, cast

from fastapi import APIRouter, FastAPI, File, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response

from api import errors
from api import logging as applog
from api.batch import manifest as manifest_mod
from api.batch.models import (
    EXPORT_FIELDS,
    BatchAccepted,
    BatchItem,
    BatchJob,
    BatchStatus,
    ItemState,
    JobCounts,
    JobState,
    RowError,
    job_cost,
    summarize,
    worst_first,
)
from api.batch.store import BatchStore
from api.batch.worker import ProviderBudget, WorkerPool
from api.config import Config
from api.models import FieldName
from api.provider.base import ExtractionProvider
from api.routes import get_config, provider_for

router = APIRouter()

#: Hard ceilings on one upload. BATCH-2 asks for 300 applications; these sit well above
#: it so a real dump is never refused, and well below "unbounded" so a malicious archive
#: cannot exhaust the disk. Numbers, not opinions, because the failure they prevent is
#: silent.
MAX_ROWS = 1000
MAX_FILES = 4000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

#: Items returned inline by `GET /batch/{id}` unless asked for more. A 300-item job with
#: every field of every result inline is megabytes of JSON on every poll; the first
#: screenful in triage order is what an agent actually reads.
DEFAULT_ITEM_LIMIT = 100
MAX_ITEM_LIMIT = 1000

_ARCHIVE_SUFFIX = ".zip"
_MANIFEST_SUFFIXES = (".csv", ".tsv", ".txt")


# --- wiring ---------------------------------------------------------------------------


def get_store(request: Request) -> BatchStore:
    """One store per process, built on first use under `Config.storage_dir`."""
    store: BatchStore | None = getattr(request.app.state, "batch_store", None)
    if store is None:
        config = get_config(request)
        store = BatchStore(Path(config.storage_dir))
        # Anything left `processing` belongs to a process that no longer exists (BATCH-6).
        recovered = store.recover()
        if recovered:
            applog.log("batch_recovered", count=recovered)
        request.app.state.batch_store = store
    return store


def get_pool(request: Request) -> WorkerPool:
    pool: WorkerPool | None = getattr(request.app.state, "batch_pool", None)
    if pool is None:
        config = get_config(request)
        budget: ProviderBudget | None = getattr(request.app.state, "provider_budget", None)
        if budget is None:
            budget = ProviderBudget(config.batch_workers)
            request.app.state.provider_budget = budget
        pool = WorkerPool(
            get_store(request), config, _provider_factory(request), budget=budget
        )
        request.app.state.batch_pool = pool
    return pool


def _provider_factory(request: Request) -> Callable[[Sequence[str]], ExtractionProvider]:
    """Resolve a provider per item, the same way `/verify` resolves one per request.

    `provider_for` reads nothing from the request but `.app`, so a stand-in carrying the
    app is enough and the job never holds a reference to a finished HTTP request. Going
    through the shared resolver rather than a copy of it is what keeps batch honest in
    sample mode: the fixture provider fails closed on artwork it does not recognise, and
    batch inherits that instead of quietly re-implementing a fallback.
    """
    stand_in = cast(Request, SimpleNamespace(app=request.app))

    def factory(filenames: Sequence[str]) -> ExtractionProvider:
        return provider_for(stand_in, list(filenames))

    return factory


#: Paths whose traffic outranks batch work. An agent working their queue is the case the
#: 5-second gate exists for; a 300-item job is not (PERF-5, BATCH-9).
INTERACTIVE_PATHS: frozenset[str] = frozenset({"/verify", "/sample"})


def install_verify_priority(app: FastAPI) -> None:
    """Give Verify Now priority over batch work. One call, from the app factory.

    Priority has to be announced by the interactive side — nothing else in the process
    knows a single verification is in flight. This is that announcement, and it is a
    middleware rather than a change inside `/verify` so the rule holds for any route that
    joins `INTERACTIVE_PATHS` later.

    It only ever *marks*: the request is never made to acquire anything, so installing
    this can never slow a verification down, and leaving it uninstalled costs only the
    priority, never correctness.
    """

    @app.middleware("http")
    async def verify_priority(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.rstrip("/") not in INTERACTIVE_PATHS:
            return await call_next(request)

        budget: ProviderBudget | None = getattr(app.state, "provider_budget", None)
        if budget is None:
            budget = ProviderBudget(get_config(request).batch_workers)
            app.state.provider_budget = budget
        with budget.interactive():
            return await call_next(request)


# --- upload ---------------------------------------------------------------------------


def _safe_name(name: str) -> str:
    return Path(name.replace("\\", "/")).name.strip()


#: Bytes moved per read while a part is spooled to disk. Big enough that a gigabyte is not
#: a million syscalls, small enough that resident memory is a rounding error whatever the
#: upload weighs.
_CHUNK_BYTES = 1024 * 1024


def _too_much() -> errors.UserError:
    return errors.UserError(
        f"That upload holds more than this tool accepts in one batch. Split it into "
        f"batches of up to {MAX_ROWS} applications and upload them separately. Nothing "
        f"has been checked.",
        next_step="reduce",
        code="batch_too_large",
    )


class _Landing:
    """Staging for one upload, copied a chunk at a time with the total cap checked per chunk.

    **This bounds memory, not disk, and the distinction cost a review round.** `create_batch`
    used to build `list[(name, bytes)]` and check `MAX_TOTAL_BYTES` after the list was
    complete, so the cap could only fire once the whole upload was resident — measured at
    693 MB of RSS for 240 MB of content, because the read, the dict and any archive
    expansion all coexist. At the 1.2 GB dump this feature exists for the container is
    OOM-killed, six workers die mid-item, and their rows sit in `processing` until the next
    `BatchStore` construction. Per-item isolation survives a bad image; it does not survive
    the process dying.

    What it does *not* do is bound what reaches the filesystem. `upload.read()` here is
    reading a spooled temp file that Starlette's multipart parser already filled from the
    socket — measured: cap set to 1 MB, 200 MB sent chunked, all 200 MB on disk before this
    class saw a byte. That door is `api.main._WireLimit`. Each temp file is closed as soon
    as it is staged so the two copies do not both peak, but the bound itself lives upstream.
    """

    def __init__(self, root: Path):
        self.root = root
        self.total = 0
        self._sequence = 0

    def _slot(self) -> Path:
        self._sequence += 1
        return self.root / f"{self._sequence:07d}"

    def _account(self, size: int) -> None:
        self.total += size
        if self.total > MAX_TOTAL_BYTES:
            raise _too_much()

    async def spool(self, upload: UploadFile) -> Path:
        """Copy one part into staging, refusing the moment the batch is too big.

        The part's temp file is closed on the way out. Starlette holds every part open
        until the request ends, so without this the upload exists twice over — once in
        `$TMPDIR` and once in staging — and peak disk is double what was sent.
        """
        path = self._slot()
        try:
            with path.open("wb") as sink:
                while chunk := await upload.read(_CHUNK_BYTES):
                    self._account(len(chunk))
                    sink.write(chunk)
        finally:
            with contextlib.suppress(Exception):
                await upload.close()
        return path

    def unpack(self, archive: zipfile.ZipFile, entry: zipfile.ZipInfo, limit: int) -> Path:
        """Decompress one entry to disk, reading at most `limit` + 1 bytes.

        The bound that actually stops a zip bomb is `entry.file_size > max_image_bytes` in
        the caller, checked before a byte is decompressed. CPython's `ZipExtFile` will not
        return more than the declared `file_size` anyway, so an *upward* lie is refused by
        that check and a *downward* lie truncates — producing a short, corrupt image that
        fails ingest later and fails one batch item, in isolation, with a readable reason.

        The `+1` here is therefore belt rather than braces: it is what would catch a zip
        implementation that does not cap at the declared size, and it is what makes an
        over-long entry visible to the caller's `size > max_image_bytes` check instead of
        silently filling the disk. An earlier version of this docstring called it the
        load-bearing check. It is not, and saying so was how it stayed untested.
        """
        path = self._slot()
        remaining = limit + 1
        with archive.open(entry) as source, path.open("wb") as sink:
            while remaining > 0:
                chunk = source.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                self._account(len(chunk))
                sink.write(chunk)
        return path


def _expand(
    staged: Sequence[tuple[str, Path]], landing: _Landing, config: Config
) -> dict[str, Path]:
    """Flatten the upload into `name -> path on disk`, expanding any archives (LP-150, LP-151).

    Multi-select and a zip land in the same place on purpose: an agent who cannot make a
    zip on a locked-down desktop should not be blocked from batch mode, and one who can
    should not have to select 600 files (UX-7).
    """
    files: dict[str, Path] = {}

    def add(name: str, path: Path) -> None:
        clean = _safe_name(name)
        if not clean or clean.startswith("."):
            return
        size = path.stat().st_size
        if size > config.max_image_bytes:
            raise errors.UserError(
                f"“{clean}” is larger than "
                f"{config.max_image_bytes // (1024 * 1024)} MB. Save the label images at "
                f"a smaller size and upload the batch again.",
                next_step="resize",
                code="file_too_large",
            )
        if len(files) >= MAX_FILES:
            raise _too_much()
        files[clean] = path

    for name, path in staged:
        if _safe_name(name).lower().endswith(_ARCHIVE_SUFFIX):
            for inner_name, inner_path in _read_archive(name, path, landing, config):
                add(inner_name, inner_path)
        else:
            add(name, path)

    return files


def _read_archive(
    name: str, path: Path, landing: _Landing, config: Config
) -> list[tuple[str, Path]]:
    """Read a zip defensively: capped entries, capped sizes, names reduced to basenames.

    Opened from the path rather than from `BytesIO`, so a 2 GB archive is read through the
    filesystem's own buffer instead of being resident in full before the first entry is
    even listed.
    """
    try:
        archive = zipfile.ZipFile(path)
    except Exception as exc:
        # Broader than BadZipFile on purpose. A half-copied archive can raise EOFError or
        # OSError from the central-directory read just as easily, and every one of those
        # is the same fact for the agent: this file did not open.
        raise errors.UserError(
            f"“{_safe_name(name)}” could not be opened as a zip file. Re-create "
            f"the archive, or upload the images individually.",
            next_step="replace",
            code="unreadable_archive",
        ) from exc

    entries = archive.infolist()
    if len(entries) > MAX_FILES:
        raise errors.UserError(
            f"That archive holds {len(entries)} files, which is more than this tool "
            f"opens in one batch. Split it into smaller batches and upload them "
            f"separately.",
            next_step="reduce",
            code="batch_too_large",
        )

    out: list[tuple[str, Path]] = []
    with archive:
        for entry in entries:
            if entry.is_dir():
                continue
            clean = _safe_name(entry.filename)
            if not clean or clean.startswith(".") or "__MACOSX" in entry.filename:
                continue
            # The declared size is checked before a byte is decompressed, so an honest
            # oversized entry is refused rather than expanded and then measured. A
            # dishonest one is caught by the read bound inside `unpack`.
            if entry.file_size > config.max_image_bytes:
                raise errors.UserError(
                    f"“{clean}” inside that archive is larger than "
                    f"{config.max_image_bytes // (1024 * 1024)} MB. Save the label images at "
                    f"a smaller size and upload the batch again.",
                    next_step="resize",
                    code="file_too_large",
                )
            try:
                out.append((clean, landing.unpack(archive, entry, config.max_image_bytes)))
            except errors.LabelProofError:
                # The batch is over its size cap. That is our refusal, not a bad entry.
                raise
            except Exception as exc:
                # Decompression is where a damaged archive actually fails, and it fails in
                # a different way every time: BadZipFile for a bad CRC, EOFError for a
                # truncated stream, RuntimeError for an encrypted entry,
                # NotImplementedError for WinZip AES. Every one of them used to leave here
                # as a 500 saying "something went wrong on our side" with next_step=retry —
                # advice that is both wrong and infinitely repeatable, for a fault that is
                # in the agent's file and fixable by re-exporting it. A 1.2 GB dump copied
                # off a flaky share hits this routinely.
                raise errors.UserError(
                    f"“{clean}” inside that archive could not be read. The archive is "
                    f"damaged, or that file is compressed in a way this tool cannot open — "
                    f"encrypted archives are not supported. Re-create the archive from the "
                    f"original images, or upload them individually. Nothing has been "
                    f"checked.",
                    next_step="replace",
                    code="unreadable_archive_entry",
                ) from exc
    return out


def _manifest_text(
    supplied: Path | None, files: dict[str, Path], config: Config
) -> str:
    """The manifest, whether it arrived as its own part or inside the archive.

    Read into memory, because parsing a CSV is not a streaming operation — but only after
    the same per-file cap every other part gets, so "the manifest" is not a way to hand
    this process a gigabyte of text.
    """

    def text_of(path: Path) -> str:
        if path.stat().st_size > config.max_image_bytes:
            raise errors.UserError(
                f"That manifest is larger than "
                f"{config.max_image_bytes // (1024 * 1024)} MB, which is far larger than a "
                f"list of {MAX_ROWS} applications. Send the manifest as a spreadsheet "
                f"exported to CSV.",
                next_step="fix_manifest",
                code="file_too_large",
            )
        return path.read_bytes().decode("utf-8-sig", errors="replace")

    if supplied is not None and supplied.stat().st_size:
        return text_of(supplied)

    candidates = [
        name
        for name in files
        if name.lower().endswith(_MANIFEST_SUFFIXES)
    ]
    if len(candidates) == 1:
        return text_of(files.pop(candidates[0]))
    if len(candidates) > 1:
        raise errors.UserError(
            "That upload holds more than one spreadsheet, so there is no way to tell "
            "which one is the manifest. Send the manifest as its own file and the label "
            "images alongside it.",
            next_step="fix_request",
            code="ambiguous_manifest",
        )
    raise errors.UserError(
        "No manifest was included, so there is nothing saying which images belong to "
        "which application. Download the manifest template, fill in one row per "
        "application, and upload it with the images.",
        next_step="fix_request",
        code="missing_manifest",
    )


@router.get("/batch/manifest-template.csv", include_in_schema=True)
def manifest_template() -> PlainTextResponse:
    """The blank manifest, generated from the parser's own column list (LP-148, LP-168)."""
    return PlainTextResponse(
        manifest_mod.template_csv(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="labelproof-manifest.csv"'
        },
    )


@router.post("/batch", response_model=BatchAccepted)
async def create_batch(
    request: Request,
    files: Annotated[list[UploadFile] | None, File()] = None,
    manifest: Annotated[UploadFile | None, File()] = None,
) -> BatchAccepted:
    """Queue a batch. Answers with a job ID, not with results (BATCH-1, BATCH-2)."""
    config = get_config(request)
    store = get_store(request)

    # Retention runs at the one moment we know the process is alive and doing batch work.
    # A sweeper thread would be a second thing to supervise for a job that has a natural
    # hook right here (SEC-2, LP-152). The read paths refuse an expired job in the
    # meantime, so this is a disk sweep and not the guarantee itself.
    if purged := store.purge_expired():
        applog.log("batch_purged", count=len(purged))
    if abandoned := store.purge_staging():
        applog.log("batch_staging_purged", count=abandoned)

    with store.staging() as scratch:
        landing = _Landing(scratch)

        # Copied into staging before anything is parsed, so peak memory is one chunk
        # rather than the whole dump. What reaches disk at all is bounded upstream by
        # `api.main._WireLimit`, not here.
        staged: list[tuple[str, Path]] = []
        for upload in files or []:
            staged.append((upload.filename or "", await landing.spool(upload)))
        manifest_path = await landing.spool(manifest) if manifest is not None else None

        supplied = _expand(staged, landing, config)
        text = _manifest_text(manifest_path, supplied, config)

        try:
            parsed = manifest_mod.parse(text)
        except manifest_mod.ManifestError as exc:
            raise errors.UserError(
                str(exc), next_step="fix_manifest", code="unreadable_manifest"
            ) from exc

        if len(parsed.rows) > MAX_ROWS:
            raise errors.UserError(
                f"That manifest lists {len(parsed.rows)} applications and this tool takes "
                f"{MAX_ROWS} at a time. Split it into smaller manifests and upload them "
                f"separately.",
                next_step="reduce",
                code="batch_too_large",
            )

        pairing = manifest_mod.pair(parsed.rows, list(supplied))
        row_errors: list[RowError] = sorted(
            parsed.errors + pairing.errors, key=lambda e: (e.row, e.column or "")
        )
        queueable = [row for row in parsed.rows if row.row in pairing.resolved]

        if not queueable:
            raise errors.UserError(
                _nothing_queueable_message(row_errors, pairing.unmatched),
                next_step="fix_manifest",
                code="no_valid_rows",
            )

        job = store.create_job(
            row_errors=row_errors,
            unmatched_files=pairing.unmatched,
            retention_hours=config.retention_hours,
        )

        # Renamed, not copied: the staged file *becomes* the stored one. Files nobody's
        # row named are never moved and go out with the staging directory.
        referenced: set[str] = set()
        for row in queueable:
            for name in pairing.resolved[row.row]:
                if name not in referenced:
                    store.adopt_image(job.job_id, name, supplied[name])
                    referenced.add(name)

        store.add_items(
            job.job_id,
            [(row.row, row.application, pairing.resolved[row.row]) for row in queueable],
        )

    get_pool(request).start()

    applog.log(
        "batch_queued",
        job_id=job.job_id,
        count=len(queueable),
        status=len(row_errors),
    )
    return BatchAccepted(
        job_id=job.job_id,
        accepted=len(queueable),
        row_errors=row_errors,
        unmatched_files=pairing.unmatched,
        message=_accepted_message(len(queueable), row_errors, pairing.unmatched),
    )


def _accepted_message(
    accepted: int, row_errors: Sequence[RowError], unmatched: Sequence[str]
) -> str:
    parts = [
        f"{accepted} application{'s' if accepted != 1 else ''} queued. "
        f"Results appear as each one finishes."
    ]
    if row_errors:
        rows = sorted({e.row for e in row_errors})
        listed = ", ".join(str(r) for r in rows[:10])
        more = f" and {len(rows) - 10} more" if len(rows) > 10 else ""
        parts.append(
            f"{len(rows)} row{'s' if len(rows) != 1 else ''} could not be read and "
            f"{'were' if len(rows) != 1 else 'was'} skipped: row {listed}{more}. Fix "
            f"those rows in the manifest and upload them as a second batch."
        )
    if unmatched:
        listed = ", ".join(unmatched[:10])
        more = f" and {len(unmatched) - 10} more" if len(unmatched) > 10 else ""
        parts.append(
            f"{len(unmatched)} uploaded file{'s were' if len(unmatched) != 1 else ' was'} "
            f"not named by any row and {'were' if len(unmatched) != 1 else 'was'} not "
            f"used: {listed}{more}."
        )
    return " ".join(parts)


def _nothing_queueable_message(
    row_errors: Sequence[RowError], unmatched: Sequence[str]
) -> str:
    first = row_errors[0] if row_errors else None
    detail = f" The first problem is on row {first.row}: {first.message}" if first else ""
    unused = (
        f" {len(unmatched)} uploaded file(s) were not named by any row: "
        f"{', '.join(unmatched[:10])}."
        if unmatched
        else ""
    )
    return (
        f"None of the rows in that manifest could be queued, so nothing has been "
        f"checked.{detail}{unused}"
    )


# --- reading --------------------------------------------------------------------------


def _require_job(
    store: BatchStore, job_id: str, retention_hours: int, *, now: float | None = None
) -> BatchJob:
    """The job, or the same refusal for "never existed" and "past its life".

    The expiry half of this is not belt-and-braces. Purging is driven by `POST /batch`,
    so a server that takes one importer dump and then goes quiet never sweeps — and
    without this check every read path went on serving that job: full status, every item,
    and an export carrying 300 applications' brand names, addresses and extracted label
    text. Not merely retained past the promise, but actively handed back, while the
    message two lines below told the caller the data was deleted hours ago. A false
    statement to a government user about what we still hold is a worse failure than the
    disk usage (SEC-2, LP-152).

    So expiry is enforced where it is read, not where it is swept. Deleting the bytes is
    still the sweeper's job — this only guarantees nobody is served them in the meantime,
    which is the part that has to be true at every instant rather than eventually.

    Expired and absent answer identically on purpose. They are the same fact from the
    agent's seat — the batch is gone and a new one is needed — and the existing message
    already says retention is why.
    """
    job = store.get_job(job_id)
    if job is not None and job.expires_at > (time.time() if now is None else now):
        return job
    raise errors.UserError(
        f"No batch with that reference is on this server. Batches and their images are "
        f"deleted {retention_hours} hours after they are started, so this one may have "
        f"expired. Upload the manifest again to start a new batch.",
        next_step="navigate",
        code="batch_not_found",
    )


def _eta_seconds(counts: JobCounts, items: Sequence[BatchItem], workers: int) -> int | None:
    """A rough finish time from what has actually been observed, or nothing.

    Deliberately returns None until enough items have finished to mean anything. A
    countdown invented from one sample is worse than no countdown: it moves, so it looks
    authoritative, and the vendor pilot died of a progress indicator nobody believed.
    """
    durations = [
        item.finished_at - item.started_at
        for item in items
        if item.started_at and item.finished_at and item.finished_at >= item.started_at
    ]
    remaining = counts.queued + counts.processing
    if len(durations) < 3 or remaining == 0:
        return None
    average = sum(durations) / len(durations)
    return max(1, round(average * remaining / max(1, workers)))


@router.get("/batch/{job_id}", response_model=BatchStatus)
def batch_status(
    request: Request,
    job_id: str,
    limit: int = DEFAULT_ITEM_LIMIT,
    include_pending: bool = False,
) -> BatchStatus:
    """Counts, summary, and the finished items so far — while the job runs (BATCH-5)."""
    config = get_config(request)
    store = get_store(request)
    job = _require_job(store, job_id, config.retention_hours)
    counts = store.counts(job_id)
    everything = store.items(job_id)

    finished = [item for item in everything if item.state in (ItemState.DONE, ItemState.FAILED)]
    visible = everything if include_pending else finished

    ordered = worst_first(visible)
    capped = max(0, min(limit, MAX_ITEM_LIMIT))

    return BatchStatus(
        job_id=job_id,
        state=job.state,
        counts=counts,
        eta_seconds=_eta_seconds(counts, finished, get_pool(request).workers),
        summary=summarize(everything),
        items=ordered[:capped],
        cost=job_cost(everything),
        row_errors=job.row_errors,
        unmatched_files=job.unmatched_files,
        expires_at=job.expires_at,
        message=_status_message(job.state, counts),
    )


def _status_message(state: JobState, counts: JobCounts) -> str:
    if state is JobState.DONE:
        failed = (
            f" {counts.failed} could not be checked and can be retried."
            if counts.failed
            else ""
        )
        return f"Finished. {counts.done} of {counts.total} checked.{failed}"
    return (
        f"{counts.done + counts.failed} of {counts.total} finished. Results below are "
        f"ready to review now — the rest are still running."
    )


@router.post("/batch/{job_id}/retry", response_model=BatchStatus)
def retry_batch(request: Request, job_id: str) -> BatchStatus:
    """Requeue the failed items and nothing else (BATCH-8, LP-157)."""
    config = get_config(request)
    store = get_store(request)
    _require_job(store, job_id, config.retention_hours)

    requeued = store.retry_failed(job_id)
    if requeued:
        get_pool(request).start()
    applog.log("batch_retry", job_id=job_id, count=requeued)

    status = batch_status(request, job_id, include_pending=True)
    status.message = (
        f"Retrying {requeued} failed application{'s' if requeued != 1 else ''}. The "
        f"{status.counts.done} already checked were left alone."
        if requeued
        else "There are no failed applications in this batch to retry."
    )
    return status


# --- export ---------------------------------------------------------------------------

_EXPORT_HEADER: tuple[str, ...] = (
    "row",
    "item_id",
    "state",
    "recommendation",
    "driving_field",
    "brand_name",
    "class_type",
    *(f"verdict_{field.value}" for field in EXPORT_FIELDS),
    "findings",
    "rationale",
    "images",
)


def _findings_text(item: BatchItem) -> str:
    if item.result is None:
        return ""
    parts: list[str] = []
    for field in item.result.fields:
        for finding in field.findings:
            citation = f" ({finding.citation})" if finding.citation else ""
            parts.append(f"{field.field.value}: {finding.message}{citation}")
    return " | ".join(parts)


def _export_row(item: BatchItem) -> list[str]:
    verdicts: dict[FieldName, str] = {}
    if item.result:
        verdicts = {field.field: field.verdict.value for field in item.result.fields}

    if item.result:
        recommendation = item.result.aggregate.recommendation.value
        driving = (
            item.result.aggregate.driving_field.value
            if item.result.aggregate.driving_field
            else ""
        )
        rationale = item.result.aggregate.rationale
    else:
        recommendation = ""
        driving = ""
        rationale = item.failure.message if item.failure else ""

    return [
        str(item.row),
        item.item_id,
        item.state.value,
        recommendation,
        driving,
        item.application.brand_name,
        item.application.class_type,
        *(verdicts.get(field, "") for field in EXPORT_FIELDS),
        _findings_text(item),
        rationale,
        " ".join(item.images),
    ]


@router.get("/batch/{job_id}/export.csv")
def export_batch(request: Request, job_id: str) -> PlainTextResponse:
    """Per-item verdicts and findings, worst first, for the case file (BATCH-7, LP-162).

    Worst-first in the file too, not only on screen. The export is what gets printed and
    handed upward, and a printout whose first page is 40 clean approvals buries the three
    rows the conversation is actually about.
    """
    config = get_config(request)
    store = get_store(request)
    _require_job(store, job_id, config.retention_hours)

    items = worst_first(store.items(job_id))

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_EXPORT_HEADER)
    for item in items:
        writer.writerow(_export_row(item))

    applog.log("batch_exported", job_id=job_id, count=len(items))
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="labelproof-{job_id}.csv"'
        },
    )
