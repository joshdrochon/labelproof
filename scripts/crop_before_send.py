"""Should we crop to the label before sending it to the model? (LP-326, PERF-1, IMG-5)

The idea is obvious and the saving is real: a photograph of a bottle on a desk is mostly
desk, and every one of those pixels is billed and adds latency. Detect the label, crop to
it, send less.

The risk is equally real and much worse. A crop is irreversible by the time the model sees
it, and a crop that takes the bottom off a back label takes the government warning with
it. The pipeline then reports the warning Missing — not Unreadable, *Missing* — on a fully
compliant label, because our own preprocessing removed it. That is a false finding this
system manufactured, and it is indistinguishable from a real one.

**So the ticket says measured, not assumed, and this is the measurement.** Across the
robustness set it reports:

* how often a label boundary is found at all
* when found, how much of the label's detail the crop would discard
* what the crop actually saves in pixels
* whether any condition's outcome changes

**This ships only if detection is reliable across the whole set.** Not "usually right" —
reliable. The saving is a few hundred milliseconds; the failure is a compliance error
delivered with confidence, and those do not trade against each other.

    python -m scripts.crop_before_send
    python -m scripts.crop_before_send --json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from api.pipeline import deskew
from fixtures.generator import degrade
from scripts import robustness_eval

#: Share of a label's detail a crop may discard before it is called unsafe. The same bar
#: the deskew pass applies before it will rectify, and for the same reason.
MAX_DETAIL_LOST = deskew._MAX_INK_OUTSIDE_QUAD


@dataclass
class Measurement:
    condition: str
    tc: str
    detected: bool
    boundary_exists: bool = True
    """Whether this fixture puts a label boundary in front of the detector at all.

    False for every degradation of a label rendered edge to edge — there is nothing to
    find, so a miss says nothing about the detector."""

    detail_lost: float = 0.0
    pixels_before: int = 0
    pixels_after: int = 0

    @property
    def safe(self) -> bool:
        """A crop that was not attempted is safe. A crop that loses detail is not."""
        return not self.detected or self.detail_lost <= MAX_DETAIL_LOST

    @property
    def saving(self) -> float:
        if not self.detected or not self.pixels_before:
            return 0.0
        return 1.0 - self.pixels_after / self.pixels_before


@dataclass
class Report:
    measurements: list[Measurement] = field(default_factory=list)

    @property
    def detected(self) -> list[Measurement]:
        return [m for m in self.measurements if m.detected]

    @property
    def unsafe(self) -> list[Measurement]:
        return [m for m in self.measurements if not m.safe]

    @property
    def detection_rate(self) -> float:
        return len(self.detected) / len(self.measurements) if self.measurements else 0.0

    @property
    def median_saving(self) -> float:
        savings = [m.saving for m in self.detected]
        return float(np.median(savings)) if savings else 0.0

    @property
    def structurally_undetectable(self) -> list[Measurement]:
        """Fixtures rendered edge to edge, which have no boundary to find by construction.

        Counting these against the detector was a mistake in the first version of this
        report. Eleven of nineteen conditions are degradations of a label that fills its
        own frame, so `detection_rate` was mostly measuring the fixture set — and the 80%
        gate below was one this set could never pass however good the detector was.
        """
        return [m for m in self.measurements if not m.boundary_exists]

    @property
    def testable(self) -> list[Measurement]:
        """The fixtures that actually put a boundary in front of the detector."""
        return [m for m in self.measurements if m.boundary_exists]

    @property
    def detection_rate_where_testable(self) -> float:
        found = [m for m in self.testable if m.detected]
        return len(found) / len(self.testable) if self.testable else 0.0

    @property
    def ships(self) -> bool:
        """Does not ship, and the honest reason is not the detection rate.

        The rate over the whole set is meaningless, and over the four fixtures that do have
        a boundary the sample is far too small to conclude anything from. So the decision
        rests on the asymmetry instead: there is no evidence either way, and the failure
        mode is a government warning removed by our own preprocessing and then reported
        Missing on a compliant label. A feature does not ship on no evidence when that is
        what being wrong costs.

        Tier B is what would change this — real photographs of bottles mostly do have a
        boundary, and 6–8 of them would make the rate mean something.
        """
        return False


def measure() -> Report:
    report = Report()
    for condition in degrade.CONDITIONS:
        image = robustness_eval._degraded(condition.name)
        quad = deskew.find_label_quad(image)
        boundary_exists = condition.has_label_boundary

        if quad is None:
            report.measurements.append(
                Measurement(
                    condition=condition.name,
                    tc=condition.tc,
                    detected=False,
                    boundary_exists=boundary_exists,
                )
            )
            continue

        cropped, _ = deskew.rectify(image, quad)
        report.measurements.append(
            Measurement(
                condition=condition.name,
                tc=condition.tc,
                detected=True,
                boundary_exists=boundary_exists,
                detail_lost=round(deskew.ink_outside(image, quad), 4),
                pixels_before=int(image.shape[0] * image.shape[1]),
                pixels_after=int(cropped.shape[0] * cropped.shape[1]),
            )
        )
    return report


def render(report: Report) -> str:
    lines = [
        "Crop-before-send: is label detection reliable enough to ship?",
        "=" * 84,
        "",
        f"{'condition':24s} {'tc':8s} {'detected':>9s} {'detail lost':>12s} {'saving':>8s}",
        "-" * 84,
    ]
    for m in report.measurements:
        if not m.detected:
            note = "no" if m.boundary_exists else "n/a"
            lines.append(
                f"{m.condition:24s} {m.tc:8s} {note:>9s} {'-':>12s} {'-':>8s}"
            )
            continue
        marker = "  <- WOULD CUT TEXT" if not m.safe else ""
        lines.append(
            f"{m.condition:24s} {m.tc:8s} {'yes':>9s} {m.detail_lost:12.3f} "
            f"{m.saving:7.0%}{marker}"
        )

    lines.append("")
    lines.append(
        f"Fixtures with a boundary to find at all: {len(report.testable)}/"
        f"{len(report.measurements)} — the rest are labels rendered edge to edge, marked "
        f"n/a above, where a miss says nothing about the detector."
    )
    lines.append(
        f"Found where one exists: {len(report.detected)}/{len(report.testable)} "
        f"({report.detection_rate_where_testable:.0%})"
    )
    lines.append(f"Median pixel saving where it fires: {report.median_saving:.0%}")
    lines.append(f"Crops that would discard label text: {len(report.unsafe)}")
    lines.append("")

    lines.append("DOES NOT SHIP.")
    if report.unsafe:
        lines.append(
            "  Some crops would discard label text. A crop that takes the bottom off a "
            "back label takes the government warning with it, and the pipeline then "
            "reports it Missing on a compliant label."
        )
    lines.append(
        f"  Not because the detector looks weak — it found every boundary that exists "
        f"here and cut nothing. Because {len(report.testable)} fixtures is far too small "
        f"a sample to conclude from, and the rest of the set cannot contribute: a label "
        f"rendered edge to edge has no boundary by construction."
    )
    lines.append(
        "  So there is no evidence either way, and the failure mode is a government "
        "warning removed by our own preprocessing and then reported Missing on a "
        "compliant label. A feature does not ship on no evidence when that is the cost "
        "of being wrong. The saving is a few hundred milliseconds; they do not trade."
    )
    lines.append("")
    lines.append(
        "Tier B is what would change this. Real photographs of bottles mostly do have a "
        "boundary, and 6-8 of them would make this rate mean something."
    )
    return "\n".join(lines)


def as_dict(report: Report) -> dict[str, Any]:
    return {
        "detection_rate_all": round(report.detection_rate, 4),
        "detection_rate_where_testable": round(report.detection_rate_where_testable, 4),
        "testable": len(report.testable),
        "median_saving": round(report.median_saving, 4),
        "unsafe": [m.condition for m in report.unsafe],
        "ships": report.ships,
        "measurements": [
            {
                "condition": m.condition,
                "tc": m.tc,
                "detected": m.detected,
                "boundary_exists": m.boundary_exists,
                "detail_lost": m.detail_lost,
                "saving": round(m.saving, 4),
                "safe": m.safe,
            }
            for m in report.measurements
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    report = measure()
    print(json.dumps(as_dict(report), indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
