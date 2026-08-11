# LabelProof

AI label verification for TTB compliance review. An agent uploads the label artwork and
the application; LabelProof returns a per-field checklist and a recommendation. It
recommends — the agent decides.

See `PRD.md` for what this is and why.

---

## Observability

The vendor pilot that came before this one died of *unexplained* slowness: 30 to 40
seconds a label, with nothing anyone could point at. Everything in this section exists so
that never has to be guessed at again — and so the speed claim in `PERF-1` is evidence
rather than assertion.

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
{"duration_ms": 2610, "event": "stage_complete", "ok": true, "request_id": "req_9f3c1a4b7e2d8055", "stage": "extract", "ts": 1786464776.227}
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

Batch work adds `job_id` and `item_id`, which correlate an item back to its job.

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
| `cost_model_unknown` | WARNING | A verification ran on a model with no entry in the price list; cost was estimated at the most expensive known tier. |
| `circuit_breaker` | WARNING | The provider circuit opened or closed. Opening is the warning; closing rides the same event. |
| `config_incomplete` | WARNING | A required setting is missing; /ready is red. |
| `image_scored` | INFO | Deterministic image-quality scores for one uploaded image. |
| `provider_bbox_dropped` | WARNING | An evidence box was unusable and was discarded rather than guessed. |
| `provider_call` | INFO | One model call, with its usage. |
| `provider_extract` | INFO | The whole extraction across every image. |
| `provider_retry` | WARNING | A provider call failed and is being retried. |
| `provider_typography_unusable` | WARNING | Typography signals could not be judged; the warning field fails closed. |
| `provider_unavailable` | WARNING | The provider could not be reached; answered 503. |
| `request_complete` | INFO | One HTTP request finished. Carries status and total duration. |
| `request_failed` | INFO | A request ended in the error taxonomy. Carries kind, code, status. |
| `stage_complete` | INFO | One pipeline stage's duration (OPS-1). One line per stage per request. |
| `unhandled_exception` | ERROR | Something broke that nobody anticipated. The agent got a sentence, not a trace. |
| `verification_cost` | INFO | Tokens and dollars for one verification (OPS-4). |
| `verify_complete` | INFO | A verification produced a recommendation. |
| `verify_over_budget` | INFO | The request budget expired; partial result returned as Needs review. |
| `verify_pregated` | INFO | Images too poor to read; returned Unreadable with zero model calls. |

<!-- LOG-EVENTS:END -->

#### Fields

**A log line may carry only these field names, and the logger raises on anything else.**

That is the mechanism, not a convention. SEC-4 says logs carry ids, timings, token counts
and verdict summaries — never label text, never extracted values, never image bytes. A
comment saying so is a rule that erodes; an allowlist that raises is a rule that holds.
Every name below is an identifier, a measurement, or a category. None of them can contain
something read off a label.

Adding a field is deliberate: put it in `ALLOWED_FIELDS` with a reason. Working around the
check is not a shortcut, it is a compliance failure.

<!-- LOG-FIELDS:BEGIN -->

| Field | What it carries |
|---|---|
| `attempt` | Which retry this is, from 1. |
| `blur` | Image sharpness score, 0–1, higher is better. |
| `bytes` | Size of something in bytes. Never its contents. |
| `cache_creation_tokens` | Prompt-cache tokens written on this call. Priced at 1.25x input. |
| `cache_read_tokens` | Prompt-cache tokens read on this call. Priced at a tenth of input. |
| `code` | Machine-readable error code from the taxonomy, e.g. `file_too_large`. |
| `commodity` | `spirits`, `wine` or `malt`. |
| `confidence` | Extractor confidence for a field, 0–1. |
| `count` | How many of the thing this line is about. |
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

So `api.logging.configure()` installs a `LogRecord` factory that strips `exc_info`,
`exc_text` and `stack_info` from every record created anywhere in the process, and
replaces any exception object passed as a message or a format argument with its class
name. What reaches stdout instead is the exception's *type*:

```
Exception in ASGI application | exception=ValidationError (traceback withheld: SEC-4)
```

Visible, not silent — a service that is failing must not look like a service that is
quiet.

**What this does not cover, stated rather than papered over:** a third-party library that
passes label text as a plain format argument, `logger.info("read %s", brand)`. Covering
that would mean redacting all foreign output, which would delete uvicorn's startup and
access lines. No code path in this repository does it. The guard can be declined in code
(`configure(guard_stdout=False)`) and deliberately cannot be switched off by an
environment variable.

### Timings

Every `/verify` response carries the stage breakdown, because PRD §Observability requires
it *surfaced in the UI* — a number that lives only in stdout cannot be shown to the agent
deciding whether to trust the tool.

```json
"timings_ms": { "ingest": 41, "quality": 18, "preprocess": 59,
                "extract": 2610, "compare": 2, "adjudicate": 0, "total": 2794 }
```

