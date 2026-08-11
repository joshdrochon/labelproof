"""Roll a log file up into latency and cost percentiles (LP-119, OPS-1, OPS-4).

    .venv/bin/python -m scripts.rollup .data/logs.jsonl
    fly logs --no-tail | .venv/bin/python -m scripts.rollup
    .venv/bin/python -m scripts.rollup logs/*.jsonl --out docs/latency.md

PERF-1 puts a 5-second gate on this product and calls it an adoption gate, not a nice to
have. This turns the log the service already writes into the evidence for or against it,
so the number in a status update is a number someone can re-derive from the same file.

Three things this deliberately does:

**It reports what it could not read.** Production stdout is a mix — our JSON lines and
uvicorn's plain text. Silently dropping the half it cannot parse would make a rollup over
twelve lines look exactly like a rollup over ten thousand.

**It uses nearest-rank percentiles.** Every number printed is a request that actually
happened, not an interpolation between two that did. A latency claim should survive
someone asking which request it was.

**It refuses to call five samples a p95.** Below twenty runs the figure is flagged, with
a footnote saying why. A p95 from a handful of samples is the maximum wearing a
percentile's name, and PERF-1 deserves better than that.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.config import Config
from api.timing import STAGE_NAMES

#: Percentiles reported, in order.
PERCENTILES: tuple[int, ...] = (50, 90, 95, 99)

#: Below this many samples a percentile is flagged rather than quietly printed.
MIN_SAMPLES_FOR_P95 = 20

#: Series that are not stages, and where they come from.
REQUEST_SERIES = "request (all HTTP)"
VERIFY_SERIES = "verification (POST /verify)"


# --------------------------------------------------------------------------------------
# Reading the log
# --------------------------------------------------------------------------------------


@dataclass
class Reading:
    """Everything a pass over the log collected."""

    latencies: dict[str, list[int]] = field(default_factory=dict)
    usd: list[float] = field(default_factory=list)
    input_tokens: list[int] = field(default_factory=list)
    output_tokens: list[int] = field(default_factory=list)
    cache_read_tokens: list[int] = field(default_factory=list)
    models: set[str] = field(default_factory=set)
    providers: set[str] = field(default_factory=set)
    parsed: int = 0
    skipped: int = 0
    first_ts: float | None = None
    last_ts: float | None = None

    def note(self, series: str, duration_ms: int) -> None:
        self.latencies.setdefault(series, []).append(duration_ms)


def iter_events(lines: Iterable[str], reading: Reading) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects in `lines`, counting what could not be read.

    Anything that is not a JSON object is skipped, not fatal. A tool that dies on
    uvicorn's first plain-text line is a tool that never runs against a real log.
    """
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        # `fly logs` prefixes each line with an instance and stream marker.
        brace = raw.find("{")
        candidate = raw[brace:] if brace > 0 else raw
        try:
            payload = json.loads(candidate)
        except (ValueError, TypeError):
            reading.skipped += 1
            continue
        if not isinstance(payload, dict) or "event" not in payload:
            reading.skipped += 1
            continue
        reading.parsed += 1
        yield payload


def read(lines: Iterable[str]) -> Reading:
    """One pass over the log."""
    reading = Reading()
    for line in iter_events(lines, reading):
        _observe_time(reading, line)
        event = line.get("event")

        if event == "request_complete" and isinstance(line.get("duration_ms"), int):
            reading.note(REQUEST_SERIES, line["duration_ms"])
        elif event == "verify_complete" and isinstance(line.get("duration_ms"), int):
            reading.note(VERIFY_SERIES, line["duration_ms"])
        elif event == "stage_complete" and isinstance(line.get("duration_ms"), int):
            stage = str(line.get("stage", "?"))
            reading.note(stage, line["duration_ms"])
        elif event == "verification_cost":
            reading.usd.append(float(line.get("usd", 0.0)))
            reading.input_tokens.append(int(line.get("input_tokens", 0)))
            reading.output_tokens.append(int(line.get("output_tokens", 0)))
            reading.cache_read_tokens.append(int(line.get("cache_read_tokens", 0)))
            if model := line.get("model"):
                reading.models.add(str(model))
            if provider := line.get("provider"):
                reading.providers.add(str(provider))

    return reading


