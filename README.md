# LabelProof

*AI-powered alcohol label verification for TTB compliance review.*

| | |
|---|---|
| **Source brief** | `TakeHome Project: AI-Powered Alcohol Label Verification App.docx`, sha `7f50443d68066298…` |
| **Requirements** | [`PRD.md`](PRD.md) **v1.0** — 2026-08-10. Source of truth; requirement IDs (Appendix A) are cited throughout the code and tests. |
| **Regulatory canon** | `PRD.md` Appendix B, verified against eCFR / ttb.gov on 2026-08-10 |
| **Developer log** | [`CHANGES.md`](CHANGES.md) — how to run it, how to test it, how to roll it back |
| **Licence** | MIT |

Everything in this repository traces back to those two pinned documents. If a behaviour
here disagrees with `PRD.md` v1.0, the PRD is right and the code is a bug — except where a
trade-off is recorded explicitly, in which case it is written down as one.

The brief's sha is the value recorded in the PRD's own front matter; the `.docx` itself is
not committed, so the digest is cited from there rather than recomputed.

---

<!--
The remaining README sections are owned by other tickets and land separately, so that
this file has one author per section rather than four partial drafts:

  LP-004  Architecture defence: stack, host, provider, with rationale
  LP-139  Setup and run instructions, verified against a cold clone
  LP-140  Approach and tools used
  LP-141  Assumptions log — every gap the brief left open, filled and stated
  LP-091 / LP-302  Endpoint and egress table (NET-1)
  LP-262 / LP-303  Production path: FedRAMP, Azure, agency IdP, records schedules
  LP-286  Performance: numbers, method, trade-offs
  LP-294  Accuracy report link

Until they land, `CHANGES.md` carries the working setup, test, and rollback
instructions. It is not a substitute for LP-139 — it is written for the next engineer
rather than for a reviewer — but nothing about running this project is undocumented in
the meantime.
-->

## Getting started

Setup, run, and test instructions live in [`CHANGES.md`](CHANGES.md#run-it) until LP-139
lands the reviewer-facing version here.

```bash
./scripts/install_hooks.sh                  # once per clone
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                  # offline, no API key needed
```
