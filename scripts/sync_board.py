#!/usr/bin/env python3
"""Sync TICKETS.md checkboxes from git history.

The board is a PROJECTION of commit history, never hand-maintained. A ticket is
closed if and only if a commit names it in a `Closes:` trailer. Editing a
checkbox by hand is meaningless - this script will overwrite it.

Trailer syntax (one per commit, comma/space separated, ranges allowed):
    Closes: LP-017
    Closes: LP-023, LP-024, LP-025
    Closes: LP-040..LP-048
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BOARD = Path(__file__).resolve().parent.parent / "TICKETS.md"
TICKET_RE = re.compile(r"^- \[([ x])\] \*\*(LP-\d{3})\*\*")
TRAILER_RE = re.compile(r"^Closes:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
RANGE_RE = re.compile(r"LP-(\d{3})\s*\.\.\s*(?:LP-)?(\d{3})")
SINGLE_RE = re.compile(r"LP-(\d{3})")


def closed_from_git() -> dict[str, str]:
    """Map ticket id -> abbreviated sha of the commit that closed it."""
    log = subprocess.run(
        ["git", "log", "--format=%h%x00%B%x1e"],
        capture_output=True, text=True, check=True,
    ).stdout
    closed: dict[str, str] = {}
    for entry in log.split("\x1e"):
        if "\x00" not in entry:
            continue
        sha, body = entry.split("\x00", 1)
        sha = sha.strip()
        for trailer in TRAILER_RE.findall(body):
            for lo, hi in RANGE_RE.findall(trailer):
                for n in range(int(lo), int(hi) + 1):
                    closed.setdefault(f"LP-{n:03d}", sha)
            for n in SINGLE_RE.findall(RANGE_RE.sub("", trailer)):
                closed.setdefault(f"LP-{n}", sha)
    return closed


def main() -> int:
    if not BOARD.exists():
        print("sync_board: TICKETS.md not found - nothing to sync", file=sys.stderr)
        return 0

    closed = closed_from_git()
    lines = BOARD.read_text().splitlines(keepends=True)

    known: set[str] = set()
    rewritten, flipped = [], []
    for line in lines:
        m = TICKET_RE.match(line)
        if not m:
            rewritten.append(line)
            continue
        was, tid = m.group(1) == "x", m.group(2)
        known.add(tid)
        now = tid in closed
        if now != was:
            flipped.append((tid, now))
        line = re.sub(r"^- \[[ x]\]", f"- [{'x' if now else ' '}]", line, count=1)
        rewritten.append(line)

    total = len(known)
    done = len(known & closed.keys())

    # The header count is written HERE, not typed. It sits next to a sentence claiming
    # the board is never hand-edited, and it was being hand-edited — which made the one
    # line asserting the board's integrity the one line violating it. Twice it drifted
    # from the checkboxes below it.
    body = "".join(rewritten)
    body = re.sub(
        r"(\| \*\*State\*\* \| \*\*)\d+ of \d+( closed\.\*\*)",
        rf"\g<1>{done} of {total}\g<2>",
        body,
        count=1,
    )
    BOARD.write_text(body)
    print(f"board: {done}/{total} closed", end="")
    if flipped:
        print("  |  " + ", ".join(f"{t}{'✓' if s else '✗'}" for t, s in flipped), end="")
    print()

    # Orphans: a commit claims a ticket the board has never heard of.
    orphans = sorted(closed.keys() - known)
    if orphans:
        print(f"  WARNING unknown ticket(s) cited in commits: {', '.join(orphans)}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
