"""Model-tier sweep — the instrument that decides which model ships (LP-329).

The ship rule this module applies: *the cheapest tier that clears ≥95% field accuracy with
zero false passes on warning rows is what ships*. Whichever tier the code currently
defaults to is a starting point, not a finding; this table is the finding.

**Correctness disqualifies. Speed does not.** A model is DISQUALIFIED for a warning false
pass, for accuracy below the floor, or for a crashed fixture — and for nothing else. p95 is
measured and printed, and a model over the 5s budget is flagged `LATENCY RISK`, but latency
never disqualifies and never rescues. The reason matters: a warning false pass is a
compliance failure, and no p95 offsets one. A fast model that reads the warning wrong is
disqualified *by this report*, not excused by it.

The inverse is also deliberate. The p95 here is extraction plus rules, measured from a
script — it is not the upload-to-verdict number PERF-1 gates on, and presenting it as one
would silently redefine the adoption gate against a partial measurement. `scripts/timed_p95.py`
owns that number.

**Latency is grouped by call shape**, because the adapter issues one concurrent call per
image and the golden set mixes one-image and two-image labels. A blended p95 across both
describes no actual request, and the split-versus-single-call difference is precisely what
the current model decision turns on.

**It costs real money.** Nothing here runs without `--model`, it prints its estimated spend
before spending it, `--dry-run` stops at the estimate, and with no API key it skips rather
than fails — an offline machine has not regressed.

    python -m eval.run --model claude-opus-5 --model claude-haiku-4-5
    python -m eval.run --model claude-haiku-4-5 --dry-run
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from api.models import Application
from api.provider.base import ExtractionProvider, ImageInput
from api.verify import verify
from eval.outcomes import ACCURACY_FLOOR, Report, expected_verdicts, outcome_for
from eval.pricing import DEFAULT_SWEEP, estimate_usd, price_for
from eval.report import ascii_safe
from fixtures.generator.catalog import warning_defects
from fixtures.generator.spec import LabelSpec

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "fixtures" / "labels"

#: The PRD's adoption gate, shown as context on the latency column. Not a disqualifier
#: here: this measurement is extraction plus rules, not upload-to-verdict (PERF-1).
P95_BUDGET_S = 5.0

#: Warning postures a model must be shown to read correctly before it can be recommended.
#:
#: This exists because the sweep was, in a reviewer's simulation, a coin flip on the
#: decision it exists to make. The golden set had ONE fixture exercising body-bold and
#: ZERO exercising header-bold, each run once per model. Against measured Haiku error
#: rates (30% body-bold, 20% header-bold, always in the false-pass direction) the sweep
#: printed "SHIPS: claude-haiku-4-5 -- 100.0% accuracy, 0 warn FP" in 277 of 400
#: simulated runs. The disqualification rule was correct; the evidence behind it was one
#: sample and zero samples.
#:
#: A model cannot be recommended on a posture that was never shown to it.
#: Posture name -> the defect set a fixture must render to be evidence FOR that posture.
#:
#: Exact sets, not predicates, because a fixture carrying two defects is not evidence for
#: either. A label that is both body-bold and title-case comes back non-passing if the
#: model catches *either* one, so it cannot show that body-bold specifically was read. Only
#: a fixture that isolates a posture can testify about it.
WARNING_POSTURES: dict[str, frozenset[str]] = {
    "header_not_all_caps": frozenset({"header_not_all_caps"}),
    "header_not_bold": frozenset({"header_not_bold"}),
    "body_bold": frozenset({"body_bold"}),
    "text_altered": frozenset({"text_altered"}),
    "prominence": frozenset({"prominence"}),
    "warning_absent": frozenset({"absent"}),
}

#: Largest chance we accept of this sweep blessing a model that misreads a posture.
MAX_FALSE_BLESSING_RISK = 0.05

#: Worst per-image misread rate to design against, from the measured Haiku 4.5 figures
#: (30% body-bold, 20% header-bold) blended to the ~44% worst case used in the round-2
#: re-simulation. Always in the false-pass direction, never abstaining.
ASSUMED_MISREAD_RATE = 0.44

def _required_fixtures() -> int:
    """How many distinct renderings a posture needs before a blessing means anything.

    Derived, not chosen. A model that misreads a posture at `ASSUMED_MISREAD_RATE` slips
    through n independent renderings with probability `(1 - rate) ** n`; solve for that
    falling under `MAX_FALSE_BLESSING_RISK`. At the measured 44% that is six.

    The previous value of two was below the honesty bar this module prints: it marked a
    posture "ok" next to "an error rate up to 78% would go unseen", and a re-simulation
    put a two-fixture set at ~30% of runs still blessing Haiku under the realistic
    deterministic-misread model. Two distinguishes "reads this posture" from "reads this
    picture"; it does not support a recommendation.
    """
    return math.ceil(
        math.log(MAX_FALSE_BLESSING_RISK) / math.log(1.0 - ASSUMED_MISREAD_RATE)
    )


#: DISTINCT renderings per posture below which the sweep declines to name a winner.
MIN_FIXTURES_PER_POSTURE = _required_fixtures()

#: Runs per label. Widens the confidence claim WITHIN a fixture — it catches a model that
#: is right on Tuesday and wrong on Wednesday — and never substitutes for a second fixture.
MIN_RUNS_PER_FIXTURE = 3

#: Builds the extractor for one model. Injected so the table, the disqualification rule
#: and the recommendation are all testable with no network and no spend.
ProviderForModel = Callable[[str], ExtractionProvider]


def image_inputs(spec: LabelSpec) -> list[ImageInput]:
    """The rendered fixture images, as the pipeline would receive them."""
    roles = ["front", "back"] if spec.face != "single" else ["single"]
    inputs: list[ImageInput] = []
    for index, role in enumerate(roles):
        name = f"{spec.name}.png" if role == "single" else f"{spec.name}_{role}.png"
        path = LABELS / name
        if not path.is_file():
            raise FileNotFoundError(
                f"{path} is missing. Run `python -m fixtures.generator.build` first."
            )
        inputs.append(
            ImageInput(index=index, data=path.read_bytes(), media_type="image/png", role=role)
        )
    return inputs


@dataclass(frozen=True)
class LabelRun:
    """One label through one model: what it cost in time and tokens."""

    fixture: str
    images: int
    seconds: float
    input_tokens: int
    output_tokens: int
    usd: float


def warning_fingerprint(spec: LabelSpec) -> tuple[object, ...]:
    """What this spec actually DRAWS in the warning region.

    Distinctness was by fixture NAME, which meant two specs differing only in `name`
    rendered byte-identical PNGs and counted as two — and two differing only in
    `brand_name` were two files but one warning rendering, which is the thing the posture
    is about. This keys on the pixels that matter instead.

    When the warning is absent there is no region to key on, so the whole label is the
    fingerprint: noticing a missing warning depends on the rest of the label, not on a
    blank space.
    """
    if not spec.include_warning:
        return (
            "absent",
            spec.brand_name,
            spec.class_type,
            spec.producer,
            spec.face,
            spec.width,
            spec.height,
            spec.background,
        )
    return (
        spec.rendered_warning(),
        spec.warning_header_bold,
        spec.warning_body_bold,
        round(spec.warning_scale, 4),
        round(spec.warning_contrast, 4),
        spec.width,
        spec.background,
    )


def posture_coverage(specs: Sequence[LabelSpec]) -> dict[str, int]:
    """DISTINCT renderings that ISOLATE each warning posture.

    Two restrictions, each closing a way to satisfy this cheaply. Distinct renderings, not
    fixture names, because re-labelling one image is not a second sample. And only fixtures
    whose defect set is exactly the posture count, because a label carrying two defects
    comes back non-passing if the model catches either one and therefore says nothing about
    which.

    Deliberately takes no `repeat`: re-sending the same PNG produces no new evidence about
    the posture, only about run-to-run stability of one rendering.
    """
    return {
        posture: len(
            {
                warning_fingerprint(spec)
                for spec in specs
                if warning_defects(spec) == defects
            }
        )
        for posture, defects in WARNING_POSTURES.items()
    }


def false_blessing_risk(fixtures: int) -> float:
    """Chance a model misreading this posture at the assumed rate still comes back clean."""
    return (1.0 - ASSUMED_MISREAD_RATE) ** max(fixtures, 0)


def undetectable_error_rate(fixtures: int, confidence: float = 0.95) -> float:
    """The largest per-posture error rate that would still likely produce a clean sweep.

    Computed from DISTINCT FIXTURES, never from repeats. The arithmetic
    (`1 - (1 - confidence) ** (1 / n)`) assumes independent draws, and repeats of one image
    are the opposite of independent — a model that misreads a rendering misreads it every
    time. Feeding `repeat` in here is what let 100 re-sends of one PNG report a 3% blind
    spot. At one fixture the honest figure is 95%: a single-shot sweep proves almost
    nothing about a model's warning reading.
    """
    if fixtures <= 0:
        return 1.0
    return float(1.0 - (1.0 - confidence) ** (1.0 / fixtures))


def evidence_problems(specs: Sequence[LabelSpec], repeat: int = 1) -> list[str]:
    """Why this set cannot support a ship recommendation, if it cannot."""
    problems: list[str] = []
    for posture, fixtures in sorted(posture_coverage(specs).items()):
        if fixtures == 0:
            problems.append(
                f"{posture}: NO fixture exercises it — the model is never tested on it"
            )
        elif fixtures < MIN_FIXTURES_PER_POSTURE:
            problems.append(
                f"{posture}: {fixtures} distinct rendering(s), need "
                f"{MIN_FIXTURES_PER_POSTURE}. A model misreading this posture "
                f"{ASSUMED_MISREAD_RATE:.0%} of the time is blessed "
                f"{false_blessing_risk(fixtures):.0%} of the time, and repeats cannot "
                f"close that — they re-read the same rendering"
            )
    if repeat < MIN_RUNS_PER_FIXTURE:
        problems.append(
            f"--repeat {repeat}: fewer than {MIN_RUNS_PER_FIXTURE} runs per label, so a "
            f"model that reads a label correctly only sometimes looks reliable"
        )
    return problems


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile.

    Nearest-rank rather than interpolated because n is 15, not 15,000: interpolating
    between two of fifteen samples invents precision the sample size does not support.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-fraction * len(ordered) // 1))))
    return ordered[rank - 1]


@dataclass
class ModelResult:
    """One model's row in the table."""

    model: str
    report: Report
    runs: list[LabelRun] = field(default_factory=list)
    priced: bool = True
    repeat: int = 1
    """What the caller asked for. `observed_repeat` is what actually happened."""

    @property
    def observed_repeat(self) -> int:
        """Runs per distinct label, derived from the runs rather than self-reported.

        The declared `repeat` was taken on trust by both `recommend` and `render` — the
        same shape as the bug where re-sends counted as samples, one field over. Reachable
        only by a caller building results by hand, which is exactly what the tests do.
        """
        if not self.runs:
            return 0
        return len(self.runs) // len({run.fixture for run in self.runs})

    # --- latency ----------------------------------------------------------------------

    def latencies(self, images: int | None = None) -> list[float]:
        return [r.seconds for r in self.runs if images is None or r.images == images]

    @property
    def p50(self) -> float:
        return percentile(self.latencies(), 0.50)

    @property
    def p95(self) -> float:
        return percentile(self.latencies(), 0.95)

    @property
    def call_shapes(self) -> list[int]:
        return sorted({r.images for r in self.runs})

    @property
    def latency_risk(self) -> bool:
        return bool(self.runs) and self.p95 > P95_BUDGET_S

    # --- cost -------------------------------------------------------------------------

    @property
    def usd_per_label(self) -> float:
        return sum(r.usd for r in self.runs) / len(self.runs) if self.runs else 0.0

    # --- the ship rule -------------------------------------------------------------------

    @property
    def disqualifiers(self) -> list[str]:
        """Correctness only. Latency is reported, never fatal — see the module docstring."""
        reasons: list[str] = []
        if self.report.false_passes:
            reasons.append(
                f"{len(self.report.false_passes)} false pass(es) on warning violations"
            )
        if not self.report.warning_violations:
            reasons.append("no warning-violation rows scored; the gate proved nothing")
        if not self.report.accuracy_ok:
            reasons.append(
                f"accuracy {self.report.accuracy:.1%} below the {ACCURACY_FLOOR:.0%} floor"
            )
        if self.report.errors:
            reasons.append(f"{len(self.report.errors)} fixture(s) crashed")
        return reasons

    @property
    def qualified(self) -> bool:
        return not self.disqualifiers