def _observe_time(reading: Reading, line: dict[str, Any]) -> None:
    ts = line.get("ts")
    if not isinstance(ts, int | float):
        return
    reading.first_ts = ts if reading.first_ts is None else min(reading.first_ts, ts)
    reading.last_ts = ts if reading.last_ts is None else max(reading.last_ts, ts)


# --------------------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------------------


def percentile(values: Sequence[float], p: int) -> float:
    """Nearest-rank percentile: the smallest observation at or above rank ceil(p/100 * n).

    Deliberately not interpolated. An interpolated p95 is a number between two requests,
    and nobody can point at the request it describes.
    """
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass
class Series:
    name: str
    samples: list[int]

    @property
    def n(self) -> int:
        return len(self.samples)

    @property
    def confident(self) -> bool:
        """Whether the sample is large enough for a p95 to mean what it says."""
        return self.n >= MIN_SAMPLES_FOR_P95

    def at(self, p: int) -> int:
        return int(percentile(self.samples, p))


def series_order(reading: Reading) -> list[str]:
    """Request, then verification, then stages in pipeline order, then anything new."""
    known = [REQUEST_SERIES, VERIFY_SERIES, *STAGE_NAMES]
    ordered = [name for name in known if name in reading.latencies]
    ordered += sorted(set(reading.latencies) - set(known))
    return ordered


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def _window(reading: Reading) -> str:
    if reading.first_ts is None or reading.last_ts is None:
        return "unknown (no timestamps in the input)"
    start = datetime.fromtimestamp(reading.first_ts, UTC)
    end = datetime.fromtimestamp(reading.last_ts, UTC)
    span = reading.last_ts - reading.first_ts
    return (
        f"{start:%Y-%m-%d %H:%M:%S}Z to {end:%Y-%m-%d %H:%M:%S}Z "
        f"({span / 60:.1f} minutes)"
    )


def render_header(reading: Reading, sources: Sequence[str]) -> list[str]:
    out = [
        "# Latency and cost rollup",
        "",
        f"| Generated | {datetime.now(UTC):%Y-%m-%d %H:%M:%S}Z |",
        "|---|---|",
        f"| Source | {', '.join(sources) or 'stdin'} |",
        f"| Window | {_window(reading)} |",
        f"| Log lines read | {reading.parsed} |",
    ]
    if reading.skipped:
        out.append(
            f"| Lines skipped | {reading.skipped} — not JSON objects (uvicorn's own "
            f"output, or a truncated line) |"
        )
    else:
        out.append("| Lines skipped | 0 |")
    out.append("")
    return out


def render_latency(reading: Reading) -> list[str]:
    names = series_order(reading)
    if not names:
        return ["## Latency", "", "No timing lines in the input. Nothing to report.", ""]

    header = " | ".join(f"p{p}" for p in PERCENTILES)
    out = [
        "## Latency",
        "",
        "Milliseconds. Nearest-rank percentiles — every figure is a request that "
        "happened.",
        "",
        f"| Series | n | min | {header} | max |",
        "|---|--:|--:|" + "--:|" * len(PERCENTILES) + "--:|",
    ]
    flagged = False
    for name in names:
        s = Series(name, reading.latencies[name])
        mark = "" if s.confident else "\\*"
        flagged = flagged or not s.confident
        cells = " | ".join(str(s.at(p)) for p in PERCENTILES)
        out.append(
            f"| `{name}`{mark} | {s.n} | {min(s.samples)} | {cells} | {max(s.samples)} |"
        )

    out.append("")
    if flagged:
        out.append(
            f"\\* Fewer than {MIN_SAMPLES_FOR_P95} samples. These percentiles are printed "
            f"because they are what the data says, but a p95 over this few runs is close "
            f"to the maximum and should not be quoted as one."
        )
        out.append("")
    out.extend(_render_gate(reading))
    return out


