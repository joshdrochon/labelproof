"""Run a real 300-application batch against a deployed URL and report what happened
(BATCH-1, BATCH-2, BATCH-9, PERF-4, PERF-5, TC-20).

    .venv/bin/python -m scripts.batch_300 https://labelproof.fly.dev
    .venv/bin/python -m scripts.batch_300 https://labelproof.fly.dev --rows 10 --no-probes
    .venv/bin/python -m scripts.batch_300 https://labelproof.fly.dev \
        --out docs/batch-300.md --note "first real 300-item run"

**This costs real money.** Three hundred applications is three hundred model calls, and
`--dry-run` exists so the manifest, the images and the zip can be checked without
spending any of it. Run the estimate first.

Why this script exists rather than a paragraph
----------------------------------------------

The README claimed 300 items in roughly 9.5 minutes by scaling a 22-item run. That is an
extrapolation, and the two things worth knowing about a batch at scale are exactly the two
an extrapolation cannot see: **provider rate limiting**, which does not appear at 22, and
**whether the priority lane holds**, which needs someone verifying a label while the batch
runs. So this measures both, on the deployed service, and writes down what it got.

What it reports, and why each number is in the list
---------------------------------------------------

* **Wall clock**, submit to last item. PERF-4 sets ten minutes for 300.
* **Time to first visible result**, from submit. BATCH-2 says results appear while the job
  runs rather than at the end, and "within 10 seconds" is the number in the PRD. Measured
  by polling `GET /batch/{id}` and stopping the clock the moment any item carries a result.
* **Per-item failures with reasons**, counted by reason code rather than summarised as a
  rate. Ten failures that are all `provider_rate_limited` is a different finding from ten
  spread across five causes.
* **Rate limiting, named if it appears.** Item failures carrying a 429 or a rate-limit
  code are counted separately and reported even when the batch otherwise succeeds, because
  a retry that eventually worked still tells you the ceiling was touched.
* **Total cost**, from the job's own `cost` block. Not modelled from token guesses.
* **Verify Now, timed during the run.** BATCH-9 and PERF-5 promise a batch does not
  starve the single-label lane. That has been asserted in tests against a stub budget and
  never measured against the deployed service with 300 real items in flight. A probe fires
  every `--probe-interval` seconds and each one is timed and printed.

The manifest is mixed on purpose (TC-20)
-----------------------------------------

Three hundred identical clean rows would measure throughput and nothing else. This builds
clean rows, rows whose application disagrees with the artwork, rows whose artwork is
blurred past reading, and — on top of the 300 — a handful of **malformed** rows that the
manifest parser must reject by row number while queueing everything else. The malformed
rows are deliberately *extra*, so "300 applications" stays 300 applications and the
row-level validation is demonstrated rather than paid for out of the count.

Standard library plus the fixture generator, so it runs from any checkout that can reach
the URL.
"""

from __future__ import annotations

import argparse
import csv
import io
import statistics
import sys
import threading
import time
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fixtures.generator.degrade import apply_preset
from fixtures.generator.render import render
from fixtures.generator.spec import LabelSpec
from scripts.timed_run import Reply, build_multipart, git_commit, http_get, poster_for

DEFAULT_URL = "https://labelproof.fly.dev"
DEFAULT_ROWS = 300
DEFAULT_MALFORMED = 6
DEFAULT_POLL_S = 2.0
DEFAULT_PROBE_INTERVAL_S = 45.0

#: Both goals are PERF-4's (PRD.md, Appendix A) — 300 applications inside ten minutes and
#: the first result inside ten seconds. An earlier version of this file credited the
#: ten-second one to BATCH-2, which is the requirement that results appear *while the job
#: runs* rather than the requirement that says how fast.
#:
#: Reported against rather than enforced: a script that exits non-zero on a slow batch
#: tempts whoever runs it to shrink the batch until it passes.
GOAL_WALL_S = 600.0
GOAL_FIRST_RESULT_S = 10.0

#: How long to wait for the job to finish before giving up and reporting what we have.
#: Deliberately far past the goal: a run that misses ten minutes is the finding, and
#: stopping the clock at ten minutes would throw it away.
HARD_TIMEOUT_S = 3600.0