def run_model(
    model: str,
    specs: Sequence[LabelSpec],
    provider: ExtractionProvider,
    *,
    repeat: int = 1,
    clock: Callable[[], float] = time.perf_counter,
    load_images: Callable[[LabelSpec], list[ImageInput]] = image_inputs,
) -> ModelResult:
    """Run the golden set through one model, timing and pricing each label.

    `repeat` runs every label that many times. A model's warning reading is stochastic —
    the same label can come back right once and wrong the next — so one pass per label
    cannot distinguish a reliable model from a lucky one. See `MIN_RUNS_PER_FIXTURE`.
    """
    price = price_for(model)
    report = Report(tier="A", fixtures=len(specs) * repeat, provider=model)
    result = ModelResult(model=model, report=report, priced=price is not None, repeat=repeat)

    for spec in [s for s in specs for _ in range(repeat)]:
        started = clock()
        try:
            images = load_images(spec)
            application = Application.model_validate(spec.application())
            verified = verify(application, images, provider)
        # Broad on purpose: one model failing a label must not abort the other models.
        except Exception as exc:
            report.errors.append((spec.name, f"{type(exc).__name__}: {exc}"))
            continue
        elapsed = clock() - started

        expected = expected_verdicts(spec)
        for field_result in verified.fields:
            report.outcomes.append(
                outcome_for(
                    spec,
                    field_result.field,
                    field_result.verdict,
                    {f.code for f in field_result.findings},
                    expected,
                )
            )

        result.runs.append(
            LabelRun(
                fixture=spec.name,
                images=len(images),
                seconds=elapsed,
                input_tokens=verified.cost.input_tokens,
                output_tokens=verified.cost.output_tokens,
                usd=(
                    price.usd(verified.cost.input_tokens, verified.cost.output_tokens)
                    if price
                    else 0.0
                ),
            )
        )

    return result


