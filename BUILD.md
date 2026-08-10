# LabelProof — Build Spec

Agent-executable build specification. `PRD.md` owns *what* and *why*; `TICKETS.md` owns the
work breakdown; **this file owns every decision those two deliberately left open**, so a build
can run end-to-end without inventing architecture.

| | |
|---|---|
| **Status** | Binding. Where this conflicts with `TICKETS.md`, this wins and the board delta is recorded in §9. |
| **Source of truth** | `PRD.md` v1.0 for requirements. This file never redefines a requirement. |
| **Version** | 1.0 — 2026-08-10 |

---

## 1. Pinned decisions

Every one of these was open in `PRD.md`/`TICKETS.md`. They are now closed.

| Decision | Value | Rationale |
|---|---|---|
| Backend | Python 3.12 + FastAPI | OpenCV/Pillow for the image pipeline (LP-183–201); the robustness suite is ~19 tickets and Node has no real perspective-correction path |
| Frontend | React + TypeScript + Vite | Static build, no SSR needed |
| Deployable | **One container.** FastAPI serves the built SPA as static files | One URL, one cold start against the 5s gate, one `docker run` for LP-306 |
| Provider | Anthropic | LP-049 abstraction keeps a gov-cloud swap to a config change |
| Extraction model | `claude-opus-5` | High-res vision tier (2576px long edge, ~4,784 tokens/image) — the resolution that makes small warning text and bold/caps typography legible. Haiku 4.5 caps at 200K context with no high-res tier; disqualified for extraction. |
| Adjudication model (Tier 3) | `claude-haiku-4-5` | Narrow text comparison, no image. Runs serially *after* extraction inside the same request, so it is the p95 threat — speed is the binding constraint here, not cost. |
| Thinking / effort | `thinking: {type: "adaptive"}`, `output_config: {effort: "low"}` | Extraction is field reading, not reasoning. Low/medium are strong on Opus 5. Raise only if eval demands it. |
| Structured output | `output_config.format` with a JSON schema | Replaces prefill (400s on Opus 5). Validated on receipt (LP-051). |
| Job queue (batch) | In-process worker pool + SQLite job store | 300 items, single container, survives restart (BATCH-6). No external broker in the egress table (NET-1). |
| Storage | Local filesystem under a TTL-swept dir | Ephemeral by design (SEC-2). No object store in the egress table. |
| Fixtures | Programmatic SVG→PNG generator + AI photos for TC-11–14 only | §5 |
| Test runners | `pytest` (backend), `vitest` (unit), `playwright` (E2E) | |
| Lint / typecheck | `ruff` + `mypy --strict`; `eslint` + `tsc --noEmit` | ENG-8 |

**Model IDs are exact.** `claude-opus-5` and `claude-haiku-4-5` — never append date suffixes.

---

## 2. Repository layout

