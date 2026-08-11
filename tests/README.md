# The LabelProof test suite

Read this first if you are grading, reviewing, or adding to the suite. It is short on
purpose; the conventions it describes are enforced by `tests/meta/test_case_coverage.py`,
so if this file and the suite ever disagree, the build goes red rather than the document
going quietly stale.

## Why the suite is shaped this way

A schema this project sent to the Anthropic API was structurally invalid. It exceeded the
documented ceiling on union-typed parameters *and* a separate compiled-grammar limit.
**Every live call returned HTTP 400 before the model ever saw an image, and 624 offline
tests passed against it across 123 tickets** — because the offline providers return
already-parsed objects and never build a request.

The suite tested behaviour thoroughly and tested **contracts** not at all. Everything
below is organised around closing that gap and keeping it closed.

## Running it

```bash
.venv/bin/python -m pytest                 # everything, with coverage and the floors enforced
.venv/bin/python -m pytest -m property     # one layer
.venv/bin/python -m pytest -m contract
.venv/bin/python -m pytest -m regression
.venv/bin/python -m pytest -m e2e
.venv/bin/python -m pytest -m "tc"         # every canonical PRD case
.venv/bin/python -m pytest --no-cov-gate   # measure coverage, do not enforce the floors
```

Coverage is measured on **every** invocation — a number you have to remember to ask for
is a number nobody has. The floors are enforced only on a full-suite run, so
`pytest tests/properties` does not fail with "coverage 5%".

The suite runs **offline**. Every socket operation is blocked for the whole session and a
test that opens one fails loudly (ENG-3). That is asserted, not assumed — see
`contract/test_offline.py`.

## Layout

| Layer | What lives there |
|---|---|
| `tests/test_*.py` | Per-module unit tests. One file per source module, testing it directly. |
| `properties/` | Claims about **all** inputs, checked with hypothesis. |
| `regression/` | One historical defect per file, each naming what it pins. |
| `contract/` | Agreements with something **outside** this process. |
| `e2e/` | Whole journeys over the real HTTP stack. |
| `meta/` | Tests about the test suite itself, including this document. |

### `properties/`

A hand-picked example set tests the inputs the author thought of. The bugs live in the
ones nobody did — a property test in this project had already caught NFKC leaving
decomposed Hangul, which no example used.

Write a property when the claim is universal: *normalization is idempotent*, *comparison
is symmetric where both sides are present*, *aggregation never returns
`ready_to_approve` unless the government warning matched*.

Two conventions that matter:

- **Build the interesting inputs; do not filter for them.** Two random strings never
  match, so filtering for "these compare equal" discards every example and hypothesis
  raises `filter_too_much`. Generate a base string and apply the transformations the rule
  is specified to fold away. Each pair is then a claim the product makes, and a failure
  names a real behaviour.
- **State the negative.** "No mutation of the warning statement reaches Match" is stronger
  than a list of the six mutations somebody thought of, and it is the form that catches
  the seventh.

### `regression/`

One file per historical defect. The module docstring says **what happened, what it
produced, and why it mattered** — not just which line changed. A regression test whose
docstring reads "fixes bug" is deleted by the next person to touch it.

Pin the *shape*, not a remembered number. `exposure_score(x) == 0.83` passes on a rewrite
that reintroduces the ceiling with different constants; *monotonicity in brightness*
cannot.

Where a limit check is involved, also assert that the **pre-fix input violates it**. A
check with no teeth is how the schema shipped.

### `contract/`

The layer whose absence caused the incident. A contract test asserts an agreement with
something this process does not control: the Messages API, the browser, `.env.example`,
`golden/set.json`.

The rule for what belongs here: assert things that are true of **the other side**. "The
model is `claude-opus-5`" is our choice and belongs in a unit test. "`temperature` is not
sent, because it is rejected on this model family" is the API's rule and belongs here.

**Anywhere a fake stands in for a real thing, something must assert the two agree.** A
fake that has drifted from what it doubles is worse than no fake, because it manufactures
confidence. `contract/test_fake_provider_agreement.py` round-trips every fixture's
extraction back through the wire format and the real parser: if a fake produces something
the schema cannot express, the suite says so offline, which is the only place it can.

### `e2e/`

Whole journeys over the real HTTP stack — routing, middleware, multipart, ingest, the
quality pre-gate, the rules engine, serialization, error handlers — against an offline
provider. Nothing is stubbed except the model call.