def run(
    models: Sequence[str],
    specs: Sequence[LabelSpec],
    provider_for_model: ProviderForModel,
    *,
    repeat: int = 1,
    clock: Callable[[], float] = time.perf_counter,
    load_images: Callable[[LabelSpec], list[ImageInput]] = image_inputs,
) -> list[ModelResult]:
    return [
        run_model(
            model,
            specs,
            provider_for_model(model),
            repeat=repeat,
            clock=clock,
            load_images=load_images,
        )
        for model in models
    ]


def recommend(
    results: Sequence[ModelResult], specs: Sequence[LabelSpec]
) -> ModelResult | None:
    """The cheapest model that clears the correctness gates on sufficient evidence.

    `specs` is REQUIRED. It was briefly optional "so existing callers keep working", which
    meant any caller that forgot it silently skipped the evidence gate and got the
    pre-fix behaviour back — including one of this project's own tests.
    """
    repeat = min((r.observed_repeat for r in results), default=0)
    if evidence_problems(specs, repeat):
        return None
    qualified = [r for r in results if r.qualified and r.priced]
    if not qualified:
        return None
    return min(qualified, key=lambda r: (r.usd_per_label, r.p95))


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------

RULE = "=" * 78


def estimate_lines(
    models: Sequence[str], specs: Sequence[LabelSpec], repeat: int = 1
) -> list[str]:
    """What this sweep is about to spend, printed before it spends it."""
    images = sum(2 if s.face != "single" else 1 for s in specs) * repeat
    out = [
        "",
        f"Planned: {len(specs)} label(s) x {repeat} run(s), {images} image(s) per model, "
        f"{len(models)} model(s) = {images * len(models)} live call(s).",
    ]
    total = 0.0
    unpriced: list[str] = []
    for model in models:
        per_image = estimate_usd(model, 1)
        if per_image is None:
            unpriced.append(model)
            continue
        # Per-image rather than per-label: a two-image label is two calls, each carrying
        # the system prompt, which is what the adapter actually sends.
        total += per_image * images
    out.append(f"Estimated spend: ~${total:.2f} at list price (upper bound — cache reads")
    out.append("  are billed at a tenth of input and are not credited here).")
    if unpriced:
        out.append(f"  No list price on file for: {', '.join(unpriced)} — excluded above.")
    return out


