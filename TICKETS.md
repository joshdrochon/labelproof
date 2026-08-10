# LabelProof — Ticket Board

Execution plan for the LabelProof build. The graded deliverables are the working app, repo,
README, and deployed URL — this board is the plan behind them, and the import source for a
tracker if one is used.

| | |
|---|---|
| **Source of truth** | `PRD.md` v1.0 — every ticket traces to a requirement ID there (Appendix A) |
| **Source brief** | Take-home docx, sha `7f50443d68066298…` |
| **MVP** | Day 2 EOD — §M tickets |
| **Early Submission** | Day 4 EOD — §E tickets |
| **Final** | Day 7 noon — §F tickets |
| **Ticket prefix** | `LP-001` … `LP-320` |

**Scope law (locked):** *"A working core application with clean code is preferred over
ambitious but incomplete features"* — no §E ticket starts while a §M checklist item is open,
no §F polish while §E core is red. Cuts are documented as trade-offs, never hidden (SCOPE-5).

**Asymmetry law (locked):** when uncertain, flag — never pass. The zero-false-pass gate on
warning violations (LP-215, LP-290) is release-blocking at every milestone.

---

## Working process

- **This file** owns what tickets *exist*. A tracker (if imported) owns what state they're *in*.
  Checkboxes here are synced from the tracker, never hand-edited into "progress."
- Closing a ticket requires a commit naming it in a **`Closes:` trailer** (`Closes: LP-040..LP-049`).
  A close with no commit behind it is a false close.
- Only mark In Progress what is actually being worked on now.

---

## Requirement traceability

The 114 requirements in `PRD.md` Appendix A, mapped to the tickets that satisfy them.
(Authority split: the PRD owns requirement definitions; this table owns the mapping.)

| Requirement family | Tickets |
|---|---|
| PERF-1..7 (5-second gate, batch clock, no dead spinners) | LP-059, 063, 078–079, 090, 099, 107, 114, 119–120, 126, 134, 144, 154, 164–165, 222, 277–286 |
| UX-1..10 (73-year-old benchmark, checklist UI, plain language) | LP-093–115, 167–180, 263–276, 314–319 |
| BATCH-1..10 (300-at-once, manifest, triage, export) | LP-147–182, 281 |
| IMG-1..8 (angles, lighting, glare, honesty, front+back) | LP-054–058, 066–067, 183–202, 217 |
| MATCH-1..10 (taxonomy, normalization, judgment tiers) | LP-019–020, 023–036, 044–045, 048, 219–232 |
| WARN-1..9 (exact text, caps+bold, prominence, honesty) | LP-021–022, 046–047, 053, 203–218 |
| FIELD-1..9 / TYPE-1..3 (field matrix, commodity rules) | LP-017–018, 032–048, 041–043 |
| SEC-1..10 (PII, retention TTL, EXIF, hardening) | LP-010, 013, 054, 076, 081–086, 152, 249–262, 305 |
| NET-1..5 (egress table, server-brokered AI, degradation) | LP-049–050, 061, 080, 091, 132, 138, 302 |
| HITL-1..6 (agent decides, evidence, overrides, report) | LP-103–106, 113, 225, 231, 270 |
| OPS-1..6 (stage timings, golden set, eval gates, cost logs) | LP-062–063, 068–071, 117–126, 199, 215, 233–236, 284, 287–290, 295–296 |
| ENG-1..9 (regression+rollback, E2E, fakes, breakers, docs) | LP-005–009, 059–061, 064–065, 116, 128–131, 136–137, 232, 237–248, 304, 313 |
| DEL-1..7 (repo, README, URL, label set, trade-offs) | LP-001–003, 068, 098, 139–146, 234, 294, 301–312 |
| SCOPE-1..5 (standalone, assistive, assumptions logged) | LP-004, 141, 261–262, 310, 317–318 |

Canonical test cases TC-01–TC-22 (PRD §Test Cases) are each a named regression test — wired
in LP-237, with fixtures across LP-068, 163, 195–198, 216, 226–229.

---

# §M · MVP — due Day 2, EOD

## M0 · Foundations

