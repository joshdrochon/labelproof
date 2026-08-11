"""LP-330 — day-zero latency spike. One real vision call, timed end to end.

The 5-second budget in `Config.request_budget_ms` is an assumption until this runs. Prior
art on this exact problem landed around 10 seconds per label (BUILD.md §5a, PERF-1), which
would make the whole synchronous design wrong. Better to learn that now than at LP-144.

Deliberately runs with a *large* provider timeout rather than the configured 4000ms. The
production timeout is a policy about when to give up; this spike is a measurement of how
long the call actually takes, and a timeout would truncate the very number we came for.

    ANTHROPIC_API_KEY=... .venv/bin/python -m scripts.spike_latency

Options:
    --runs N          calls per configuration (default 3)
    --effort a,b      effort levels to sweep (default low,medium)
    --model ID        extraction model (default from env / claude-opus-5)
    --fixture NAME    fixture to read (default tc01_old_tom_clean)
    --two-image       also time a front/back application, to confirm concurrency
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

from api.config import Config
from api.models import Commodity
from api.pipeline import ingest
from api.provider.anthropic_adapter import AnthropicVisionProvider, estimated_usd
from api.provider.base import ExtractionRequest, ImageInput

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "fixtures" / "labels"


def _images_for(fixture: str, config: Config) -> list[ImageInput]:
    """Run the real ingest path so preprocessing time is inside the measurement."""
    paths = sorted(LABELS.glob(f"{fixture}*.png"))
    if not paths:
        raise SystemExit(
            f"No rendered images for {fixture!r} in {LABELS}. "
            f"Run: .venv/bin/python -m fixtures.generator.build"
        )

    roles = {"_front": "front", "_back": "back"}
    out: list[ImageInput] = []
    for path in paths:
        role = next((r for suffix, r in roles.items() if suffix in path.stem), "single")
        for image in ingest.ingest_one(path.read_bytes(), config, index=len(out)):
            out.append(
                ImageInput(
                    index=image.index,
                    data=image.data,
                    media_type=image.media_type,
                    role=role,
                )
            )
    return out


def _time_one(
    provider: AnthropicVisionProvider, images: list[ImageInput]
) -> tuple[float, dict[str, int | float]]:
    started = time.perf_counter()
    response = provider.extract(ExtractionRequest(commodity=Commodity.SPIRITS, images=images))
    elapsed_ms = (time.perf_counter() - started) * 1000
    usage = response.usage
    return elapsed_ms, {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "usd": estimated_usd(usage),
    }


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "min_ms": round(ordered[0]),
        "median_ms": round(statistics.median(ordered)),
        "max_ms": round(ordered[-1]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LP-330 latency spike")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--effort", default="low,medium")
    parser.add_argument("--model", default=None)
    parser.add_argument("--fixture", default="tc01_old_tom_clean")
    parser.add_argument("--two-image", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    base = Config.from_env()
    if not base.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set. This spike needs a real key.")

    # Measure, do not enforce. See the module docstring.
    base = replace(
        base,
        provider_timeout_ms=120_000,
        extraction_model=args.model or base.extraction_model,
    )

    plans: list[tuple[str, str]] = [(args.fixture, "single")]
    if args.two_image:
        plans.append(("tc16_front_back", "two-image"))

    # `runs` is bound separately rather than written inline, so it keeps a real type.
    # Inline, `report["runs"]` is `object`, `.append` is an error, and the two call sites
    # carried `# type: ignore[union-attr]` for an error that is actually `attr-defined` —
    # a suppression that silenced nothing and hid the next problem behind it.
    runs: list[dict[str, object]] = []
    report: dict[str, object] = {
        "model": base.extraction_model,
        "target_long_edge_px": base.target_long_edge_px,
        "budget_ms": Config().request_budget_ms,
        "runs": runs,
    }

    for fixture, shape in plans:
        images = _images_for(fixture, base)
        print(f"\n{fixture} ({shape}, {len(images)} image(s))", file=sys.stderr)

        for effort in [e.strip() for e in args.effort.split(",") if e.strip()]:
            config = replace(base, effort=effort)
            provider = AnthropicVisionProvider(config)
            samples: list[float] = []
            last: dict[str, int | float] = {}

            for index in range(args.runs):
                try:
                    elapsed_ms, usage = _time_one(provider, images)
                except Exception as exc:  # noqa: BLE001 — a spike reports, never crashes
                    print(f"  effort={effort} run={index + 1} FAILED: {exc}", file=sys.stderr)
                    continue
                samples.append(elapsed_ms)
                last = usage
                cached = usage["cache_read_tokens"]
                print(
                    f"  effort={effort} run={index + 1}  {elapsed_ms:7.0f} ms  "
                    f"in={usage['input_tokens']} cached={cached} "
                    f"out={usage['output_tokens']}  ${usage['usd']:.4f}",
                    file=sys.stderr,
                )

            if not samples:
                runs.append(
                    {
                        "fixture": fixture,
                        "shape": shape,
                        "effort": effort,
                        "error": "all runs failed",
                    }
                )
                continue

            stats = _summary(samples)
            verdict = "within budget" if stats["median_ms"] <= 4000 else "OVER BUDGET"
            print(
                f"  effort={effort} -> median {stats['median_ms']} ms  ({verdict})",
                file=sys.stderr,
            )
            runs.append(
                {
                    "fixture": fixture,
                    "shape": shape,
                    "effort": effort,
                    "images": len(images),
                    **stats,
                    "usage_last": last,
                }
            )

    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"\nwrote {args.out}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
