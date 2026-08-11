"""LP-331 — can the model actually read *bold* and *all-caps* off a label photo?

WARN-2 and WARN-7 are the headline catch: 27 CFR 16.22 requires the GOVERNMENT WARNING
heading to be bold and, inversely, requires the body that follows it *not* to be. If the
model cannot tell bold from regular at 6pt, those two checks have no mechanism, and the
honest design is to route every warning to Needs review — by decision, not by surprise at
F4.

The spike renders samples where ground truth is known by construction (the renderer chose
the font), asks for a normal extraction, and scores three signals against it:

    header_is_all_caps   — expected to be easy; it is a character-level property
    header_is_bold       — the WARN-2 mechanism
    body_is_bold         — the WARN-7 mechanism, and the hard one: TC-04 makes both runs
                           bold, which removes the contrast that makes either legible

Scored three ways, because they are not the same question:

    correct    — the signal matched ground truth
    abstained  — the model returned null, which is honest and routes to Needs review
    WRONG      — the model asserted the opposite of the truth

Only the third is dangerous. An abstention costs a reviewer a second look; a confident
wrong `body_is_bold=false` on a bold body is a false pass on the one field that must never
produce one (WARN-6). The pass criterion is therefore about wrong answers, not accuracy.

    ANTHROPIC_API_KEY=... .venv/bin/python -m scripts.spike_typography --model claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from api.config import Config
from api.models import Commodity, WarningTypography
from api.pipeline import ingest
from api.provider.anthropic_adapter import AnthropicVisionProvider
from api.provider.base import ExtractionRequest, ImageInput
from fixtures.generator.render import render_to
from fixtures.generator.spec import LabelSpec

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "fixtures" / "spike"

#: Ten samples spanning the grid that matters: header bold or not, body bold or not, at
#: three sizes. The compliant case and TC-04 are both here, and so are the two
#: combinations that never appear in the golden set — because a detector that only ever
#: sees one answer can score well by always guessing it.
SAMPLES: list[tuple[str, dict[str, Any]]] = [
    ("compliant", {"warning_header_bold": True, "warning_body_bold": False}),
    ("tc04_body_bold", {"warning_header_bold": True, "warning_body_bold": True}),
    ("header_not_bold", {"warning_header_bold": False, "warning_body_bold": False}),
    ("both_regular_body_bold", {"warning_header_bold": False, "warning_body_bold": True}),
    ("compliant_small", {"warning_header_bold": True, "warning_body_bold": False, "warning_scale": 0.7}),
    ("body_bold_small", {"warning_header_bold": True, "warning_body_bold": True, "warning_scale": 0.7}),
    ("compliant_tiny", {"warning_header_bold": True, "warning_body_bold": False, "warning_scale": 0.55}),
    ("body_bold_tiny", {"warning_header_bold": True, "warning_body_bold": True, "warning_scale": 0.55}),
    ("compliant_large", {"warning_header_bold": True, "warning_body_bold": False, "warning_scale": 1.3}),
    ("title_case_header", {"warning_header_bold": True, "warning_body_bold": False, "warning_header_case": "title"}),
]

#: The three signals under test, and how to read ground truth for each off the spec.
SIGNALS: dict[str, str] = {
    "header_is_all_caps": "warning_header_case",
    "header_is_bold": "warning_header_bold",
    "body_is_bold": "warning_body_bold",
}


@dataclass
class Score:
    correct: int = 0
    wrong: int = 0
    abstained: int = 0

    @property
    def total(self) -> int:
        return self.correct + self.wrong + self.abstained

    def as_dict(self) -> dict[str, Any]:
        return {
            "correct": self.correct,
            "wrong": self.wrong,
            "abstained": self.abstained,
            "accuracy": round(self.correct / self.total, 3) if self.total else 0.0,
            "wrong_rate": round(self.wrong / self.total, 3) if self.total else 0.0,
        }


def _truth(spec: LabelSpec, signal: str) -> bool:
    if signal == "header_is_all_caps":
        return spec.warning_header_case == "upper"
    if signal == "header_is_bold":
        return spec.warning_header_bold
    return spec.warning_body_bold


def _observed(typography: WarningTypography, signal: str) -> bool | None:
    return getattr(typography, signal)  # type: ignore[no-any-return]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LP-331 typography spike")
    parser.add_argument("--model", default=None)
    parser.add_argument("--runs", type=int, default=1, help="repeats per sample")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    base = Config.from_env()
    if not base.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set. This spike needs a real key.")
    config = replace(
        base,
        provider_timeout_ms=120_000,
        extraction_model=args.model or base.extraction_model,
    )
    provider = AnthropicVisionProvider(config)

    OUT.mkdir(parents=True, exist_ok=True)
    scores = {signal: Score() for signal in SIGNALS}
    rows: list[dict[str, Any]] = []

    for name, knobs in SAMPLES:
        spec = LabelSpec(name=f"spike331_{name}", **knobs)
        image_path = render_to(spec, OUT)[0]
        images = [
            ImageInput(index=0, data=page.data, media_type=page.media_type, role="single")
            for page in ingest.ingest_one(image_path.read_bytes(), config)
        ]

        for run in range(args.runs):
            try:
                response = provider.extract(
                    ExtractionRequest(commodity=Commodity.SPIRITS, images=images)
                )
                typography = response.extractions[0].warning_typography
                read_ok = response.extractions[0].warning_text is not None
            except Exception as exc:  # noqa: BLE001 — a spike reports, never crashes
                print(f"{name:24} run={run + 1} FAILED: {exc}", file=sys.stderr)
                continue

            row: dict[str, Any] = {"sample": name, "run": run + 1, "warning_read": read_ok}
            marks: list[str] = []
            for signal in SIGNALS:
                truth = _truth(spec, signal)
                seen = _observed(typography, signal)
                if seen is None:
                    scores[signal].abstained += 1
                    outcome = "abstain"
                elif seen == truth:
                    scores[signal].correct += 1
                    outcome = "ok"
                else:
                    scores[signal].wrong += 1
                    outcome = "WRONG"
                row[signal] = {"truth": truth, "seen": seen, "outcome": outcome}
                marks.append(f"{signal.replace('_is_', ':')}={outcome}")

            rows.append(row)
            flag = "" if read_ok else "  [warning not read]"
            print(f"{name:24} run={run + 1}  " + "  ".join(marks) + flag, file=sys.stderr)

    print("\n--- summary ---", file=sys.stderr)
    for signal, score in scores.items():
        d = score.as_dict()
        print(
            f"{signal:20} correct={d['correct']:3} wrong={d['wrong']:3} "
            f"abstain={d['abstained']:3}   accuracy={d['accuracy']:.2f} "
            f"WRONG-RATE={d['wrong_rate']:.2f}",
            file=sys.stderr,
        )

    # An empty run is NOT a pass. "No wrong answers" is vacuously true when nothing ran,
    # and the first version of this gate cheerfully reported MECHANISM INTACT for a model
    # that had 400ed on all twenty calls. That is the same false-green that let a
    # structurally invalid schema survive 123 tickets, reproduced inside the very
    # instrument built to catch it. An instrument that cannot fail cannot measure.
    expected = len(SAMPLES) * args.runs
    observed = scores["header_is_bold"].total
    if observed < expected:
        mechanism_intact = False
        verdict = (
            f"INCONCLUSIVE — {observed}/{expected} samples returned. Nothing is proven "
            f"about this model either way; fix the calls and re-run."
        )
    elif all(scores[signal].wrong == 0 for signal in ("header_is_bold", "body_is_bold")):
        # The gate is wrong answers, not accuracy. An abstention is correct behaviour that
        # costs a Needs review; a confident inversion on body_is_bold is a false pass on
        # the warning statement, which the PRD names as the worst outcome this product
        # can produce.
        mechanism_intact = True
        verdict = "MECHANISM INTACT — WARN-2/WARN-7 can rely on these signals"
    else:
        mechanism_intact = False
        verdict = "MECHANISM UNSAFE — route warning typography to Needs review (WARN-2, WARN-7)"
    print(f"\n{verdict}", file=sys.stderr)

    report = {
        "model": config.extraction_model,
        "runs_per_sample": args.runs,
        "samples_expected": expected,
        "samples_observed": observed,
        "scores": {signal: score.as_dict() for signal, score in scores.items()},
        "mechanism_intact": mechanism_intact,
        "verdict": verdict,
        "rows": rows,
    }
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
