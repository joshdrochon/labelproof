#!/usr/bin/env python3
"""Answer "is this test stale, or did the merge break something?" mechanically.

A failing test on a merge commit carries no information about *why* it was expected to
hold, so someone has to reconstruct the intent from the assertion. That reconstruction is
slow, it is a judgment call, and it is where a real regression gets waved through as "the
test was probably out of date".

Git already knows the answer. A merge has two parents, and the test either passed on them
or it did not:

    parent A   parent B   merge     verdict
    pass       pass       fail      THE MERGE BROKE IT. A real regression.
    pass       fail       fail      B changed that behaviour; the test is stale w.r.t. B.
    fail       fail       fail      Pre-existing. Not this merge's doing.
    fail       pass       pass      Fixed by the merge. Nothing to do.

No reading, no judgment, no archaeology. This script runs the failing tests against each
parent in a throwaway worktree and prints the table.

It does not tell you what to DO about a stale test — that is still a decision. It tells
you which of the three questions you are actually answering, which is the part that was
costing hours.

    scripts/triage_merge.py                 # triage the current HEAD (a merge commit)
    scripts/triage_merge.py --rev abc1234   # triage a specific merge
    scripts/triage_merge.py --tests a b c   # skip discovery, triage these node ids

Runs each parent in its own `git worktree`, so the working tree is never touched and an
interrupted run cannot leave the repo on the wrong commit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Verdicts, worst first. The order is the point: a reviewer reads the top of the table
#: and stops when they hit something that needs them.
BROKE_BY_MERGE = "BROKE BY MERGE"
NEW_AGAINST_OTHER = "new vs other side"
STALE = "stale"
PRE_EXISTING = "pre-existing"
FIXED = "fixed by merge"
UNKNOWN = "inconclusive"

_SEVERITY: dict[str, int] = {
    BROKE_BY_MERGE: 0,
    NEW_AGAINST_OTHER: 1,
    UNKNOWN: 2,
    STALE: 3,
    PRE_EXISTING: 4,
    FIXED: 5,
}

#: Three outcomes, not two. Collapsing ABSENT into either PASSED or FAILED gives a
#: confidently wrong answer in opposite directions, and I wrote both before writing this:
#: scoring absence as FAILED labels every incoming test "already failing on the target
#: branch", which invites deleting it; scoring absence as PASSED calls every incoming
#: test a regression the merge introduced.
#:
#: A test that does not exist at a commit did not pass there and did not fail there. The
#: honest answer is a third value, and the verdicts below are graded by how much evidence
#: each one actually rests on.
PASSED, FAILED, ABSENT = "passed", "failed", "absent"

#: Read from pytest's exit code, not from its output: 0 passed, 1 failed, 4 usage error
#: (the node id does not resolve), 5 nothing collected. The "file or directory not found"
#: line goes to STDERR, and the first version of this only read stdout.
_ABSENT_CODES: frozenset[int] = frozenset({4, 5})


@dataclass
class Triage:
    """One test, and how it behaved on each side of the merge."""

    node_id: str
    on_merge: bool
    on_parents: dict[str, str] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        states = list(self.on_parents.values())
        if not states:
            return UNKNOWN
        if self.on_merge:
            return FIXED if FAILED in states else UNKNOWN

        if FAILED in states:
            # Some parent already disagreed with this test. The behaviour moved there,
            # deliberately or not, and that parent's commit is where it was argued.
            return STALE if PASSED in states or ABSENT in states else PRE_EXISTING

        existed = [ref for ref, state in self.on_parents.items() if state == PASSED]
        if len(existed) == len(states):
            # It existed on every side, passed on every side, and fails here. Nothing
            # about that is stale — the merge combined two changes that were each fine.
            return BROKE_BY_MERGE
        if existed:
            # New on one side. The merge has put it against code it was never written
            # for, which is a real incompatibility rather than a stale expectation — but
            # it is a weaker claim than the row above and should not read the same.
            return NEW_AGAINST_OTHER
        return UNKNOWN

    @property
    def blame(self) -> str:
        """The parent worth reading, and why."""
        if self.verdict == STALE:
            failed = [r for r, s in self.on_parents.items() if s == FAILED]
            return "already failing on " + ", ".join(failed)
        if self.verdict == NEW_AGAINST_OTHER:
            absent = [r for r, s in self.on_parents.items() if s == ABSENT]
            return "new; absent from " + ", ".join(absent)
        return ""


def _run(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def _git(*args: str, cwd: Path = ROOT) -> str:
    result = _run(["git", *args], cwd)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def parents_of(rev: str) -> list[str]:
    """The commits this merge joined. Two for an ordinary merge, more for an octopus."""
    line = _git("rev-list", "--parents", "-n", "1", rev)
    return line.split()[1:]


def failing_tests(cwd: Path) -> list[str]:
    """Node ids of everything currently failing, straight from pytest's own report."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        report = Path(handle.name)
    try:
        _run(
            [
                sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "--json-report", f"--json-report-file={report}",
            ],
            cwd,
        )
        if report.stat().st_size:
            payload = json.loads(report.read_text())
            return [
                t["nodeid"] for t in payload.get("tests", []) if t.get("outcome") == "failed"
            ]
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    finally:
        report.unlink(missing_ok=True)

    # `pytest-json-report` is not a dependency of this project, and adding one to run a
    # diagnostic would be the wrong trade. Fall back to parsing the summary lines, which
    # pytest has emitted in the same shape for years.
    result = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd)
    return sorted(
        {
            line.removeprefix("FAILED ").split(" - ")[0].strip()
            for line in result.stdout.splitlines()
            if line.startswith("FAILED ")
        }
    )


