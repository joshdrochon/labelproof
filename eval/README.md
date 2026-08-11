# Eval harness — CI contract

Written for whoever wires `.github/workflows/ci.yml`. This directory owns the accuracy
number and the release gates (OPS-2, OPS-3, OPS-6); the workflow only has to run one
command and branch on its exit code.

## The command

```bash
python -m eval.run --report-json eval/out/report.json
```

That is the whole gating run: Tier A (synthetic fixtures), every gate, no flags. It needs
**no network and no API key** — the entire path runs against the offline spec-backed
provider (ENG-3). Do not add `--fixture`; it narrows the set and marks the run a subset,
which suspends the coverage gate.

`eval/out/` is already in `.gitignore`. Keep `report.json` as a build artifact — it is the
run-over-run accuracy record the PRD asks for.

### Threshold

`--min-accuracy FLOAT` sets the field-accuracy bar; it defaults to OPS-3's `0.95`. The flag
**ratchets one way** — a value below `0.95` is a usage error (exit `2`), not a lower bar.
A gate whose threshold can be lowered until it passes is not a gate, and the floor is a
PRD requirement rather than a CI argument. Raising it (`--min-accuracy 0.99`) is fine and
is how you tighten once the number is comfortably clear.

## Exit codes

Branch on these. When several gates fail, the worst one wins, in this order:
`3 > 5 > 4 > 1`.

| Code | Gate | Meaning |
|---|---|---|
| `0` | — | every blocking gate passed |
| `1` | `field_accuracy` | field accuracy below the 95% floor (OPS-3) |
| `2` | — | usage error: unknown `--fixture` name, bad flag |
| `3` | `warning_zero_false_pass` | **a government-warning violation was reported compliant** |
| `4` | `harness_ran_clean` | a fixture crashed and was never scored |
| `5` | `warning_gate_exercised` | no warning-violation rows were scored, so the zero proves nothing |

**Code 3 is hard-blocking and has no override.** No flag, environment variable, threshold
or fixture annotation lets a run with a warning false pass exit zero. A false pass on the
government warning is the worst outcome this product can produce (PRD §What it must never
do), so it fails the run on its own regardless of overall accuracy. If CI ever needs to
land a change while code 3 is red, the change is wrong, not the gate.

That sentence was **false until 2026-08-11**, and the fix is worth knowing about. A
fixture marked `pending="LP-nnn"` used to drop out of both the numerator and the
denominator of this gate, so one word in `fixtures/generator/catalog.py` turned a live
false pass into `"false_passes": 0`, exit `0`, `PASS`. `pending` now excuses an
*inaccurate* verdict and never a *passing* one — see `FieldOutcome.is_warning_false_pass`.

Code 5 has three triggers, all about the denominator:

- **No violation rows scored at all.** `0 false passes` out of zero checks is
  arithmetically true and worthless — it is what a broken fixture load produces.
- **The denominator shrank.** `REQUIRED_WARNING_VIOLATIONS` in
  `fixtures/generator/catalog.py` pins the fixtures that must be checked on every full
  run. Marking one `pending` no longer quietly reduces five checks to four; it fails here
  and the report names the missing fixture.
- **A fixture stopped declaring its violation.** `MUST_DECLARE_WARNING_VIOLATION` pins the
  fixtures whose `expect` must say the warning fails. Hardening `pending` left `expect` —
  one line above it in the catalog — doing exactly the same job: delete
  `expect={"government_warning": "mismatch"}`, regenerate the manifest, and the row stops
  being a violation at all, vanishes from the report, and the run exits `0` printing
  "0 false passes across 4 violation row(s)". This list is a superset of the one above and
  deliberately includes `tc06_buried_warning`, which is `pending` and therefore the one
  fixture the scoring pin cannot cover. **Unlike the other two, this check applies to
  `--fixture` subset runs too** — a missing declaration is a property of the catalog, not
  of what this run selected.

Shrinking either list means editing a committed file, in a diff someone has to approve.

### Current known gap