```
labelproof/
├── pyproject.toml
├── Dockerfile                    # multi-stage: build web/ → serve from api/
├── .env.example                  # every var, app fails fast when missing (LP-011)
├── api/
│   ├── main.py                   # app factory, static mount, middleware
│   ├── config.py                 # env-only, fail-fast
│   ├── errors.py                 # taxonomy: user | image | provider | internal (LP-012)
│   ├── logging.py                # structured; NO-CONTENT RULE stated at module top (LP-013)
│   ├── models.py                 # pydantic: Application, Extraction, FieldResult, Verdict
│   ├── routes/
│   │   ├── verify.py             # POST /verify
│   │   ├── batch.py              # POST /batch, GET /batch/{id}, POST /batch/{id}/retry
│   │   ├── sample.py             # GET /sample
│   │   └── health.py             # GET /health, GET /ready
│   ├── pipeline/
│   │   ├── ingest.py             # sniff, EXIF orient, strip ALL metadata, re-encode, HEIC, PDF
│   │   ├── quality.py            # blur/exposure/glare/skew scores + PRE-GATE (§6)
│   │   ├── preprocess.py         # deskew, perspective, contrast normalize
│   │   ├── extract.py            # provider orchestration, parallel per image
│   │   └── merge.py              # multi-image merge, per-field provenance
│   ├── provider/
│   │   ├── base.py               # ExtractionProvider protocol — the single choke point (LP-049)
│   │   ├── anthropic_adapter.py  # real; server-side only (NET-2)
│   │   └── fake.py               # fixture replay; CI uses this exclusively (LP-065)
│   ├── rules/                    # pure functions, zero I/O, millisecond unit tests
│   │   ├── normalize.py          # NFKC, case fold, quotes, diacritics, hyphenation
│   │   ├── compare.py            # Tier 1 / Tier 2 comparators
│   │   ├── commodity.py          # DATA-DRIVEN required/optional table (LP-041)
│   │   ├── abv.py                # parse, proof cross-check, tolerance context
│   │   ├── fills.py              # standards-of-fill tables (Appendix B)
│   │   ├── warning.py            # 16.21 verbatim + 16.22 typography
│   │   └── aggregate.py          # worst-of + warning-first ranking
│   ├── adjudicate.py             # Tier 3, budgeted (§7)
│   └── canon.py                  # CANONICAL_WARNING + regulatory constants, source-commented
├── web/src/
│   ├── App.tsx
│   ├── routes/{VerifyNow,Batch}.tsx
│   └── components/{Dropzone,ApplicationForm,VerdictCard,FieldRow,EvidenceOverlay,DiffView}.tsx
├── fixtures/
│   ├── generator/                # SVG→PNG label generator + degradation transforms
│   ├── labels/                   # generated PNGs (committed)
│   └── recorded/                 # recorded provider responses (committed)
├── golden/set.json               # expected per-field verdicts
├── eval/run.py                   # accuracy, confusion matrix, zero-false-pass gate
└── scripts/timed_p95.py          # 20-run p95 against any URL (LP-120)
```

**Hard rule:** `api/rules/` imports nothing from `api/routes/`, `api/provider/`, or `api/pipeline/`.
The rules engine is pure and unit-testable in milliseconds (ENG-8).

---

## 3. API contract

### `POST /verify` — multipart

| Part | Type | Notes |
|---|---|---|
| `images` | 1..4 files | JPEG/PNG/WebP/HEIC/PDF. ≤10MB each. |
| `application` | JSON string | `Application` object below |

```jsonc
// Application
{
  "commodity": "spirits",              // spirits | wine | malt
  "brand_name": "Old Tom Distillery",
  "class_type": "Kentucky Straight Bourbon Whiskey",
  "alcohol_content": 45.0,             // percent ABV, nullable
  "net_contents": "750 mL",
  "producer_name": "Old Tom Distillery",
  "producer_address": "Bardstown, Kentucky",
  "country_of_origin": null,           // required when is_import
  "is_import": false
}
```

```jsonc
// 200 response
{
  "request_id": "req_01J...",
  "aggregate": {
    "recommendation": "needs_review",  // ready_to_approve | needs_review | reject_candidate
    "rationale": "Warning statement header is not in all caps.",
    "driving_field": "government_warning"
  },
  "fields": [
    {
      "field": "brand_name",
      "verdict": "acceptable_variation", // match | acceptable_variation | mismatch |
                                         // missing | unreadable | not_applicable
      "extracted": "STONE'S THROW",
      "expected": "Stone's Throw",
      "confidence": 0.97,
      "rationale": "Label uses all caps; same name.",
      "tier": 2,                         // 1 | 2 | 3 | null
      "evidence": {
        "image_index": 0,
        "bbox": [0.12, 0.30, 0.71, 0.44] // normalized x0,y0,x1,y1
      },
      "findings": []                     // format/compliance findings, independent of the match
    }
  ],
  "images": [
    { "index": 0, "role": "front", "quality": { "blur": 0.82, "exposure": 0.91,
      "glare": 0.05, "skew_deg": 3.1, "verdict": "ok" } }
  ],
  "timings_ms": { "ingest": 41, "quality": 18, "preprocess": 122,
                  "extract": 2610, "compare": 2, "adjudicate": 0, "total": 2794 },
  "cost": { "input_tokens": 9840, "output_tokens": 1120, "usd": 0.0772 }
}
```

**Errors** speak the taxonomy, always with a next step (UX-6):

```jsonc
{ "error": { "kind": "image", "code": "unreadable_image",
             "message": "Glare covers the lower third of the label — retake without flash or request a new image.",
             "next_step": "retake" } }
```

`kind` ∈ `user | image | provider | internal`. Provider-down returns **503** with
`kind: "provider"` and a plain-language message — never a hang, never a silent queue (TC-21).