def render(results: Sequence[ModelResult], specs: Sequence[LabelSpec]) -> str:
    """The table the model decision is made from."""
    lines = [
        "",
        RULE,
        f"MODEL-TIER SWEEP — {len(specs)} label(s), live models (LP-329)",
        RULE,
        "Ship rule: the CHEAPEST tier clearing >=95% accuracy with ZERO",
        "false passes on warning rows ships. Correctness disqualifies; speed does not.",
        "A model that is fast and reads the warning wrong is disqualified here, not excused.",
        "",
        f"{'model':20s}{'accuracy':>10s}{'warn FP':>12s}{'p50':>8s}{'p95':>8s}"
        f"{'$/label':>10s}   verdict",
    ]

    for result in results:
        report = result.report
        cost = f"${result.usd_per_label:.4f}" if result.priced else "no price"
        verdict = "QUALIFIED" if result.qualified else "DISQUALIFIED"
        if result.qualified and result.latency_risk:
            verdict = "QUALIFIED (LATENCY RISK)"
        # The count carries its denominator here for the same reason it does in the Tier A
        # report: `0` alone reads identically whether it was 0-of-4 or 0-of-1, and this is
        # the table that decides which model ships.
        false_passes = f"{len(report.false_passes)}/{len(report.warning_violations)}"
        lines.append(
            f"{result.model:20s}{report.accuracy:>9.1%}{false_passes:>12s}"
            f"{result.p50:>7.1f}s{result.p95:>7.1f}s{cost:>10s}   {verdict}"
        )
        for reason in result.disqualifiers:
            lines.append(f"{'':20s}  -> {reason}")

    lines.append("")
    lines.append("'warn FP' is false passes / warning-violation rows checked.")

    lines += evidence_section(specs, results)
    lines += latency_by_shape(results)

    lines += [
        "",
        f"p95 shown against the {P95_BUDGET_S:.0f}s adoption gate as CONTEXT only. This is",
        "extraction plus rules measured from a script, not upload-to-verdict — PERF-1's",
        "number comes from scripts/timed_p95.py against the deployed URL.",
    ]

    repeat = min((r.observed_repeat for r in results), default=0)
    problems = evidence_problems(specs, repeat)
    winner = recommend(results, specs)
    lines += ["", RULE]
    if problems:
        lines += [
            "NO RECOMMENDATION — THE EVIDENCE DOES NOT SUPPORT ONE.",
            "",
            "Every model above may show a clean warning column and still misread the",
            "warning in production, because these postures were barely tested or not",
            "tested at all. Fix the set, not the threshold:",
        ]
        lines += [f"  - {p}" for p in problems]
        lines += [
            "",
            "Add fixtures for the uncovered postures and raise --repeat, then re-run.",
        ]
    elif winner is None:
        lines += [
            "NO MODEL QUALIFIES.",
            "Every tier measured fails a correctness gate. Do not ship on latency or cost;",
            "fix the extraction or raise the tier and re-run.",
        ]
    else:
        lines.append(
            f"SHIPS: {winner.model} — cheapest tier clearing both gates at "
            f"${winner.usd_per_label:.4f}/label, p95 {winner.p95:.1f}s."
        )
        cheaper = [
            r for r in results
            if r.priced and r.usd_per_label < winner.usd_per_label and not r.qualified
        ]
        for r in cheaper:
            lines.append(
                f"  {r.model} is cheaper at ${r.usd_per_label:.4f}/label and is "
                f"disqualified: {'; '.join(r.disqualifiers)}."
            )
        if winner.latency_risk:
            lines.append(
                f"  Note: p95 {winner.p95:.1f}s is over the {P95_BUDGET_S:.0f}s budget. "
                f"That is a product decision, not a gate — see PERF-1."
            )
    lines.append(RULE)
    return ascii_safe("\n".join(lines))