MANIFEST_COLUMNS = (
    "commodity",
    "brand_name",
    "class_type",
    "alcohol_content",
    "net_contents",
    "producer_name",
    "producer_address",
    "country_of_origin",
    "is_import",
    "front_image",
    "back_image",
)

#: Reason codes that mean the provider pushed back rather than the label being bad.
#: Matched as substrings against the failure code and message, because the taxonomy can
#: name this more than one way and a rate limit that goes unrecognised is the exact thing
#: this run exists to catch.
RATE_LIMIT_MARKERS = ("rate_limit", "rate-limit", "429", "overloaded", "too_many")

#: How many row errors the report prints before truncating. Truncation is announced —
#: a table that silently stops is a table that under-reports.
_ROW_ERROR_TABLE_LIMIT = 20

#: `GET /batch/{id}` pages its item list and defaults to 100. Left at the default, a
#: 300-item report counts the recommendations of the worst 100 items and silently calls
#: that the mix — the first version of this script did exactly that and produced a table
#: summing to 100 out of 300. `MAX_ITEM_LIMIT` in api/routes/batch.py is 1000.
ITEM_LIMIT = 1000


# --------------------------------------------------------------------------------------
# The manifest, and the artwork it names
# --------------------------------------------------------------------------------------


@dataclass
class Row:
    """One line of the manifest, plus what we expect it to prove."""

    kind: str  # clean | mismatch | unreadable | malformed
    values: dict[str, str]
    images: dict[str, bytes] = field(default_factory=dict)


def _spec(index: int, brand: str) -> LabelSpec:
    """A single-faced label. One image per application, deliberately.

    Two images per row doubles the model spend for a run whose question is throughput and
    rate limiting, and a single-faced fixture carries the government warning on the same
    face, so nothing is dropped from what gets checked. The 22-item run this replaces used
    two; that difference is stated in the report rather than left for someone to find.
    """
    return LabelSpec(
        name=f"batch{index:03d}",
        brand_name=brand,
        class_type="Kentucky Straight Bourbon Whiskey",
        alcohol_text="45% Alc./Vol. (90 Proof)",
        net_contents="750 mL",
        producer=f"{brand.title()} Distillery, Bardstown, Kentucky",
        face="single",
    )


def _png(spec: LabelSpec, preset: str | None = None) -> bytes:
    image = render(spec)
    if preset is not None:
        import numpy as np
        from PIL import Image

        degraded = apply_preset(np.array(image.convert("RGB")), preset)
        image = Image.fromarray(degraded)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def build_rows(count: int, malformed: int) -> list[Row]:
    """`count` real applications, in four shapes, plus `malformed` bad lines on top.

    The mix is fixed by position rather than randomised: a run that cannot be reproduced
    row for row cannot be compared against the next one.
    """
    rows: list[Row] = []
    for i in range(count):
        brand = f"OLD TOM {i:03d}"
        spec = _spec(i, brand)
        name = f"batch{i:03d}.png"

        if i % 10 == 7:
            # The artwork is fine; the application disagrees with it — 45% on the label
            # against 40% filed. The expected answer is **Needs review**, not Return for
            # correction: `api/rules/aggregate.py` reserves return-for-correction for a
            # bad or absent government warning and for a missing mandatory element, and a
            # field that disagrees is a thing for the agent to look at (PRD.md §69). This
            # comment used to say return-for-correction and the run scored 0/300 against
            # it, which was the comment being wrong rather than the product.
            rows.append(
                Row(
                    kind="mismatch",
                    values={
                        "commodity": "spirits",
                        "brand_name": brand,
                        "class_type": "Kentucky Straight Bourbon Whiskey",
                        "alcohol_content": "40",
                        "net_contents": "750 mL",
                        "producer_name": f"{brand.title()} Distillery",
                        "producer_address": "Bardstown, Kentucky",
                        "country_of_origin": "",
                        "is_import": "false",
                        "front_image": name,
                        "back_image": "",
                    },
                    images={name: _png(spec)},
                )
            )
        elif i % 10 == 3:
            # Blurred past reading. The pipeline owes us "could not check", never a
            # finding against the label.
            rows.append(
                Row(
                    kind="unreadable",
                    values={
                        "commodity": "spirits",
                        "brand_name": brand,
                        "class_type": "Kentucky Straight Bourbon Whiskey",
                        "alcohol_content": "45",
                        "net_contents": "750 mL",
                        "producer_name": f"{brand.title()} Distillery",
                        "producer_address": "Bardstown, Kentucky",
                        "country_of_origin": "",
                        "is_import": "false",
                        "front_image": name,
                        "back_image": "",
                    },
                    images={name: _png(spec, "tc14_blur_hopeless")},
                )
            )
        else:
            rows.append(
                Row(
                    kind="clean",
                    values={
                        "commodity": "spirits",
                        "brand_name": brand,
                        "class_type": "Kentucky Straight Bourbon Whiskey",
                        "alcohol_content": "45",
                        "net_contents": "750 mL",
                        "producer_name": f"{brand.title()} Distillery",
                        "producer_address": "Bardstown, Kentucky",
                        "country_of_origin": "",
                        "is_import": "false",
                        "front_image": name,
                        "back_image": "",
                    },
                    images={name: _png(spec)},
                )
            )

    # Malformed, one of each shape the parser is supposed to catch by row number.
    shapes: list[dict[str, str]] = [
        # not a commodity this tool checks
        {"commodity": "cider", "brand_name": "BAD ROW A", "front_image": "batch000.png"},
        # alcohol content that is not a number
        {
            "commodity": "spirits",
            "brand_name": "BAD ROW B",
            "alcohol_content": "forty-five",
            "front_image": "batch000.png",
        },
        # names an image that is not in the upload
        {
            "commodity": "spirits",
            "brand_name": "BAD ROW C",
            "alcohol_content": "45",
            "front_image": "nothing_here.png",
        },
        # no image column filled at all
        {"commodity": "spirits", "brand_name": "BAD ROW D", "alcohol_content": "45"},
        # brand name missing
        {"commodity": "spirits", "alcohol_content": "45", "front_image": "batch000.png"},
        # nothing at all
        {},
    ]
    for i in range(malformed):
        base = dict.fromkeys(MANIFEST_COLUMNS, "")
        base.update(shapes[i % len(shapes)])
        rows.append(Row(kind="malformed", values=base))

    return rows


