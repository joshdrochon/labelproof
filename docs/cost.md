# Cost analysis

Every figure here is measured on the deployed application, not estimated. Nothing is
modelled from token-count guesses.

| | |
|---|---|
| Date | 2026-08-12 |
| Model | `claude-sonnet-5` |
| List price | $3.00 / MTok input, $15.00 / MTok output |
| Cache pricing | read 0.1×, write 1.25× of input |
| Source | `POST /verify` and `GET /batch/{id}` responses from <https://labelproof.fly.dev> |

---

## Measured, per verification

A two-image application (front and back), warm, on the deployed URL:

| | Tokens | Cost |
|---|--:|--:|
| Input | 3,700 | $0.0111 |
| Output | 1,175 | $0.0176 |
| Cache read | 8,702 | $0.0026 |
| Cache write | 0 | $0.0000 |
| **Total** | | **$0.0313** |

**Output dominates.** 1,175 output tokens cost more than 3,700 input tokens, because
output is priced 5× higher. The lever on cost is the size of the extraction response, not
the size of the image — which is the opposite of the intuition that led to the compression
work, and worth knowing before anyone optimises the wrong end.

## Measured, per label in a batch

A real 22-application batch, submitted as one manifest and a zip:

| | |
|---|---|
| Applications | 22 |
| Wall clock | 42s |
| Failures | 0 |
| Total | **$0.3945** |
| **Per label** | **$0.0179** |

**43% cheaper per label than a single verification**, and the reason is the prompt cache:
100,073 cached tokens were read across the batch. The static system prompt carries a
`cache_control` breakpoint, so every item after the first reads the instruction prefix at
a tenth of the input price instead of re-processing it.

This also means the single-verification figure above is the *warm* one. A genuinely cold
first request pays a cache write at 1.25×, roughly $0.005 more, once.

---

## Projections

Working days only (21/month). Both columns are measured rates, not models.

| Volume | Single / day | Single / month | Batch / day | Batch / month |
|---|--:|--:|--:|--:|
| 130 labels/day | $4.07 | $85.55 | $2.33 | $48.95 |
| 600 labels/day | $18.80 | $394.83 | $10.76 | $225.94 |
| 1,200 labels/day | $37.60 | $789.66 | $21.52 | $451.88 |

Arithmetic: `labels × rate` and `× 21`. The rates are $0.0313 and $0.0179 from the two
sections above.

**Add hosting**: ~$10–15/month for one always-on `shared-cpu-2x` machine, plus ~$7/month
for the keep-warm ping (one `max_tokens=1` call every four minutes, which exists to hold
the prompt cache open). Hosting is flat and small enough to disappear against the model
spend at any of the volumes above.

---

## Assumptions behind the projections

Each is stated because each is a place the number could move.

| Assumption | Value | If wrong |
|---|---|---|
| Images per application | 2 (front + back) | One image is roughly half the input tokens; input is a third of cost, so ~15% cheaper |
| Output tokens per verification | ~1,175 | The dominant term. A more verbose schema moves this the most |
| Tier-3 adjudication rate | **0%** | Not wired. Every gray case currently falls to Mismatch, which costs nothing extra |
| Retry overhead | 0 | 22/22 succeeded. `MAX_ATTEMPTS` allows retries; none were needed |
| Cache hit rate in batch | ~100% after item 1 | A cache entry lives 5 minutes; a slow trickle of single verifications will not hold it |
| Image long edge | 2,576px | Below 1,568px the high-resolution tier is lost, which is what makes small warning text legible. Not a cost lever we would pull |

---

## Cost cliffs

**Resolution ↔ tokens.** Image tokens scale with area. Halving the long edge quarters the
image tokens, and would save roughly $0.006 per verification — about 18% — at the cost of
the vision tier that reads the government warning. Not a trade this product makes.

**Concurrency ↔ throttling.** Batch runs 6 workers. Raising it does not raise cost per
label, but it does raise the chance of provider rate limiting, which converts into retries
and *does* cost money. 6 was chosen as a starting value and has not been tuned against a
real 300-item job.

**Cache TTL ↔ traffic shape.** The 5-minute ephemeral cache is what makes batch cheap.
An agent doing one verification every ten minutes pays the full input price every time —
$0.0313 rather than $0.0179. The keep-warm loop holds the entry open for exactly this
reason, and it is the cheapest line in the system at ~$0.0006 per ping.

---

## Development spend

Not separately instrumented, and worth saying plainly rather than inventing a number.
Development ran largely against `fake:spec` — recorded fixtures with no model in the loop —
so the paid calls were the model-tier spike (`scripts/spike_latency.py`,
`scripts/spike_typography.py`, roughly 120 calls across three models), the Tier B
photograph runs, the 20-sample p95 table ($0.63), and two 22-application batches ($0.79).
The order of magnitude is tens of dollars, not hundreds.

`api/timing.cost_line` logs tokens and dollars for every verification from day one, so a
production deployment has this per-request without further work.

---

## ROI

At $0.0179 per label in batch, **560 labels cost about ten dollars**.

The comparison the brief invites: an agent spends 5–10 minutes per label doing this by
eye against a paper checklist. At 600 labels a day the model spend is $10.76 — against
50 to 100 hours of agent time. The tool does not remove that time, because the agent still
makes every determination; it removes the search — finding the warning on the back label,
reading 4-point type, checking a fill against the standards table.

The honest framing: this is cheap enough that cost should not be the deciding factor.
Accuracy and the false-pass rate should be.
