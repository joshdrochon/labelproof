# LabelProof

AI label verification for TTB compliance review. An agent uploads the label artwork and
the application data; LabelProof returns a per-field checklist with evidence and a
recommendation. **It recommends — the agent decides.**

| | |
|---|---|
| **Source brief** | `TakeHome Project: AI-Powered Alcohol Label Verification App.docx`, sha `7f50443d68066298…` |
| **Requirements** | [`PRD.md`](PRD.md) **v1.0** — 2026-08-10. Source of truth; requirement IDs (Appendix A) are cited throughout the code and tests. |
| **Regulatory canon** | `PRD.md` Appendix B, verified against GPO CFR XML and Cornell LII, with retrieval dates recorded per item in `api/canon.py` |
| **Developer log** | [`CHANGES.md`](CHANGES.md) — deploy, roll back, operate |
| **Execution plan** | [`TICKETS.md`](TICKETS.md) — 332 tickets, each traced to a requirement ID |
| **Start here (reviewers)** | [`docs/evaluation.md`](docs/evaluation.md) — the brief's criteria, each with something to open |
| **PRD audit** | [`docs/prd-audit.md`](docs/prd-audit.md) — both PRD checklists, line by line — MVP 14/15, Final 7/11 |
| **Accuracy** | [`docs/accuracy.md`](docs/accuracy.md) — Tier A 100% on 175 rows, Tier B 71.4% on 21, confusion matrices, every miss explained |
| **Cost** | [`docs/cost.md`](docs/cost.md) — $0.031 a verification, $0.018 in batch, measured |
| **Latency** | [`docs/perf-deployed.md`](docs/perf-deployed.md) — 20 timed runs on the deployed URL |
| **Robustness** | [`docs/robustness.md`](docs/robustness.md) — angle, blur, glare, occlusion |
| **Live** | <https://labelproof.fly.dev> |
| **Licence** | MIT |

If a behaviour here disagrees with `PRD.md` v1.0, the PRD is right and the code is a bug —
except where a trade-off is recorded explicitly, in which case it is written down as one.
The brief's sha is quoted from the PRD's front matter; the `.docx` is not committed.

---

## Contents

