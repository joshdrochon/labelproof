"""Batch domain types — the job, its items, and the triage view over them.

Two things here are worth reading before the code.

**A batch item is not a verification.** It is a unit of *work* that may or may not have
produced a verification yet. Those are different states and conflating them is how a
batch reports "300 done" while sixty of them silently produced nothing. So an item
carries a work state (`queued/processing/done/failed`) alongside its result, and the
summary counts them separately — an item that failed is never counted as an outcome.

**Ranking is borrowed, never reinvented.** `api.rules.aggregate.triage_order` already
decides which field is worst and which one an agent should read first. The batch table
sorts items by *their* worst field, and it gets that ordering by handing the worst fields
back to `triage_order`. One ranking rule, one place, so a row means the same thing in the
single checklist and in a 300-row table (PRD §The Two Modes).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from api.models import (
    AgentDecision,
    Application,
    Cost,
    FieldName,
    FieldResult,
    Recommendation,
    Verdict,
    VerificationResult,
)
from api.rules.aggregate import triage_order


class ItemState(StrEnum):
    """Where one application is in the pipeline (BATCH-4)."""

    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


#: States from which no further work happens without an explicit retry.
TERMINAL_STATES: frozenset[ItemState] = frozenset({ItemState.DONE, ItemState.FAILED})


class JobState(StrEnum):
    """Where the job as a whole is.

    There is deliberately no `failed` job state. A job whose items all failed is still a
    *finished* job with 300 failed items, and saying so is more useful than a single word
    that hides which ones (BATCH-6).
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"


class RowError(BaseModel):
    """One problem with one manifest row, addressed the way a spreadsheet addresses it.

    `row` is the line number an agent sees in Excel: the header is row 1, so the first
    application is row 2. Reporting a zero-based index would make the agent do arithmetic
    on 300 rows to find the one that is wrong (TC-20, BATCH-3).
    """

    row: int
    column: str | None = None
    message: str


class ItemFailure(BaseModel):
    """Why one item could not be verified, in words an agent can act on (UX-6)."""

    code: str
    message: str
    next_step: str = "retry"
    attempts: int = 0


class BatchItem(BaseModel):
    """One application in the batch."""

    item_id: str
    job_id: str
    row: int
    state: ItemState = ItemState.QUEUED
    attempts: int = 0
    application: Application
    images: list[str] = Field(default_factory=list)
    result: VerificationResult | None = None
    failure: ItemFailure | None = None
    decisions: dict[FieldName, AgentDecision] = Field(default_factory=dict)
    """What the agent ruled on each row, for the fields they have ruled on (HITL-5).

    On the item rather than beside it, so it rides along with every read of the item —
    `GET /batch/{id}`, the export, the PATCH response. An agent who records a decision and
    then reloads the page must see it still there; a decisions map served from only one of
    the three endpoints is the "it looked saved but wasn't" failure this exists to prevent.

    Sparse on purpose. An absent field is one nobody has ruled on yet, which is a different
    fact from either decision and is not worth a third enum value to say.
    """

    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def recommendation(self) -> Recommendation | None:
        return self.result.aggregate.recommendation if self.result else None


class JobCounts(BaseModel):
    """Progress at a glance (BATCH-4)."""

    total: int = 0
    queued: int = 0
    processing: int = 0
    done: int = 0
    failed: int = 0

    @property
    def finished(self) -> int:
        return self.done + self.failed

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.finished >= self.total


class BatchJob(BaseModel):
    """The job record. Everything needed to rebuild the view after a restart (BATCH-6)."""

    job_id: str
    state: JobState = JobState.QUEUED
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    expires_at: float = 0.0
    row_errors: list[RowError] = Field(default_factory=list)
    unmatched_files: list[str] = Field(default_factory=list)


class BatchSummary(BaseModel):
    """The triage view: how the batch came out, worst first (UX-10, BATCH-7)."""

    by_recommendation: dict[str, int] = Field(default_factory=dict)
    by_verdict: dict[str, int] = Field(default_factory=dict)
    worst_first: list[str] = Field(default_factory=list)
    """Item IDs in the order an agent should work them."""

    headline: str = ""


class BatchStatus(BaseModel):
    """What `GET /batch/{id}` answers with."""

    job_id: str
    state: JobState
    counts: JobCounts
    eta_seconds: int | None = None
    summary: BatchSummary
    items: list[BatchItem] = Field(default_factory=list)
    cost: Cost = Field(default_factory=Cost)
    row_errors: list[RowError] = Field(default_factory=list)
    unmatched_files: list[str] = Field(default_factory=list)
    expires_at: float = 0.0
    message: str = ""