### Other endpoints

| Route | Purpose |
|---|---|
| `GET /sample` | Old Tom demo pair — powers the one-click demo (LP-098) |
| `POST /batch` | manifest CSV + images (zip or multi-select) → `{job_id}` |
| `GET /batch/{id}` | counts by state, ETA, completed items so far (progressive, BATCH-5) |
| `POST /batch/{id}/retry` | failed items only (BATCH-8) |
| `GET /batch/{id}/export.csv` | per-item verdicts + findings (BATCH-7) |
| `GET /health` | process up |
| `GET /ready` | config valid + provider reachable (NET-5) |

---

## 4. Sample application data

`assets/samples/old_tom.json`. Field values are derived from the sample label described in
`PRD.md` (§Test Cases TC-01, Appendix A FIELD-2/3/4, MATCH-7). **Assumption logged:** the source
brief `.docx` is not in the repo, so any field it specifies beyond these must be reconciled on
first read of the brief.

```json
{
  "commodity": "spirits",
  "brand_name": "Old Tom Distillery",
  "class_type": "Kentucky Straight Bourbon Whiskey",
  "alcohol_content": 45.0,
  "net_contents": "750 mL",
  "producer_name": "Old Tom Distillery",
  "producer_address": "Bardstown, Kentucky",
  "country_of_origin": null,
  "is_import": false
}
```

Its label images are generated by the fixture generator (§5) as a clean, fully-compliant pair.

---

## 5. Fixture strategy

**Programmatic backbone.** `fixtures/generator/` renders labels as SVG → PNG (`cairosvg` or
headless Chromium). Every canonical test case that turns on *text content or typography* is
generated, because a diffusion model cannot reliably render 50 words of legalese verbatim, nor
produce the controlled defects the tests require.

Generator takes a spec and emits a label:

```python
LabelSpec(
    brand="STONE'S THROW",          # curly apostrophe → TC-02
    warning_header_case="title",    # → TC-03 (Jenny's catch)
    warning_body_bold=True,         # → TC-04
    warning_text=REWORDED,          # → TC-05
    warning_scale=0.4,              # → TC-06 prominence
    warning=None,                   # → TC-07 missing
    net_contents="733 mL",          # → TC-10 non-standard fill
    abv_text="45% ABV",             # → TC-22 spirits abbreviation
)
```

Degradations applied in code (`fixtures/generator/degrade.py`): perspective warp (15°/30°/45°),
gaussian blur, exposure curve, synthetic glare overlay, cylinder warp for bottle curvature.
These cover TC-11–14 and LP-195–198 **deterministically** — regenerable byte-identical, which
LP-123 requires.

**AI photos supplement, never replace.** A handful of real-looking bottle photos for TC-11–14 add
the one thing code cannot fake: true specular highlights on curved glass. They are committed as
files, so the set stays reproducible after generation. Generation prompts ship in the repo (DEL-5).

**Golden set:** ≥10 at MVP (LP-068), ≥25 at Early (LP-233). `golden/set.json` maps each fixture
to expected per-field verdicts.

---

## 6. Image pipeline and the pre-gate

Order is fixed:

1. **Sniff** content type from magic bytes, never the extension (SEC-5).
2. **EXIF auto-orient**, then **strip all metadata including GPS** (SEC-3).
3. **Re-encode** — neutralizes polyglot uploads (LP-252).
4. **Quality score** — blur (Laplacian variance), exposure (histogram), glare (saturated-pixel
   clustering), skew (Hough). Pure OpenCV, no model call, tens of milliseconds.
5. **PRE-GATE.** If an image scores below the hopeless threshold, **return without any model
   call**: per-field `unreadable` + a plain-language retake reason. Verdict in ~300ms.
6. **Preprocess** — deskew/perspective, contrast normalize.
7. **Extract** — §7.

The pre-gate spends *less* and can never produce a false pass, because its outcome is "we did not
verify," not "we verified." Thresholds are tuned against the robustness fixtures and recorded with
rationale (LP-200).

**Pre-gate limits, stated honestly:** it catches illegible images (TC-14). It does **not** catch
wrong-subject images — a cat photo (TC-15) is sharp and well-exposed and still needs the model.