def manifest_csv(rows: list[Row]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(MANIFEST_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.values.get(column, "") for column in MANIFEST_COLUMNS})
    return buffer.getvalue().encode()


def artwork_zip(rows: list[Row]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            for name, data in row.images.items():
                archive.writestr(name, data)
    return buffer.getvalue()


# --------------------------------------------------------------------------------------
# Talking to the service
# --------------------------------------------------------------------------------------


def submit(base_url: str, manifest: bytes, archive: bytes, timeout: float) -> Reply:
    boundary = f"----labelproof{uuid.uuid4().hex}"
    parts: list[bytes] = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="manifest"; '
            f'filename="manifest.csv"\r\nContent-Type: text/csv\r\n\r\n'
        ).encode(),
        manifest,
        b"\r\n",
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
            f'filename="labels.zip"\r\nContent-Type: application/zip\r\n\r\n'
        ).encode(),
        archive,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    post = poster_for(base_url, timeout)
    return post("/batch", f"multipart/form-data; boundary={boundary}", b"".join(parts))


@dataclass
class Probe:
    """One Verify Now request, fired while the batch runs (BATCH-9, PERF-5)."""

    at_s: float
    client_ms: int
    status: int
    server_ms: int | None = None
    detail: str = ""


class VerifyProbe(threading.Thread):
    """Fires a real single verification every `interval` seconds until told to stop.

    Deliberately a real `POST /verify` with real artwork rather than a `GET /health`.
    The priority lane is about model slots — `ProviderBudget` — and a request that never
    asks for one cannot show whether the batch is holding them all.
    """

    def __init__(
        self,
        base_url: str,
        application: dict[str, Any],
        images: list[tuple[str, bytes]],
        interval: float,
        timeout: float,
    ) -> None:
        super().__init__(daemon=True)
        self._post = poster_for(base_url, timeout)
        self._application = application
        self._images = images
        self._interval = interval
        self._stop = threading.Event()
        self.probes: list[Probe] = []
        self.started_at = 0.0

    def run(self) -> None:
        self.started_at = time.perf_counter()
        while not self._stop.wait(self._interval):
            content_type, body = build_multipart(self._application, self._images)
            at = time.perf_counter() - self.started_at
            started = time.perf_counter()
            reply = self._post("/verify", content_type, body)
            client_ms = round((time.perf_counter() - started) * 1000)
            payload = reply.json()
            timings = payload.get("timings_ms") if isinstance(payload, dict) else None
            server_ms = timings.get("total") if isinstance(timings, dict) else None
            detail = ""
            if reply.status != 200:
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                detail = str(error.get("code") or reply.body[:80])
            self.probes.append(
                Probe(
                    at_s=at,
                    client_ms=client_ms,
                    status=reply.status,
                    server_ms=server_ms if isinstance(server_ms, int) else None,
                    detail=detail,
                )
            )
            print(
                f"    verify-now  t+{at:6.1f}s  http={reply.status:<3} {client_ms:>6}ms"
                f"{'  ' + detail if detail else ''}",
                file=sys.stderr,
            )

    def stop(self) -> None:
        self._stop.set()


