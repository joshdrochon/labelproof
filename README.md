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
| `verification_cost` | INFO | Tokens and dollars for one verification (OPS-4). |
| `unhandled_exception` | ERROR | Something broke that nobody anticipated. The agent got a sentence, not a trace. |
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
| `adjudicate` | Tier-3 text adjudication, when it fires. |
| `total` | The whole request, measured by the outermost clock. |

Two things about this table are load-bearing.

**`preprocess` is a roll-up, so do not add the column up.** PRD §Observability names the
stages upload → preprocess → extract → compare → render. This pipeline measures the two
halves of preprocessing separately, and `preprocess` reports their sum so the PRD's
vocabulary maps onto real numbers. Summing every field double-counts. (There is no
separate deskew/perspective pass in this build; if one is added it becomes a third part of
the roll-up, in `api/timing.PREPROCESS_PARTS`.)

**`total` is measured, not derived.** It comes from a clock started before the request is
parsed and read after the last verdict is computed — never from adding the stages up.
Adding them up would silently omit whatever nobody instrumented, and the gap between "what
we measured" and "how long it took" is exactly the number worth having.

`tests/test_timing.py` puts an independent stopwatch around a real HTTP request and holds
the server's own claim to it. PRD §232: *if the number on the screen and the number on the
stopwatch disagree, the stopwatch wins.*
