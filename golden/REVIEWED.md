# Expected verdicts — hand-verified (LP-235, OPS-2)

| | |
|---|---|
| **Reviewed by** | Josh Rochon (JR) |
| **Date** | 2026-08-13 |
| **What was reviewed** | All 25 fixtures, 175 field rows — the label image beside the application values and the expected verdict, as laid out in [`docs/golden-review.md`](../docs/golden-review.md) |
| **Result** | Verified. No expected verdict changed. |
| **`golden/set.json` sha256** | `4b69ef1a99df646d…` |

## Why this file exists

The eval asserts that the engine agrees with `golden/set.json`. Nothing asserts that
`golden/set.json` is right — it was written by one author and, until this review, checked
only against itself. A test suite that grades itself against its own answer key can be
100% correct and completely wrong at the same time, and no amount of green tells you which.

This is the human step that closes that loop. It is deliberately not automatable: the
question is whether a TTB agent would give these verdicts, and no test can answer that.

## Why the digest is here

It pins WHICH answer key was reviewed. `golden/set.json` is regenerated from
`fixtures/generator/catalog.py`, so an expectation can change without anyone touching
this file — and a review of a document that has since changed is not a review of the
document in the repository.

`tests/test_eval.py::test_the_reviewed_golden_set_is_the_one_that_ships` compares the
digest above against the file. If it fails, the set changed after review: re-read the rows
that moved, update the date and the digest, or revert the change. **Do not just update the
digest** — that turns an attestation into a formality, which is worse than not having one.