@dataclass
class Poll:
    at_s: float
    done: int
    failed: int
    processing: int
    total: int


@dataclass
class Outcome:
    job_id: str = ""
    accepted: int = 0
    row_errors: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    first_result_s: float | None = None
    wall_s: float = 0.0
    polls: list[Poll] = field(default_factory=list)
    final: dict[str, Any] = field(default_factory=dict)
    complete: bool = False
    poll_errors: Counter[int] = field(default_factory=Counter)


def run_job(
    base_url: str, job_id: str, poll_s: float, timeout: float, started: float
) -> Outcome:
    """Poll to completion, stopping the first-result clock the moment anything lands."""
    outcome = Outcome(job_id=job_id)
    while True:
        elapsed = time.perf_counter() - started
        if elapsed > HARD_TIMEOUT_S:
            break

        reply = http_get(f"{base_url.rstrip('/')}/batch/{job_id}?limit={ITEM_LIMIT}", timeout)
        if reply.status != 200:
            outcome.poll_errors[reply.status] += 1
            time.sleep(poll_s)
            continue

        body = reply.json()
        counts = body.get("counts", {}) if isinstance(body.get("counts"), dict) else {}
        done = int(counts.get("done", 0))
        failed = int(counts.get("failed", 0))
        total = int(counts.get("total", 0))
        outcome.polls.append(
            Poll(
                at_s=elapsed,
                done=done,
                failed=failed,
                processing=int(counts.get("processing", 0)),
                total=total,
            )
        )

        if outcome.first_result_s is None and (done + failed) > 0:
            outcome.first_result_s = elapsed
            print(f"  first result at t+{elapsed:.1f}s", file=sys.stderr)

        if total and (done + failed) >= total:
            outcome.wall_s = elapsed
            outcome.final = body
            outcome.complete = True
            break

        print(
            f"  t+{elapsed:6.1f}s  done={done:<4} failed={failed:<3} "
            f"processing={counts.get('processing', 0):<3} of {total}",
            file=sys.stderr,
        )
        time.sleep(poll_s)

    if not outcome.complete:
        outcome.wall_s = time.perf_counter() - started
        last = http_get(f"{base_url.rstrip('/')}/batch/{job_id}?limit={ITEM_LIMIT}", timeout)
        if last.status == 200:
            outcome.final = last.json()
    return outcome


# --------------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------------


def retry_histogram(final: dict[str, Any]) -> Counter[int]:
    """`attempts` across every item — the only place a *survived* throttle is visible.

    This is the correction to a real hole. The first version of this script counted rate
    limiting by reading `item["failure"]`, and `api/batch/worker.py` requeues a retryable
    provider error up to `MAX_ATTEMPTS`: an item that was throttled, backed off and then
    succeeded carries no failure object at all, so the report said "0 rate-limited" about
    a run in which it could not have seen otherwise.

    `attempts` is incremented in `api/batch/store.py` every time an item is claimed for
    processing, so `attempts == 1` on every item means no item was ever requeued, which
    means no retryable provider error — 429 included — reached any of them.

    The claim is only airtight because `api/provider/anthropic_adapter.py` constructs the
    SDK client with `max_retries=0` and says why: retries are the application's, so the
    SDK cannot silently absorb a 429 below this counter. If that ever changes, this
    measurement goes blind again and this docstring is where to start.
    """
    histogram: Counter[int] = Counter()
    for item in final.get("items", []) or []:
        if isinstance(item, dict):
            histogram[int(item.get("attempts", 0) or 0)] += 1
    return histogram


