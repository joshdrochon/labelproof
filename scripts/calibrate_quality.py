"""Threshold calibration for the image-quality gate (LP-200, LP-292, OPS-3).

The values in `api/rules/thresholds.py` are calibrated against *rendered* fixtures. Real
optics differ, and that gap is the most likely place for this pipeline to be wrong. When
the Tier B photographs land, retuning has to be running a script and reading a table —
not adjusting a number until the tests go green, which is how a gate gets quietly widened
until it stops gating.

So this sweeps one threshold at a time across a range and reports, at every level:

* **false passes** — an illegible image or region treated as legible
* **false flags** — a legible one treated as illegible
* how many images the gate rejected outright

**The rule the table exists to enforce: a threshold change that reduces flags by letting
a bad label through is a regression, not an improvement.** Flags and false passes trade
against each other, and reading only the flag column is how that trade gets made without
anyone deciding to make it. Both columns are always printed, and a level that introduces
a false pass is marked whatever it does to the flag count.

    python -m scripts.calibrate_quality                       # sweep everything, Tier A
    python -m scripts.calibrate_quality --threshold HOPELESS
    python -m scripts.calibrate_quality --photos fixtures/photos   # Tier B
    python -m scripts.calibrate_quality --json

Tier A and Tier B are swept separately and never averaged (BUILD.md §5).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.rules import thresholds as T
from scripts import robustness_eval

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Knob:
    """One threshold, the range to sweep it over, and which way is stricter."""

    values: tuple[float, ...]
    stricter: str  # "higher" | "lower"
    effect: str


#: What to sweep, and over what. Ranges bracket the current value on both sides — a sweep
#: that only looked in one direction could not tell you the current value is already too
#: loose, which is the answer it most needs to be able to give.
SWEEPS: dict[str, Knob] = {
    "HOPELESS": Knob(
        (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40),
        stricter="higher",
        effect="raising it pre-gates more images: fewer false passes, more retake requests",
    ),
    "DEGRADED": Knob(
        (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70),
        stricter="higher",
        effect="raising it calls more images degraded, discounting confidence sooner",
    ),
    "SHARP_GRADIENT_VARIANCE": Knob(
        (600, 900, 1200, 1600, 2200, 3000),
        stricter="higher",
        effect="raising it means less is treated as fully sharp, so blur scores drop",
    ),
    "BLUR_HOPELESS_VARIANCE": Knob(
        (30, 60, 90, 110, 140, 180, 240),
        stricter="higher",
        effect="raising it means more images score zero blur and get pre-gated",
    ),
    "EXPOSURE_FLOOR": Knob(
        (50, 70, 90, 110, 130, 160),
        stricter="higher",
        effect="raising it means more photographs count as underexposed",
    ),
    "GLARE_SATURATION_FRACTION": Knob(
        (0.05, 0.10, 0.15, 0.25, 0.35, 0.50),
        stricter="lower",
        effect="lowering it means less blow-out counts as total glare, so glare scores drop",
    ),
    "MIN_LONG_EDGE_PX": Knob(
        (600, 900, 1200, 1568, 2000),
        stricter="higher",
        effect="raising it flags more images as too small for small text to be verifiable",
    ),
}


@contextmanager
def threshold(name: str, value: float) -> Iterator[None]:
    """Set one threshold for the duration of a run, then put it back.

    Module attributes rather than a config object because that is where the constants
    actually live, and a sweep that measured a *copy* of the thresholds would be measuring
    something the pipeline does not use.
    """
    original = getattr(T, name)
    setattr(T, name, value)
    try:
        yield
    finally:
        setattr(T, name, original)


@dataclass
class Level:
    """One threshold value, and what the whole set does at it."""

    value: float
    false_passes: int
    false_flags: int
    gated: int
    conditions_met: int
    total: int

    @property
    def regression(self) -> bool:
        return self.false_passes > 0


@dataclass
class Sweep:
    name: str
    current: float
    tier: str
    knob: Knob
    levels: list[Level] = field(default_factory=list)

    @property
    def safe_levels(self) -> list[Level]:
        """Levels with no false passes. The only ones eligible to be chosen at all."""
        return [level for level in self.levels if not level.regression]

    @property
    def safe_band(self) -> tuple[float, float] | None:
        """The range of values that produce no false passes and no false flags.

        A band rather than a single winner. With a set this size, several levels tie at
        zero and zero, and picking one of them would be dressing up a coin flip as a
        measurement. What is actually useful is where the edges are.
        """
        clean = [
            level.value
            for level in self.levels
            if not level.regression and not level.false_flags
        ]
        return (min(clean), max(clean)) if clean else None

    @property
    def on_grid(self) -> bool:
        """Was the shipped value one of the levels actually measured?

        If someone edits a threshold without widening the sweep, every level below was
        measured and none of them was the value in use. Nothing can be concluded about it,
        and saying so is the only honest option.
        """
        return self.current in [level.value for level in self.levels]

    @property
    def margin(self) -> int:
        """How many sweep steps the current value sits from the nearest false pass.

        This is the number to watch when Tier B lands. A threshold that is technically
        correct but one step from producing a false pass is not calibrated, it is lucky,
        and real optics will spend that luck.

        `-1` means the shipped value was never measured — see `on_grid`. It is deliberately
        not 0 or a large number: either would read as an answer.
        """
        if not self.on_grid:
            return -1
        values = [level.value for level in self.levels]
        here = values.index(self.current)
        regressions = [i for i, level in enumerate(self.levels) if level.regression]
        return min((abs(here - i) for i in regressions), default=len(values))

    @property
    def unproven(self) -> bool:
        """The shipped value has no measurement behind it, one way or the other."""
        return not self.on_grid

    @property
    def verdict(self) -> str:
        if not self.on_grid:
            return (
                f"{self.current:g} was never measured — it is not one of the swept levels. "
                f"Add it to SWEEPS before trusting anything here about it."
            )
        band = self.safe_band
        if band is None:
            return (
                "no level in this range is clean — the measure is wrong, not the threshold"
            )
        if not (band[0] <= self.current <= band[1]):
            return f"current value is outside the clean band {band[0]:g}–{band[1]:g}; move it"  # noqa: RUF001 - en dash spans a numeric range
        if self.margin == 0:
            return "current value produces a false pass — move it now"
        if self.margin == 1:
            return (
                f"clean, but one step from a false pass — {self.knob.stricter} is safer"
            )
        return f"clean, {self.margin} steps of margin"


def measure(tier_report: robustness_eval.Report) -> tuple[int, int, int, int]:
    return (
        len(tier_report.false_passes),
        len(tier_report.false_flags),
        sum(1 for o in tier_report.outcomes if o.pregated),
        tier_report.met,
    )


def sweep_one(name: str, knob: Knob, *, photos: Path | None = None) -> Sweep:
    current = getattr(T, name)
    result = Sweep(
        name=name, current=current, tier="B" if photos else "A", knob=knob
    )

    for value in knob.values:
        with threshold(name, value):
            report = (
                robustness_eval.evaluate_photos(photos)
                if photos
                else robustness_eval.evaluate()
            )
        false_passes, false_flags, gated, met = measure(report)
        result.levels.append(
            Level(
                value=value,
                false_passes=false_passes,
                false_flags=false_flags,
                gated=gated,
                conditions_met=met,
                total=len(report.outcomes),
            )
        )
    return result


def sweep_all(
    names: list[str] | None = None, *, photos: Path | None = None
) -> list[Sweep]:
    wanted = names or list(SWEEPS)
    return [sweep_one(name, SWEEPS[name], photos=photos) for name in wanted]


def regressions(sweeps: list[Sweep]) -> list[Sweep]:
    """Sweeps whose *current* value already produces a false pass."""
    return [s for s in sweeps if s.margin == 0]


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


def render_sweep(sweep: Sweep) -> str:
    lines = [
        f"{sweep.name}   (current: {sweep.current:g})",
        f"  {sweep.knob.effect}",
        "-" * 78,
        f"{'value':>10s} {'false passes':>13s} {'false flags':>12s} "
        f"{'gated':>6s} {'met':>8s}",
    ]
    for level in sweep.levels:
        marker = "  <- FALSE PASS" if level.regression else ""
        current = "  (current)" if level.value == sweep.current else ""
        lines.append(
            f"{level.value:10g} {level.false_passes:13d} {level.false_flags:12d} "
            f"{level.gated:6d} {level.conditions_met:4d}/{level.total:<3d}"
            f"{marker}{current}"
        )

    lines.append("")
    band = sweep.safe_band
    if band is not None:
        lines.append(f"  Clean band: {band[0]:g} to {band[1]:g}")
    lines.append(f"  {sweep.verdict}")
    return "\n".join(lines)


def render(sweeps: list[Sweep]) -> str:
    tier = sweeps[0].tier if sweeps else "A"
    out = [
        f"Image-quality threshold calibration — Tier {tier}",
        "=" * 78,
        "",
        "A threshold change that reduces flags by letting a bad label through is a",
        "regression, not an improvement. Read both columns.",
        "",
    ]
    for sweep in sweeps:
        out.append(render_sweep(sweep))
        out.append("")

    broken = regressions(sweeps)
    unproven = [s for s in sweeps if s.unproven]
    if broken:
        out.append(
            "CURRENT VALUES PRODUCING FALSE PASSES: "
            + ", ".join(s.name for s in broken)
        )
    elif unproven:
        # Never claim a clean bill for values that were not among the levels measured.
        out.append(
            "No *measured* threshold produces a false pass — but "
            + ", ".join(s.name for s in unproven)
            + " were never swept at their shipped value, so nothing here covers them."
        )
    else:
        out.append("No current threshold produces a false pass on this set.")
    thin = [s for s in sweeps if s.margin == 1]
    if thin:
        out.append(
            "One step from a false pass (calibrated by luck, not by measurement): "
            + ", ".join(s.name for s in thin)
        )
    out.append("")

    if tier == "B":
        out.append(
            "Tier B is never averaged with Tier A. Real optics are the honest number; "
            "the generated set only proves the pipeline can read our own renderer."
        )
    else:
        out.append(
            "Calibrated against rendered fixtures. Re-run with --photos once Tier B "
            "lands — that is the number that decides these values."
        )
    return "\n".join(out)


def as_dict(sweeps: list[Sweep]) -> dict[str, Any]:
    return {
        "tier": sweeps[0].tier if sweeps else "A",
        "sweeps": [
            {
                "threshold": s.name,
                "current": s.current,
                "clean_band": list(s.safe_band) if s.safe_band else None,
                "margin_steps": s.margin,
                "verdict": s.verdict,
                "stricter": s.knob.stricter,
                "levels": [
                    {
                        "value": level.value,
                        "false_passes": level.false_passes,
                        "false_flags": level.false_flags,
                        "gated": level.gated,
                        "conditions_met": level.conditions_met,
                        "total": level.total,
                        "regression": level.regression,
                    }
                    for level in s.levels
                ],
            }
            for s in sweeps
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--threshold",
        action="append",
        choices=sorted(SWEEPS),
        help="sweep only the named threshold(s); default is all of them",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument(
        "--photos", type=Path, help="calibrate against a directory of real photographs"
    )
    args = parser.parse_args(argv)

    if args.photos and not args.photos.is_dir():
        print(f"no such directory: {args.photos}", file=sys.stderr)
        return 2

    sweeps = sweep_all(args.threshold, photos=args.photos)
    print(json.dumps(as_dict(sweeps), indent=2) if args.json else render(sweeps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