class BatchAccepted(BaseModel):
    """What `POST /batch` answers with.

    The row errors ride along with the job ID rather than replacing it. A manifest with
    three bad rows out of 300 is not a rejected upload — the 297 good ones are already
    queued, and making an agent fix a typo before any work starts is the batch equivalent
    of processing them one at a time (TC-20).
    """

    job_id: str
    accepted: int
    row_errors: list[RowError] = Field(default_factory=list)
    unmatched_files: list[str] = Field(default_factory=list)
    message: str = ""


#: Item-level triage tiers. Field-level ranking belongs to `triage_order` and is not
#: re-decided here; this only says which *bucket* an item lands in first.
#:
#: A failed item sits above Needs review deliberately. "We could not check this" is an
#: action for the agent — retry it or verify it by hand — where "needs your eyes" is a
#: reading task. Burying five failures under fifty reviews is how they get missed.
_ITEM_TIER: dict[str, int] = {
    Recommendation.RETURN_FOR_CORRECTION.value: 0,
    "failed": 1,
    Recommendation.NEEDS_REVIEW.value: 2,
    Recommendation.READY_TO_APPROVE.value: 3,
}
_UNFINISHED_TIER = 4


def _tier(item: BatchItem) -> int:
    if item.state is ItemState.FAILED:
        return _ITEM_TIER["failed"]
    if item.result is None:
        return _UNFINISHED_TIER
    return _ITEM_TIER.get(item.result.aggregate.recommendation.value, _UNFINISHED_TIER)


def worst_first(items: list[BatchItem]) -> list[BatchItem]:
    """Order items the way an agent should work them.

    Within a tier the order comes from `triage_order`: each item's worst field is handed
    back to the single ranking function, and the order it returns is the order of the
    items those fields came from. Nothing about warning-first or verdict severity is
    restated here — if that policy changes in `aggregate.py`, this table follows it.
    """
    worst_of: dict[int, FieldResult] = {}
    for item in items:
        if item.result and item.result.fields:
            worst_of[id(item)] = triage_order(item.result.fields)[0]

    ranked = triage_order(list(worst_of.values()))
    position = {id(field): index for index, field in enumerate(ranked)}

    def key(item: BatchItem) -> tuple[int, int, int]:
        worst = worst_of.get(id(item))
        return (
            _tier(item),
            position.get(id(worst), len(position)) if worst is not None else len(position),
            item.row,
        )

    return sorted(items, key=key)


def summarize(items: list[BatchItem]) -> BatchSummary:
    """Roll a batch into counts plus a precomputed working order (LP-161).

    Computed on read rather than stored. The alternative — maintaining running counters
    as items land — is a second source of truth that drifts the first time a retry
    rewrites an item, and a batch summary that disagrees with the rows under it destroys
    the trust the whole product runs on.
    """
    by_recommendation: dict[str, int] = {r.value: 0 for r in Recommendation}
    by_recommendation["failed"] = 0
    by_verdict: dict[str, int] = {v.value: 0 for v in Verdict}

    for item in items:
        if item.state is ItemState.FAILED:
            by_recommendation["failed"] += 1
        elif item.result is not None:
            by_recommendation[item.result.aggregate.recommendation.value] += 1
            for field in item.result.fields:
                by_verdict[field.verdict.value] += 1

    ordered = worst_first(items)
    attention = (
        by_recommendation[Recommendation.RETURN_FOR_CORRECTION.value]
        + by_recommendation["failed"]
        + by_recommendation[Recommendation.NEEDS_REVIEW.value]
    )
    clean = by_recommendation[Recommendation.READY_TO_APPROVE.value]

    if attention == 0 and clean == 0:
        headline = "Nothing has finished yet."
    elif attention == 0:
        headline = (
            f"All {clean} applications checked out. The final decision is yours."
        )
    else:
        noun = "application needs" if attention == 1 else "applications need"
        headline = (
            f"{attention} {noun} your attention, listed first. {clean} checked out. "
            f"The final decision is yours."
        )

    return BatchSummary(
        by_recommendation=by_recommendation,
        by_verdict=by_verdict,
        worst_first=[item.item_id for item in ordered],
        headline=headline,
    )


def job_cost(items: list[BatchItem]) -> Cost:
    """What the batch spent (LP-166, OPS-4)."""
    total = Cost()
    for item in items:
        if item.result is None:
            continue
        cost = item.result.cost
        total.input_tokens += cost.input_tokens
        total.output_tokens += cost.output_tokens
        # Both cache counters, because both are BILLED and neither is inside
        # `input_tokens`: a cached read costs 0.1x an input token and writing an entry
        # costs 1.25x. They were dropped here, so a job that read 95,722 cached tokens
        # reported zero of them — which does not make the total conservative, it makes
        # those tokens free.
        total.cache_read_tokens += cost.cache_read_tokens
        total.cache_creation_tokens += cost.cache_creation_tokens
        total.usd += cost.usd
    total.usd = round(total.usd, 6)
    return total


#: Field order used by the CSV export, so a column means the same thing in every export.
EXPORT_FIELDS: tuple[FieldName, ...] = tuple(FieldName)