def _render_gate(reading: Reading) -> list[str]:
    """PERF-1 is stated against the verification, not against every HTTP request."""
    samples = reading.latencies.get(VERIFY_SERIES)
    if not samples:
        return [
            "PERF-1 gate: no `verify_complete` lines in this input, so the 5-second gate "
            "is unmeasured here.",
            "",
        ]

    budget = Config().request_budget_ms
    p95 = int(percentile(samples, 95))
    verdict = "within budget" if p95 <= budget else "**OVER BUDGET**"
    caveat = (
        ""
        if len(samples) >= MIN_SAMPLES_FOR_P95
        else f" (only {len(samples)} samples — see the footnote above)"
    )
    return [
        f"PERF-1 gate: verification p95 is **{p95}ms** against a {budget}ms budget — "
        f"{verdict}{caveat}.",
        "",
        "This is server-side time. It excludes upload, network and render, so it is a "
        "floor for what a person with a stopwatch sees, never the whole of it. "
        "`scripts/timed_run.py` measures the rest.",
        "",
    ]


def render_cost(reading: Reading) -> list[str]:
    if not reading.usd:
        return [
            "## Cost",
            "",
            "No `verification_cost` lines in the input — nothing was priced.",
            "",
        ]

    n = len(reading.usd)
    out = [
        "## Cost",
        "",
        "| Measure | Value |",
        "|---|--:|",
        f"| Verifications priced | {n} |",
        f"| Total | ${sum(reading.usd):.4f} |",
        f"| Mean per verification | ${sum(reading.usd) / n:.4f} |",
        f"| p95 per verification | ${percentile(reading.usd, 95):.4f} |",
        f"| Max | ${max(reading.usd):.4f} |",
        f"| Mean input tokens | {sum(reading.input_tokens) // n} |",
        f"| Mean output tokens | {sum(reading.output_tokens) // n} |",
        f"| Mean cached-read tokens | {sum(reading.cache_read_tokens) // n} |",
    ]
    if reading.models:
        out.append(f"| Models | {', '.join(sorted(reading.models))} |")
    if reading.providers:
        out.append(f"| Providers | {', '.join(sorted(reading.providers))} |")
    out.append("")

    simulated = {p for p in reading.providers if p.startswith("fake")}
    if simulated:
        out.append(
            f"**These runs include sample mode** (`{', '.join(sorted(simulated))}`), "
            f"which makes no model call and costs nothing. The averages above are "
            f"diluted and are not a cost per real verification."
        )
        out.append("")
    return out


def render(reading: Reading, sources: Sequence[str]) -> str:
    body = [
        *render_header(reading, sources),
        *render_latency(reading),
        *render_cost(reading),
    ]
    return "\n".join(body).rstrip() + "\n"


def as_json(reading: Reading) -> dict[str, Any]:
    out: dict[str, Any] = {
        "lines_read": reading.parsed,
        "lines_skipped": reading.skipped,
        "window": {"first_ts": reading.first_ts, "last_ts": reading.last_ts},
        "latency_ms": {},
    }
    for name in series_order(reading):
        s = Series(name, reading.latencies[name])
        out["latency_ms"][name] = {
            "n": s.n,
            "min": min(s.samples),
            "max": max(s.samples),
            "enough_for_p95": s.confident,
            **{f"p{p}": s.at(p) for p in PERCENTILES},
        }
    if reading.usd:
        n = len(reading.usd)
        out["cost_usd"] = {
            "n": n,
            "total": round(sum(reading.usd), 6),
            "mean": round(sum(reading.usd) / n, 6),
            "p95": round(percentile(reading.usd, 95), 6),
            "models": sorted(reading.models),
            "providers": sorted(reading.providers),
        }
    return out


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def _lines_from(paths: Sequence[str]) -> Iterator[str]:
    if not paths:
        yield from sys.stdin
        return
    for path in paths:
        yield from Path(path).read_text(errors="replace").splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.rollup",
        description="p50/p95 latency and cost from LabelProof's structured logs.",
    )
    parser.add_argument("paths", nargs="*", help="log files; omit to read stdin")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    parser.add_argument("--out", default="", help="write the report to this file too")
    args = parser.parse_args(argv)

    reading = read(_lines_from(args.paths))

    if reading.parsed == 0:
        print(
            "No LabelProof log lines found in the input. This tool reads the JSON "
            "objects the service writes to stdout; if you are looking at "
            "`fly logs`, pipe it straight in.",
            file=sys.stderr,
        )
        return 1

    text = (
        json.dumps(as_json(reading), indent=2) + "\n"
        if args.json
        else render(reading, args.paths)
    )
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}", file=sys.stderr)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