| Field | What it measures |
|---|---|
| `ingest` | Sniff, EXIF-orient, strip all metadata, re-encode, downscale. |
| `quality` | Blur, exposure, glare and skew scoring. No model call. |
| `preprocess` | **Roll-up of `ingest + quality`.** See below. |
| `extract` | Every vision call, wall-clock. Concurrent across images, so this is `max`, not `sum`. |
| `compare` | The deterministic rules engine. Pure functions; usually 0–2ms. |
| `adjudicate` | Tier-3 text adjudication. **Not implemented in this build — always `null`.** |
| `total` | The whole request, measured by the outermost clock. |

Two things about this table are load-bearing.

**`preprocess` is a roll-up, so do not add the column up.** PRD §Observability names the
stages upload → preprocess → extract → compare → render. This pipeline measures the two
halves of preprocessing separately, and `preprocess` reports their sum so the PRD's
vocabulary maps onto real numbers. Summing every field double-counts. (There is no
separate deskew/perspective pass in this build; if one is added it becomes a third part of
the roll-up, in `api/timing.PREPROCESS_PARTS`.)

**A stage that did not run reports `null`, not `0`.** This is the same rule as the one
above, seen from the other side: `0` reads as "instant", and `"adjudicate": 0` would invite
a reader to conclude Tier-3 adjudication ran and cost nothing. It does not run at all.
`api/timing.UNIMPLEMENTED_STAGES` lists the stages the API declares but this build never
executes, and a test fails if one of them starts producing a number without coming off the
list.

**`total` is measured, not derived.** It comes from a clock started before the request is
parsed and read after the last verdict is computed — never from adding the stages up.
Adding them up would silently omit whatever nobody instrumented, and the gap between "what
we measured" and "how long it took" is exactly the number worth having.

`tests/test_timing.py` puts an independent stopwatch around a real HTTP request and holds
the server's own claim to it. PRD §232: *if the number on the screen and the number on the
stopwatch disagree, the stopwatch wins.*


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
| `verification (POST /verify)` | Server-side total. **This is the PERF-1 series.** | See the ladder in `BUILD.md` §8. |
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

Commit `docs/perf-deployed.md`. That file, not a sentence in a status update, is the
evidence for the speed claim: it carries the URL, the timestamp, the commit, the payload
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
the p95, mean tokens in and out, and mean cached reads.

Five things to know before quoting a cost figure:

- **It is list price, computed locally.** The price table lives in `api/timing.PRICES`,
  keyed by model. It is not a bill, and it does not know about discounts.
- **The price follows the configured model.** `LABELPROOF_EXTRACTION_MODEL` is an
  environment variable, and Opus 5, Sonnet 5 and Haiku 4.5 differ by 5x. A model with no
  entry in the table is priced at the most expensive known tier and logged as
  `cost_model_unknown` — guessing low would put an under-stated number into a budget.
- **Three token counters, three prices.** `input_tokens` excludes both cache counters.
  Cached reads cost a tenth of an input token; cache writes cost 1.25x one. All three are
  on the cost line.
- **Sample-mode runs cost nothing** and would drag any average down. The line carries
  `provider`, the rollup names them, and it says so in the report rather than folding them
  in.
- **Cost is per verification, not per image.** One `/verify` with a front and a back is one
  line covering both calls.

**Known gap:** the Anthropic adapter reports `cache_read_input_tokens` but not
`cache_creation_input_tokens`, so cache writes currently arrive as zero and are priced at
zero. Everything downstream of the adapter carries and prices them correctly; only the one
line that reads them off the API response is missing. The rollup detects the signature —
cached reads in a window with no writes anywhere — and stamps the cost section as a lower
bound rather than letting the figure look complete.

### The honesty check

PRD §232: *if the number on the screen and the number on the stopwatch disagree, the
stopwatch wins.* Two mechanisms hold the service to that.

`tests/test_timing.py` wraps a real `POST /verify` in an independent stopwatch and asserts
the server's own `timings_ms.total` matches it — and, with a provider that sleeps a known
duration, that the total actually contains the extraction rather than being computed
before it. A fabricated total, a total measured too early, or a total that omits a stage
all fail.

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
| p95 crept up | `extract` in the rollup | Almost always the model. Everything else is tens of milliseconds. |
| Rising `provider_retry` | Degraded-but-handled table | An outage forming. `circuit_breaker` opening is the next line you will see. |
| Unverified rate rising, error rate flat | Verification-outcome table | Pre-gate or budget stops. Both answer 200 — the service is up and is not checking labels. |
| Any `unhandled_exception` | Its `request_id`, then that request's other lines | A bug. The traceback is deliberately not printed (SEC-4); the exception type is on the line and the request id tells you what it was doing. |