def outcomes_at(rev: str, node_ids: Sequence[str], keep: Path | None = None) -> dict[str, str]:
    """Run these tests at `rev` in a throwaway worktree — PASSED, FAILED, or ABSENT.

    A worktree rather than a checkout so the caller's working tree is never touched and
    an interrupted run cannot leave the repo somewhere surprising.

    ABSENT is a first-class answer. A test that does not exist at `rev` neither passed
    nor failed there, and pretending otherwise is how this tool becomes confidently
    wrong — see the note on `_ABSENT_CODES`.
    """
    scratch = keep or Path(tempfile.mkdtemp(prefix="triage-"))
    tree = scratch / rev[:12]
    _git("worktree", "add", "--detach", str(tree), rev)
    try:
        # The venv lives outside the worktree; symlink it so imports resolve identically.
        venv = ROOT / ".venv"
        if venv.exists() and not (tree / ".venv").exists():
            (tree / ".venv").symlink_to(venv)

        outcomes: dict[str, bool] = {}
        for node_id in node_ids:
            result = _run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node_id],
                tree,
            )
            if result.returncode in _ABSENT_CODES:
                outcomes[node_id] = ABSENT
            else:
                outcomes[node_id] = PASSED if result.returncode == 0 else FAILED
        return outcomes
    finally:
        _git("worktree", "remove", "--force", str(tree))
        if keep is None:
            shutil.rmtree(scratch, ignore_errors=True)


def render(triages: Iterable[Triage], parents: Sequence[str]) -> str:
    rows = sorted(triages, key=lambda t: (_SEVERITY[t.verdict], t.node_id))
    width = max((len(t.node_id) for t in rows), default=20)

    lines = [
        "",
        f"{'test':{width}}  verdict            evidence",
        f"{'-' * width}  -----------------  --------",
    ]
    for row in rows:
        lines.append(f"{row.node_id:{width}}  {row.verdict:17}  {row.blame}")

    counts: dict[str, int] = {}
    for row in rows:
        counts[row.verdict] = counts.get(row.verdict, 0) + 1

    lines += ["", "parents: " + "  ".join(f"{p[:12]}" for p in parents), ""]
    for verdict in sorted(counts, key=lambda v: _SEVERITY[v]):
        lines.append(f"  {counts[verdict]:3}  {verdict}")

    if counts.get(BROKE_BY_MERGE):
        lines += [
            "",
            f"{counts[BROKE_BY_MERGE]} test(s) passed on EVERY parent and fail here.",
            "Those are regressions this merge introduced. Nothing about them is stale.",
        ]
    if counts.get(NEW_AGAINST_OTHER):
        lines += [
            "",
            f"{counts[NEW_AGAINST_OTHER]} test(s) are NEW on one side and fail against "
            "the other's code.",
            "Not stale — nobody has ever run them together before. Decide which side is",
            "right; the test may be asserting something the other branch deliberately changed.",
        ]
    if counts.get(STALE):
        lines += [
            "",
            f"{counts[STALE]} test(s) were already failing on a parent. Read that parent's",
            "change before touching the test — the behaviour moved deliberately, or it did",
            "not, and the parent's commit message is where that was argued.",
        ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rev", default="HEAD", help="the merge commit to triage")
    parser.add_argument("--tests", nargs="*", help="node ids to triage (skips discovery)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    rev = _git("rev-parse", args.rev)
    parents = parents_of(rev)
    if len(parents) < 2:
        raise SystemExit(
            f"{args.rev} is not a merge commit — it has {len(parents)} parent(s). "
            f"This script answers a question only a merge can pose."
        )

    node_ids = args.tests or failing_tests(ROOT)
    if not node_ids:
        print("nothing failing — no triage needed")
        return 0

    print(
        f"triaging {len(node_ids)} failing test(s) against {len(parents)} parents…",
        file=sys.stderr,
    )

    triages = [Triage(node_id=n, on_merge=False) for n in node_ids]
    by_id = {t.node_id: t for t in triages}
    for parent in parents:
        print(f"  running on {parent[:12]}…", file=sys.stderr)
        for node_id, passed in outcomes_at(parent, node_ids).items():
            by_id[node_id].on_parents[parent[:12]] = passed

    if args.json:
        print(json.dumps(
            [
                {
                    "test": t.node_id,
                    "verdict": t.verdict,
                    "already_failing_on": t.blame,
                    "parents": t.on_parents,
                }
                for t in sorted(triages, key=lambda t: (_SEVERITY[t.verdict], t.node_id))
            ],
            indent=2,
        ))
    else:
        print(render(triages, parents))

    # Non-zero only for the verdict that needs a person. A stale test is information; a
    # regression is a stop sign, and the exit code should be able to tell them apart.
    return 1 if any(t.verdict == BROKE_BY_MERGE for t in triages) else 0


if __name__ == "__main__":
    raise SystemExit(main())