- [ ] **LP-001** Initialize `labelproof` repo, default branch `main`, license, `.gitignore` (DEL-2)
- [ ] **LP-002** Commit `PRD.md` (source of truth) and `TICKETS.md`
- [ ] **LP-003** Seed `CHANGES.md` with run/test/rollback skeleton (ENG-5)
- [ ] **LP-004** Architecture Defense note: stack, host, provider — decisions + rationale recorded in README (SCOPE-5)
- [ ] **LP-005** Scaffold workspace per stack decision; empty build passes
- [ ] **LP-006** Lint + formatter config, CI-enforced (ENG-8)
- [ ] **LP-007** Typecheck in strict mode (ENG-8)
- [ ] **LP-008** Unit test runner wired; one passing placeholder
- [ ] **LP-009** CI pipeline v1: install → lint → typecheck → unit on every push (ENG-1)
- [ ] **LP-010** Pre-commit secrets scan (SEC-6)
- [ ] **LP-011** `.env.example` documenting every config var; app fails fast when missing
- [ ] **LP-012** Error taxonomy module: user / image / provider / internal (OPS-5)
- [ ] **LP-013** Structured logger with request IDs; **no-content rule** stated at module top (SEC-4)
- [ ] **LP-014** `assets/samples/`: Old Tom Distillery application JSON (brief's exact sample fields)
- [ ] **LP-015** README header: brief sha + PRD version pinned
- [ ] **LP-016** Commit convention: `Closes: LP-…` trailers (board integrity)

## M1 · Domain model & rules engine

- [ ] **LP-017** Commodity enum (spirits/wine/malt) + application schema: brand, class/type, ABV, proof?, net contents, producer name+address, origin, import flag (FIELD-8)
- [ ] **LP-018** Extraction schema: per-field value + confidence + evidence region + source-image provenance (MATCH-6, HITL-3)
- [ ] **LP-019** Verdict enum exactly per PRD taxonomy — six values, no more (MATCH-1)
- [ ] **LP-020** `FieldResult`: verdict, extracted, expected, confidence, rationale, evidence (MATCH-5)
- [ ] **LP-021** Canonical warning constant — **character-for-character from PRD Appendix B**, source comment to 27 CFR 16.21 (WARN-1)
- [ ] **LP-022** Unit test: constant matches Appendix B string (guards typo drift) (WARN-1)
- [ ] **LP-023** Normalization: Unicode NFKC + case folding (MATCH-3)
- [ ] **LP-024** Normalization: whitespace collapse + trim (MATCH-3)
- [ ] **LP-025** Normalization: straight/curly quote + apostrophe unification — **the STONE'S THROW character** (MATCH-3)
- [ ] **LP-026** Normalization: diacritic folding (MATCH-3)
- [ ] **LP-027** Normalization: line-break hyphenation rejoin (MATCH-3)
- [ ] **LP-028** Normalization unit tests incl. idempotency property tests
- [ ] **LP-029** Tier-1 comparator: exact-after-normalization → Match — applied to brand, class/type, producer, origin (FIELD-1, FIELD-2, FIELD-5)
- [ ] **LP-030** Tier-2 classifier: case style / punctuation / hyphenation variation → Acceptable variation + auto note (MATCH-2, MATCH-9)
- [ ] **LP-031** Named test TC-02: `STONE'S THROW` vs `Stone's Throw` → Acceptable variation with visible note
- [ ] **LP-032** ABV parser: `45% Alc./Vol.`, `Alcohol 45% by volume`, `alc. 45% by vol.`, bare `45%` (MATCH-7, FIELD-3)
- [ ] **LP-033** Proof cross-check: proof = 2 × ABV else internal-consistency finding — TC-09 (MATCH-7)
- [ ] **LP-034** Spirits abbreviation rule: bare "ABV" on spirits → format finding — TC-22 (Appendix B)
- [ ] **LP-035** Numeric comparator: equality after normalization; Mismatch carries the delta (MATCH-8)
- [ ] **LP-036** Tolerance context table (±0.3 / ±1.5 / ±1.0 / ±0.3 pp) rendered as context, **never** as a pass-excuse (MATCH-8)
- [ ] **LP-037** Net contents parser: `750 mL`/`750ML`/`75 cl`/`1 L`/fl oz → canonical mL (FIELD-4)
- [ ] **LP-038** Spirits standards-of-fill table (25 sizes, Appendix B) + validator finding — TC-10 (FIELD-4)
- [ ] **LP-039** Wine standards-of-fill table + even-liter ≥4L rule (FIELD-4)
- [ ] **LP-040** Malt: no fill standards — accurate-statement rule only (TYPE-2)
- [ ] **LP-041** Per-commodity required/optional matrix — **data-driven table, not branching code** (FIELD-9, TYPE-2)
- [ ] **LP-042** Wine ABV exception: "table wine"/"light wine" ≤14% → Not applicable — TC-17 (TYPE-3)
- [ ] **LP-043** Malt ABV optionality — TC-18 (TYPE-3)
- [ ] **LP-044** Country-of-origin comparator + import-flag rule — TC-19 (FIELD-6)
- [ ] **LP-045** Producer name/address comparator (normalized, address-format tolerant) (FIELD-5)
- [ ] **LP-046** Warning presence detector: absent everywhere → Missing → aggregate Reject candidate — TC-07 (WARN-6, FIELD-7)
- [ ] **LP-047** Warning verbatim comparator emitting tokenized diff (WARN-1, WARN-8)
- [ ] **LP-048** Aggregate verdict: worst-of + warning-first ranking + recommendation; unit tests per rule (MATCH-10)

## M2 · Extraction pipeline

- [ ] **LP-049** Provider abstraction: `extractLabel(images, commodity) → Extraction` — the single choke point every AI call passes through (NET-4)
- [ ] **LP-050** Vision adapter v1 behind the interface; **server-side only — the browser never talks to the provider** (NET-2)
- [ ] **LP-051** Structured-output schema: all fields + confidence + regions + typography signals, validated on receipt
- [ ] **LP-052** Extraction prompt v1: one call per image set, cross-field context preserved (proof↔ABV) (PRD §Processing Model)
- [ ] **LP-053** Typography signals: warning header caps? bold? body bold? relative size class (WARN-2/7 groundwork)
- [ ] **LP-054** Ingest: EXIF auto-orient, then **strip all metadata incl. GPS** (SEC-3, IMG-6)
- [ ] **LP-055** Re-encode + downscale strategy that preserves small warning text (SEC-5, IMG-6)
- [ ] **LP-056** HEIC → JPEG conversion (IMG-7)
- [ ] **LP-057** PDF label-proof rendering, constrained (page cap, timeout) (IMG-7, SEC-5)
- [ ] **LP-058** Multi-image merge: per-field provenance, best-confidence wins, conflicts → Needs review — TC-16 (IMG-8)
- [ ] **LP-059** Explicit timeouts on provider calls, budgeted against the 5s gate (ENG-4, PERF-1)
- [ ] **LP-060** Bounded retries, exponential backoff + jitter (ENG-4)
- [ ] **LP-061** Circuit breaker; open → plain-language degradation — TC-21 (NET-3)
- [ ] **LP-062** Token + cost capture on every call (OPS-4)
- [ ] **LP-063** Stage latency capture: preprocess / extract / compare (OPS-1)
- [ ] **LP-064** Fixture capture tool: record real provider responses as replayable fixtures (ENG-3)
- [ ] **LP-065** Fake adapter replaying fixtures; **CI uses fakes only — zero live calls** (ENG-3)
- [ ] **LP-066** Non-label detection: the cat photo gets a graceful sentence, not a stack trace — TC-15 (IMG-7)
- [ ] **LP-067** Fabrication guard: unreadable region ⇒ null + Unreadable, never a value — asserted — TC-14 (IMG-5)
- [ ] **LP-068** Golden set v1: 10 labels incl. Old Tom clean case; generation prompts committed (OPS-2, DEL-5)
- [ ] **LP-069** Expected-verdicts JSON format for golden labels (OPS-2)
- [ ] **LP-070** Eval CLI v1: golden set → field accuracy + confusion matrix (OPS-2)
- [ ] **LP-071** Eval in CI on fixtures with threshold gate (OPS-6)
- [ ] **LP-072** Integration test: full extraction pipeline on fixtures

## M3 · API service

- [ ] **LP-073** API scaffold; JSON errors speak the error taxonomy (OPS-5)
- [ ] **LP-074** `POST /verify`: multipart (1..N images + application JSON) → FieldResults + aggregate + timings
- [ ] **LP-075** Request validation with plain-language 400s (UX-6)
- [ ] **LP-076** Upload caps (size, dimensions, count) + content-type **sniffing**, not extension trust (SEC-5)
- [ ] **LP-077** Request-ID middleware + structured request logs, content-free (SEC-4)
- [ ] **LP-078** Stage-timing middleware; timings in the response payload (OPS-1, PERF-2)
- [ ] **LP-079** Total-budget enforcement: over-deadline → partial results + Needs review, never a blown request (PERF-7)
- [ ] **LP-080** `/health` (up) + `/ready` (provider reachable, config valid) (NET-5)
- [ ] **LP-081** Rate limiting (SEC-9)
- [ ] **LP-082** Security headers + strict CORS (SEC-6)
- [ ] **LP-083** HTTPS-only + HSTS at the edge (SEC-6)
- [ ] **LP-084** Retention TTL: scheduled purge of uploads/results, TTL configurable, default 24h (SEC-2)
- [ ] **LP-085** Purge verification test: artifacts provably gone after TTL (SEC-2)
- [ ] **LP-086** No-PII log test: run a verify, scan logs for any label string (SEC-4)
- [ ] **LP-087** Config from env only; fail-fast on missing keys
- [ ] **LP-088** `GET /sample`: serves the Old Tom demo pair for the one-click demo (UX-1)
- [ ] **LP-089** API integration tests: happy, invalid, oversized, wrong-type, provider-down (fixtures)
- [ ] **LP-090** API-layer latency ceiling test in fixture mode (PERF-1)
- [ ] **LP-091** Endpoint + egress documentation section drafted (NET-1)
- [ ] **LP-092** Stateless request path verified: restart loses nothing a user cares about

## M4 · Web UI — Verify Now

- [ ] **LP-093** Web scaffold + design tokens: ≥16px base, WCAG AA palette, ≥44px targets (UX-3)
- [ ] **LP-094** Single-screen verify layout, one primary action, zero nav maze (UX-2)
- [ ] **LP-095** Image dropzone: drag-drop + click-to-browse + multi-image with thumbnails (IMG-8)
- [ ] **LP-096** Application form: commodity select + field inputs; import toggle reveals origin (FIELD-*)
- [ ] **LP-097** Paste-friendly inputs — agents copy out of COLA screens (UX-7)
- [ ] **LP-098** **"Try a sample" button**: Old Tom loaded, grader reaches a verdict in one click (UX-1, UX-8, HITL-6, DEL-1)
- [ ] **LP-099** Submit → progress within 1s; stage narration ("Reading label…") (PERF-7)
- [ ] **LP-100** Results as a per-field checklist card — **deliberately the paper checklist, digitized** (UX-5)
- [ ] **LP-101** Verdict chips: icon + word, never color alone (UX-3)
- [ ] **LP-102** Aggregate banner: recommendation phrased as advice, agent decides (MATCH-10, HITL-1, SCOPE-3)
- [ ] **LP-103** Evidence: application vs extracted value side-by-side per field (UX-9)
- [ ] **LP-104** Image region highlight on hover/focus per field (HITL-3)
- [ ] **LP-105** "Why" rationale affordance on every non-exact verdict (HITL-4)
- [ ] **LP-106** Confirm / override per field; session-tracked (HITL-2)
- [ ] **LP-107** Elapsed-time chip on every result card (PERF-2)
- [ ] **LP-108** Unreadable UI: the retake reason in the agents' own phrase — "request a better image" (IMG-4)
- [ ] **LP-109** Error states: bad file, too big, non-label, provider down — plain words + next step (UX-6)
- [ ] **LP-110** Empty state: upload → enter → verify, three steps pictured (UX-1)
- [ ] **LP-111** Keyboard: full flow operable, visible focus, shortcuts for confirm/override/next (UX-4)
- [ ] **LP-112** Screen-reader labels + live-region result announcement (UX-4)
- [ ] **LP-113** Report export: findings + agent decisions, print-friendly — **Dave will print it** (HITL-5)
- [ ] **LP-114** Loading skeletons everywhere; no dead spinners (PERF-7)
- [ ] **LP-115** Copy pass: agents' vocabulary, zero ML jargon (UX-6)
- [ ] **LP-116** E2E smoke: sample → verdict, fixture-mode, asserts the 5s ceiling (ENG-2)

## M5 · Observability & evaluation wiring

- [ ] **LP-117** Log schema doc: fields, levels, correlation (OPS-5)
- [ ] **LP-118** Per-request cost line: tokens in/out + computed cost (OPS-4)
- [ ] **LP-119** Timing rollup script: p50/p95 from logs (OPS-1)
- [ ] **LP-120** 20-run timed p95 script against any URL; outputs a committable table (PERF-1)
- [ ] **LP-121** Eval reports: accuracy, confusion matrix, **zero-false-pass check on warning rows** (OPS-3)
- [ ] **LP-122** CI gates: eval thresholds; warning zero-false-pass is hard-blocking (OPS-6)
- [ ] **LP-123** Fixture determinism: two CI runs, byte-identical eval output (ENG-3)
- [ ] **LP-124** Error-rate summary in the rollup (OPS-5)
- [ ] **LP-125** README ops section: reading logs, timings, costs (ENG-5)
- [ ] **LP-126** Honesty check: UI-displayed time equals server-measured time (PERF-2)

## M6 · Deployment

- [ ] **LP-127** Host + region decision recorded with rationale (Architecture Defense)
- [ ] **LP-128** Infra-as-config: service, env config, secret refs — no secrets in repo (ENG-6)
- [ ] **LP-129** Reproducible build from a clean clone (ENG-6)
- [ ] **LP-130** Deploy gated on green CI (ENG-1)
- [ ] **LP-131** Auto-rollback on failed post-deploy health (ENG-1)
- [ ] **LP-132** `/health` + `/ready` wired into platform checks (NET-5)
- [ ] **LP-133** HTTPS verified on the public URL (SEC-6)
- [ ] **LP-134** Keep-warm: first grader hit answers ≤5s, no cold-start ambush (PERF-6)
- [ ] **LP-135** Post-deploy smoke: sample verify on production
- [ ] **LP-136** Destroy-and-redeploy test: environment rebuilds from config alone; output recorded (ENG-6)
- [ ] **LP-137** Rollback procedure documented in `CHANGES.md` (ENG-5)
- [ ] **LP-138** Egress table: every outbound domain from production (NET-1)

## M7 · Docs & MVP gate

- [ ] **LP-139** README v1: setup/run/test — verified by cold clone (DEL-3, ENG-7)
- [ ] **LP-140** README: approach + tools used (DEL-4)
- [ ] **LP-141** README: assumptions log seeded — every gap the brief left, filled and stated (SCOPE-5)
- [ ] **LP-142** `CHANGES.md` current through MVP (ENG-5)
- [ ] **LP-143** MVP checklist self-audit vs `PRD.md` §MVP; gaps become tickets before anything else does
- [ ] **LP-144** Timed p95 run on the deployed URL, table committed (PERF-1)
- [ ] **LP-145** Demo walkthrough script v1 (DEL: demo)
- [ ] **LP-146** Tag `v0.1-mvp`

---

# §E · Early Submission — due Day 4, EOD

## E1 · Batch backend

- [ ] **LP-147** Job model: batch, items, states, timestamps (BATCH-4)
- [ ] **LP-148** Manifest CSV schema: application fields + image filename refs; template committed (BATCH-3)
- [ ] **LP-149** Manifest parser: per-row validation, **row-numbered errors** — TC-20 (BATCH-3)
- [ ] **LP-150** Zip intake + image↔row pairing; unmatched files reported by name (BATCH-3)
- [ ] **LP-151** Alternative pairing via multi-select upload — no zip tooling required (UX-7)
- [ ] **LP-152** Job store with TTL purge — batches are as ephemeral as singles (SEC-2)
- [ ] **LP-153** Worker pool, configurable concurrency (BATCH-9)
- [ ] **LP-154** Provider throttle: shared budget, **Verify Now holds priority** (BATCH-9, PERF-5)
- [ ] **LP-155** Per-item isolation: one bad image fails one item, never the batch — TC-20 (BATCH-6)
- [ ] **LP-156** Per-item bounded retries, then failed-with-reason (BATCH-8)
- [ ] **LP-157** Retry endpoint: failed items only, no reprocessing the finished 290 (BATCH-8)
- [ ] **LP-158** Job survives service restart (state persisted) (BATCH-6)
- [ ] **LP-159** Progressive results: completed items pollable/streamable immediately (BATCH-5)
- [ ] **LP-160** Job status: counts by state + ETA (BATCH-4)
- [ ] **LP-161** Summary aggregation: verdict counts, worst-first order precomputed (UX-10)
- [ ] **LP-162** CSV export: per-item verdicts + findings (BATCH-7)
- [ ] **LP-163** 300-item load fixture generator — TC-20 (BATCH-2)
- [ ] **LP-164** Throughput test: 300 ≤ 10 min at chosen concurrency; fixture-mode + live spot-check (PERF-4)
- [ ] **LP-165** Verify-Now-during-batch test: p95 holds under load (PERF-5)
- [ ] **LP-166** Batch cost accounting per job (OPS-4)

## E2 · Batch UI

- [ ] **LP-167** Batch page, first-class in nav — **Janet has been asking for years; don't bury it** (BATCH-10)
- [ ] **LP-168** Manifest template download + format help (UX-1)
- [ ] **LP-169** Upload flow: manifest + images with inline per-row validation errors (BATCH-3)
- [ ] **LP-170** Progress view: counts, bar, ETA, elapsed (BATCH-4)
- [ ] **LP-171** Progressive results table: rows land as items finish (BATCH-5)
- [ ] **LP-172** Triage order: Reject candidates → Needs review → clean (UX-10)
- [ ] **LP-173** Filters: verdict, field, failure kind (UX-10)
- [ ] **LP-174** Item drill-in reuses the Verify Now results view — one results language everywhere
- [ ] **LP-175** Retry-failed button (BATCH-8)
- [ ] **LP-176** Export button → summary CSV (BATCH-7)
- [ ] **LP-177** Batch summary header: totals, duration, cost
- [ ] **LP-178** Malformed-manifest UX: fix-list with row numbers — TC-20 (UX-6)
- [ ] **LP-179** Keyboard navigation through the triage table (UX-4)
- [ ] **LP-180** Batch empty/error states in plain language (UX-6)
- [ ] **LP-181** E2E: batch flow incl. progressive results (ENG-2)
- [ ] **LP-182** Batch docs: README section + in-app help blurb

## E3 · Image robustness

- [ ] **LP-183** Quality assessor: blur score (IMG-4)
- [ ] **LP-184** Quality assessor: exposure score (IMG-2)
- [ ] **LP-185** Quality assessor: glare detection (IMG-3)
- [ ] **LP-186** Quality assessor: skew/perspective estimate (IMG-1)
- [ ] **LP-187** Quality gate thresholds + per-image quality report (IMG-4)
- [ ] **LP-188** Retake reasons, one per failure kind, in agent phrasing (IMG-4, UX-6)
- [ ] **LP-189** Deskew / perspective correction pass (IMG-1)
- [ ] **LP-190** Contrast/exposure normalization pass (IMG-2)
- [ ] **LP-191** Glare handling: enhancement only — **no inpainting; honesty over cleverness** (IMG-3, IMG-5)
- [ ] **LP-192** Per-field readability: quality judged per region, not per image (IMG-5)
- [ ] **LP-193** Named test TC-12: glare over warning ⇒ warning Unreadable, brand still verified (IMG-5)
- [ ] **LP-194** Fabrication sweep: whole robustness set asserts zero invented values — TC-14 (IMG-5)
- [ ] **LP-195** Angle fixtures: 15° / 30° / 45° — TC-11 (IMG-1)
- [ ] **LP-196** Glare fixtures — TC-12 (IMG-3)
- [ ] **LP-197** Dim-light fixtures — TC-13 (IMG-2)
- [ ] **LP-198** Blur fixtures incl. the hopeless case — TC-14 (IMG-4)
- [ ] **LP-199** Robustness eval: per-condition accuracy tracked in the harness (OPS-2)
- [ ] **LP-200** Threshold tuning round vs eval; chosen values recorded with rationale
- [ ] **LP-201** Curved-surface handling exercised by fixture (bottle cylinder) (IMG-1)
- [ ] **LP-202** Robustness limitations documented — what's handled, what isn't (DEL-6)

## E4 · Government warning deep checks

- [ ] **LP-203** Tokenized diff engine vs canonical text (WARN-8)
- [ ] **LP-204** Diff view UI: word-level highlights — TC-05 (WARN-8)
- [ ] **LP-205** Header caps check: `GOVERNMENT WARNING:` exact casing (WARN-2)
- [ ] **LP-206** Header bold check from typography signals (WARN-2)
- [ ] **LP-207** Body non-bold check — the inverse rule almost everyone misses — TC-04 (WARN-7)
- [ ] **LP-208** **Named regression: Jenny's catch** — title-case header → violation — TC-03 (WARN-3)
- [ ] **LP-209** Reword/paraphrase detection → Mismatch + diff — TC-05 (WARN-4)
- [ ] **LP-210** Omission/truncation detection (partial warning) (WARN-4)
- [ ] **LP-211** Relative-prominence heuristic: warning vs surrounding text size — TC-06 (WARN-5)
- [ ] **LP-212** Low-contrast / buried-text detection → Needs review + region (WARN-5)
- [ ] **LP-213** Type-size honesty: "not verifiable from image" caveat + applicable minimum (from net contents) shown as context (WARN-9)
- [ ] **LP-214** Warning findings pinned first in aggregate + UI (MATCH-10)
- [ ] **LP-215** **Zero-false-pass gate** wired across all warning cases in eval — release-blocking (OPS-3)
- [ ] **LP-216** Warning fixture set: TC-03…TC-07 all present (OPS-2)
- [ ] **LP-217** Multi-image: warning searched across every image before Missing is declared — TC-16 (IMG-8)
- [ ] **LP-218** Warning check docs: checks, regulation cites, honesty limits (DEL-6)

## E5 · Judgment tier (Tier 3)

- [ ] **LP-219** Adjudicator interface: (field, expected, extracted, context) → verdict + confidence + rationale (MATCH-4)
- [ ] **LP-220** Adjudicator prompt: TTB context, judgment framing, strict output schema (MATCH-4)
- [ ] **LP-221** Invocation policy: gray cases only, past Tier 1/2; trigger rate logged (PRD §Processing Model)
- [ ] **LP-222** Time budget: adjudication fits the request budget or returns Needs review (PERF-1)
- [ ] **LP-223** Cost cap per verification; over-cap → Needs review (OPS-4)
- [ ] **LP-224** Confidence threshold routing → Needs review, never silent (MATCH-6)
- [ ] **LP-225** Rationale surfaced in UI on every judged field (MATCH-5, HITL-4)
- [ ] **LP-226** Fixtures: abbreviations — `Co.`/`Company`, `&`/`and` (MATCH-4)
- [ ] **LP-227** Fixtures: reordering — `Old Tom Distillery`/`Distillery of Old Tom` (MATCH-4)
- [ ] **LP-228** Fixtures: DBA / producer variants (MATCH-4)
- [ ] **LP-229** Cost-discipline guard: STONE'S THROW still resolves at Tier 2 — adjudicator not invoked (TC-02)
- [ ] **LP-230** Judgment eval subset: accuracy on judged cases tracked (OPS-2)
- [ ] **LP-231** Judged verdicts overridable; overrides in the report (HITL-2)
- [ ] **LP-232** Adjudicator fakes for CI (ENG-3)

## E6 · Test suite & CI hardening

- [ ] **LP-233** Golden set → ≥25 labels spanning every TC row (OPS-2)
- [ ] **LP-234** Label generation prompts/scripts committed — the brief invites AI-generated labels (DEL-5)
- [ ] **LP-235** Expected-verdicts review: every golden label hand-verified once, initialed in the file (OPS-2)
- [ ] **LP-236** Eval harness v2: per-field + per-condition accuracy, confusion matrix, trend vs last run (OPS-2)
- [ ] **LP-237** Regression suite: TC-01…TC-22 each a named automated test (ENG-1)
- [ ] **LP-238** E2E: Verify Now happy path (ENG-2)
- [ ] **LP-239** E2E: unreadable path (ENG-2)
- [ ] **LP-240** E2E: provider-down degradation — TC-21 (ENG-2)
- [ ] **LP-241** E2E: batch flow consolidated in CI (with LP-181) (ENG-2)
- [ ] **LP-242** CI stages: lint → typecheck → unit → integration → eval → E2E (ENG-1)
- [ ] **LP-243** Red CI blocks deploy — verified with a deliberately failing PR (ENG-1)
- [ ] **LP-244** Auto-rollback drill: forced bad deploy rolls itself back; output recorded (ENG-1)
- [ ] **LP-245** Coverage report on the rules engine; floor enforced on comparators (ENG-8)
- [ ] **LP-246** Flaky policy: no CI retries; determinism or it doesn't merge (ENG-3)
- [ ] **LP-247** CI runtime budget < 10 min (developer experience guards the schedule)
- [ ] **LP-248** Test docs in `CHANGES.md`: how to run each layer (ENG-5)

## E7 · Security & retention

- [ ] **LP-249** EXIF/GPS strip verification test (SEC-3)
- [ ] **LP-250** TTL purge verification incl. batch artifacts (SEC-2)
- [ ] **LP-251** Log audit test: golden-set run produces zero label strings in logs (SEC-4)
- [ ] **LP-252** Upload re-encode verification: polyglot file neutralized (SEC-5)
- [ ] **LP-253** Content-sniffing tests: renamed executable, scripted SVG, absurd dimensions (SEC-5)
- [ ] **LP-254** PDF renderer constrained: page cap, no external fetches, timeout (SEC-5)
- [ ] **LP-255** Rate-limit tests: burst → 429 with a plain-language body (SEC-9)
- [ ] **LP-256** Security headers scan in CI (SEC-6)
- [ ] **LP-257** Dependency audit in CI, pinned versions, baseline documented (SEC-10)
- [ ] **LP-258** Secrets scan: pre-commit + CI (SEC-6)
- [ ] **LP-259** Provider data-handling documented: retention/training posture (SEC-7)
- [ ] **LP-260** Retention policy user-facing: UI notice + README (SEC-2)
- [ ] **LP-261** Synthetic-data disclaimer in the UI footer (SEC-1, SCOPE-4)
- [ ] **LP-262** Production-path doc: FedRAMP-authorized endpoints, Azure alignment, agency IdP, records schedules — documented, not built (SEC-8, SCOPE-2)

---

# §F · Final — due Day 7, noon

## F1 · Accessibility & UX polish

- [ ] **LP-263** Automated a11y audit (axe) on both modes; criticals fixed (UX-4)
- [ ] **LP-264** Keyboard-only full walkthrough, recorded (UX-4)
- [ ] **LP-265** Screen-reader pass: labels, roles, live regions (UX-4)
- [ ] **LP-266** Contrast verification, all states incl. verdict chips (UX-3)
- [ ] **LP-267** Focus order + visible-focus audit (UX-4)
- [ ] **LP-268** 200% zoom usable (UX-4)
- [ ] **LP-269** Reduced-motion support (UX-4)
- [ ] **LP-270** Print stylesheet for the report — **Dave prints; the printout must read** (HITL-5)
- [ ] **LP-271** Copy final pass: reading level, zero jargon (UX-6)
- [ ] **LP-272** Hallway test protocol: tasks + success criteria written first (UX-1)
- [ ] **LP-273** **Run the 73-year-old test:** ≥3 cold users reach a verdict, zero instructions; notes committed (UX-1)
- [ ] **LP-274** Fix rounds from hallway findings (UX-1)
- [ ] **LP-275** Grayscale audit: verdicts distinguishable without color (UX-3)
- [ ] **LP-276** Title/favicon/meta; landing carries no stale results

## F2 · Performance verification

- [ ] **LP-277** p95 timed run (20×) on the deployed URL; table committed — **the stopwatch wins** (PERF-1)
- [ ] **LP-278** Stage-breakdown analysis; attack the slowest stage first (OPS-1)
- [ ] **LP-279** Client-side downscale before upload (payload is the cheapest latency) (PERF-1)
- [ ] **LP-280** Parallel extraction when >1 image (front/back concurrently) (PERF-1)
- [ ] **LP-281** 300-item batch, live: wall-clock + throttle behavior recorded (PERF-4)
- [ ] **LP-282** Verify-during-batch live check: priority lane holds (PERF-5)
- [ ] **LP-283** Keep-warm verified across 48h of first-hit samples (PERF-6)
- [ ] **LP-284** Latency regression gate in CI (fixture ceilings) (OPS-6)
- [ ] **LP-285** UI-time vs server-time honesty check on production (PERF-2)
- [ ] **LP-286** README performance section: numbers, method, trade-offs (DEL-6)

## F3 · Accuracy report

- [ ] **LP-287** Final eval on the full golden set; inputs frozen (OPS-3)
- [ ] **LP-288** Field accuracy vs the ≥95% floor; table published (OPS-3)
- [ ] **LP-289** Confusion matrix published (OPS-2)
- [ ] **LP-290** **Zero false passes on warning violations — verified and published** (OPS-3)
- [ ] **LP-291** Failure analysis: every miss explained; fixed or documented as a limitation (DEL-6)
- [ ] **LP-292** Final thresholds recorded with rationale (OPS-2)
- [ ] **LP-293** Known-limitations list, evidence-backed (DEL-6)
- [ ] **LP-294** Accuracy report committed + linked from README (OPS-3)

## F4 · Cost analysis

- [ ] **LP-295** Dev spend tally from logs: tokens in/out, invocations, total (Cost Analysis)
- [ ] **LP-296** Measured cost per verification: single + batch-amortized (OPS-4)
- [ ] **LP-297** Projections filled: 130 / 600 / 1,200 per day, arithmetic shown (Cost Analysis)
- [ ] **LP-298** Assumptions documented: images/app, tokens/call, Tier-3 rate, retry overhead (Cost Analysis)
- [ ] **LP-299** Cost cliffs documented: resolution↔tokens; concurrency↔throttling (Cost Analysis)
- [ ] **LP-300** ROI stated plainly: cents per label vs 5–10 minutes of agent time (PERF-3, Background)

## F5 · Submission package

- [ ] **LP-301** README final: approach, tools, assumptions, trade-offs, limitations (DEL-4, DEL-6)
- [ ] **LP-302** README: final egress table (NET-1)
- [ ] **LP-303** README: production-path section final (SEC-8)
- [ ] **LP-304** `CHANGES.md` final: complete build log, run/test/rollback current (ENG-5)
- [ ] **LP-305** Repo hygiene: history scanned for secrets, structure clean (SEC-6)
- [ ] **LP-306** Fresh-clone test on a clean machine; README gaps fixed (DEL-3)
- [ ] **LP-307** Deployed URL final smoke + keep-warm confirmed (DEL-1)
- [ ] **LP-308** Sample label set downloadable inside the app — graders test in seconds (DEL-5)
- [ ] **LP-309** Demo script final: 3 minutes — speed, checklist, STONE'S THROW, the warning catch, batch triage (DEL: demo)
- [ ] **LP-310** PRD final sync: shipped reality matches the doc; deltas listed as trade-offs (DEL-7)
- [ ] **LP-311** TICKETS final sync: board true, counts updated (DEL-7)
- [ ] **LP-312** Tag `v1.0` + release notes

## F6 · Final QA

- [ ] **LP-313** Full regression + eval + E2E green on the final commit (ENG-1)
- [ ] **LP-314** Cross-browser: Chrome, Safari, Firefox, Edge (UX)
- [ ] **LP-315** Tablet sanity pass (UX)
- [ ] **LP-316** Error-path sweep: every error state visited by hand (UX-6)
- [ ] **LP-317** PRD checklists (MVP + Final): line-by-line self-audit (ENG-9)
- [ ] **LP-318** Evaluation-criteria walkthrough: the brief's six criteria, each with pointable evidence (BR)
- [ ] **LP-319** Fresh-eyes full walkthrough; final fixes (UX-1)
- [ ] **LP-320** Submission sign-off: deliverables table complete, everything reachable (DEL-1..7)

---

## Counts

| Section | Tickets | Range |
|---|---|---|
| §M · MVP | 146 | LP-001 – LP-146 |
| §E · Early Submission | 116 | LP-147 – LP-262 |
| §F · Final | 58 | LP-263 – LP-320 |
| **Total** | **320** | |

---

## Sequencing risks

1. **Provider latency variance vs. the 5s gate.** The budget is the product. Run LP-120 timed
   checks from day one, not at F2 — if p95 trends past ~4s, LP-279/280 (payload + parallelism)
   jump the queue.
2. **Typography detection reliability (bold/caps).** The riskiest extraction ask. LP-053
   signals + LP-211 prominence heuristics both feed it; the fallback is honest Needs review,
   never a guessed Match — the asymmetry law absorbs model weakness.
3. **Rate limits vs. the 300-batch clock.** LP-154 throttling and LP-164 throughput tests must
   run against real provider limits early in §E; concurrency is a measured number, not a hope.
4. **Golden-set labeling time.** Hand-verifying 25+ labels (LP-235) is slow, boring, and
   non-parallelizable late. Start at LP-068 (MVP) and grow continuously.
5. **Cold starts on hobby-tier hosting.** LP-134 keep-warm is an MVP ticket precisely because
   graders click once — the vendor pilot died on first impressions.
6. **Scope creep vs. the brief's own law.** *"Working core preferred over ambitious
   incomplete."* Milestone gates (LP-143, LP-317) are the enforcement; cuts land in the
   trade-offs doc, not in silence.
7. **Fixture drift vs. live behavior.** Fakes keep CI green while production rots. LP-164 and
   LP-281 live spot-checks are the tether; any fixture/live mismatch becomes a ticket the same
   day it's seen.
