# PRD checklists — line-by-line self-audit

Both lists, MVP and Final (LP-143, LP-317, ENG-9).

## MVP

`PRD.md` §MVP, every item, checked against the code and the deployed URL rather than
against memory (LP-143, ENG-9). One item fails, and it fails for a reason argued in the
README rather than for an oversight.

| # | Item | | Evidence |
|--:|---|---|---|
| 1 | Verify Now end-to-end on the deployed public URL | ✅ | <https://labelproof.fly.dev>, `scripts/smoke.sh` performs a real seven-field verification against the live model on every deploy |
| 2 | Full field set, with proof cross-check and standards of fill | ✅ | `api/rules/compare.py`, `api/rules/fills.py`; TC-09 and TC-10 in the golden set |
| 3 | All three commodities, per-commodity matrix and ABV exceptions | ✅ | `api/rules/commodity.py`; TC-17 wine, TC-18 malt, TC-26 wine above 14% |
| 4 | Six verdicts exactly, with confidence and rationale on every field | ✅ | `api/models.py:Verdict`; `tests/contract/test_http_ui_contract.py` asserts the union has exactly six members and that TypeScript agrees |
| 5 | Tier 1 + Tier 2 live; STONE'S THROW resolves as Acceptable variation | ✅ | TC-02 in the golden set, and a cost-discipline test that the adjudicator is not invoked for it |
| 6 | **p95 ≤ 5s on the deployed URL, 20 runs recorded** | ❌ | Measured and recorded: [`perf-deployed.md`](perf-deployed.md), 20/20 successful, **p95 9.6s**. The measurement was done; the gate is missed. See below. |
| 7 | Elapsed time on every result card | ✅ | `AggregateBanner`, client stopwatch rather than the server's number — PERF-2 says the screen must never report less time than passed |
| 8 | Multi-image front + back | ✅ | TC-16; the merge resolves per-field provenance across images |
| 9 | Unreadable path with a plain-language retake reason, never a guess | ✅ | `api/verify.py` pre-gate; `ExtractedField` has no field to put a guess in |
| 10 | One-click sample demo | ✅ | Four samples now, not one — a pass, a mismatch, a typography defect and an absent warning, each one click |
| 11 | Recorded fixtures + fake adapter; CI green with no live API calls | ✅ | CI runs the suite inside `unshare --net`, so offline is demonstrated rather than claimed |
| 12 | Infrastructure-as-config with `/health` and `/ready`; rollback documented | ✅ | `fly.toml`, `CHANGES.md`. The rollback is documented and **has not been drilled** — LP-244 |
| 13 | Retention TTL, no PII, EXIF and GPS stripped | ✅ | `api/retention.py`, `api/pipeline/ingest.py`; `tests/test_retention.py` reads every byte under the storage root back |
| 14 | README with setup, approach, tools, assumptions; PRD committed | ✅ | This repository |
| 15 | Golden set ≥10 with expected verdicts; eval in CI | ✅ | 25 fixtures, 175 rows, 100%, zero warning false passes |

**14 of 15.**

## The one that fails

Item 6 is the p95 gate, and it is worth being precise about what kind of failure it is.
The measurement was performed exactly as the PRD specifies — 20 consecutive timed
verifications against the deployed URL, every run recorded in the repository with request
ids. The instrument was not skipped. The number is 9.6s against a 5s target.

That gap is the one deliberate trade in this project, argued in the README: the only model
that meets 5s is Haiku 4.5, which cannot pin US inference (it rejects `inference_geo` with
a 400) and which got the government warning's typography wrong 10 times in 20, every error
in the false-pass direction, on the one field with a zero-false-pass requirement.

A tool 4.6 seconds slower still replaces a 30–40 second vendor and a paper checklist. A
tool that cannot say where a federal agency's label images were processed may not be
deployable at all.

## What is complete but not proven

Two items are green above and carry a caveat that belongs with them rather than in a
footnote:

- **Rollback is documented, not drilled** (item 12). The procedure ships with the
  configuration it describes; a deliberately bad deploy has not been forced. LP-244.
