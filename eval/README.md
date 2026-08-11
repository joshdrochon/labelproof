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

**Code 3 is hard-blocking and has no override.** There is no flag, environment variable or
threshold that lets a run with a warning false pass exit zero. A false pass on the
government warning is the worst outcome this product can produce (PRD §What it must never
do), so it fails the run on its own regardless of overall accuracy. If CI ever needs to
land a change while code 3 is red, the change is wrong, not the gate.

Code 5 exists because `0 false passes` out of zero checks is arithmetically true and
worthless — it is exactly what a broken fixture load produces. A gate that reports green
after checking nothing is worse than no gate.

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

## Not part of CI

Two commands in this directory cost real money and must never run in the default job:

- `python -m eval.run --model claude-haiku-4-5 ...` — the model-tier sweep (LP-329) runs
  against a live model. It is opt-in by flag, and skips with exit `0` when
  `ANTHROPIC_API_KEY` is unset.
- `python -m eval.run --tier b` — Tier B is real bottle photographs (LP-332). It needs a
  live model, is reported separately, and **never gates**.

## Regenerating fixtures

```bash
python -m fixtures.generator.build
```

Deterministic: same specs in, byte-identical PNGs and `golden/set.json` out (LP-123). If
this produces a diff without a spec change, something in the renderer has become
non-reproducible and the eval number is no longer comparable run over run.