**Escalation, not downgrade.** Where extraction returns low confidence on a field, re-run a second
pass cropped to that field's evidence region at full resolution. Extra compute lands only on cases
likely to fail. Never route a "clean-looking" image to a weaker model: a global blur scalar
predicts "nice photo," not "will the warning read correctly" — a label can be tack-sharp except
for glare sitting on the warning.

---

## 7. Model call specification

### Extraction

- **One call per image**, images run **concurrently** (LP-280). Wall-clock is `max()`, not `sum()`.
- `model: "claude-opus-5"`, `thinking: {type: "adaptive"}`, `output_config: {effort: "low"}`.
- `output_config.format` carries the extraction JSON schema; validate on receipt.
- Explicit timeout budgeted against the 5s gate; bounded retries with jittered backoff; circuit
  breaker on the provider path (ENG-4).

**Prompt caching — two rules that are easy to get wrong:**

1. The system prompt is **fully static**. All three commodity rule sets live in it; the active
   commodity is passed in the *user message*. Interpolating commodity (or a timestamp, or a
   request ID) into the system prompt fragments the cache and it silently stops paying.
2. `cache_control: {type: "ephemeral"}` goes on the **last system block**, so images land after it
   in `messages` and never invalidate the prefix. Opus 5's minimum cacheable prefix is 512 tokens;
   the system prompt plus schema clears that easily.

Verify with `usage.cache_read_input_tokens` — if it is 0 across repeated requests, something in the
prefix is varying.

**Cache pre-warm** rides the keep-warm ping (LP-134): a `max_tokens: 0` request at startup and on
an interval under the TTL. Graders click once; the first hit should *read* a warm cache, not write
one.

### Tier 3 adjudication

- `model: "claude-haiku-4-5"`, text only, strict output schema.
- Fires only on gray cases past Tier 1/2. Trigger rate logged (LP-221).
- **Hard time budget:** if less than 1.2s of the request budget remains, skip and return
  `needs_review` with partial evidence. Never blows the deadline (LP-222, PERF-7).

---

## 8. Latency budget