`tc06_buried_warning` is a **live false pass** on this branch: a shrunk, low-contrast but
verbatim warning, which the rules engine reports as `match` because prominence heuristics
(LP-211) are not implemented here. `python -m eval.run` therefore exits `3` and CI is
correctly blocked. LP-211 has landed on `wave/warning`; merging it clears this. The
developer test suite asserts this exact state and is written to keep passing once the gap
closes, so the fix landing cannot disguise whether the gate hole is really shut.

## Machine-readable output

Two forms, same content:

- `--json` on stdout — the full payload instead of the human report.
- `--report-json PATH` — writes the same payload to a file, alongside whichever renderer
  is on stdout.

Payload shape (abridged):

```jsonc
{
  "status": "pass",              // "pass" | "fail"
  "exit_code": 0,
  "tier": "A",
  "subset": false,
  "accuracy": 1.0,
  "accuracy_floor": 0.95,
  "warning_rows": 14,
  "warning_violations": 4,       // the gate's denominator — a zero here is a failure
  "false_passes": 0,
  "gates": [
    { "name": "warning_zero_false_pass", "status": "pass", "blocking": true,
      "exit_code": 3, "summary": "0 false passes across 4 violation row(s)" }
  ],
  "failures": [ /* fixture, field, expected, actual, missing_findings */ ],
  "false_pass_rows": [ /* fixture, expected, actual */ ]
}
```

The human report also ends with one greppable line for a CI log:

```
::labelproof-eval:: tier=A status=pass exit=0 accuracy=1.0000 false_passes=0 warning_violations=4 subset=false
```

## What the workflow should do on failure

Print the report (it already names the fixture and field for every failure) and fail the
job. On exit code `3`, say so in the job name or the step summary — that one is not an
accuracy regression, it is a compliance failure, and it should read differently to whoever
is looking at the red build.

## Tiers

`--tier` selects the golden set. The default is `a`.

| Value | Set | Gates CI |
|---|---|---|
| `a` | synthetic fixtures, deterministic, offline | **yes** — this is the CI run |
| `b` | real bottle photographs, live model | never |
| `all` | both, reported separately with the A↔B gap | only `a`'s gates count |

Tier B's status appears in **every** run, including the default, so the gap is never
invisible. When it is empty the report says so in plain language and reports no accuracy
figure — `0/0` is not 100%, and a section that rendered 100% is the number that would end
up in a submission. Populating it is documented in `../golden/tier_b/README.md`.

Tier B never changes the exit code, even on a Tier B warning false pass. Small n and a live
model make it flaky by nature, and gating on it would pressure whoever is on the hook into
weakening the expectations. The one thing that *does* fail is a Tier B manifest that does
not validate under `--tier b` — that is a repo defect rather than a model result, and it
exits `2` **only when no Tier A gate failed**. A gate failure always outranks a
configuration error: a broken manifest used to mask exit `3`, sending CI a compliance
failure labelled "bad flag" while the JSON still said `3`.

The payload's `exit_code` is always the number the process returns. `tier_b.errors` and
`tier_b.ran` distinguish "Tier B never ran" from "every Tier B label failed" — without
them both looked like `total: 0, accuracy: null` in the artifact CI keeps.

## Not part of CI

Two commands in this directory cost real money and must never run in the default job:

- `python -m eval.run --model claude-haiku-4-5 ...` — the model-tier sweep (LP-329) runs
  against a live model. It is opt-in by flag, and skips with exit `0` when
  `ANTHROPIC_API_KEY` is unset.
- `python -m eval.run --tier b` — Tier B is real bottle photographs (LP-332). It needs a
  live model, is reported separately, and **never gates**.

### Model-tier sweep

```bash
python -m eval.run --model claude-opus-5 --model claude-sonnet-5 --model claude-haiku-4-5
python -m eval.run --model claude-haiku-4-5 --dry-run   # estimate only, spends nothing
```

Reports per model: field accuracy, **warning-field false passes**, p50/p95, and cost per
label, then applies the ship rule — *the cheapest tier clearing ≥95% accuracy with
zero false passes on warning rows ships.*

