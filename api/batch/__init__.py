"""Batch mode — the importer dump (PRD §The Two Modes, BATCH-1..10).

Big importers file 200-300 applications at once and the team processes them one at a
time. This package is the queued job that replaces that: a manifest plus artwork in,
progressive per-item results out, worst findings first.

**It is a job, not a big request.** Known prior art tried 300 verifications inside one
HTTP call and it broke at two or three images against an HTTP timeout. Nothing about that
is fixable by tuning a timeout — a browser connection held open for ten minutes is a
design that fails on a tab close, a proxy, a redeploy, or a laptop lid. So the upload
returns a job ID in under a second and every result is fetched afterwards.

Layering: `manifest` parses, `store` persists, `worker` executes, `models` describes.
Nothing here imports from `api.routes`; the route layer depends on this package and not
the reverse.
"""

from __future__ import annotations

from api.batch.models import (
    BatchAccepted,
    BatchItem,
    BatchJob,
    BatchStatus,
    BatchSummary,
    ItemFailure,
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

__all__ = [
    "BatchAccepted",
    "BatchItem",
    "BatchJob",
    "BatchStatus",
    "BatchStore",
    "BatchSummary",
    "ItemFailure",
    "ItemState",
    "JobCounts",
    "JobState",
    "ProviderBudget",
    "RowError",
    "WorkerPool",
    "job_cost",
    "summarize",
    "worst_first",
]