| Stage | Target | Lever |
|---|---|---|
| Upload | 0.3–1.0s | Client-side downscale **to 2576px** (not below — that's the high-res ceiling we chose Opus 5 for), WebP q-from-eval, **4:4:4 chroma** |
| Ingest + quality | ~60ms | `pyvips` over Pillow |
| Preprocess | ~150ms | Parallel across images |
| Extract | **the unknown** | Parallel calls, prompt cache, `effort: low` |
| Compare | ~1ms | Pure functions |
| Render | ~50ms | — |

**Compression speeds transfer, not extraction.** Token count is a function of pixel dimensions, not
file bytes — a 2576px image costs ~4,784 image tokens whether the file is 200KB or 2MB. Compress to
shrink the upload; do not expect it to move the extract number.

### Client-side encode — pinned pipeline

Resize and encode are two steps, not two options. One path, no branching:

```ts
// 1. Decode + resize off the main thread, GPU-backed. Never draw-to-canvas to resize —
//    it janks the UI and is slower.
const bmp = await createImageBitmap(file, {
  resizeWidth: 2576,              // long edge; see note below
  resizeQuality: 'high',
});

// 2. Encode. OffscreenCanvas keeps this off the main thread too.
const canvas = new OffscreenCanvas(bmp.width, bmp.height);
canvas.getContext('2d')!.drawImage(bmp, 0, 0);
const blob = await canvas.convertToBlob({ type: 'image/webp', quality: WEBP_Q });
```

Compute the long edge from the source orientation — clamp `resizeWidth` for landscape,
`resizeHeight` for portrait. Never upscale: skip the resize entirely when the source is already
under 2576px.

| Choice | Verdict |
|---|---|
| `createImageBitmap` for resize | **Picked.** Off-main-thread, GPU-backed, ~10–20ms. |
| WebP for encode | **Picked.** ~25–35% smaller than JPEG at matched quality, hardware-accelerated everywhere modern. |
| `OffscreenCanvas.convertToBlob` | **Picked** over `HTMLCanvasElement.toBlob` — keeps encode off the main thread. |
| WebCodecs `ImageEncoder` | **Rejected as baseline.** Faster, but Safari support is partial. Not worth a second code path. |
| AVIF | **Rejected.** Encoder costs hundreds of ms — spends more compressing than it saves transferring. |

**Fallback:** if `OffscreenCanvas.convertToBlob` or WebP encode is unavailable, fall back to
`canvas.toBlob(cb, 'image/jpeg', WEBP_Q)` with **4:4:4 chroma**. Default 4:2:0 subsampling halves
color resolution — harmless for photos, not harmless for small colored text, and the warning
statement is the smallest text on the label.

`WEBP_Q` is set by LP-322, not by feel.

**JPEG/WebP quality is an eval output, not a constant.** Compression artifacts are ringing on
high-contrast edges — exactly what small text is. Run the golden set at q95/q85/q75 and measure
**warning-field accuracy specifically**, then pin the value. (New ticket, §9.)

**Avoid AVIF for the client-side encode.** Excellent ratios, but the encoder costs hundreds of
milliseconds — you would spend more compressing than you save transferring.

**Measure from hour one.** `scripts/timed_p95.py` runs against the real provider on day one, not at
F2. If p95 trends past ~4s, the ladder is: fast mode on Opus 5 (~2.5× output speed, Claude API only,
`speed: "fast"` + beta `fast-mode-2026-02-01`) → `claude-sonnet-5` (same 2576px high-res tier, so no
loss on warning legibility) → re-examine. LP-079 is the floor: over-budget returns partial results
as `needs_review`, never a hang.

---

## 9. Ticket board deltas

Recorded rather than silently applied. `TICKETS.md` should be updated to match.

**Promoted to §M (MVP)** — both are latency-critical and currently sit in F2, after the gate they
protect:

| Ticket | Was | Why |
|---|---|---|
| LP-279 client-side downscale before upload | F2 | Largest single latency lever; also decouples p95 from the grader's network |
| LP-280 parallel extraction when >1 image | F2 | TC-16 is a two-image case; serial doubles the dominant term |

**New tickets:**

| ID | Section | Ticket |
|---|---|---|
| LP-321 | M2 | Pre-gate: hopeless-quality image short-circuits to Unreadable with **zero model calls** |
| LP-322 | E3 | Compression-quality sweep — q95/q85/q75 vs warning-field accuracy; pin the value with evidence |
| LP-323 | M2 | Prompt-cache wiring + `cache_read_input_tokens` assertion test (static system prompt, commodity in user message) |
| LP-324 | M6 | Cache pre-warm (`max_tokens: 0`) on the keep-warm ping |
| LP-325 | E5 | Confidence-triggered escalation: re-extract cropped evidence region at full resolution |
| LP-326 | E3 | Label-region crop-before-send — **measured, not assumed**; ships only if detection proves reliable across the robustness set (a bad crop can slice off the warning) |

---

## 10. Out of scope for an automated build

These cannot be produced by a coding agent and remain human-run. They are **not** cut from the
project — they become a checklist to execute against the built app.

| Ticket | Why |
|---|---|
| LP-273 hallway test (≥3 cold users) | Requires real humans |
| LP-235 hand-verified golden verdicts, initialed | Requires human sign-off by definition |
| LP-281 300-item live batch | Requires real provider spend at scale |
| LP-283 keep-warm across 48h | Requires 48h of wall-clock |
| LP-314 cross-browser (Chrome/Safari/Firefox/Edge) | Requires real browsers |
| LP-315 tablet pass | Requires a device |
| LP-244 auto-rollback drill | Requires a live deploy to break |
| LP-264 keyboard-only walkthrough (recorded) | Automated axe pass ships; the recording does not |

Deploy (LP-127–138) runs as soon as `ANTHROPIC_API_KEY` and a host token are in the environment.

---

## 11. Definition of done

```bash
ruff check . && mypy --strict api/        # lint + types
npm --prefix web run lint && npm --prefix web run typecheck
pytest -q                                 # unit + integration, fixtures only, no network
npm --prefix web run test
python eval/run.py --golden golden/set.json   # accuracy + ZERO false passes on warning rows
npx playwright test                       # E2E: happy, batch, unreadable, provider-down
docker build -t labelproof . && docker run -p 8000:8000 labelproof
python scripts/timed_p95.py http://localhost:8000 --runs 20
```

All green, `eval/run.py` reporting **0 false passes on warning violations**, and the p95 table
committed. Anything short of that is reported as incomplete with the specific gap named — never
rounded up to "done."