def evidence_section(
    specs: Sequence[LabelSpec], results: Sequence[ModelResult]
) -> list[str]:
    """How many DISTINCT labels of each warning posture each model was shown.

    Both numbers are printed — fixtures and runs — because they answer different
    questions and only the first one bounds the blind spot. A reader given a single
    'samples' figure cannot tell two labels from one label sent twice.
    """
    repeat = min((r.observed_repeat for r in results), default=0)
    coverage = posture_coverage(specs)
    out = [
        "",
        "Warning-posture evidence. Distinct renderings bound what this sweep can claim;",
        "runs only widen the claim within a rendering. Repeats are not extra evidence:",
    ]
    for posture, fixtures in sorted(coverage.items()):
        risk = false_blessing_risk(fixtures)
        # "ok" is earned by clearing the risk tolerance, not by clearing a count. It used
        # to sit next to "an error rate up to 78% would go unseen".
        mark = "ok " if risk <= MAX_FALSE_BLESSING_RISK else "!! "
        out.append(
            f"  {mark}{posture:22s}{fixtures:2d} rendering(s) x {repeat} run(s)   "
            f"a {ASSUMED_MISREAD_RATE:.0%} misreader passes {risk:5.1%} of the time"
        )
    out.append(
        f"  Risk figures come from the rendering count alone: need "
        f"{MIN_FIXTURES_PER_POSTURE} isolating rendering(s) per posture and "
        f"{MIN_RUNS_PER_FIXTURE} runs each"
    )
    out.append(
        f"  to hold false blessing under {MAX_FALSE_BLESSING_RISK:.0%} against a "
        f"{ASSUMED_MISREAD_RATE:.0%} misreader."
    )
    return out