**Correctness disqualifies; speed does not.** A model is `DISQUALIFIED` for a warning false
pass, for accuracy below the floor, or for a crashed fixture, and for nothing else. p95 is
printed and a model over the 5s budget is flagged `LATENCY RISK`, but latency never
disqualifies and never rescues — a fast model that reads the warning wrong is disqualified
by this report, not excused by it. When a cheaper tier is passed over, the report names it
and says why.

**It refuses to answer on thin evidence.** The disqualification rule was always right, but
until 2026-08-11 the evidence behind it was one sample for body-bold and none for
header-bold, run once per model. A reviewer simulated it against measured Haiku error rates
and got `SHIPS: claude-haiku-4-5` in 277 of 400 runs — a coin flip on the decision the
instrument exists to make. Two changes:

- The sweep names no winner unless every warning posture (`header_not_all_caps`,
  `header_not_bold`, `body_bold`, `text_altered`, `prominence`, `warning_absent`) is
  exercised by at least `MIN_FIXTURES_PER_POSTURE` **distinct renderings that isolate it**.
- `--repeat N` (default `MIN_RUNS_PER_FIXTURE` = 3) runs every label N times. A model's
  warning reading is stochastic; one pass cannot tell a reliable model from a lucky one.

Three things count as evidence and three things do not.

**Repeats are not evidence.** The first version multiplied the fixture count by `--repeat`,
so the default cleared its own threshold: at `--repeat 100` the artifact read
`body_bold 100 sample(s) — an error rate up to 3% would go unseen`, from a single PNG.
Re-sending one image is the opposite of an independent read.

**A rename or a relabel is not a second rendering.** Distinctness was keyed on the fixture
*name*, so two specs differing only in `name` rendered byte-identical PNGs and counted as
two — and two differing only in `brand_name` were two files but one warning region, which
is the thing the posture is about. It now keys on `warning_fingerprint`: the rendered
statement, its weights, scale, contrast and type size.

**A fixture carrying two defects is evidence for neither.** A label that is both body-bold
and title-case comes back non-passing if the model catches *either*, so it cannot show that
body-bold specifically was read. Only isolating renderings count.

**The threshold is derived, not chosen.** `MIN_FIXTURES_PER_POSTURE` solves
`(1 - ASSUMED_MISREAD_RATE) ** n <= MAX_FALSE_BLESSING_RISK` — at the measured 44% misread
rate and a 5% tolerance, six. It was two, which marked a posture `ok` next to *"an error
rate up to 78% would go unseen"* and left a minimum-compliant set blessing Haiku in ~30% of
simulated runs. `ok` is now earned by clearing the risk tolerance, never by clearing a
count, and each line states plainly how often a 44% misreader would pass.

On this branch every posture has **one** isolating rendering and `header_not_bold` has
**none**, so the sweep reports `NO RECOMMENDATION` and names each gap. Closing it needs six
distinct renderings per posture — more than `tc03b_non_bold_warning_header` on
`wave/warning` alone provides, and the report enumerates exactly what is missing.

The warn-FP column reads `false passes / violation rows checked`, because `0` alone reads
identically whether it was 0-of-4 or 0-of-1.

The p95 here is extraction plus rules, measured from a script. It is **not** PERF-1's
upload-to-verdict number — that one comes from `scripts/timed_p95.py` against the deployed
URL — and latency is grouped by call shape (one image is one call; two images are two
concurrent calls) because a blended figure describes no request anyone makes.

Every exit is `0`: a sweep is a measurement, not a gate. Prices come from `eval/pricing.py`
at first-party list rates; cache reads are not credited, so every cost figure is an upper
bound.

## Regenerating fixtures

```bash
python -m fixtures.generator.build
```

Deterministic: same specs in, byte-identical PNGs and `golden/set.json` out (LP-123). If
this produces a diff without a spec change, something in the renderer has become
non-reproducible and the eval number is no longer comparable run over run.