Assert **what an agent sees at the end**: the recommendation, the rows, the sentence
explaining why, and the next step when it fails. A pipeline correct in pieces and useless
as a whole passes every unit test and fails these.

### `meta/`

Computes the claims the suite makes about itself, so they cannot rot: every canonical case
(TC-01 … TC-22) has a named test, every layer directory is described here, every xfail is
strict and names its owner.

## Conventions

**Names describe behaviour, not functions.** `test_a_missing_warning_is_disqualifying`,
not `test_recommend_2`. The name is what a grader reads in the failure output and often
the only part of a test that gets read. Enforced (minimum three words) for the layered
directories.

**Every module has a docstring** saying what it is for. A grader opening
`test_aggregate_warning_holes.py` needs the sentence explaining what a warning hole is
before anything else. Enforced.

**Every module declares its marker at the top** — `pytestmark = pytest.mark.contract` —
so `-m contract` selects the whole layer. Per-function markers get forgotten on the next
test. Enforced.

**Canonical cases carry `@pytest.mark.tc("TC-03")`.** That marker is how the suite proves
LP-237 rather than asserting it. The four implemented warning cases (TC-03, TC-04, TC-05,
TC-07) must be covered in **at least two layers**: the rules layer proves the function,
end-to-end proves the product. Enforced.

**Docstrings say why, not what.** The assertion already says what. The docstring says what
breaks in the real world if it fails — which regulation, which person's workflow, which
false pass. If you cannot write that sentence, the test may not be worth keeping.

## Known gaps and pinned defects

Open defects found while building this suite are pinned as `xfail(strict=True)` in the
file that would exercise them. **Strict** is the point: the test fails today, and the
moment somebody fixes the defect it turns **red**, forcing them to remove the marker. A
non-strict xfail passes forever once fixed and the pin rots.

Every pinned defect names the defect, its consequence, a suggested fix, and the **owning
file**. Both are enforced by `meta/test_case_coverage.py`.

`pytest -rx` lists them all with their reasons. That output is the current gap list; it is
deliberately not duplicated here, because a hand-maintained copy would be wrong within a
week.

## Coverage policy

| Scope | Floor | Why |
|---|---|---|
| Each `api/rules/*.py` module | 100% | These decide verdicts. A line that has never run has never been checked against the regulation it encodes. |
| `api/rules/fills.py` | 98% | One unreachable defensive branch; see the comment in `conftest.py`. |
| Whole project | 88% | A backstop that fires when a test file is deleted, not on ordinary drift. |

Enforced in `conftest.py:pytest_sessionfinish` against the **live** coverage object rather
than a report file, so the gate cannot pass by measuring a stale artifact.

**Raise a floor when coverage rises. Never lower one to make a build pass** — if a line
genuinely cannot be reached, say so in writing in `RULES_COVERAGE_FLOORS`, the way
`fills.py` does.

**Deliberate exclusions** (`pyproject.toml`, `[tool.coverage.run] omit`):

- `api/provider/record.py` — captures live provider responses for later replay.
  Exercising it means making a live call, which ENG-3 forbids. A developer tool, not a
  request path; nothing it does can produce a verdict.
- `fixtures/generator/build.py` — the fixture-build CLI. Its logic is in `render.py` and
  `spec.py`, both covered; what remains is argument parsing and file writing.
- `if TYPE_CHECKING:`, `__main__` guards, and `raise AssertionError("unreachable…")` —
  states the type system already rules out.

## Determinism

No retries, ever (ENG-3, LP-246). A flaky test is a broken test: if the suite can be made
green by running it again, it stops being a gate and becomes a formality.

That constrains how fixtures are built. Test images are rendered from the fixture
generator and degraded through `fixtures/generator/degrade.py` rather than drawn ad hoc —
a synthetic bar pattern rings at large Gaussian radii and makes blur look non-monotonic,
and a flat rectangle scores as hopelessly blurred and never reaches the code under test.

## Adding a test

1. **Which layer?** Is the claim universal (`properties/`), a defect that already
   happened (`regression/`), an agreement with something outside this process
   (`contract/`), a whole journey (`e2e/`), or one module's behaviour (`tests/test_*.py`)?
2. Name it for the behaviour.
3. Write the docstring first, saying what breaks in the real world if it fails.
4. If it covers a canonical case, add `@pytest.mark.tc("TC-nn")`.
5. Run the full suite. Green means green — including the coverage floors.