def latency_by_shape(results: Sequence[ModelResult]) -> list[str]:
    """Latency split by how many concurrent calls the label needed.

    The adapter issues one call per image, concurrently, so a two-image label's wall clock
    is max() and not sum(). Blending the two shapes into one p95 produces a number that
    describes no request anyone actually makes, and the single-versus-split difference is
    what the current model decision turns on.
    """
    shapes = sorted({s for r in results for s in r.call_shapes})
    if len(shapes) < 2:
        return []

    out = ["", "Latency by call shape (the adapter issues one concurrent call per image):"]
    thin = False
    for result in results:
        parts = []
        for shape in shapes:
            samples = result.latencies(shape)
            if not samples:
                continue
            # A p95 from a handful of samples is the maximum wearing a percentile's name.
            # Say so rather than letting it be quoted as one.
            marker = "" if len(samples) >= MIN_RUNS_PER_FIXTURE else " [thin]"
            thin = thin or bool(marker)
            parts.append(
                f"{shape} image(s) n={len(samples)}{marker}: "
                f"p50 {percentile(samples, 0.5):.1f}s p95 {percentile(samples, 0.95):.1f}s"
            )
        out.append(f"  {result.model:20s}{'   '.join(parts)}")
    if thin:
        out.append(
            f"  [thin] fewer than {MIN_RUNS_PER_FIXTURE} samples — that p95 is the "
            f"maximum, not a percentile. Raise --repeat."
        )
    return out


def default_models() -> list[str]:
    return list(DEFAULT_SWEEP)


__all__ = [
    "P95_BUDGET_S",
    "LabelRun",
    "ModelResult",
    "default_models",
    "estimate_lines",
    "image_inputs",
    "percentile",
    "recommend",
    "render",
    "run",
    "run_model",
]