- [Run it](#run-it) · [What it checks](#what-it-checks) · [Approach](#approach) · [Checking a batch](#checking-a-batch) · [Assumptions](#assumptions) · [What is not done](#what-is-not-done)
- [Observability](#observability) — the log, the fields, the timings, and what the numbers actually are
- [Ops runbook](#ops-runbook) — read the log, the timings, the cost; the honesty check
- [Network egress](#network-egress) — every external domain, allowlist-ready (NET-1)
- [Security, privacy, and data retention](#security-privacy-and-data-retention) — SEC-1…SEC-10
- [Deployment](#deployment) — host, residency, health, keep-warm, rollback

---

## Run it

Nothing below needs an API key. The suite and the demo both run offline.

```bash
git clone <this repo> && cd labelproof
python3.14 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/python -m pytest                        # 3585 tests, offline, ~5 min
.venv/bin/python -m eval.run                      # the accuracy gate
LABELPROOF_FAKE_PROVIDER=1 .venv/bin/uvicorn api.main:app --reload
```

Then open <http://localhost:8000> and click one of the samples. There are four, and
between them they show every shape of answer the tool gives: a label that checks out, a
value that disagrees with the application, a warning heading in the wrong case, and a
label with no warning at all. Each loads a real application and its artwork and returns a
verdict in one click.

**Start there rather than with the form.** In COLA the application already exists and the
agent is confirming it against submitted artwork — they are not typing it in. The manual
form on that screen stands in for a record this prototype does not fetch (see Assumptions),
so filling it by hand is the least representative way to see the product.

To run it against the real model, put a key in `.env` (gitignored, see `.env.example`):

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
.venv/bin/uvicorn api.main:app
```

Sample mode is honest about itself: `/ready` reports `simulated: true`, and an upload it
does not recognise is **refused** rather than answered from a fixture. An early version
fell back to the clean fixture, which returned *Ready to approve* with a government
warning that was never on the image — a false pass with fabricated evidence. It now fails
closed.

**The gates**, all three of which CI runs:

```bash
.venv/bin/ruff check .
.venv/bin/mypy --strict api/
.venv/bin/python -m pytest        # inside `unshare --net` in CI, so "offline" is shown, not claimed
```

---

## What it checks

Seven mandatory fields, six verdicts, three recommendations.

| Field | Verdict | Recommendation |
|---|---|---|
| Brand name | Match | Ready to approve |
| Class / type designation | Acceptable variation | Needs review |
| Alcohol content | Mismatch | Return for correction |
| Net contents | Missing | |
| Producer name and address | Unreadable | |
| Country of origin | Not applicable | |
| Government health warning | | |

The seventh field is not like the other six. 27 CFR 16.21 fixes its wording exactly and
16.22 fixes its appearance, so it is the one field where "close enough" is a
non-answer — and it is the field the whole design is bent around.

---

## Approach

**One synchronous request.** Upload → sanitize → quality-score → one vision call per
image → deterministic rules → response. No queue, no polling. The vendor that came before
this one took 30–40 seconds a label and agents went back to doing it by eye, so latency is
an adoption gate rather than a nice-to-have.

**The model reads; it does not decide.** The extractor returns what is printed on the
label and nothing else. Every verdict is computed by deterministic code in `api/rules/`,
which is unit-testable in milliseconds and cannot have an opinion. `ExtractedField` has no
field to put a guess in — a value the model could not read comes back
`value=None, legible=False`, and a field that is not on the image is omitted entirely.
Those two are different findings (Unreadable versus Missing) and collapsing them would be
a false finding either way.

**Three tiers of matching**, and the tier is shown to the agent:

1. Normalization — `STONE'S THROW` is `Stone's Throw`. Case, punctuation, accents, spacing.
2. Explainable variation — the difference is named, not folded away silently.
3. Adjudication — genuinely gray cases go to a model, with the reasoning recorded.

**Flag, never pass.** A false flag costs an agent seconds. A false pass costs the agency a
compliance failure. Every ambiguity in this codebase resolves toward Needs review, and the
government warning fails closed unconditionally. It is exempt from every threshold that
could EXCUSE a defect, and subject to one that can only refuse to certify: a reading the
model itself disclaimed cannot clear a label. That floor demotes Match to Unreadable and
never the reverse, so low confidence can neither pass a violation nor wash one away.

**A pre-gate before any spend.** An image nobody could read gets a plain-language retake
reason and **zero** model calls. That path cannot produce a false pass, because its
outcome is "we did not verify this."

**Two tiers of evidence.** Tier A is 25 synthetic fixtures, deterministic and byte-stable,
and it gates CI. Tier B is real photographs of real bottles, reported separately and never
gating. The gap between them is a published number rather than an embarrassment.

### Tools, and what each was chosen over

| Choice | Over | Because |
|---|---|---|
| **Python 3.14 + FastAPI** | Node, Go | The image work is OpenCV and Pillow, and the regulatory logic is a rules engine that has to be readable by someone checking it against the CFR. Async matters here only for holding a connection open during a model call, which FastAPI does without ceremony. |
| **Claude Sonnet 5** | Haiku 4.5, Opus 5 | Measured, not assumed — the table below. Haiku is the only model that meets the 5-second gate and the only one that cannot pin US inference, and it got typography wrong 10 times in 20, always toward a false pass. |
| **One vision call per image, concurrent** | One call for all images | Wall clock is the slower of two rather than the sum, and a per-image call keeps provenance honest: a field's evidence box belongs to a specific photograph. |
| **Structured outputs (`output_config.format`)** | Prompt-and-parse | The schema is closed — `additionalProperties: false`, every key required — so `ExtractedField` has no channel for a guess. A value that cannot be read comes back `value=None, legible=False` because the grammar forces both keys to exist. |
| **Deterministic rules in `api/rules/`** | Asking the model for verdicts | The model reads; it does not decide. Every verdict is computed by code that is unit-testable in milliseconds, cannot have an opinion, and can be audited line-by-line against 27 CFR by someone who does not know Python well. |
| **Fly.io, one always-on machine** | Render, Railway, AWS | `auto_stop_machines = "off"` in one line — the previous vendor lost this account to slow first impressions, so a cold start on the grader's first click is that failure in miniature. Region is pinnable to `iad`, and the whole environment is one file, which is what makes the rebuild drill a real test rather than a paragraph. |
| **SQLite on the machine's disk for batch** | Postgres, Redis | Uploads and results are ephemeral by policy — 24-hour TTL — and a managed database would be durable storage for exactly the data the retention rule exists to destroy. It also costs a second egress host. The trade is that batch requires a single machine; `fly.toml` and the smoke test both enforce it. |
| **React + TypeScript, no UI framework** | Material, Chakra, Tailwind | Seven verdict states, one table, one dialog. A component library would ship 200KB to render a checklist and would have to be fought to meet the contrast and type-size floors that Section 508 actually requires. |
| **Recorded fixtures + a fake adapter** | Mocking the SDK | CI runs inside `unshare --net`, so "offline" is demonstrated rather than claimed, and the fake replays real recorded responses — including their failure shapes — instead of whatever a mock author imagined. |

Full build log and the decisions that were reversed along the way are in
[`CHANGES.md`](CHANGES.md).

---

## Checking a batch

The second tab. An agent with a queue rather than a label — Janet has been asking for this
for years, and the brief puts it at 300 applications at once (BATCH-1).

**What you upload.** A CSV with one row per application, and the images those rows name —
either as loose files or as a single `.zip`. `GET /batch/manifest-template.csv` is the
template, linked from the page; the columns are the same seven fields the single-label
form asks for, plus `front_image` and `back_image`.

**A bad row does not reject the upload.** Three malformed rows out of 300 means 297 are
queued and the three are reported by row number with the column that failed (TC-20).
Making an agent fix a typo before any work starts is the batch equivalent of doing them
one at a time.

**Results appear while the job runs.** The table is not gated on completion — a 300-item
batch takes minutes and the first rejections are triageable within seconds. Rows arrive
**worst first**: return-for-correction, then items that could not be checked at all, then
needs-review, then the clean ones. That order is computed on the server by the same ladder
the single-label view uses, and the page never re-sorts it. Filters hide rows; they do not
reorder them.

**Failed is not the same as rejected.** An item that errored shows "Could not check" and
says so on its own row — it never appears as a finding against the label. Retry requeues
only the failed items and leaves the finished ones alone.

**When it is done**: `Export CSV` writes one row per application with every field verdict,
the driving field, the findings and the rationale — the file that goes in the case file
and gets printed. Cells that a spreadsheet would execute are neutralised.

Measured on the deployed URL: **22 applications in 42 seconds, 0 failures, $0.0179 per
label** — cheaper than a single verification because the prompt cache is read on every
item after the first. A 300-item run has not been performed; the extrapolation is roughly
9.5 minutes against a 10-minute goal, and an extrapolation is exactly what cannot see rate
limiting at that scale.

---

## Assumptions

The brief left these open. Each was decided deliberately; each is reversible.

| Assumption | Why | If wrong |
|---|---|---|
| **The seven fields are TTB's mandatory elements**, not an arbitrary list | 27 CFR parts 4, 5, 7 and 16 | The field set is a table in `api/rules/commodity.py` |
| **The application is typed in, not parsed from a COLA filing** | The brief shows an agent entering data | An importer is additive; nothing downstream changes |
| **Commodity is known** (spirits / wine / malt) | Requirements differ by commodity — ABV is optional on malt, origin only on imports | It is one field on the form |
| **Artwork may be a photograph, not just print-ready art** | Jenny described phone photos of bottles | The whole image pipeline exists for this |
| **"Verify" means compare label to application** — not adjudicate the application itself | The brief's scope | Out of scope, and stated as such in the UI |
| **A recommendation is advisory** | No regulator would accept an automated approval | The three values are advisory by name |
| **Sonnet 5 over the faster model** | See below — this is the one assumption with a cost | One line in `api/config.py` |

### The one trade we made against the brief

**PERF-1 asks for p95 ≤ 5s. We do not meet it. Measured p95 on the deployed URL is 9.6s.**

That number comes from 20 consecutive verifications against
<https://labelproof.fly.dev>, warm, two images per request — the full table, every run,
with request ids, is in [`docs/perf-deployed.md`](docs/perf-deployed.md).

| | Deployed, 20 runs |
|---|---|
| p50 | 8.5s |
| **p95** | **9.6s** |
| max | 9.9s |
| Successful | 20 / 20 |
| Cost | $0.031 per verification |

An earlier version of this section said **6.9s**, taken from the model spike below. That
was a lab number for one image on one call, and production sends two. It has been
replaced rather than explained away: the spike is how we *chose* the model, and the
deployed p95 is how the product *performs*. Where the two disagree, the deployed number
is the one that counts, and it is 4.6s over the gate rather than 1.9s.

The spike, for the model decision only — three runs each, one label, live:

| Model | Single call | Split | Typography errors (of 20) | Can pin US inference |
|---|---|---|---|---|
| Opus 5 | 9.6s | 8.4s | 0 | yes |
| **Sonnet 5 (shipped)** | **9.0s** | **6.9s** | **0** | **yes** |
| Haiku 4.5 | 5.5s | 4.7s | 10 | **no — rejects `inference_geo`** |

Haiku is the only model that fits the gate. It is also the only one that cannot be pinned
to US inference — it rejects the parameter with a 400 — and over 20 samples per model it
was wrong 4/20 on header-bold and 6/20 on body-bold, **never abstained once**, and every
single error ran in the false-pass direction, on the one field with a zero-false-pass
gate.

The 5-second figure is a stakeholder's quote about adoption. Data residency is a
procurement condition, and those do not negotiate. A tool 4.6s slower still replaces a
30–40 second vendor and a paper checklist; a tool that cannot say where a federal agency's
label images were processed may not be deployable at all.

Reversible in one line, and the budgets follow the model automatically.

---

## What is not done

Stated plainly, because a reviewer will find these anyway and it is better they read them
here.

- **Dependencies are floors, not pins (SEC-10).** `pyproject.toml` gives every runtime
  dependency a `>=` and there is no lockfile and no hashes, so two builds a week apart can
  resolve differently. `pip-audit` and `npm audit` run on every commit and were clean when
  this was written, but they are advisory — they cannot fail a build, deliberately, because
  a gate that goes red on someone else's publication schedule gets switched off inside a
  week. The reasoning is in the `Dockerfile`; the consequence is that reproducibility here
  is weaker than the rest of this project claims.
- **The destroy-and-redeploy drill (LP-136) has not been run.** The app *is* deployed and
  `scripts/smoke.sh` passes against it, but the drill that proves the environment rebuilds
  from configuration alone — destroy the app, redeploy from a clean clone, smoke it — has
  not been performed. Its table stays **blank and labelled unrun** rather than filled with
  plausible output.
- **Cropped content is reported as Missing, not Unreadable.** The worst defect currently
  known, found by a photograph whose frame cuts off the right edge of the label. Class
  type, alcohol content and net contents are not in the picture, and the pipeline calls
  them **Missing** — a finding against the label and grounds to return an application.
  The truth is **Unreadable**, a statement about the photograph. Fixing it needs a signal
  the pipeline does not compute yet: whether the label runs past the frame boundary. Four
  of the nine Tier B misses are this one bug. See [`docs/accuracy.md`](docs/accuracy.md).
- **The 300-item batch has not been run.** A real 22-application batch completed on the
  deployed URL in 42s with no failures, which extrapolates to roughly 9.5 minutes for 300
  against a 10-minute goal. Extrapolation is not measurement, and rate limiting at that
  scale is exactly what an extrapolation cannot see.
- **Tier B is six photographs, three of them scored.** They earned their place — each
  found a real defect the synthetic fixtures could not — but six is a sample, not a
  corpus, none of the ground truth is hand-transcribed, and every image-quality threshold
  in the system is still calibrated against rendered PNGs. Tier B scores **71.4%** against
  Tier A's 100%; that 28.6-point gap is the honest answer to "does this work", and it is
  published in [`docs/accuracy.md`](docs/accuracy.md) rather than averaged away.
- **Accessibility is gated as data, in three browser engines.** axe runs in CI over all
  five screens with zero violations and nothing disabled; contrast is computed for all 21
  ink-and-ground pairs (worst 5.41:1 against a 4.5 floor), and both of UX-3's floors —
  16px of type, 44px of click target — are enforced by tests over the stylesheet.
  `web/e2e/a11y.spec.ts` drives the rest in Chromium, Firefox and a tablet viewport:
  keyboard navigation, a focus ring that is visibly painted rather than merely present,
  no keyboard trap, and the accessibility tree — accessible names, landmarks, heading
  order, `aria-describedby` resolving to a real node, focus landing on the first invalid
  field. 75 checks. Those gates are newer than the claims they check: the type gate
  shipped enforcing 15px while its own docstring said 16, and nothing checked the 44px
  rule at all until the evidence chips were found at 27px — see
  [`docs/prd-audit.md`](docs/prd-audit.md).
- **The 73-year-old test has not been run.** UX-1 asks for three cold users reaching a
  verdict with no instructions. That needs three people and cannot be simulated. The
  protocol is written and fixed in advance — [`docs/hallway-protocol.md`](docs/hallway-protocol.md)
  — so the success criteria cannot be adjusted to whatever happens on the day.
- **The second-look re-reader is built and switched off.** `api/reread.py` re-reads a
  low-confidence field from a crop of its own region, so text that was a few dozen pixels
  inside a downscaled frame becomes the whole image. It is bounded, it can only replace a
  reading with a strictly better one, and it has 20 tests. `reread_enabled` defaults to
  **False** for the same reason Tier 3 runs with no adjudicator: there is no measurement
  showing it helps. Tier A fixtures render cleanly and read at high confidence, so the
  trigger never fires and the eval cannot score it; Tier B is three bottles. Turning it on
  without that evidence would be trusting a second reading nobody scored, which is the
  move this product argues against everywhere else.
- **Geometric correction does not run on a real verification.** `api/pipeline/preprocess.py`
  and `api/pipeline/deskew.py` — deskew, perspective correction, contrast lifting — have
  **no caller in the request path**. What does run is ingest (magic-byte sniffing, EXIF and
  GPS stripping, re-encode, downscale to 2,576px), quality scoring, and the pre-gate. The
  model receives the cleaned original.

  Not an oversight, and not something to fix in a hurry: the skew estimator was measured
  returning **-45.0° on square-on photographs** and 34° on a good one, and `correct()`
  acts on that number at 1.5°. Wiring it as it stood would have rotated compliant labels
  on the strength of a number the estimator invented. The estimator is fixed now; the
  correction step still has not been proven to help on real photographs, and the vision
  model handles rotation natively — a warning sticker applied 90° sideways reads at 0.95
  confidence with no correction at all. `api/pipeline/limitations.py` marks these
  `runs_in_production=False`, and [`docs/robustness.md`](docs/robustness.md) measures them
  as an offline analysis rather than as shipped behaviour.

- **Tier-3 adjudication is not wired.** Gray cases fall through to Mismatch, which is the
  safe direction.
- **The warning's escalation path is built and unwired.** The interface, the trigger and
  the merge rules are tested against stubs; no adapter implements it.

### What the real photographs found

| Bottle | Defect | |
|---|---|---|
| Fireball, back label | The warning set in ALL CAPS — legal, and we returned the label for correction | fixed |
| Found North, back label | "DISTILLED IN CANADA" did not match an application saying "Canada" | fixed |
| Found North, back label | "BOTTLED BY X, CAMBRIDGE, WI" did not match an application saying "X, Cambridge, WI" | fixed |
| Courtyard rosé, back label | The skew estimator reported **-45.0°** on a square-on photograph | fixed |
| Fireball, back label | The same estimator reported **34°** on a good photograph, and `correct()` acts at 1.5° | fixed |
| Courtyard rosé, back label | A producer printed across two lines matched nothing, because the application's name and address are joined with a comma the label does not print | fixed |
| Bacardi 151, back label | Content cropped out of frame reported as **Missing** — a finding against the label — rather than Unreadable | **open** |

Four of these are one shape: **the label prints the value inside a phrase, the
application holds the bare value, and we called that a mismatch.** The fourth arrived
after the first three were fixed and the fix was believed complete — the synthetic fixture
pinning it happened to carry a comma the real label does not, so the fixture and the code
agreed with each other about a detail neither had considered. That is the argument for
Tier B in one sentence. All four are fixed and pinned in
`tests/test_real_photo_regressions.py`. A torn beer label separately confirmed
the extractor does not recite the warning from memory — occlude a line and it returns
nothing, `legible=False`, rather than completing it.

---

## Observability

The vendor pilot that came before this one died of *unexplained* slowness: 30 to 40
seconds a label, with nothing anyone could point at. Everything in this section exists so
that never has to be guessed at again — and so anything said about PERF-1 is evidence
rather than assertion, including the part where the gate is not currently met.

Three artifacts, in the order you would reach for them:

| You want to know | Read |
|---|---|
| What happened on one request | the log line carrying its `request_id` |
| How fast it is across many requests | `scripts/rollup.py` over a log file |
| How fast the deployed URL is, right now | `scripts/timed_run.py` against that URL |

### The log

One JSON object per line, on stdout. Nothing else. Fly captures stdout directly, so
`fly logs` is the log — there is no agent, no shipper, and no second place for a line to
be.

```json
{"duration_ms": 9612, "event": "stage_complete", "ok": true, "request_id": "req_9f3c1a4b7e2d8055", "stage": "extract", "ts": 1786464776.227}
```

Keys are sorted, so two runs diff meaningfully. Every line carries `event` and `ts`
(epoch seconds, 3dp). Every line emitted *during a request* also carries `request_id`.

#### Correlation

`request_id` is assigned by the middleware in `api/main.py` before routing, echoed to the
caller in the `X-Request-ID` header, and returned in the response body as `request_id`.
The reference an agent reads off the screen is the string to grep for.

It is **generated, never accepted from a header**. An id the caller chooses is an id that
can be forged to blend two agents' requests together in the log, and correlation is the
only reason to have one.

Lines written outside a request — startup, config warnings — carry no `request_id` at all
rather than an empty one.

Batch work adds `job_id` and `item_id`, which correlate an item back to its job. A worker
thread inherits no ContextVar and therefore no request id, so those two names are how a
batch line is attributed at all.

#### Levels

| Level | Means | What to do |
|---|---|---|
| `INFO` | Something happened, and it is what should happen. | Nothing. This is the stream you compute p95 from. |
| `WARNING` | Degraded but handled. The agent still got an honest answer. | Watch the rate. A rising `provider_retry` is the shape of an outage forming. |
| `ERROR` | A failure nobody chose. | Look at it. `unhandled_exception` should be zero. |

There is no `DEBUG` tier and no `CRITICAL`. Three levels that mean something beat five
that get used interchangeably.

#### Events

Every event this service can emit. This table is generated from `api.logging.EVENTS`, and
`tests/test_logging.py` fails if the code emits an event that is not here, or if this
lists one nothing emits.

<!-- LOG-EVENTS:BEGIN -->

| Event | Level | Meaning |
|---|---|---|
| `app_started` | INFO | Process is up and serving. |
| `batch_exported` | INFO | A batch result CSV was produced. |
| `batch_item_complete` | INFO | One batch item reached a verdict. |
| `batch_item_failed` | WARNING | One batch item failed; the rest of the job continues. |
| `batch_item_retry` | WARNING | One batch item is being retried. |
| `batch_item_unrecorded` | ERROR | A batch item finished but its result could not be stored. |
| `batch_purged` | INFO | A batch job's data passed its TTL and was deleted. |
| `batch_queued` | INFO | A batch job was accepted. |
| `batch_recovered` | INFO | Unfinished batch items were picked back up after a restart. |
| `batch_retry` | INFO | Failed items in a batch were requeued. |
| `circuit_breaker` | WARNING | The provider circuit opened or closed. Opening is the warning; closing rides the same event. |
| `config_incomplete` | WARNING | A required setting is missing; /ready is red. |
| `cost_model_unknown` | WARNING | A verification ran on a model with no entry in the price list; cost was estimated at the most expensive known tier. |
| `image_scored` | INFO | Deterministic image-quality scores for one uploaded image. |
| `log_containment_reasserted` | WARNING | Something replaced the log record factory and traceback containment was reinstalled (SEC-4). |
| `provider_bbox_dropped` | WARNING | An evidence box was unusable and was discarded rather than guessed. |
| `provider_call` | INFO | One model call, with its usage. |
| `provider_extract` | INFO | The whole extraction across every image. |
| `provider_price_unknown` | WARNING | The adapter priced a call at the unknown-model tier; the cost line is an over-estimate, not a quote. |
| `provider_retry` | WARNING | A provider call failed and is being retried. |
| `provider_typography_unusable` | WARNING | Typography signals could not be judged; the warning field fails closed. |
| `provider_unavailable` | WARNING | The provider could not be reached; answered 503. |
| `rate_limit_trusts_client_header` | WARNING | The rate limiter is identifying clients by a header. If no proxy overwrites that header, the limiter can be bypassed. |
| `rate_limited` | WARNING | A request was refused with 429. Carries the lane and a correlation ID. |
| `request_complete` | INFO | One HTTP request finished. Carries status and total duration. |
| `request_failed` | INFO | A request ended in the error taxonomy. Carries kind, code, status. |
| `retention_compaction_incomplete` | WARNING | The database was not compacted, so deleted content may remain in unused pages. |
| `retention_purge_failed` | WARNING | A sweep could not delete expired data; it stays on disk until the next sweep. |
| `retention_purged` | INFO | A sweep deleted expired data. Carries jobs removed and bytes reclaimed. |
| `retention_started` | INFO | The retention sweeper is running. Carries the TTL and the sweep interval. |
| `retention_state_unwritable` | WARNING | The retention bookkeeping file could not be written; the next sweep redoes work. |
| `retention_sweep_failed` | ERROR | A whole retention cycle raised. The loop survives, but data is outliving its TTL. |
| `security_installed` | INFO | The security middleware stack is installed. Carries the rate-limit ceiling. |
| `stage_complete` | INFO | One pipeline stage's duration (OPS-1). One line per stage per request. |
| `unhandled_exception` | ERROR | Something broke that nobody anticipated. The agent got a sentence, not a trace. |
| `unhandled_thread_exception` | ERROR | A worker thread died uncaught. The batch pool's leak path, and stderr-direct otherwise. |
| `verification_cost` | INFO | Tokens and dollars for one verification (OPS-4). |
| `verify_complete` | INFO | A verification produced a recommendation. |
| `adjudication` | INFO | Tier 3 saw at least one gray row. Carries how many were considered, how many were judged and how many changed, so the trigger rate is a number rather than an impression (LP-221). |
| `verify_over_budget` | INFO | The request budget expired; partial result returned as Needs review. |
| `prepare_complete` | INFO | A label was read ahead of its application, while the agent was still typing. No verdict was reached. |
| `prepare_unavailable` | WARNING | The provider was down when a label was read ahead. Nothing is shown to the agent. |
| `prepared_reading` | INFO | Whether a verification used a reading taken earlier, or declined it and read the label again. |
| `reread` | INFO | One or more fields were read again from a crop of their own region (LP-325). Carries how many were eligible, how many were re-read and how many improved. |
| `reread_failed` | WARNING | A re-read call failed. The first reading stands and the verification is unaffected — failing to improve is not failing to verify. |
| `verify_pregated` | INFO | Images too poor to read; returned Unreadable with zero model calls. |

<!-- LOG-EVENTS:END -->

#### Fields

**A log line may carry only these field names. Anything else is dropped before the line
is written, and the line goes out at ERROR naming what was refused.**

That is the mechanism, not a convention. SEC-4 says logs carry ids, timings, token counts
and verdict summaries — never label text, never extracted values, never image bytes. A
comment saying so is a rule that erodes; an allowlist enforced in the writer is a rule
that holds. It used to raise instead, which sounds stricter and was not: one unlisted
counter in `verify.py` turned into a 500 on every label whose class or producer
disagreed, while the check that would have caught it — an AST walk over every call site
in `api/`, now in `tests/test_logging.py` — did not exist. Every name below is an identifier, a measurement, or a category. None of
them can contain something read off a label.

Adding a field is deliberate: put it in `api.logging.ALLOWED_FIELDS` with a reason.
Working around the check is not a shortcut, it is a compliance failure.

<!-- LOG-FIELDS:BEGIN -->

| Field | What it carries |
|---|---|
| `attempt` | Which retry this is, from 1. |
| `blur` | Image sharpness score, 0–1, higher is better. |
| `changed` | How many rows Tier 3 actually moved. A count, never a row's value. |
| `considered` | How many rows were eligible for Tier 3. A count. |
| `bytes` | Size of something in bytes. Never its contents. |
| `cache_creation_tokens` | Prompt-cache tokens written on this call. Priced at 1.25x input. |
| `cache_read_tokens` | Prompt-cache tokens read on this call. Priced at a tenth of input. |
| `code` | Machine-readable error code from the taxonomy, e.g. `file_too_large`. |
| `commodity` | `spirits`, `wine` or `malt`. |
| `confidence` | Extractor confidence for a field, 0–1. |
| `count` | How many of the thing this line is about. |
| `dropped` | Field names this logger refused to write. Identifiers from our own source, never values. |
| `duration_ms` | Elapsed milliseconds. |
| `event` | The event name. Always present. |
| `exposure` | Image exposure score, 0–1, higher is better. |
| `field` | Which of the seven label fields, e.g. `government_warning`. Never its value. |
| `fixture` | Name of a built-in test fixture, in sample mode only. |
| `glare` | Image glare score, 0–1, higher is better. |
| `height` | Pixel height. |
| `image_index` | Which uploaded image, from 0. |
| `input_tokens` | Prompt tokens billed on this call, excluding cache reads. |
| `item_id` | One item inside a batch job. |
| `job_id` | One batch job. |
| `judged` | How many rows Tier 3 was actually asked about. A count. |
| `kind` | Error taxonomy class: `user`, `image`, `provider` or `internal`. |
| `media_type` | Detected content type, e.g. `image/png`. Sniffed, never taken from a filename. |
| `model` | Model id the call was made against. |
| `ok` | Whether the thing this line describes succeeded. |
| `output_tokens` | Completion tokens billed on this call. |
| `provider` | Which extraction provider served this, e.g. `anthropic` or `fake:spec`. |
| `quality` | Image pre-gate outcome: `ok`, `degraded` or `hopeless`. |
| `reason_code` | Why a decision went the way it did, as a category. |
| `recommendation` | `ready_to_approve`, `needs_review` or `return_for_correction`. |
| `request_id` | Correlation id. On every line emitted during a request. |
| `skew_deg` | Estimated page rotation in degrees. |
| `stage` | Pipeline stage name, e.g. `extract`. |
| `status` | HTTP status code. |
| `tier` | Which comparison tier decided a field: 1, 2 or 3. |
| `usd` | Estimated list-price cost in US dollars. |
| `verdict` | Per-field outcome, e.g. `match`, `unreadable`. Never the value compared. |
| `width` | Pixel width. |

<!-- LOG-FIELDS:END -->

#### Nothing else in the process can print a traceback either

The allowlist governs *our* log calls. It cannot govern uvicorn's, asyncio's, or a
library's — and the one thing those reliably print is a traceback, which contains the
exception's message. That is not theoretical here: the pipeline runs in a worker thread,
and a `pydantic.ValidationError` raised while validating an extraction quotes the label
text that failed validation.

Containment is a second layer, in `api/security.py`, installed by `harden()`:

| Channel | Covered by |
|---|---|
| An unhandled exception on the request path | A containment middleware installed *outside* Starlette's `ServerErrorMiddleware`, so it catches the exception before the server can format it. It logs one scrubbed line naming only the exception class and returns the taxonomy 500 with a request id. |
| `logger.error(..., exc_info=True)` from any logger | A process-wide `LogRecord` factory that strips `exc_info` and `stack_info` from every record created anywhere. |
| An uncaught exception on the main thread | A scrubbed `sys.excepthook`. |
| An uncaught exception on a worker thread | A scrubbed `threading.excepthook` — the batch pool's path, which writes to stderr with no logging involved. |
| `logger.error("failed: %s", exc)` or `logger.error(exc)` | `api.logging.scrub_exception_arguments`, called from that same factory. No traceback is involved on this path, so stripping `exc_info` does nothing for it. Exceptions nested inside lists, tuples, sets and dicts are replaced too, because that is the shape a batch worker collects them in. |

**There is exactly one record factory in this process, on purpose.** Two independent
factories look like belt and braces and are a bug: each captures the other as "the
original", so whichever is uninstalled first silently disables the other while the
liveness check still reports true.

The retention timer re-asserts the guard on every sweep and logs
`log_containment_reasserted` when it has to. A library that installs its own
`logging.setLogRecordFactory` after startup would otherwise switch containment off for the
life of the process with nothing to notice.

What survives to stdout is the exception's *type*:

```
Exception in ASGI application | ValidationError suppressed: traceback withheld (SEC-4)
```

Visible, not silent — a service that is failing must not look like a service that is
quiet.

**What is still not covered, stated rather than papered over.** The gap is the shape of
the guard, not an edge case: containment intercepts the logging module, the two exception
hooks and the ASGI error path, so anything that writes to the file descriptor directly
goes around it.

- A bare `print()` or `sys.stdout.write()` anywhere in the process.
- A subprocess inheriting stdout and writing to it.
- Label text interpolated as a plain format argument with no exception involved —
  `logger.info("read %s", brand)`. Covering that would mean redacting all foreign output,
  which would delete the startup and access lines an ops team reads. Content someone
  chose to log is the allowlist's job, and the allowlist raises on it.

Nothing in this repository does any of these, and nothing on the label path writes to
stdout by a route other than `logging`. `LABELPROOF_DEBUG_TRACEBACKS=1` turns the
process-wide layer off for local debugging; there is no switch on the allowlist itself.

Tests: `tests/test_logging.py`, `tests/test_security.py` — including one that demonstrates
the leak is real with containment removed.

### Timings

Every `/verify` response carries the stage breakdown (LP-063), because PRD §Observability
requires it *surfaced in the UI* — a number that lives only in stdout cannot be shown to
the agent deciding whether to trust the tool.

```json
"timings_ms": { "ingest": 94, "quality": 44, "preprocess": 138,
                "extract": 9612, "compare": 2, "adjudicate": 0, "total": 9885 }
```

> One honest caveat about that sample: **the live API returns `"adjudicate": null`, not
> `0`.** Tier-3 adjudication does not run in this build, and the difference between null
> and zero is the subject of the second rule below. It is written as a number here only so
> that `tests/test_timing.py` can check the example's own arithmetic
> (`preprocess == ingest + quality`, and `total` at least the sum of the measured stages)
> without tripping over a null.

| Field | What it measures |
|---|---|
| `ingest` | Sniff, EXIF-orient, strip all metadata, re-encode, downscale. |
| `quality` | Blur, exposure, glare and skew scoring. No model call. |
| `preprocess` | **Roll-up of `ingest + quality`.** See below. |
| `extract` | Every vision call, wall-clock. Concurrent across images, so this is `max`, not `sum`. |
| `compare` | The deterministic rules engine. Pure functions; usually 0–2ms. |
| `adjudicate` | Tier-3 text adjudication. **Not implemented in this build — always `null`.** |
| `total` | The whole request, measured by the outermost clock. |

`ingest` and `quality` are measured inside `api.verify.prepare_images`, the single
pre-model path — ingest, quality scoring and the pre-gate — that both Verify Now and Batch
call (LP-321). It returns the two durations it measured and the request timer records
them, so the same stage names mean the same thing on both entry points and the pre-gate
cannot be true of one and quietly false of the other.

Three things about this table are load-bearing.

**`preprocess` is a roll-up, so do not add the column up.** PRD §Observability names the
stages upload → preprocess → extract → compare → render. This pipeline measures the two
halves of preprocessing separately, and `preprocess` reports their sum so the PRD's
vocabulary maps onto real numbers. It is derived once, in `api.timing.seal`, and it is
never a term in `total` — `total` comes off the outermost clock, so the roll-up cannot
double-count into it. Summing every field in the JSON above would. (There is no separate
deskew/perspective pass in this build; if one is added it becomes a third part of the
roll-up, in `api.timing.PREPROCESS_PARTS`.)

**A stage that did not run reports `null`, not `0`.** This is the same rule as the one
above, seen from the other side: `0` reads as "instant", and `"adjudicate": 0` would invite
a reader to conclude Tier-3 adjudication ran and cost nothing. It does not run at all.
`api.timing.UNIMPLEMENTED_STAGES` lists the stages the API declares but this build never
executes, and a test fails if one of them starts producing a number without coming off the
list.

**`total` is measured, not derived.** It comes from a clock started before the request is
parsed and read after the last verdict is computed — never from adding the stages up.
Adding them up would silently omit whatever nobody instrumented, and the gap between "what
we measured" and "how long it took" is exactly the number worth having.

`tests/test_timing.py` puts an independent stopwatch around a real HTTP request and holds
the server's own claim to it. PRD §232: *if the number on the screen and the number on the
stopwatch disagree, the stopwatch wins.*

### How fast it actually is, and where that leaves PERF-1

PERF-1 is a p95 of 5 seconds upload-to-verdict, quoted from a stakeholder as a hard
adoption gate. **This build does not meet it on the configured model, and nothing here
should be read as claiming it does.**

Median single-call extraction latency, measured against the live API on one 2576px label
by `scripts/spike_latency.py` and pinned in `api.config.MEASURED_EXTRACTION_MS`:

| Extraction model | Median call | Split into two concurrent calls | Input / output per MTok | Can pin `inference_geo` |
|---|---|---|---|---|
| `claude-sonnet-5` *(shipped default)* | ~9,000 ms | ~6,900 ms | $3 / $15 | Yes |
| `claude-opus-5` | ~9,600 ms | not measured | $5 / $25 | Yes |
| `claude-haiku-4-5` | ~5,500 ms | ~4,700 ms | $1 / $5 | **No — 400s the parameter** |

Our own non-provider work — ingest, quality scoring, preprocessing, rules, serialization
— is about **1,120 ms**, measured on the deployed app: ingest ~260, quality ~300,
preprocess ~570, compare ~1, against an extract of ~6,800. That is roughly **14%** of the
request, and `api.config._OVERHEAD_MS` reserves 1,500 ms against it.

This number has now been wrong twice, both times too small, and both corrections are
recorded rather than quietly applied. It first said **130 ms**, which was a single-image
figure while production sends two. It was then corrected to **570 ms** — which is the
`preprocess` row alone, mistaken for the total, by someone reading their own table too
fast. The itemisation was printed correctly beside it both times and sums to ~1,120.

The conclusion does not move: dedicated cores might halve our share, taking perhaps 560 ms
off a 9.6 s p95 for 8x the machine price, and the model is still 86% of the request. What
does move is how confidently the figure should be quoted — twice wrong in the same
direction is a pattern, not an accident, and the per-stage table in
[`docs/perf-deployed.md`](docs/perf-deployed.md) is the thing to read rather than this
sentence.

The honest reading:

- The shipped configuration is **Sonnet 5**, split into two concurrent extraction calls
  (`api/provider/anthropic_adapter.py`, LP-280). In the lab that measured ~6.9 s. **In
  production it measures a 9.6 s p95** — 20 runs, [`docs/perf-deployed.md`](docs/perf-deployed.md).
  The split is real and it helps; two concurrent calls still cost the slower of the two,
  and the slower of two is not the median of one. **PERF-1 is not met**, by 4.6 s, and no
  number in this repository should be read as claiming otherwise.
- Haiku 4.5 is the only model that fits the gate — ~4.7 s split — and it was **rejected
  deliberately**, for two reasons a federal deployment cannot spend. Haiku **rejects
  `inference_geo` with a 400**, so US data residency cannot be pinned on it at all
  (`api.provider.anthropic_adapter.supports_inference_geo` / `describe_residency`). And in
  the typography spike, over 20 samples per model, Haiku got the header-bold judgement
  wrong 4 times and the body-bold judgement wrong 6 times where Sonnet 5 and Opus 5 got
  all three signals — header-bold, body-bold, all-caps — wrong zero times. Haiku never
  abstained, and every one of its errors was a **false pass**: the direction that ships a
  non-compliant label, on the one field carrying a zero-false-pass gate.
- So the trade is stated rather than hidden. The 5 s figure is a stakeholder quote about
  adoption; data residency is a procurement condition, and procurement conditions do not
  negotiate. A tool 4.6 s over the gate still replaces a 30–40 s vendor pilot and a paper
  checklist. A tool that cannot say where a federal agency's label images were processed
  may not be deployable at all.
- **The gate has been measured end-to-end on the deployed URL, and it is missed.**
  20 consecutive timed runs against <https://labelproof.fly.dev>: p50 8.5s, **p95 9.6s**,
  max 9.9s, 20/20 successful. Every run, with request ids and stage breakdowns, is in
  [`docs/perf-deployed.md`](docs/perf-deployed.md) — the file rather than this sentence is
  the evidence.

The latency target is reported, never enforced. `LABELPROOF_LATENCY_TARGET_MS` (5000) is
what the product is *held to*; the request budget and the provider timeout default from
the model's measured latency instead. Enforcing the target as a timeout is what once
returned `provider_unavailable` on every single verification — a 4,000 ms timeout against
a model whose calls take 9.4–10.1 s. Startup warns and `/ready` says so when the
configured model cannot meet the target.

---

## Ops runbook

Four things an operator does with this service. Each is one command.

### Read the log

Locally the log is stdout. On Fly it is `fly logs`, which is the same stream.

```bash
# Follow it.
fly logs -a labelproof

# Keep a file to roll up later.
fly logs -a labelproof --no-tail > /tmp/labelproof.jsonl
```

Every line is one JSON object, so `jq` works without any parsing:

```bash
# One request's whole story, in order. This is the id the agent reads off the
# screen under "Check reference", and the one in the X-Request-ID header.
jq -c 'select(.request_id == "req_9f3c1a4b7e2d8055")' /tmp/labelproof.jsonl

# Only what went wrong, with the taxonomy code that explains it.
jq -c 'select(.event | test("failed|unavailable|exception|retry"))' /tmp/labelproof.jsonl

# The slowest verifications first.
jq -c 'select(.event == "verify_complete")' /tmp/labelproof.jsonl \
  | jq -s 'sort_by(-.duration_ms) | .[:10]'

# Every stage of every request, as a table.
jq -r 'select(.event == "stage_complete")
       | [.request_id, .stage, .duration_ms] | @tsv' /tmp/labelproof.jsonl
```

**There is no label text in there and there cannot be** — see the field allowlist above.
That is also why nothing logs a path or a filename: an uploaded filename can carry a brand
name.

### Read the timings

```bash
# p50/p95 per stage, plus the error summary and the cost total.
.venv/bin/python -m scripts.rollup /tmp/labelproof.jsonl

# Straight off the deployment, no intermediate file.
fly logs -a labelproof --no-tail | .venv/bin/python -m scripts.rollup

# Machine-readable, for a dashboard or a CI check.
.venv/bin/python -m scripts.rollup /tmp/labelproof.jsonl --json
```

How to read what comes back:

| Row | Means | If it is high |
|---|---|---|
| `preprocess` | Decode, strip, downscale, quality score. Expect tens of ms. | The uploads are arriving full-size. The client is supposed to downscale to 2576px before sending. |
| `extract` | The vision call(s). The dominant term, by a lot. | This is the model. Concurrency across images is already `max` not `sum`; the levers left are effort, model tier and prompt caching. |
| `compare` | The rules engine. Expect 0–2ms. | Something in `api/rules/` started doing I/O. It is not supposed to be able to. |
| `verification (POST /verify)` | Server-side total. **This is the PERF-1 series.** | It is the model. See *How fast it actually is* above and the measured table in `api/config.py`. |
| `request (all HTTP)` | Every request including `/health`. | Only useful for spotting a slow static asset. Never quote it as the p95. |

The rollup flags any percentile drawn from fewer than 20 samples. That flag is not
decoration — a p95 over five runs is the maximum with a better name, and PERF-1 is an
adoption gate.

Server-side time is a **floor** for what a person with a stopwatch sees. It excludes
upload, network and render. For the whole number:

```bash
# 20 runs against whatever URL, with the full sample printed.
.venv/bin/python -m scripts.timed_run https://labelproof.fly.dev \
  --runs 20 --note "fly iad, min_machines_running=1, warm ~4h" \
  --out docs/perf-deployed.md
```

Commit the resulting file. It exists: [`docs/perf-deployed.md`](docs/perf-deployed.md),
20 runs against the live URL at commit `9b04ed7`. That file rather than a sentence in a
status update is the evidence: it carries the URL, the timestamp, the commit, the payload
size, every individual run, and whether the server was in sample mode.

Exit codes: `0` measured, `1` nothing succeeded, `2` the server's clock and the caller's
stopwatch disagreed — see *The honesty check* below.

### Read the cost

Every verification writes one `verification_cost` line.

```bash
# What a run of labels cost.
jq -s 'map(select(.event == "verification_cost"))
       | {n: length, usd: (map(.usd) | add), mean: (map(.usd) | add / length)}' \
  /tmp/labelproof.jsonl
```

Or let the rollup do it — the **Cost** section of its report carries the total, the mean,
the p95, mean tokens in and out, mean cached reads and mean cache writes.

Five things to know before quoting a cost figure:

- **It is list price, computed locally.** The price table lives in `api.timing.PRICES`,
  keyed by model: Opus 5 at $5/$25 per MTok in/out, Sonnet 5 at $3/$15, Haiku 4.5 at $1/$5
  (Anthropic first-party list, checked 2026-08-11). It is not a bill, and it does not know
  about discounts. Sonnet 5's introductory $2/$10 rate is deliberately *not* used — a cost
  analysis built on a rate that expires in three weeks has a short shelf life, and
  over-stating is the safe direction for a number someone budgets against.
- **The price follows the configured model.** `LABELPROOF_EXTRACTION_MODEL` is an
  environment variable, and Opus 5 and Haiku 4.5 are a 5x spread on both counters. A model
  with no entry in the table is priced at the most expensive known tier and logged as
  `cost_model_unknown` (or `provider_price_unknown` when the adapter is the one guessing)
  — guessing low would put an under-stated number into a budget.
- **Three token counters, three prices.** `input_tokens` excludes both cache counters.
  Cached reads cost a tenth of an input token; cache writes cost 1.25x one. All three are
  on the cost line, and the adapter reports all three
  (`api.provider.anthropic_adapter._usage_from`).
- **Sample-mode runs cost nothing** and would drag any average down. The line carries
  `provider`, the rollup names them, and it says so in the report rather than folding them
  in.
- **Cost is per verification, not per image.** One `/verify` with a front and a back is one
  line covering both calls.

The rollup keeps a standing guard on the last of those: cached reads in a window with no
cache writes anywhere is the signature of a provider that reports
`cache_read_input_tokens` but not `cache_creation_input_tokens`. Every cached prefix has to
be written once before it can be read, so that combination is not a warm cache, it is an
unpriced one. When the rollup sees it, it stamps the cost section as a lower bound rather
than letting the figure look complete.

### The honesty check

PRD §232: *if the number on the screen and the number on the stopwatch disagree, the
stopwatch wins.* Two mechanisms hold the service to that.

`tests/test_timing.py` wraps a real `POST /verify` in an independent stopwatch and asserts
the server's own `timings_ms.total` matches it — and, with a provider that sleeps a known
duration, that the total actually contains the extraction rather than being computed
before it. A fabricated total, a total measured too early, or a total that omits a stage
all fail. There is also a test that makes that check go red on purpose, by making the
timer report as if the clock had stopped at the top of the request: a guard nobody has
watched fail is a guard nobody knows works.

The front end carries **tripwires, not tests** — substring assertions on `VerifyNow.tsx`
read as text. They cannot prove the rendered number is right; they go red the day someone
deletes the client clock, points the banner at the server's total, or wires the progress
animation to the result card. A real assertion on the rendered string would be a `vitest`
test in `web/src`, which does not exist yet.

`scripts/timed_run.py` does the same across a real network boundary and prints the gap per
run. The gap is always positive: the client's stopwatch contains the server's work plus
upload and network. If it ever goes negative the report says so in bold, withholds the
claim, and the command exits `2`.

**The elapsed time on the result card is the client's wall clock**, measured from submit to
response, not the server's `timings_ms.total`. That is deliberate. The server's number is
always the smaller of the two, and a product whose entire argument is speed must not report
less time than actually passed. The server number is in the response body for anyone who
wants the breakdown; the headline number is the one the stopwatch would agree with.

### When something is wrong

| Symptom | Where to look | Likely cause |
|---|---|---|
| `/ready` is red | `config_incomplete` lines | A missing environment variable. `/health` stays green on purpose — the process is fine. |
| `/ready` says `sample_mode` | `verification_cost` lines with `provider: "fake:*"` | No API key. The service replays fixtures and says so on every verdict; it is not verifying uploads. |
| `/ready` warns about the latency target | Startup lines | The configured model's measured latency is above `LABELPROOF_LATENCY_TARGET_MS`. This is the expected state on the shipped Sonnet 5 default — see *How fast it actually is*. |
| p95 crept up | `extract` in the rollup | Almost always the model. Everything else is tens of milliseconds. |
| Rising `provider_retry` | Degraded-but-handled table | An outage forming. `circuit_breaker` opening is the next line you will see. |
| Unverified rate rising, error rate flat | Verification-outcome table | Pre-gate or budget stops. Both answer 200 — the service is up and is not checking labels. |
| Any `unhandled_exception` | Its `request_id`, then that request's other lines | A bug. The traceback is deliberately not printed (SEC-4); the exception type is on the line and the request id tells you what it was doing. |

---

## Network egress

NET-1: an agency network admin should be able to allowlist this app from one table.
Marcus: *"our network blocks outbound traffic to a lot of domains."*

| Destination | Who calls it | Why | Blocked means |
|---|---|---|---|
| `api.anthropic.com` (HTTPS 443) | The server only, via `api/provider/anthropic_adapter.py` | The vision extraction call. The single external dependency at runtime. | Every verification returns `provider_unavailable` (503) and says so. Sample mode (`LABELPROOF_FAKE_PROVIDER=1`) still works with no network at all. |
| The hosting platform's own control plane | The platform | Deploys, logs, health checks. Not the app. | Deploys fail; a running container keeps serving. |

That is the whole list. **The browser never contacts the provider** — all AI calls are
server-brokered (NET-2), so no label artwork leaves a user's machine for anywhere but this
service. The SPA loads no fonts, scripts, styles or images from any external host; every
request it makes is a relative path to its own origin, and the CSP (`default-src 'none'`,
no external host anywhere in the policy) enforces that rather than merely intending it.

---

## Security, privacy, and data retention

> **Wiring.** Every control below is installed by one call — `api.security.harden(app,
> config)` — made from the app factory in `api/main.py`, after `_install_middleware` so the
> security layers end up outermost. If that call is ever removed, none of it is live: no CSP,
> no rate limiting, no CORS enforcement, no exception containment, no retention sweeper. Five
> tests in `tests/test_security.py` take the app exactly as the process serves it and assert
> the controls are on; they are the only tests here that can see the wiring, so do not give
> them their own `harden()` call to make them pass.

Marcus set the posture: *"there's PII considerations, document retention policies, the usual
federal compliance stuff. But for a prototype? Just don't do anything crazy."* This section is
the literal answer — prototype-grade implementation, production-aware documentation. Every
claim here has a test behind it, named at the end of its paragraph.

### No PII, by design

There are no accounts, no names, no email addresses, and no login. The app processes label
artwork and the application field data an agent types in, and nothing else. All demo data
and every Tier A fixture is synthetic, and nothing in this repository came from a real
applicant or a real TTB submission.

The one exception is deliberate and worth naming: **Tier B is six photographs of retail
bottles** in `golden/tier_b/photos/` — Fireball, a torn IPA, Found North, a Courtyard
Winery rosé, a Bacardi 151 shot into glare, and a growler. Three of the six are declared
as scored rows in `golden/tier_b/manifest.json`; the other three are pinned as unit
regressions in `tests/test_real_photo_regressions.py`. All are pictures of products
already on a shelf, not applicant material, and they carry no personal data.

Between them they have found six real defects, listed below. That is the argument for
Tier B existing. It is also a corpus of six, and not a sample from which an accuracy claim
about real photographs can be made — which is why Tier B is reported separately, never
averaged into Tier A, and never gates CI.

### What is stored, and for how long

| Artefact | Where | Lifetime |
|---|---|---|
| A **Verify Now** upload | Nowhere. Read into memory, ingested, re-encoded, discarded. | The request |
| A **Verify Now** result | Returned in the response body. Never written to disk. | The request |
| A **batch** manifest row | `jobs.db` (SQLite, local disk) | TTL |
| A **batch** label image | `<storage>/batches/<job_id>/` | TTL |
| A **batch** result | `jobs.db` | TTL |
| Logs | stdout | The platform's log retention |

**TTL is 24 hours by default** and is set by `LABELPROOF_RETENTION_HOURS`. A timer
(`api/retention.py`) sweeps on startup and then every `LABELPROOF_RETENTION_SWEEP_SECONDS`
(default 900), so purging does not depend on anyone using the app — a container left running
overnight still empties itself. **Worst-case artefact lifetime is therefore the TTL plus one
sweep interval**, 24h 15m at the defaults. That is stated rather than rounded down to 24h.

Deleting rows from SQLite does not remove their bytes: `secure_delete` is off by default, so a
`DELETE` leaves the data in freed pages and `strings jobs.db` still finds every brand name. The
sweep therefore follows a purge with `VACUUM` and `PRAGMA wal_checkpoint(TRUNCATE)`, and the
test reads **every byte of every file** under the storage root — database, write-ahead log and
all — to assert the brand name, the producer address and the artwork are gone.

That cleanup is an *obligation*, and the sweep works out whether it is owed **from the
database file rather than from whoever did the deleting**. It records a fingerprint (size and
mtime of `jobs.db` and its sidecars) each time a compaction verifiably finishes, and compacts
unless the file still matches. A delete cannot avoid writing to the database, so there is no
delete path — present or future — that can leave residue this does not notice.

That matters because not every delete is the sweep's. `api/routes/batch.py` purges on its way
into `POST /batch`, and an earlier design that had the deleter record the obligation missed it
entirely: rows gone, nothing marked, next sweep reporting clean over a database from which
`strings` still yielded every brand name. Inserts move the fingerprint too, so this compacts
somewhat more often than strictly necessary — the correct direction to be wrong in, at the cost
of a VACUUM of a small database on a fifteen-minute timer.

A compaction is only treated as finished when VACUUM did not raise, `wal_checkpoint(TRUNCATE)`
reported not-busy (it **returns** `(busy, log, checkpointed)`; it does not raise, and an
earlier version read success into a measured `(1, 17, 0)`), and the WAL is measurably empty.
One that loses the database lock to a running batch warns
(`retention_compaction_incomplete`) and is retried next sweep. The sweeper announces itself
with `retention_started`, reports each cycle with `retention_purged`, and every way it can
fall short — `retention_purge_failed`, `retention_state_unwritable`,
`retention_sweep_failed` — is its own event, because "data is outliving its TTL" is not a
thing to infer from silence. Tests: `tests/test_retention.py`.

**Known gap, another file's to close.** `GET /batch/{id}` and `GET /batch/{id}/export.csv`
serve a job for as long as its row survives, which is at least TTL + one sweep interval — while
the 404 copy tells the agent batches are deleted after 24 hours. Deleting on a timer and
refusing to serve are different guarantees and only the first is implemented. `is_expired()` in
`api/retention.py` is the single predicate the read paths should import so the two cannot drift.

### Uploads are treated as hostile

Content type is sniffed from magic bytes, never from the filename. Size, count and page caps
are enforced before decode. **All metadata is stripped, including GPS** — phone photos of
bottles carry the location they were taken. Every image is re-encoded on ingest, which
neutralises polyglot files, and PDFs are rendered through a page-capped path. This is the
first half of `api.verify.prepare_images`, so it is the same boundary on Verify Now and on
Batch (LP-321). Tests: `tests/test_ingest.py`.

### Nothing from a label reaches the logs (SEC-4)

Two layers, both documented in full under
[Observability → Fields](#fields) and
[Nothing else in the process can print a traceback either](#nothing-else-in-the-process-can-print-a-traceback-either):

1. **An allowlist enforced in the writer, and a build-time gate over every caller.**
   `api/logging.py` writes only the field names in the table above and drops anything else,
   loudly. `tests/test_logging.py` parses every `applog` call under `api/` and fails the
   build on an unlisted keyword — including call sites no test executes. There is no
   channel through which a brand name can be logged deliberately.
2. **Process-wide traceback containment.** The allowlist governs `applog.log` and nothing
   else, so a second layer covers the way a leak would actually happen — a traceback. A
   `pydantic.ValidationError` on the extraction path renders `input_value=...`, which is the
   label the model just read. Containment replaces the log record factory, both excepthooks
   and the ASGI error path, reducing any traceback from any library on any thread to
   `<ExceptionType> suppressed: traceback withheld (SEC-4)`.

The section above also states, plainly, what the guard does *not* cover: bare `print()`,
direct writes to `sys.stdout`/`sys.stderr`, subprocess output on inherited descriptors, and
label text interpolated as a plain string argument with no exception involved. Nothing in this
repository does any of them.

`LABELPROOF_DEBUG_TRACEBACKS=1` turns the second layer off while debugging locally. It is off
by default in every other configuration. Tests: `tests/test_security.py`, including one that
demonstrates the leak is real with containment removed.

### Transport and headers

HTTPS only. The platform redirects and this app emits, on every response:

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'none'` with `script-src 'self'`; full policy in `api/security.py` |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` (HTTPS requests only) |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` (with `frame-ancestors 'none'`) |
| `Referrer-Policy` | `no-referrer` |
| `Permissions-Policy` | every device capability denied |
| `Cross-Origin-Opener-Policy` / `Cross-Origin-Resource-Policy` | `same-origin` |
| `Cache-Control` | `no-store`, except content-hashed `/assets/` |

`Cache-Control: no-store` is a retention decision as much as a security one: a verdict body
carries extracted label text, and an intermediary cache holding one is retention nobody
documented.

**The policy has no relaxation in it — no `'unsafe-inline'`, no `'unsafe-eval'`, no external
host.** An earlier draft carried `style-src 'unsafe-inline'` on the theory that the evidence
overlay's React inline `style` props needed it. That theory was wrong: react-dom applies the
`style` prop through `node.style.setProperty`, a CSSOM mutation, and CSP governs style
attributes parsed from markup and `<style>` elements, not the CSSOM. Checked in a browser
against the built SPA under the shipped policy — an injected `<style>` element and an inline
`<script>` were both refused, so enforcement was genuinely on, and the evidence box still
rendered over the brand name it cites.

**One casualty, and it is the right one: `/docs` (Swagger UI) does not render.** It loads from
`cdn.jsdelivr.net` with an inline bootstrap script. Adding a CDN to the egress table so a
developer convenience page works is the opposite of what NET-1 is for, and an agency firewall
would block it anyway. `/openapi.json` is unaffected.

**Which HSTS a browser receives.** The edge sets it (`fly.toml`) and Fly's edge REPLACES
the application's header rather than adding to it — the same mechanism that silently
overrode the CSP for several deploys. So `api/security.py`'s one-year value never ships;
what a browser gets is the two-year value in the table above. Both omit `preload`.

`preload` is deliberately absent from HSTS: it is a commitment to browser vendors over an
entire apex domain, and this prototype rents a subdomain.

### CORS is strict, not permissive-with-a-comment

The SPA is served from the same origin as the API — one container, one URL — so there is no
legitimate cross-origin caller. No `Access-Control-Allow-Origin` is ever emitted unless an
origin appears in `LABELPROOF_ALLOWED_ORIGINS` (empty by default; `*` is not honoured as a
value). A preflight from a disallowed origin is refused 403, and **a non-safe method carrying a
foreign `Origin` is refused before the route runs** — a browser blocks the read of a
cross-origin `POST /verify`, but without this the server has already ingested the images and
spent the model call. Requests with no `Origin` (curl, smoke tests) are allowed.

Running the Vite dev server against a local API needs
`LABELPROOF_ALLOWED_ORIGINS=http://localhost:5173`. The deployed single-origin container needs
nothing.

### Rate limiting

Per-client token buckets, refilled continuously, in **separate lanes** so one kind of traffic
cannot starve another:

| Lane | Paths | Limit |
|---|---|---|
| exempt | `/health`, `/ready` | unlimited |
| verify | `/verify` | `LABELPROOF_RATE_LIMIT_PER_MINUTE` (default 30) |
| batch submit | `POST /batch`, `POST /batch/{id}/retry` | 10/min |
| batch read | `GET /batch/...` | 240/min |
| default | SPA assets, `/sample`, everything else | 600/min |

The lanes exist for PRD §225. A single shared budget would be spent by the batch progress
poller during a 300-item job, so an agent's next Verify Now would 429 while a batch ran — the
priority lane would never even get a say. Health checks are exempt so the limiter cannot take
the machine out of rotation under the load it exists to survive.

The bucket starts full, so the first minute's worth of requests never wait; a grader cannot
throttle the demo by clicking. A refusal is a 429 with `Retry-After`, a request ID, and a
plain-language body in the same error taxonomy as everything else, and it logs `rate_limited`
with the lane that refused it.

**Client identity is the socket peer by default.** `LABELPROOF_CLIENT_IP_HEADER` is **empty**
unless an operator sets it, and setting it is a security decision the app logs a warning about
at startup (`rate_limit_trusts_client_header`).

Whatever header it names is read straight off the request. Unless something between the client
and this process *overwrites* that header every time, the client controls it and can rotate it
for an unlimited number of buckets — measured at 200 requests against a 3/min limit: 200
allowed, 0 refused, with nothing to indicate the limiter had stopped working. Failing open
silently is the wrong direction for a control, so the default is the value that can never be
forged.

| Deployment | Setting |
|---|---|
| **Fly.io** | `LABELPROOF_CLIENT_IP_HEADER=fly-client-ip` — Fly's proxy overwrites it, and without this every user shares one bucket because the socket peer is the proxy |
| Cloudflare | `cf-connecting-ip`, same reasoning |
| Behind nginx / a generic reverse proxy | **Not `x-forwarded-for`.** It is an append-only chain and this code reads the leftmost entry, which is whatever the client sent. Terminate identity at the proxy instead, or leave this unset and rate-limit at the proxy |
| Direct, no proxy | leave unset |

**Two limitations, both real:**

- Buckets are in-process. On the single machine this deploys to that is exact; at N machines
  the effective ceiling becomes N times the limit. Redis would fix it and would add a host to
  the egress table that a prototype with one machine should not carry.
- Per-IP limiting is per-*address*. Anyone holding an IPv6 /64 — a single residential
  allocation — has 2^64 of them. This raises the cost of a flood; it does not stop one.

Tests: `tests/test_rate_limit.py`.

### Secrets

`ANTHROPIC_API_KEY` lives in the deployment platform's secret store and is read from the
environment. It is never in the repository, never in an image layer, and never in a log line. A
secrets scan runs pre-commit and in CI. `.env` is git-ignored; `.env.example` documents every
variable the app reads and holds no values.

### Provider data handling (SEC-7)

All AI calls are server-brokered — the browser never talks to the provider (NET-2), so there is
no path by which label artwork reaches a third party from a user's machine. Anthropic's API
does not train on inputs or outputs submitted through it, and enterprise agreements support
zero-retention processing, which is the configuration a federal deployment should require in
writing before launch. The provider is reached through one interface (`api/provider/base.py`),
so moving to a gov-cloud or Azure-hosted endpoint is a config and adapter change rather than a
rewrite (NET-4).

**Data residency is asserted, not assumed — where the model allows it.** The adapter sends
`inference_geo="us"` on every extraction call to a model that accepts it. The value is
`Config.inference_geo` (`api/config.py`, default `"us"`); there is deliberately no environment
override, because a data-residency guarantee that an operator can quietly relax with an env var
is not a guarantee. Without the parameter, requests follow the workspace default inference
geography — `global` unless someone configured otherwise — and the claim that label images
never leave the United States is one the code does not make.

This is a property of the model, not of our configuration, and it is not universal:

| Model | `inference_geo` |
|---|---|
| `claude-opus-5` | Accepted. Inference pinned to `us`. |
| `claude-sonnet-5` | Accepted. Inference pinned to `us`. |
| `claude-haiku-4-5` | **Rejected with a 400.** Sending it fails the request; it is not a no-op. |

So a Haiku 4.5 deployment cannot pin US data residency at all. `describe_residency()` in
`api/provider/anthropic_adapter.py` renders that as one sentence an operator or a grader can
act on, rather than letting the parameter be silently dropped by the request builder. For a
federal customer this is a procurement question, not a preference — and it is one of the two
reasons the faster, cheaper model is not simply the default (the other is the typography
false-pass rate; see *How fast it actually is*).

### The production path — documented, not built (SEC-8)

Scope-fenced per the brief. This is what changes between this prototype and something a federal
agency could actually run:

| Area | Prototype today | Production |
|---|---|---|
| Model endpoint | Anthropic API | FedRAMP-authorized endpoint; the customer is already on Azure, so Azure-hosted models with a signed zero-retention term |
| Data residency | `inference_geo="us"` where the model accepts it | Contractual, not a request parameter — and verified for every model in the deployment |
| Identity | None. No accounts by design. | Agency IdP via SAML/OIDC, PIV/CAC where required, role separation between agent and supervisor |
| Retention | 24h TTL on local disk | Aligned to the applicable NARA records schedule, not to a convenient number. Verification artefacts likely become part of the COLA case record, which changes the answer from "delete in 24h" to "retain per schedule, then dispose on schedule" |
| Audit logging | Structured logs, no content | Tamper-evident audit trail of who verified what and when, retained per schedule, with the same no-content rule |
| Rate limiting | In-process buckets | Shared store or an API gateway policy, per-identity rather than per-IP |
| Network | One public URL | Behind the agency perimeter; egress allowlisted from the [Network egress](#network-egress) table above |
| Data classification | Synthetic only | A review before any real applicant data touches it — this prototype has never held any and its retention story assumes it never will |

### Environment variables this section refers to

| Variable | Default | Effect |
|---|---|---|
| `LABELPROOF_RETENTION_HOURS` | `24` | TTL for batch uploads and results |
| `LABELPROOF_RETENTION_SWEEP_SECONDS` | `900` | How often the retention timer runs |
| `LABELPROOF_RATE_LIMIT_PER_MINUTE` | `30` | The `/verify` lane's budget |
| `LABELPROOF_CLIENT_IP_HEADER` | *(empty)* | Header naming the real client. Empty means the socket peer, which cannot be forged. Set to `fly-client-ip` on Fly |
| `LABELPROOF_ALLOWED_ORIGINS` | *(empty)* | Comma-separated cross-origin allowlist; empty means same-origin only |
| `LABELPROOF_HSTS` | `1` | Emit HSTS on HTTPS requests |
| `LABELPROOF_DEBUG_TRACEBACKS` | `0` | Set to `1` to allow tracebacks on stdout while debugging locally |

Inference geography is **not** on this list on purpose — see above. `.env.example` lists every
variable the app does read, including the extraction model, the latency budgets and the upload
caps that this section does not cover.

---

## Deployment

One container, one URL. FastAPI serves the built single-page app as static files, so
there is one deployable, one cold start against the 5-second budget, and one `docker run`
that reproduces production locally.

| | |
|---|---|
| **Host** | Fly.io, `iad` (Ashburn, Virginia) |
| **Configuration** | `fly.toml` — the complete environment, no console settings |
| **Image** | `Dockerfile` — multi-stage; the runtime carries no toolchain |
| **Pipeline** | `.github/workflows/deploy.yml` — gated on green, auto-rollback on red |
| **Verification** | `scripts/smoke.sh` — a real verification against the deployed URL |
| **Secrets** | Fly's secret store. Nothing sensitive is in this repository. |

### Why `iad`

The users are a federal agency in Washington DC. Northern Virginia is the closest Fly
region to them — roughly 5–15 ms round trip against 60–70 ms from the west coast. On a
budget dominated by a multi-second model call that is not decisive, but it costs nothing
and it is the reason that survives scrutiny.

One claim that used to be here is gone: *"the shortest hop to the provider."* Plausible,
never measured. Nobody has timed `iad → api.anthropic.com` against another region from
inside this app, so it stays out until someone does.

### Data residency

**The compute runs in Virginia and inference is pinned to the United States.** Both halves
are in the code rather than inferred from the region: `api/config.py` sets
`inference_geo = "us"`, `api/provider/anthropic_adapter.py` puts it on every extraction
request, and `tests/test_adapter.py` asserts the outgoing request carries it. Without that
parameter a request follows the workspace's default inference geography — `global` unless
someone configured otherwise — so this is a setting, not a consequence of deploying to
`iad`.

**The caveat that remains, and it is worth raising before an agency does.** The pin is
made by this application. It is enforced against the Anthropic workspace's
`allowed_inference_geos` allowlist, which is configured outside this repository — so the
guarantee is only as strong as that workspace's configuration, and confirming it is part
of any procurement conversation rather than something this deployment can demonstrate on
its own.

*(An earlier version of this section said the adapter did not set `inference_geo` at all.
That was written while the fix was in flight and became false when it landed — it
instructed staff to withhold a true compliance property from the customer who most needs
it. Both directions of that error are the same mistake: describing the code from memory
instead of reading it.)*

### Deploying

The pipeline does this on every push to `main`. The manual path exists for the first
deploy and for the rebuild drill:

```bash
fly apps create labelproof
fly secrets set ANTHROPIC_API_KEY="sk-ant-..."   # the only secret this service needs
fly deploy
scripts/smoke.sh https://labelproof.fly.dev
```

There is no fourth step. No volume to create, no dashboard toggle, no environment
variable set out of band — that is the property `fly.toml` is designed to have, and
[CHANGES.md](CHANGES.md) records the drill that tests it.

### Health and readiness

Two endpoints answering two different questions, wired to two different consequences.

| Endpoint | Question | Red means |
|---|---|---|
| `GET /health` | Is the process alive? Touches no config, no provider, no disk. | Restart me. |
| `GET /ready` | Is this process *configured* to check a label — required settings present, provider client constructible? | Take me out of rotation. |

Both are platform checks, and `/ready` is the gate a new release must pass before it
receives traffic: a release shipped **without** `ANTHROPIC_API_KEY` fails there and never
serves a request.

**`/ready` does not contact the provider.** It validates configuration and constructs the
SDK client; the client exposes no reachability probe, so nothing leaves the machine. A key
that is *present but revoked, expired, or scoped to the wrong workspace* answers
`{"status":"ready","simulated":false}` with a 200. A missing key is genuinely caught — that
half holds — but "provider reachable" was an overstatement and is not what this endpoint
measures.

What actually proves the provider works is `scripts/smoke.sh`, which performs a real
verification after every deploy. If you need continuous assurance rather than
per-deploy, the keep-warm loop calls the provider every four minutes and logs the
outcome — that is the closest thing to a live provider check this service has.

**`/ready` returning 200 is not the same as the service being usable.** In sample mode it
answers 200 with `simulated: true` — a server that can replay the built-in example labels
and nothing else. It looks healthy to every status-code check in existence while handing a
compliance reviewer demonstration verdicts that are indistinguishable from findings. Three
layers treat that as the deployment failure it is:

1. `LABELPROOF_FAKE_PROVIDER = "0"` is pinned in `fly.toml`, so it cannot be inherited.
2. `scripts/smoke.sh` asserts on the field after every deploy and triggers a rollback.
3. The keep-warm loop checks it every four minutes and logs an error if it ever flips.

### Keeping it warm

*"If we can't get results back in about 5 seconds, nobody's going to use it."* A grader
clicks the link once, cold. That first click must not be the request that pays for a
machine wake and an unprimed prompt cache.

- **The machine never stops.** `min_machines_running = 1` with `auto_stop_machines = "off"`.
  Not `"suspend"` — suspend is cheaper and still charges the first request for the resume,
  and the first request is the one being protected.
- **The prompt cache stays primed.** `scripts/keepwarm.py` re-warms the extraction prompt's
  cached prefix every four minutes, under the provider's five-minute cache TTL. Every ping
  reports whether the cache actually engaged, because a cache that silently fails to engage
  is worse than none — the latency budget was planned around it.

What this does *not* fix, said plainly: the TLS connection pool lives in the server
process and expires after a few seconds idle regardless, so the first request still pays
one handshake. The prompt cache was the part worth buying.

### Egress (NET-1)

Every outbound destination, so a network administrator can allowlist this app from one
table. The list is short by design, and verifiable — there are no external URLs anywhere
in `api/` or `web/src/`:

```bash
grep -rnoE "https?://[a-zA-Z0-9./_-]+" api web/src web/index.html | grep -v localhost
```

**From the running service, in production:**

| Destination | Port | Protocol | Purpose | Required? |
|---|---|---|---|---|
| `api.anthropic.com` | 443 | HTTPS | Vision extraction, Tier-3 adjudication, and the keep-warm cache pre-warm. The only runtime dependency. | **Yes.** Blocked, the app stays up and says so in plain language; it cannot verify labels. |
| DNS resolver | 53 | UDP/TCP | Resolving the above. | Yes |

That is the whole list. No CDN, no font host, no analytics, no error-reporting service, no
object store, no external queue or broker — the job store is SQLite in the container and
uploads are local files on a TTL.

**From the browser:** same-origin only. Every asset is served by the app itself and the
Content-Security-Policy in `fly.toml` enforces it. **The browser never contacts the model
provider** (NET-2) — all AI calls are brokered server-side, which is also what makes a
single-domain allowlist sufficient for an agency workstation.

**Inbound:** 443. Port 80 answers only with a redirect to 443.

**Build and deploy time only — not needed on an agency network:**

| Destination | Purpose |
|---|---|
| `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com` | Base images (`python:3.12-slim`, `node:22-slim`) |
| `deb.debian.org`, `security.debian.org` | One system package (`libglib2.0-0`) |
| `pypi.org`, `files.pythonhosted.org` | Python dependencies |
| `registry.npmjs.org` | Web dependencies |
| `nodejs.org` | Node runtime download, on a `setup-node` cache miss |
| `github.com`, `objects.githubusercontent.com` | Source checkout, Actions, and the `flyctl` binary download |
| `api.github.com` | Actions API |
| `registry.fly.io`, `api.machines.dev` | Image push and machine API, from CI |

*(`nodejs.org` and the `flyctl` download were missing from an earlier version of this
table, which was presented as complete. If you are allowlisting a build network, prefer
verifying against a run with egress logging over trusting this list.)*

**Swapping providers.** All AI calls go through one server-side interface
(`api/provider/base.py`). Pointing the service at an Azure-hosted or gov-cloud endpoint is
an adapter plus a config value — it changes exactly one row of the first table above and
nothing else about this deployment.

### Transport and security headers (SEC-6)

HTTPS only. Fly terminates TLS at the edge and redirects plain HTTP rather than serving
it, and the headers below are set at the edge — where they still stand if the application
is mid-restart.

| Header | Value | Why |
|---|---|---|
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains` | Two years, subdomains included. **No `preload`** — see below |
| `Content-Security-Policy` | same-origin; `data:`/`blob:` images | The SPA loads nothing third-party; upload previews are object URLs |
| `X-Content-Type-Options` | `nosniff` | An upload echoed back must never be re-interpreted as script |
| `X-Frame-Options` | `DENY` | Verdicts carry regulatory weight; they must not render inside someone else's chrome |
| `Referrer-Policy` | `no-referrer` | Nothing to leak, so leak nothing |

### Retention

Uploads and results live in the container's `/data` and are purged on a TTL (24h,
configurable). **There is deliberately no mounted volume** — a volume would be a durable
home for exactly the data the retention policy exists to destroy, and it would add a
manual `fly volumes create` ahead of `fly deploy`, which would falsify the claim that this
environment rebuilds from configuration alone.

### Rolling back

See **[CHANGES.md](CHANGES.md)** for the rollback runbook, the automatic trigger, and the
destroy-and-redeploy drill.
