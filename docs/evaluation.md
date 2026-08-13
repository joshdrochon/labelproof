# Evaluation walkthrough

The brief's criteria, each with something you can open (LP-318). Where the answer is
"partly", it says partly and points at the gap rather than at the nearest success.

**Start here:** <https://labelproof.fly.dev> — click any of the four samples. Nothing to
type, and between them they show every verdict this tool produces.

---

## 1. Does it work?

| | |
|---|---|
| Live | <https://labelproof.fly.dev>, always-on, no cold start |
| Proof it works *now* | `scripts/smoke.sh https://labelproof.fly.dev` — HTTPS, headers, both health endpoints, the SPA, the samples, a real seven-field verification against the live model, and a batch that stays reachable |
| Accuracy | [`accuracy.md`](accuracy.md) — Tier A 175/175, Tier B 15/21 (71.4%) on real photographs, gap published |
| Batch | 22 applications in 42s, 0 failures, measured on the deployed URL |

**The honest limit:** Tier B is three labels across six photographs — 21 scored rows.
71.4% on real bottles is the number to argue with, not the 100%.

## 2. Verification quality — does it catch the right things?

Seven fields, six verdicts, three recommendations. The design rule is **flag, never pass**:
a false flag costs an agent seconds, a false pass costs the agency a compliance failure,
and every ambiguity resolves toward Needs review.

| Claim | Where to look |
|---|---|
| The model reads; it does not decide | Every verdict is computed in `api/rules/`. `compare` costs **1ms** — the whole rules engine, measured |
| No channel for a guess | `ExtractedField` has no field to put one in. Unreadable and Missing are different findings and never collapsed |
| The warning is exempt from anything that could excuse a defect | `api/rules/warning.py`; 560 tests, including exhaustive sweeps over every word-drop, word-alteration and truncation of the canonical statement |
| Zero false passes, and the gate is exercised | Release-blocking. 9 violation rows scored, 0 passed. The harness also fails if *fewer than four* violations were scored — a zero-false-pass gate over zero violations is vacuous |
| Tier 3 can only refuse | `api/rules/adjudicate.py` — Mismatch → Acceptable variation only, never the warning, bounded before it is called, and any failure leaves the Mismatch standing |

## 3. Technical choices

[`README.md` → Tools](../README.md#tools-and-what-each-was-chosen-over) names what each
choice was made **over**, because a decision without its alternative is a preference.

The one that cost something: **Sonnet 5 over Haiku 4.5.** Haiku is the only model that
meets the 5-second gate. It also cannot pin US inference — it rejects `inference_geo` with
a 400 — and over 20 samples it got the warning's typography wrong 10 times, every error
toward a false pass, on the one field with a zero-false-pass requirement. We took 9.6s.
[`perf-deployed.md`](perf-deployed.md) has the measurements.

## 4. Code quality and organisation

| | |
|---|---|
| Gates | `ruff`, `mypy --strict` over `api/`, 3585 tests, the accuracy eval — all in CI |
| Offline | CI runs the suite inside `unshare --net`, so "no network" is demonstrated, not claimed |
| Determinism | Tier A fixtures are byte-stable and generated from committed specs |
| Structure | `api/rules/` is the regulated logic and imports nothing from the web layer; `api/provider/` is the only place an AI call happens |

The suite is large (3585) and the concentration is deliberate: 560 in `test_warning.py`,
mostly four exhaustive sweeps over the canonical statement. That is a safety property
proved by enumeration rather than by sampling.

## 5. Error handling

Every failure mode returns a sentence an agent can act on, never a stack trace and never a
spinner. `api/errors.py` is the taxonomy; `next_step` drives what the UI offers.

| Failure | What happens |
|---|---|
| Provider unreachable | Plain-language degradation, bounded retries, circuit breaker. Nothing is reported as verified |
| Image too poor to read | Refused **before** any model call — zero spend, and that path cannot produce a false pass |
| A batch item fails | Isolated. It shows "Could not check", never a finding against the label, and retry requeues only the failures |
| Malformed manifest row | Reported by row number and column; the other rows still run |
| Decompression bomb | 170KB PNG declaring 156 megapixels refused in 1ms, before decode |
| Rate limiting | 429 with a body an agent can read |

## 6. Documentation

| | |
|---|---|
| [`README.md`](../README.md) | Setup, approach, tools, assumptions, trade-offs, limitations, egress, production path |
| [`PRD.md`](../PRD.md) | Requirements, 114 of them, cited by ID throughout the code |
| [`TICKETS.md`](../TICKETS.md) | 332 tickets; a checkbox is derived from a `Closes:` trailer, never hand-set |
| [`CHANGES.md`](../CHANGES.md) | Run, test, deploy, roll back |
| [`prd-audit.md`](prd-audit.md) | Both PRD checklists, line by line — MVP 14/15, Final 6/11 |

---

## What is not done

Named in full in the README, and repeated here because a reviewer should not have to hunt
for it:

- **PERF-3 is unmeasured, and it is the headline claim.** The requirement is that
  tool + human beats the 5–10 minute manual baseline. The TOOL is measured — 9.6s p95,
  20 runs. Tool + human never was: that needs cold users timed on the real screen, and
  none were available. The argument is that triage means an agent reads two flagged rows
  rather than seven fields, and every Tier B miss ran toward over-flagging rather than
  under-flagging. That is an argument. It is not a measurement, and it should not be read
  as one.
- **p95 is 9.6s against a 5s target.** Measured, recorded, and traded deliberately.
- **Cropped content reads as Missing, not Unreadable.** The one open correctness defect.
  A finding against the label where the truth is a finding about the photograph.
- **Geometric correction does not run in production.** Written, tested, and not wired —
  the skew estimator was inventing angles until this week.
- **The deploy pipeline ran green for the first time on 2026-08-13** — release gate, then
  deploy and verify, producing release v27 from a GitHub runner. Every deployment before
  that was by hand. **Both failure directions are now drilled too:** a red check refuses
  the merge and therefore the deploy ([`ci-gate-drill.txt`](ci-gate-drill.txt)), and a
  forced bad deploy was caught by smoke and rolled back to the previous image by digest in
  about 90 seconds ([`rollback-drill.txt`](rollback-drill.txt)).
- **No human has used this.** Keyboard navigation, the accessibility tree, error paths
  and two browsers are now driven by `web/e2e/` — 75 checks, three engines. What no
  machine covered: **three cold users** (descoped, none available), **a screen reader**
  actually speaking, **Safari**, and a fresh-eyes walkthrough. The markup was written for
  all of it; written-for is not tested-for.
- **The destroy-and-redeploy drill has not been run.** The forced-rollback drill has —
  see above. Destroying the app and rebuilding it from the repository alone is still
  the one that proves the configuration is complete rather than merely accepted.

Each of these is a thing a reviewer would find. Finding them here first is the point.
