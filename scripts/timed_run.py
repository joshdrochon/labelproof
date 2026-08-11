"""Time N real verifications against any URL and print a committable table (LP-120).

    .venv/bin/python -m scripts.timed_run http://localhost:8000
    .venv/bin/python -m scripts.timed_run https://labelproof.fly.dev --runs 20 \
        --out docs/perf-deployed.md --note "fly, min_machines_running=1, warm 4h"

This is the evidence artifact behind the PERF-1 claim. Everything about it is built so
the claim survives someone hostile reading it:

**It prints every run, not just the summary.** A p95 with the sample hidden is a number
you are asked to trust. With the sample printed, a reader can recompute it, spot the one
outlier that moved it, and see how many runs there were.

**It records what it was measuring.** URL, UTC timestamp, commit, image sizes, run count,
and whether the server said it was in sample mode. A latency table with no provenance
proves nothing a month later.

**It never calls run 1 "cold".** Run 1 is the first request *this script* made. Against a
machine with `min_machines_running = 1` that is usually warm, and labelling it cold would
let the table claim it proved PERF-6 when it proved nothing of the sort. Use `--note` to
record the deployment state you actually arranged.

**It refuses to present sample-mode timings as a latency result.** Sample mode replays
recorded fixtures with no model call. Those runs are tens of milliseconds and would
produce a beautiful, meaningless p95 — the easiest way for this artifact to become
accidentally fraudulent. If `/ready` reports `simulated`, the report is stamped and the
gate verdict is withheld.

**It checks the server's own timing against its stopwatch** (LP-126, PRD §232). If the
server claims more elapsed time than the caller measured, that is impossible, and the
table says so loudly rather than averaging it in.

Standard library only, so it runs from any machine that can reach the URL.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.rollup import MIN_SAMPLES_FOR_P95, PERCENTILES, percentile

DEFAULT_URL = "http://localhost:8000"
DEFAULT_RUNS = 20
DEFAULT_TIMEOUT_S = 60.0

#: A stopwatch, not a policy. The production timeout is `Config.provider_timeout_ms`;
#: truncating a slow run here would delete the very measurement we came for.
Poster = Callable[[str, str, bytes], "Reply"]


# --------------------------------------------------------------------------------------
# HTTP, by hand
# --------------------------------------------------------------------------------------


@dataclass
class Reply:
    status: int
    body: bytes

    def json(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.body)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}


def build_multipart(
    application: dict[str, Any], images: Sequence[tuple[str, bytes]]
) -> tuple[str, bytes]:
    """Encode the `/verify` form. Written out rather than pulled from a library so this
    script has no dependencies and can be run from a laptop against a deployed URL."""
    boundary = f"----labelproof{uuid.uuid4().hex}"
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )

    for name, data in images:
        media = mimetypes.guess_type(name)[0] or "application/octet-stream"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="images"; '
            f'filename="{name}"\r\nContent-Type: {media}\r\n\r\n'.encode()
        )
        parts.append(data)
        parts.append(b"\r\n")

    field("application", json.dumps(application))
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def http_get(url: str, timeout: float = DEFAULT_TIMEOUT_S) -> Reply:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return Reply(response.status, response.read())
    except urllib.error.HTTPError as exc:
        return Reply(exc.code, exc.read())
    except Exception as exc:  # a measurement tool reports, never crashes
        return Reply(0, str(exc).encode())


def poster_for(base_url: str, timeout: float = DEFAULT_TIMEOUT_S) -> Poster:
    def post(path: str, content_type: str, body: bytes) -> Reply:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}{path}",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return Reply(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return Reply(exc.code, exc.read())
        except Exception as exc:
            return Reply(0, str(exc).encode())

    return post


# --------------------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------------------


@dataclass
class Run:
    index: int
    status: int
    client_ms: int
    server_total_ms: int | None = None
    stages: dict[str, int] = field(default_factory=dict)
    recommendation: str = ""
    request_id: str = ""
    usd: float = 0.0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def label(self) -> str:
        """Run 1 is the first request this script made. That is all it is (J-07)."""
        return "first-hit" if self.index == 1 else "warm"

    @property
    def overhead_ms(self) -> int | None:
        """Stopwatch minus the server's own claim: upload, network and deserialisation.

        Negative is impossible and means the two clocks disagree (LP-126).
        """
        if self.server_total_ms is None:
            return None
        return self.client_ms - self.server_total_ms

    @property
    def impossible(self) -> bool:
        overhead = self.overhead_ms
        return overhead is not None and overhead < 0


def run_once(
    index: int,
    post: Poster,
    application: dict[str, Any],
    images: Sequence[tuple[str, bytes]],
) -> Run:
    content_type, body = build_multipart(application, images)

    started = time.perf_counter()
    reply = post("/verify", content_type, body)
    client_ms = round((time.perf_counter() - started) * 1000)

    payload = reply.json()
    if reply.status != 200:
        error = payload.get("error", {}) if isinstance(payload.get("error"), dict) else {}
        detail = str(error.get("code") or error.get("message") or reply.body[:120])
        return Run(index=index, status=reply.status, client_ms=client_ms, detail=detail)

    timings = payload.get("timings_ms", {})
    if not isinstance(timings, dict):
        timings = {}
    aggregate = payload.get("aggregate", {})
    cost = payload.get("cost", {})

    return Run(
        index=index,
        status=reply.status,
        client_ms=client_ms,
        server_total_ms=timings.get("total"),
        stages={k: int(v) for k, v in timings.items() if isinstance(v, int)},
        recommendation=(
            str(aggregate.get("recommendation", "")) if isinstance(aggregate, dict) else ""
        ),
        request_id=str(payload.get("request_id", "")),
        usd=float(cost.get("usd", 0.0)) if isinstance(cost, dict) else 0.0,
    )


# --------------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------------


@dataclass
class Report:
    url: str
    runs: list[Run]
    started_at: str
    commit: str = ""
    note: str = ""
    simulated: bool = False
    ready_status: str = ""
    model: str = ""
    budget_ms: int = 5000
    image_names: list[str] = field(default_factory=list)
    image_bytes: int = 0

    @property
    def successes(self) -> list[Run]:
        return [run for run in self.runs if run.ok]

    @property
    def client_samples(self) -> list[int]:
        return [run.client_ms for run in self.successes]

    @property
    def gate_samples(self) -> list[int]:
        """Whichever clock reported more time on each run.

        Against a real URL that is always the client stopwatch, because it contains the
        server's work plus upload and network. It differs only when the two disagree —
        and on those runs the gate must not be allowed to quote the smaller number.
        """
        return [max(run.client_ms, run.server_total_ms or 0) for run in self.successes]

    @property
    def server_samples(self) -> list[int]:
        return [r.server_total_ms for r in self.successes if r.server_total_ms is not None]

    @property
    def confident(self) -> bool:
        return len(self.successes) >= MIN_SAMPLES_FOR_P95

    @property
    def impossible_runs(self) -> list[Run]:
        return [run for run in self.successes if run.impossible]


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _stats_row(name: str, samples: Sequence[int]) -> str:
    if not samples:
        return f"| {name} | 0 | — | — | — | — | — | — |"
    cells = " | ".join(str(int(percentile(samples, p))) for p in PERCENTILES)
    return (
        f"| {name} | {len(samples)} | {min(samples)} | {cells} | {max(samples)} |"
    )


def render(report: Report) -> str:
    out: list[str] = ["# Timed run", ""]

    if report.simulated:
        out += [
            "> ## SAMPLE MODE — NOT A LATENCY MEASUREMENT",
            ">",
            "> `/ready` reported this server is in sample mode. It replays recorded "
            "fixtures and makes no model call, so the times below measure ingest, "
            "quality scoring and the rules engine — not a verification. They are not "
            "evidence for or against PERF-1.",
            "",
        ]

    out += [
        "| | |",
        "|---|---|",
        f"| URL | `{report.url}` |",
        f"| Started | {report.started_at} |",
        f"| Runs | {len(report.runs)} requested, {len(report.successes)} succeeded |",
        f"| Server mode | {report.ready_status or 'unknown'}"
        + (" — **simulated**" if report.simulated else "")
        + " |",
        f"| Model reported | {report.model or 'unknown'} |",
        f"| Request budget | {report.budget_ms}ms |",
        f"| Payload | {len(report.image_names)} image(s), "
        f"{report.image_bytes / 1024:.0f} KB total |",
    ]
    if report.commit:
        out.append(f"| Commit | `{report.commit}` |")
    if report.note:
        out.append(f"| Note | {report.note} |")
    out += [
        "",
        "Run 1 is labelled **first-hit**, not *cold*: it is the first request this script "
        "made, which is only a genuine cold start if the server had just started or had "
        "scaled to zero. Record what you actually arranged in `Note`.",
        "",
    ]

    header = " | ".join(f"p{p}" for p in PERCENTILES)
    out += [
        "## Summary",
        "",
        f"| Measure | n | min | {header} | max |",
        "|---|--:|--:|" + "--:|" * len(PERCENTILES) + "--:|",
        _stats_row("client stopwatch (ms)", report.client_samples),
        _stats_row("server-reported total (ms)", report.server_samples),
        "",
        "**client stopwatch** is submit-to-response measured by this script: upload, "
        "network, server work, and serialisation. It is the number a person with a "
        "stopwatch sees, minus render. **server-reported total** is the server's own "
        "`timings_ms.total` from the same requests.",
        "",
    ]
    out += _render_gate(report)
    out += _render_honesty(report)
    out += _render_sample(report)
    return "\n".join(out).rstrip() + "\n"


def _render_gate(report: Report) -> list[str]:
    if report.simulated:
        return [
            "PERF-1 gate: **withheld.** These runs made no model call; see the banner "
            "above.",
            "",
        ]
    if not report.gate_samples:
        return ["PERF-1 gate: **no successful runs**, so nothing was measured.", ""]

    p95 = int(percentile(report.gate_samples, 95))
    verdict = "within budget" if p95 <= report.budget_ms else "**OVER BUDGET**"
    caveat = (
        ""
        if report.confident
        else (
            f" — but this is {len(report.successes)} runs, under the "
            f"{MIN_SAMPLES_FOR_P95} a p95 needs to mean anything. Treat it as close to "
            f"the maximum."
        )
    )
    return [
        f"PERF-1 gate: observed p95 is **{p95}ms** against a "
        f"{report.budget_ms}ms budget — {verdict}{caveat}",
        "",
    ]


def _render_honesty(report: Report) -> list[str]:
    """LP-126, across a real network boundary. PRD §232: the stopwatch wins."""
    if not report.server_samples:
        return []
    if report.impossible_runs:
        indexes = ", ".join(str(run.index) for run in report.impossible_runs)
        return [
            f"**The clocks disagree.** On run(s) {indexes} the server reported more "
            f"elapsed time than this script measured from submit to response, which "
            f"cannot happen. The server's `timings_ms.total` is not measuring what it "
            f"claims to (PERF-2, PRD §232). Do not quote either number until this is "
            f"fixed.",
            "",
        ]
    overheads = [r.overhead_ms for r in report.successes if r.overhead_ms is not None]
    return [
        f"Clock check: the server's reported total is below the client stopwatch on "
        f"every run, by {min(overheads)} to {max(overheads)}ms. That gap is upload, network "
        f"and serialisation, and it is why the screen shows the client's number rather "
        f"than the server's — the screen must never report less time than passed "
        f"(PERF-2).",
        "",
    ]


def _render_sample(report: Report) -> list[str]:
    """Every run, always. A summary without its sample is a number you must trust."""
    out = [
        "## Every run",
        "",
        "| # | | HTTP | client ms | server ms | overhead | preprocess | extract | "
        "compare | recommendation | request id |",
        "|--:|---|--:|--:|--:|--:|--:|--:|--:|---|---|",
    ]
    for run in report.runs:
        if not run.ok:
            out.append(
                f"| {run.index} | {run.label} | **{run.status or 'no reply'}** | "
                f"{run.client_ms} | — | — | — | — | — | {run.detail} | — |"
            )
            continue
        overhead = run.overhead_ms
        flag = " ⚠" if run.impossible else ""
        out.append(
            f"| {run.index} | {run.label} | {run.status} | {run.client_ms} | "
            f"{run.server_total_ms if run.server_total_ms is not None else '—'} | "
            f"{overhead if overhead is not None else '—'}{flag} | "
            f"{run.stages.get('preprocess', '—')} | {run.stages.get('extract', '—')} | "
            f"{run.stages.get('compare', '—')} | {run.recommendation or '—'} | "
            f"`{run.request_id or '—'}` |"
        )
    out.append("")

    priced = [run.usd for run in report.successes if run.usd]
    if priced:
        out += [
            f"Cost across {len(priced)} priced run(s): ${sum(priced):.4f} total, "
            f"${sum(priced) / len(priced):.4f} mean.",
            "",
        ]

    failures = [run for run in report.runs if not run.ok]
    if failures:
        out += [
            f"{len(failures)} of {len(report.runs)} runs did not return a verdict. They "
            f"are excluded from the percentiles above and listed in the table — a p95 "
            f"computed only over the requests that succeeded, without saying so, is the "
            f"oldest way to make a slow service look fast.",
            "",
        ]
    return out


# --------------------------------------------------------------------------------------
# Driving it
# --------------------------------------------------------------------------------------


def measure(
    runs: int,
    post: Poster,
    application: dict[str, Any],
    images: Sequence[tuple[str, bytes]],
    *,
    on_run: Callable[[Run], None] | None = None,
) -> list[Run]:
    out: list[Run] = []
    for index in range(1, runs + 1):
        run = run_once(index, post, application, images)
        out.append(run)
        if on_run:
            on_run(run)
    return out


def load_payload(
    base_url: str, application_path: str, image_paths: Sequence[str]
) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    """The application and images to send, from disk or from the server's own `/sample`.

    Fetched once, before timing starts. Downloading the images inside the loop would put
    this script's own network time inside the number it is reporting.
    """
    if application_path and image_paths:
        application = json.loads(Path(application_path).read_text())
        application = {k: v for k, v in application.items() if not k.startswith("_")}
        images = [(Path(p).name, Path(p).read_bytes()) for p in image_paths]
        return application, images

    reply = http_get(f"{base_url.rstrip('/')}/sample")
    if reply.status != 200:
        raise SystemExit(
            f"GET {base_url}/sample answered {reply.status or 'nothing'}. Point this at "
            f"a running LabelProof, or pass --application and --image explicitly."
        )
    payload = reply.json()
    application = payload.get("application", {})
    fetched: list[tuple[str, bytes]] = []
    for entry in payload.get("images", []):
        url = entry.get("url", "")
        if url.startswith("/"):
            url = f"{base_url.rstrip('/')}{url}"
        image = http_get(url)
        if image.status != 200:
            raise SystemExit(f"Could not fetch the sample image at {url}.")
        fetched.append((entry.get("filename", "label.png"), image.body))
    if not fetched:
        raise SystemExit("The server's /sample returned no images to send.")
    return application, fetched


def probe_ready(base_url: str) -> tuple[str, bool, str, int]:
    """Ask the server what it is before timing it (J-08)."""
    reply = http_get(f"{base_url.rstrip('/')}/ready")
    payload = reply.json()
    fallback = f"HTTP {reply.status}" if reply.status else "unreachable"
    status = str(payload.get("status", "")) or fallback
    simulated = bool(payload.get("simulated", False)) or status == "sample_mode"
    model = str(payload.get("model", ""))
    budget = int(payload.get("request_budget_ms", 5000) or 5000)
    return status, simulated, model, budget


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.timed_run",
        description="Time N verifications against a URL and print a committable table.",
    )
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--out", default="", help="write the Markdown report here")
    parser.add_argument("--note", default="", help="what you arranged: warm, cold, region")
    parser.add_argument("--application", default="", help="application JSON to send")
    parser.add_argument("--image", action="append", default=[], help="image to send")
    args = parser.parse_args(argv)

    if args.runs < 1:
        raise SystemExit("--runs must be at least 1.")

    status, simulated, model, budget = probe_ready(args.url)
    application, images = load_payload(args.url, args.application, args.image)

    print(
        f"{args.url} — {args.runs} runs, {len(images)} image(s), server says {status!r}",
        file=sys.stderr,
    )
    if simulated:
        print(
            "  WARNING: sample mode. No model call is made; these are not PERF-1 "
            "numbers.",
            file=sys.stderr,
        )

    def announce(run: Run) -> None:
        server = run.server_total_ms if run.server_total_ms is not None else "—"
        print(
            f"  run {run.index:>3}  {run.label:<9} http={run.status or 'x':<3} "
            f"client={run.client_ms:>6}ms  server={server}",
            file=sys.stderr,
        )

    report = Report(
        url=args.url,
        runs=measure(args.runs, poster_for(args.url, args.timeout), application, images,
                     on_run=announce),
        started_at=f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S}Z",
        commit=git_commit(),
        note=args.note,
        simulated=simulated,
        ready_status=status,
        model=model,
        budget_ms=budget,
        image_names=[name for name, _ in images],
        image_bytes=sum(len(data) for _, data in images),
    )

    text = render(report)
    if args.out:
        Path(args.out).write_text(text)
        print(f"\nwrote {args.out}", file=sys.stderr)
    print(text, end="")

    if report.impossible_runs:
        return 2
    return 0 if report.successes else 1


if __name__ == "__main__":
    raise SystemExit(main())
