# MVP checklist — line-by-line self-audit

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
- **The deploy pipeline has never run.** `.github/workflows/deploy.yml` triggers on push
  to `main` and the work is on a branch, so every deployment so far was issued by hand and
  the release gate has never executed against the artifact. Stated in `CHANGES.md`.
