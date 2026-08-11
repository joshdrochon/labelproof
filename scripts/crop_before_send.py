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
    def ships(self) -> bool:
        """The gate. Every attempted crop must be safe, and detection must fire often
        enough to be worth the code that does it.

        Both halves matter. Perfect safety on a detector that fires twice out of fifteen
        is a feature that does nothing, carried forever, on a path where a future
        regression is a compliance error.
        """
        return not self.unsafe and self.detection_rate >= 0.8


def measure() -> Report:
    report = Report()
    for condition in degrade.CONDITIONS:
        image = robustness_eval._degraded(condition.name)
        quad = deskew.find_label_quad(image)

        if quad is None:
            report.measurements.append(
                Measurement(condition=condition.name, tc=condition.tc, detected=False)
            )
            continue

        cropped, _ = deskew.rectify(image, quad)
        report.measurements.append(
            Measurement(
                condition=condition.name,
                tc=condition.tc,
                detected=True,
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
            lines.append(
                f"{m.condition:24s} {m.tc:8s} {'no':>9s} {'-':>12s} {'-':>8s}"
            )
            continue
        marker = "  <- WOULD CUT TEXT" if not m.safe else ""
        lines.append(
            f"{m.condition:24s} {m.tc:8s} {'yes':>9s} {m.detail_lost:12.3f} "
            f"{m.saving:7.0%}{marker}"
        )

    lines.append("")
    lines.append(
        f"Boundary found on {len(report.detected)}/{len(report.measurements)} conditions "
        f"({report.detection_rate:.0%})"
    )
    lines.append(f"Median pixel saving where it fires: {report.median_saving:.0%}")
    lines.append(f"Crops that would discard label text: {len(report.unsafe)}")
    lines.append("")

    if report.ships:
        lines.append("SHIPS — detection is reliable across the set and no crop cuts text.")
    else:
        lines.append("DOES NOT SHIP.")
        if report.unsafe:
            lines.append(
                "  Some crops would discard label text. A crop that takes the bottom off "
                "a back label takes the government warning with it, and the pipeline then "
                "reports it Missing on a compliant label."
            )
        if report.detection_rate < 0.8:
            lines.append(
                f"  A boundary is only found {report.detection_rate:.0%} of the time. A "
                f"label photographed edge to edge, and every proof rendered to the frame, "
                f"has no boundary to find — there is nothing there to detect, and the "
                f"honest answer is to send the whole image."
            )
        lines.append(
            "  The saving is a few hundred milliseconds. The failure is a compliance "
            "error delivered with confidence. Those do not trade against each other."
        )

    lines.append("")
    lines.append(
        "Read the detection rate carefully: most of this set is generated labels rendered "
        "edge to edge, which genuinely have no boundary. Real phone photographs of bottles "
        "mostly do. Re-run against Tier B before concluding the detector is weak — the "
        "number that would change this decision is the one that has not been taken yet."
    )
    return "\n".join(lines)


def as_dict(report: Report) -> dict[str, Any]:
    return {
        "detection_rate": round(report.detection_rate, 4),
        "median_saving": round(report.median_saving, 4),
        "unsafe": [m.condition for m in report.unsafe],
        "ships": report.ships,
        "measurements": [
            {
                "condition": m.condition,
                "tc": m.tc,
                "detected": m.detected,
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