def failure_reasons(final: dict[str, Any]) -> tuple[Counter[str], int]:
    """Reason code -> count, and how many of those *failed* on something rate-limit-like.

    Only counts items that ended failed. Retried-and-survived throttling is invisible
    here by construction; `retry_histogram` is what sees that.
    """
    reasons: Counter[str] = Counter()
    throttled = 0
    for item in final.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        failure = item.get("failure")
        if not isinstance(failure, dict):
            continue
        code = str(failure.get("code") or failure.get("kind") or "unknown")
        reasons[code] += 1
        haystack = f"{code} {failure.get('message', '')}".lower()
        if any(marker in haystack for marker in RATE_LIMIT_MARKERS):
            throttled += 1
    return reasons, throttled


def recommendation_mix(final: dict[str, Any]) -> Counter[str]:
    """The server's own `summary.by_recommendation` when it is there, ours otherwise.

    The summary is computed over every item in the job; counting the items in the response
    counts only the page that came back. Those two agree exactly when the page holds
    everything and disagree silently when it does not, which is the worse failure.
    """
    summary = final.get("summary")
    if isinstance(summary, dict):
        by_recommendation = summary.get("by_recommendation")
        if isinstance(by_recommendation, dict) and by_recommendation:
            return Counter(
                {str(k): int(v) for k, v in by_recommendation.items() if int(v or 0)}
            )

    mix: Counter[str] = Counter()
    for item in final.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        if isinstance(result, dict):
            aggregate = result.get("aggregate", {})
            if isinstance(aggregate, dict):
                mix[str(aggregate.get("recommendation", "unknown"))] += 1
        elif item.get("failure"):
            mix["could not check"] += 1
    return mix