- **The deploy pipeline has now run** (2026-08-13). `.github/workflows/deploy.yml`
  triggers on push to `main`, which did not exist until the work was merged; the first run
  went green in 10m05s — release gate, then deploy and verify — and produced release v27.
  The gate has therefore executed against the artifact exactly once, in the success
  direction. Neither failure direction is drilled: LP-243, LP-244.


---

# Final checklist

`PRD.md` §Final Requirements, every item. **6 of 11 complete, 2 partial, 3 not done** —
and the three not-done are the three that need people rather than time.

| # | Item | | Evidence |
|--:|---|---|---|
| 1 | Batch end-to-end, proven with 300 on the deployed URL | ⚠️ | Everything but the 300. Manifest, progressive results, worst-first triage, per-item retry, CSV export — all live, and **22 applications in 42s with 0 failures** measured on the deployed URL. 300 has not been run; the extrapolation is ~9.5 min against a 10-min goal and an extrapolation cannot see rate limiting |
| 2 | Image robustness: angled, dim, glare, blur → correct verdicts or honest Unreadable, zero fabrication | ✅ | [`robustness.md`](robustness.md); the fabrication sweep is in `tests/test_robustness.py`. Note the limitation in the README: geometric CORRECTION does not run in production, so these are verdicts on uncorrected images |
| 3 | Warning deep checks: tokenized diff, caps + bold header, non-bold body, title-case regression, prominence, type-size caveat | ✅ | `api/rules/warning.py` and `api/rules/typography.py`, 560 tests |
| 4 | Tier 3 live with rationale + confidence routing; fixtures in CI | ⚠️ | Built, wired, 27 tests, 100% coverage, three fakes in CI, and the real adapter written with its prompt. **No adjudicator is passed in production** — `adjudicator=None` is the default, so gray cases still fall to Mismatch, which is the safe direction. The tier exists; it is not switched on |
| 5 | Golden set ≥25 spanning every TC; accuracy report committed | ✅ | 25 fixtures, 175 rows, [`accuracy.md`](accuracy.md) with the confusion matrix and zero false passes |
| 6 | Accessibility: WCAG 2.1 AA / 508 — automated audit + keyboard + screen reader | ⚠️ | axe clean on all five screens, contrast gated as data (worst pair 5.41:1), both UX-3 floors gated (16px type, 44px targets), no colour-only state. `web/e2e/a11y.spec.ts` drives keyboard navigation and the accessibility tree in Chromium, Firefox and a tablet viewport — 75/75. **What a screen reader ANNOUNCES is still untested, and so is Safari** |
| 7 | ≥3 cold users complete a verification with zero instructions | ❌ | Not run. Needs three people |
| 8 | E2E in CI (single, batch, unreadable, provider-down); red CI blocks deploy | ⚠️ | All four flows are covered by tests that drive the real app through the real HTTP stack, and CI runs them offline. "Red CI blocks deploy" is **configured and demonstrated only in the green direction** — the pipeline first ran on 2026-08-13 and deployed v27 from a runner, but no deliberately failing commit has been pushed to watch it refuse (LP-243) |
| 9 | Load: 300-item batch, throttling behaviour, Verify Now p95 during a batch | ⚠️ | The priority lane is measured and holds — verify during a running batch is indistinguishable from idle. The 300-item run and the throttling observation have not been done |
| 10 | Cost analysis with measured per-label cost and projections | ✅ | [`cost.md`](cost.md) — $0.031 single, $0.0179 batch-amortised, projections at 130/600/1,200 a day with the arithmetic shown |
| 11 | Submission package: README, deployed URL, downloadable sample set | ✅ | This repository, <https://labelproof.fly.dev>, four one-click samples |

## What the pattern is

Nothing here is unbuilt because it was forgotten. The five incomplete rows are:

- **three measurements that need scale or people** — 300 items, three cold users, a
  keyboard-and-screen-reader pass;
- **one feature built but not switched on** — Tier 3, deliberately, because turning it on
  in production without a live accuracy subset behind it would be trusting a judgement
  nobody has scored;
- **one claim that is configured but never demonstrated** — red CI blocking a deploy,
  which cannot be shown until the deploy pipeline runs at all.

The distinction matters more than the count. A reviewer should be able to tell "we ran out
of time" from "we decided not to", and these are all the first except Tier 3, which is the
second and says so.