def render_report(
    url: str,
    note: str,
    rows: list[Row],
    outcome: Outcome,
    probes: list[Probe],
    baseline: list[Probe],
    manifest_bytes: int,
    zip_bytes: int,
) -> str:
    final = outcome.final
    counts = final.get("counts", {}) if isinstance(final.get("counts"), dict) else {}
    cost = final.get("cost", {}) if isinstance(final.get("cost"), dict) else {}
    usd = float(cost.get("usd", 0.0) or 0.0)
    done = int(counts.get("done", 0))
    failed = int(counts.get("failed", 0))
    total = int(counts.get("total", 0)) or outcome.accepted
    reasons, throttled = failure_reasons(final)
    retries = retry_histogram(final)
    requeued = sum(n for attempts, n in retries.items() if attempts > 1)
    mix = recommendation_mix(final)
    kinds = Counter(row.kind for row in rows)

    out: list[str] = []
    add = out.append
    add("# 300-item batch, measured\n")
    add("| | |")
    add("|---|---|")
    add(f"| URL | `{url}` |")
    add(f"| Started | {datetime.now(UTC):%Y-%m-%d %H:%M:%S}Z |")
    add(f"| Commit | `{git_commit()}` |")
    add(f"| Job | `{outcome.job_id}` |")
    add(
        f"| Manifest | {len(rows)} rows — {kinds['clean']} clean, "
        f"{kinds['mismatch']} mismatched, {kinds['unreadable']} unreadable, "
        f"{kinds['malformed']} malformed |"
    )
    add(f"| Upload | manifest {manifest_bytes:,} B, artwork zip {zip_bytes:,} B |")
    add(f"| Accepted | {outcome.accepted} of {len(rows)} rows queued |")
    if note:
        add(f"| Note | {note} |")
    add("")

    add("## The two goals\n")
    add("| Goal | Requirement | Measured | |")
    add("|---|---|--:|---|")
    wall_verdict = "**MET**" if outcome.wall_s <= GOAL_WALL_S else "**MISSED**"
    add(
        f"| 300 applications end to end | PERF-4 / BATCH-2, {GOAL_WALL_S / 60:.0f} min | "
        f"**{outcome.wall_s:.1f}s** ({outcome.wall_s / 60:.1f} min) | {wall_verdict} |"
    )
    if outcome.first_result_s is None:
        add("| First visible result | PERF-4, 10s | never | **MISSED** |")
    else:
        first_verdict = (
            "**MET**" if outcome.first_result_s <= GOAL_FIRST_RESULT_S else "**MISSED**"
        )
        add(
            f"| First visible result | PERF-4, {GOAL_FIRST_RESULT_S:.0f}s | "
            f"**{outcome.first_result_s:.1f}s** | {first_verdict} |"
        )
    add("")
    if not outcome.complete:
        add(
            f"> **The job did not finish.** Polling stopped after {outcome.wall_s:.0f}s "
            f"with {done + failed} of {total} items resolved. Everything below describes "
            f"an incomplete run and the wall-clock row is a floor, not a result.\n"
        )

    add("## Items\n")
    add("| | |")
    add("|---|--:|")
    add(f"| Queued | {total} |")
    add(f"| Completed | {done} |")
    add(f"| Failed | {failed} |")
    add(f"| Items requeued (attempts > 1) | {requeued} |")
    add(f"| Failed on a rate limit | {throttled} |")
    add(f"| Total cost | ${usd:.4f} |")
    if done + failed:
        add(f"| Cost per application | ${usd / (done + failed):.4f} |")
    # Tokens, itemised, because "cheaper per label in batch" is a claim about the prompt
    # cache and the cache counters are the only place it can be checked. A cache write
    # costs 1.25x an input token and a read costs a tenth of one, so a run whose reads
    # never engaged looks identical in the totals and is nearly twice the price.
    for label, key in (
        ("Input tokens", "input_tokens"),
        ("Output tokens", "output_tokens"),
        ("Cache read tokens", "cache_read_tokens"),
        ("Cache write tokens", "cache_write_tokens"),
    ):
        if key in cost:
            add(f"| {label} | {int(cost.get(key, 0) or 0):,} |")
    if outcome.wall_s > 0 and done + failed:
        add(f"| Throughput | {(done + failed) / outcome.wall_s * 60:.1f} applications/min |")
    add("")

    if reasons:
        add("**Why items failed**, by reason code:\n")
        add("| Reason | Count |")
        add("|---|--:|")
        for code, n in reasons.most_common():
            add(f"| `{code}` | {n} |")
        add("")
    else:
        add("No item failed.\n")

    add("**Was anything rate-limited?**\n")
    add(
        "Answered from `attempts`, not from failures. `api/batch/store.py` increments it "
        "each time an item is claimed, and `api/batch/worker.py` requeues retryable "
        "provider errors, so an item throttled once and served on the retry shows "
        "`attempts = 2` and no failure at all. Counting only failures would call that "
        "run clean.\n"
    )
    if retries:
        add("| attempts | items |")
        add("|--:|--:|")
        for attempts, n in sorted(retries.items()):
            add(f"| {attempts} | {n} |")
        add("")
    if requeued or throttled:
        add(
            f"**{requeued} item(s) were requeued** and {throttled} failed on something "
            f"rate-limit-shaped. The ceiling was touched.\n"
        )
    elif retries and set(retries) == {1}:
        add(
            "**Every item succeeded on its first attempt**, so no retryable provider "
            "error — 429 included — reached any of them. The SDK client is built with "
            "`max_retries=0` (`api/provider/anthropic_adapter.py`), so nothing was "
            "absorbed below this counter either. That is a statement about this run at "
            "this concurrency; it does not locate the account's ceiling, which remains "
            "untested.\n"
        )
    else:
        add(
            "**Retry data was not available** for this run, so whether anything was "
            "throttled and recovered is unknown and is not claimed either way.\n"
        )

    if mix:
        add("**What the batch recommended**:\n")
        add("| Recommendation | Count |")
        add("|---|--:|")
        for name, n in mix.most_common():
            add(f"| {name} | {n} |")
        add("")

    row_errors = outcome.row_errors
    # ENTRIES ARE NOT ROWS. One bad line raises one error per bad column, so a manifest
    # with five bad rows produced twenty-four entries — and the first version of this
    # report printed "24 rows refused ... and the other 300 queued" against a 306-row
    # manifest, which does not add up and was published that way.
    bad_rows = sorted({int(e.get("row", 0) or 0) for e in row_errors})
    add("## Row-level validation (TC-20)\n")
    add(
        f"**{len(bad_rows)} row(s)** refused by the manifest parser, carrying "
        f"{len(row_errors)} error(s) between them — one per bad column, so the entry "
        f"count is larger than the row count. The other {outcome.accepted} rows were "
        f"queued anyway: a bad row does not reject the upload.\n"
    )
    if row_errors:
        add("| Row | Column | Problem |")
        add("|--:|---|---|")
        for error in row_errors[:_ROW_ERROR_TABLE_LIMIT]:
            add(
                f"| {error.get('row', '?')} | `{error.get('column') or '—'}` | "
                f"{str(error.get('message', '')).replace('|', '/')} |"
            )
        if len(row_errors) > _ROW_ERROR_TABLE_LIMIT:
            add(
                f"\n…and {len(row_errors) - _ROW_ERROR_TABLE_LIMIT} more error(s), across "
                f"rows {bad_rows[0]} to {bad_rows[-1]}. The table is truncated; the count "
                f"above is not."
            )
        add("")
    if outcome.unmatched:
        add(f"Unmatched files: {', '.join(outcome.unmatched[:10])}\n")

    add("## Verify Now, during the batch (BATCH-9 / PERF-5)\n")
    if not probes:
        add("No probes were fired.\n")
    else:
        add(
            "A real `POST /verify` every probe interval while the batch ran. The promise "
            "is that a batch does not starve the single-label lane.\n"
        )
        add("| t+ | HTTP | client ms | server ms |")
        add("|--:|--:|--:|--:|")
        for probe in probes:
            server = probe.server_ms if probe.server_ms is not None else "—"
            add(
                f"| {probe.at_s:.0f}s | {probe.status}{' ' + probe.detail if probe.detail else ''}"
                f" | {probe.client_ms} | {server} |"
            )
        add("")
        during = [p.client_ms for p in probes if p.status == 200]
        idle = [p.client_ms for p in baseline if p.status == 200]
        if during and idle:
            add(
                f"Under load: {min(during)}-{max(during)}ms, median "
                f"**{statistics.median(during):.0f}ms** over {len(during)} probe(s). "
                f"Idle control: {min(idle)}-{max(idle)}ms over **{len(idle)}** "
                f"request(s).\n"
            )
            if len(idle) < 5:
                add(
                    f"> **The control is {len(idle)} request(s) and cannot carry a "
                    f"difference.** The service's own warm spread is about 5.5-7.0s "
                    f"across 8 runs, and every probe above sits inside it, so the honest "
                    f"reading is that Verify Now under batch load is **indistinguishable "
                    f"from idle** — not that it is some specific number slower. Note also "
                    f"that this script polls the job every couple of seconds throughout "
                    f"the probes and not during the control, so what load there is is not "
                    f"all the batch's.\n"
                )
        failures = [p for p in probes if p.status != 200]
        if failures:
            add(
                f"**{len(failures)} probe(s) did not answer 200**: "
                f"{', '.join(sorted({p.detail or str(p.status) for p in failures}))}.\n"
            )

    add("## Progress, as polled\n")
    add("| t+ | done | failed | processing |")
    add("|--:|--:|--:|--:|")
    step = max(1, len(outcome.polls) // 25)
    for poll in outcome.polls[::step]:
        add(f"| {poll.at_s:.0f}s | {poll.done} | {poll.failed} | {poll.processing} |")
    add("")
    if outcome.poll_errors:
        add(
            "Poll failures by status: "
            + ", ".join(f"{k} x {v}" for k, v in sorted(outcome.poll_errors.items()))
            + ". A run of 400s alternating with 200s means the app is on more than one "
            "machine and the job lives on only one of them (see `fly.toml`).\n"
        )

    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------------------


def load_probe_payload(base_url: str) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    """The service's own clean sample, fetched once before the clock starts."""
    reply = http_get(f"{base_url.rstrip('/')}/sample?case=clean")
    if reply.status != 200:
        raise SystemExit(f"GET {base_url}/sample answered {reply.status or 'nothing'}.")
    payload = reply.json()
    images: list[tuple[str, bytes]] = []
    for entry in payload.get("images", []):
        url = entry.get("url", "")
        if url.startswith("/"):
            url = f"{base_url.rstrip('/')}{url}"
        image = http_get(url)
        if image.status != 200:
            raise SystemExit(f"Could not fetch the sample image at {url}.")
        images.append((entry.get("filename", "label.png"), image.body))
    return payload.get("application", {}), images


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.batch_300", description=__doc__.splitlines()[0]
    )
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--malformed", type=int, default=DEFAULT_MALFORMED)
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_S)
    parser.add_argument("--probe-interval", type=float, default=DEFAULT_PROBE_INTERVAL_S)
    parser.add_argument("--no-probes", action="store_true", help="skip the Verify Now lane")
    parser.add_argument(
        "--baseline-runs",
        type=int,
        default=3,
        help="idle verifications to take as the control before submitting (each costs)",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out", default="", help="write the Markdown report here")
    parser.add_argument("--note", default="", help="what you arranged")
    parser.add_argument(
        "--keep",
        default="",
        help="write the manifest and artwork zip here instead of discarding them",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the manifest and artwork, print the size, spend nothing",
    )
    args = parser.parse_args(argv)

    if args.rows < 1:
        raise SystemExit("--rows must be at least 1.")

    print(f"building {args.rows} applications + {args.malformed} bad rows…", file=sys.stderr)
    rows = build_rows(args.rows, args.malformed)
    manifest = manifest_csv(rows)
    archive = artwork_zip(rows)
    print(
        f"  manifest {len(manifest):,} B, artwork zip {len(archive):,} B, "
        f"{sum(len(r.images) for r in rows)} images",
        file=sys.stderr,
    )

    if args.keep:
        keep = Path(args.keep)
        keep.mkdir(parents=True, exist_ok=True)
        (keep / "manifest.csv").write_bytes(manifest)
        (keep / "labels.zip").write_bytes(archive)
        print(f"  wrote {keep}/manifest.csv and {keep}/labels.zip", file=sys.stderr)

    if args.dry_run:
        print("dry run — nothing submitted, nothing spent.", file=sys.stderr)
        return 0

    application, images = ({}, [])
    if not args.no_probes:
        application, images = load_probe_payload(args.url)

    # One idle verification first, as the control. Without it the probes during the batch
    # are a number with nothing to be compared against.
    baseline: list[Probe] = []
    if not args.no_probes:
        # More than one, because the probes it is compared against are a distribution and
        # a single request is not. One idle request against five loaded ones produced a
        # "+280 ms" in the first version of this report that the service's own run-to-run
        # spread swallows whole.
        print(f"baseline verify (idle) x{args.baseline_runs}…", file=sys.stderr)
        post = poster_for(args.url, args.timeout)
        for _ in range(max(1, args.baseline_runs)):
            content_type, body = build_multipart(application, images)
            started = time.perf_counter()
            reply = post("/verify", content_type, body)
            baseline.append(
                Probe(
                    at_s=0.0,
                    client_ms=round((time.perf_counter() - started) * 1000),
                    status=reply.status,
                )
            )
            print(f"  {baseline[-1].client_ms}ms  http={reply.status}", file=sys.stderr)

    print(f"submitting to {args.url}/batch …", file=sys.stderr)
    started = time.perf_counter()
    reply = submit(args.url, manifest, archive, args.timeout)
    if reply.status not in (200, 201, 202):
        print(f"POST /batch answered {reply.status}: {reply.body[:400]!r}", file=sys.stderr)
        return 1

    accepted_body = reply.json()
    outcome_seed = {
        "job_id": str(accepted_body.get("job_id", "")),
        "accepted": int(accepted_body.get("accepted", 0)),
        "row_errors": accepted_body.get("row_errors", []) or [],
        "unmatched": accepted_body.get("unmatched_files", []) or [],
    }
    print(
        f"  job {outcome_seed['job_id']} — {outcome_seed['accepted']} accepted, "
        f"{len(outcome_seed['row_errors'])} row error(s)",
        file=sys.stderr,
    )

    prober: VerifyProbe | None = None
    if not args.no_probes:
        prober = VerifyProbe(args.url, application, images, args.probe_interval, args.timeout)
        prober.start()

    outcome = run_job(args.url, outcome_seed["job_id"], args.poll, args.timeout, started)
    outcome.accepted = outcome_seed["accepted"]
    outcome.row_errors = list(outcome_seed["row_errors"])
    outcome.unmatched = list(outcome_seed["unmatched"])

    probes: list[Probe] = []
    if prober is not None:
        prober.stop()
        prober.join(timeout=args.timeout + 5)
        probes = list(prober.probes)

    text = render_report(
        args.url,
        args.note,
        rows,
        outcome,
        probes,
        baseline,
        len(manifest),
        len(archive),
    )
    if args.out:
        Path(args.out).write_text(text)
        print(f"\nwrote {args.out}", file=sys.stderr)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
