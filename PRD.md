# LabelProof

*An AI-Powered Alcohol Label Verification App for TTB Compliance Review*

| | |
|---|---|
| **Status** | Source of truth for the LabelProof build. Tickets in `TICKETS.md` trace to requirement IDs defined here. |
| **Source brief** | `TakeHome Project: AI-Powered Alcohol Label Verification App.docx`, sha `7f50443d68066298…` |
| **Format blueprint** | GFA Week 5 FleetGraph PRD (structure only — content is 100% take-home derived) |
| **Version** | 1.0 — 2026-08-10 |
| **Regulatory canon** | Appendix B (verified against eCFR / ttb.gov, retrieved 2026-08-10) |

---

## Background

**The TTB reviews about 150,000 label applications a year with 47 agents. Half of that work is matching a number on a form to a number on a label.**

An agent pulls up an application, looks at the label artwork, and checks that what's on the label matches what's in the application. Brand name matches? Check. ABV is correct? Check. Government warning is there? Check. Five to ten minutes per application for a simple one — longer if there are issues. The team has worked this way since the COLA system went online in 2003, and headcount has fallen from over 100 agents to 47. The agents aren't incapable of complex analysis; they're drowning in routine verification that is essentially data entry.

They have tried automation before, and it failed for a specific, instructive reason: the scanning vendor's system took 30–40 seconds per label, so agents went back to doing it by eye — they could do five labels in the time the machine did one. Speed is not a nice-to-have here. It is the adoption gate.

Your job is to build a prototype that verifies alcohol beverage label artwork against its application data: extract what's on the label, compare it field-by-field to what's in the application, and present per-field verdicts with evidence — fast enough that an agent never waits, clear enough that the least technical agent on the team needs no training, and honest enough that a 28-year veteran trusts its judgment calls. The app must be **assistive, not decisive**: it surfaces findings and recommendations; the agent always makes the final determination.

The underlying goal is not to demo an AI extraction call. It is to prove that routine label verification can be compressed from minutes to seconds without sacrificing the judgment, exactness, and trust that compliance review demands — with working software a TTB agent could pick up cold.

---

## Project Overview

One-week build with three checkpoints (the brief sets no deadlines; this cadence is self-imposed and mirrors how the work will be sequenced and gated):

| Checkpoint | Deadline | Gate |
|---|---|---|
| Architecture Defense | Day 0 + 4 hours | Stack, AI pipeline, and processing model decisions written down and defensible |
| MVP | Day 2, EOD | Single-label verify, end-to-end, on the deployed URL, under budget |
| Final Submission | Day 7, noon | Batch, robustness, warning deep checks, judgment tier, full test suite, accessibility, performance verification, cost analysis, submission package |

The brief is explicit about priorities: *"A working core application with clean code is preferred over ambitious but incomplete features."* Milestone gates enforce that — no Early-scope feature merges while an MVP checklist item is open.

---

## The Two Modes of LabelProof

LabelProof operates in two distinct modes. You must implement both.

### Verify Now — one application, instant answer

An agent working their queue uploads label artwork (one or more images — front and back labels are separate photos), enters or pastes the application's data, and gets a per-field verdict card. Results return in about 5 seconds. This is the mode that replaces the eyeball checklist an agent runs 20+ times a day.

### Batch — the importer dump

During peak season, big importers file 200–300 label applications at once, and today the team processes them one at a time. Batch mode accepts a manifest of application data plus the corresponding images, processes them as a background job with progressive results, and produces a triage table — worst findings first — plus an exportable summary. An agent reviews a batch the way they'd want to: mismatches surfaced immediately, clean matches confirmed at a glance.

**Both modes run through the same verification pipeline.** The difference is the entry point and the processing model (synchronous request vs. queued job), not the verification logic. A field verdict means the same thing everywhere.

---

## What the App Is Responsible For

This section defines the app's authority and its limits. Every downstream design decision inherits from it.

### What it verifies

For each application, the app compares label artwork against application data across the mandatory TTB label elements: brand name, class/type designation, alcohol content, net contents, name and address of the bottler/producer, country of origin (imports), and the government health warning statement. Field-level rules, per-commodity variations, and matching policy are specified in **Verification Requirements** below.

### What it decides autonomously

- Per-field verdicts from a fixed taxonomy (below), each with confidence and a plain-language rationale.
- An aggregate recommendation per application: **Ready to approve** / **Needs review** / **Reject candidate** — derived from the worst field verdict, with warning-statement findings always ranked first.
- Image-quality determinations: whether each field was legible enough to verify at all.

### What it must never do

- Approve or reject an application. The agent decides; the app recommends. *(Dave: "You need judgment." The app supplies judgment as evidence, not as authority.)*
- Guess. A field it cannot read is **Unreadable**, never a fabricated value. A false "match" on an unreadable warning statement is the worst possible failure of this product.
- Hide its reasoning. Every non-exact verdict shows what was extracted, where on the image it came from, and why the verdict was chosen.

### Verdict taxonomy

| Verdict | Meaning | Example |
|---|---|---|
| **Match** | Label value equals application value after format normalization | `750 mL` vs `750ML` |
| **Acceptable variation** | Differs, but explainably equivalent; shown to the agent as a judgment call, never silently passed | `STONE'S THROW` vs `Stone's Throw` |
| **Mismatch** | Substantively different | ABV 45% on application, 40% on label |
| **Missing** | Required element absent from the label | No government warning found |
| **Unreadable** | Image quality prevents verification of this field | Glare obscures the warning text |
| **Not applicable** | Not required for this commodity/case | ABV on a malt beverage with no state mandate |

Every verdict carries: extracted value, application value, confidence, rationale string, and evidence region on the source image. The UI renders the taxonomy as a checklist — deliberately mirroring the printed checklist agents use today *(Jenny: "I literally have a printed checklist on my desk")* — with icon + word, never color alone.

### Who acts, and when

The agent confirms or overrides each finding; overrides are captured in the exported report. The app never contacts applicants, never files decisions, and never mutates any system of record — it is a standalone prototype by explicit constraint *(Marcus: "we're not looking to integrate with COLA directly")*.

---

## Verification Requirements

The core domain logic. Field rules first, then the matching policy that governs all of them, then the two hard sub-problems: the government warning and imperfect images.

### Field matrix

| Field | Spirits | Wine | Malt | Comparison method |
|---|---|---|---|---|
| Brand name | Required | Required | Required | Normalized + judgment tiers |
| Class/type designation | Required | Required | Required | Normalized + judgment tiers |
| Alcohol content | Required | Required unless "table wine"/"light wine" ≤14% | Optional (state law may mandate) | Numeric equivalence after format parsing |
| Net contents | Required | Required | Required | Numeric + standards-of-fill validation |
| Bottler/producer name & address | Required | Required | Required | Normalized + judgment tiers |
| Country of origin | Required for imports | Required for imports | Required for imports | Normalized |
| Government warning | Required (all beverages ≥0.5% ABV) | Required | Required | Verbatim canonical text + typography checks |

Commodity is selected per application (beer/malt, wine, distilled spirits) and drives required/optional status, format rules, and the standards-of-fill list *(brief: "The exact requirements vary by beverage type")*. The rule engine is data-driven — a per-commodity rules table, not branching code — so rules can be corrected without touching pipeline logic.

Field-specific rules the engine must encode (regulatory detail and sources in Appendix B):

- **Alcohol content parsing.** `45% Alc./Vol. (90 Proof)`, `Alcohol 45% by volume`, `alc. 45% by vol.` are the same value. Proof, when present, must equal 2× ABV — `90 Proof` alongside `40% Alc./Vol.` is an internal label inconsistency and is flagged as its own finding, distinct from the application comparison. On spirits labels only `alc.` and `vol.` abbreviations are permitted — a bare "ABV" on a spirits label is flagged as a format finding.
- **Net contents.** Parse `750 mL`, `750ML`, `75 cl`, `1 L`, `1.75L` to a canonical value; verify the value appears in the authorized standards-of-fill list for the commodity (spirits: 25 authorized sizes; wine: authorized list + even-liter ≥4L rule). A net contents of `733 mL` is a compliance finding even if it matches the application.
- **ABV numeric comparison.** Label-vs-application comparison expects numeric equality after normalization (`45`, `45.0`, `45%` are equal). Any delta is a **Mismatch** with the delta shown. Regulatory production tolerances (spirits ±0.3 pp; wine ±1.5 pp ≤14% / ±1.0 pp >14%; malt ±0.3 pp) are surfaced as context on the finding — they govern liquid-vs-label, which this tool cannot measure, and the UI must not imply otherwise.
- **Brand name.** The headline fuzzy-matching case — see matching policy.

### Matching policy — "You need judgment"

Dave's case is the specification: *"the brand name was 'STONE'S THROW' on the label but 'Stone's Throw' in the application. Technically a mismatch? Sure. But it's obviously the same thing. You need judgment."* Three tiers, cheapest first:

1. **Tier 1 — exact after normalization.** Unicode normalization, case folding, whitespace collapse, straight/curly quote and apostrophe unification, diacritic folding, terminal punctuation. Equal → **Match**. Deterministic, zero cost, covers the majority.
2. **Tier 2 — explainable variation.** Differences fully accounted for by normalization classes worth telling the agent about (all-caps stylization, punctuation style, line-break hyphenation) → **Acceptable variation** with an auto-generated note: *"Label uses all caps; same name."* Deterministic. STONE'S THROW resolves here, visibly, as a judgment call the agent can see and override.
3. **Tier 3 — LLM adjudication.** What normalization can't explain (abbreviations: `Co.`/`Company`; reordering: `Old Tom Distillery`/`Distillery of Old Tom`; DBA variants) goes to a judgment call with a one-line rationale and confidence. Below the confidence threshold → **Needs review**, never a silent verdict. Tier 3 runs only on gray cases — it is the exception path, not the pipeline.

Asymmetry rule: when in doubt, the pipeline errs toward flagging, never toward passing. A false flag costs an agent seconds; a false pass costs the agency a compliance failure.

### The government warning — exact means exact

Jenny's specification: *"It has to be exact. Like, word-for-word, and the 'GOVERNMENT WARNING:' part has to be in all caps and bold... people try to get creative with the warning all the time. Smaller font, different wording, burying it in tiny text."* She rejected a label for title-case `Government Warning`. The app must catch everything she catches:

- **Verbatim text check** against the canonical 27 CFR 16.21 statement (full text in Appendix B), with a tokenized diff view showing exactly which words differ. Paraphrase, omission, insertion, or reordering → **Mismatch** with the diff as evidence.
- **`GOVERNMENT WARNING:` must be all caps and bold.** Title case, sentence case, or non-bold header → finding. *(This is Jenny's real catch — it is a named regression test.)*
- **The remaining statement text must NOT be bold** (27 CFR 16.22 requires the body in non-bold type — the inverse gotcha almost everyone misses).
- **Prominence heuristics** for "creative" evasion: warning text sized suspiciously small relative to other label text, low contrast against background, crowded/compressed spacing. These produce **Needs review** findings with the evidence region highlighted.
- **Honesty limit:** absolute type size minimums (1mm/2mm/3mm by container volume, with characters-per-inch caps) are not measurable from an unscaled photo. The app reports relative-prominence findings and explicitly states "type size not verifiable from image" rather than pretending. If net contents establishes container class, the applicable minimum is displayed as context for the agent's own eye.
- **Missing warning = automatic Reject-candidate aggregate**, ranked above every other finding. Zero false passes on warning violations is a release gate (see Performance Requirements).

### Imperfect images — read what agents can't, admit what you can't

Jenny again: *"it would be amazing if the tool could handle images that aren't perfectly shot... photographed at weird angles, or the lighting is bad, or there's glare on the bottle."* Today the fallback is reject-and-request-a-better-image. The app keeps that fallback but earns its keep by needing it less:

- **Preprocessing pipeline:** EXIF auto-orientation, deskew/perspective correction, contrast normalization, intelligent downscaling that preserves small warning text.
- **Quality gate:** every image is scored (blur, exposure, glare, skew) before extraction. A failing image produces a specific, plain-language reason — *"Glare covers the lower third of the label — retake without flash or request a new image"* — mirroring the agents' existing workflow verb.
- **Per-field honesty:** quality is judged per field, not per image. A glare spot over the warning with a legible brand name yields brand = verified, warning = **Unreadable**. The pipeline never extrapolates through unreadable pixels — asserted by a named test.
- **Multi-image applications:** front + back label photos are one application; extraction merges across images and records which image each field came from. (The warning statement commonly lives on the back label.)
- **Input formats:** JPEG, PNG, WebP, HEIC (phone photos), and PDF label proofs (rendered per page), with size caps and clear errors for unsupported files — including the non-label image (someone will upload a photo of their cat; the app responds gracefully).

---

## Processing Model

The proactive analog of a trigger-model decision: how work executes under the 5-second constraint. The decision, and its defense:

- **Verify Now is a synchronous request.** One request → preprocess → one structured vision-extraction call → deterministic rules → response. No queue, no polling — queue overhead is latency spent against a 5-second budget that exists because the last vendor took 30–40 seconds and lost the room. Tier-3 adjudication, when triggered, runs within the same request against a strict time budget; if the budget is exhausted the field returns **Needs review** with partial evidence rather than blowing the deadline.
- **Batch is a queued job.** 300 synchronous requests would be 300 browser-held connections; a job with N workers respects provider rate limits, isolates per-item failures, and survives a tab close. Results stream progressively — the agent triages finished items while the job runs. Worker concurrency is a measured tradeoff: high enough that 300 items complete within the coffee-break budget, low enough to stay inside provider rate limits with headroom for concurrent Verify Now traffic, which always has priority.
- **Extraction is one vision call per image, not one per field.** Seven fields × separate calls would multiply cost and latency sevenfold and lose cross-field context (proof vs. ABV consistency). One call returns the full structured extraction; deterministic code does the comparing. The LLM extracts and adjudicates; it does not decide verdicts arithmetic can decide.
- **Failure posture:** timeouts, retries with exponential backoff, and a circuit breaker on the provider path. If the AI endpoint is unreachable — the exact failure Marcus watched kill the vendor pilot behind the agency firewall — the app says so in plain language, queues nothing silently, and stays up. Degraded, honest, alive.

---

## Observability

The vendor pilot died of unexplained slowness; LabelProof instruments against exactly that.

- **Stage-level latency** (upload → preprocess → extract → compare → render) recorded per request and **surfaced in the UI** — every result card shows elapsed time, e.g. `3.8s`. Speed is a product feature and the UI proves it on every run.
- **Structured logs** with request IDs, per-stage timings, token counts, model cost, verdict summary — and **zero label content or PII** (log discipline is a tested requirement, not a convention).
- **Evidence traceability:** every verdict links to its extraction evidence (value, image region, confidence, rationale) — the audit trail an agent shows their supervisor when a call is questioned.
- **Eval harness:** a golden label set with expected per-field verdicts runs as a CLI and in CI against recorded fixtures. Accuracy is a number tracked run-over-run, not a vibe. The harness reports field-level accuracy, a verdict confusion matrix, and the zero-false-pass check on warning violations.
- **Cost accounting:** tokens and cost per verification logged from day one — the inputs the Cost Analysis section commits to reporting.

---

## MVP Requirements (Due Day 2, EOD)

All items required to pass:

- [ ] Verify Now working end-to-end on the deployed public URL: image upload + application form → per-field verdict checklist with evidence
- [ ] Full field set verified: brand, class/type, alcohol content (with proof cross-check), net contents (with standards-of-fill validation), producer name/address, country of origin, government warning presence + verbatim text check
- [ ] All three commodities active (spirits, wine, malt): per-commodity required/optional matrix, ABV exceptions, per-commodity standards of fill
- [ ] Verdict taxonomy implemented exactly as specified (Match / Acceptable variation / Mismatch / Missing / Unreadable / Not applicable) with confidence + rationale on every field
- [ ] Tier 1 + Tier 2 matching live — STONE'S THROW case resolves as Acceptable variation with a visible note
- [ ] p95 ≤ 5s demonstrated on the deployed URL: 20 consecutive timed verifications, results recorded in the repo
- [ ] Elapsed time displayed on every result card
- [ ] Multi-image (front + back) applications supported
- [ ] Unreadable path working: a bad image produces per-field Unreadable verdicts and a plain-language retake reason — never a guessed value
- [ ] One-click sample demo: a "Try a sample" button loads the Old Tom Distillery example (brief's sample data) so a grader reaches a result in one click
- [ ] Recorded LLM fixtures + fake adapter; unit and integration tests green in CI without live API calls
- [ ] Deployed via infrastructure-as-config with `/health` and `/ready`; deploy + rollback procedure documented
- [ ] Retention TTL live: uploads and results auto-purge; no PII collected anywhere; EXIF (including GPS) stripped on ingest
- [ ] `README.md` with setup/run instructions; `PRD.md` + `TICKETS.md` committed; `CHANGES.md` started
- [ ] Golden set v1 (≥10 labels including Old Tom) with expected verdicts; eval harness runs in CI

## Final Requirements (Due Day 7, Noon)

Everything in MVP, plus:

- [ ] Batch mode end-to-end: manifest + images in, progressive results, triage table (worst first), per-item retry, CSV export — proven with a 300-item batch on the deployed URL
- [ ] Image robustness suite passing: angled, dim, glare, blur fixtures each produce correct verdicts or honest Unreadable — zero fabrication across the robustness set
- [ ] Government warning deep checks: tokenized diff view, all-caps + bold header check, non-bold body check, title-case regression test (Jenny's catch), prominence heuristics, type-size honesty caveat
- [ ] Tier 3 judgment live with rationale + confidence routing; adjudication fixtures in CI
- [ ] Golden set ≥25 labels spanning every canonical test case; final accuracy report committed (field accuracy, confusion matrix, zero false passes on warning violations)
- [ ] Accessibility: WCAG 2.1 AA / Section 508 pass (automated audit + keyboard-only walkthrough + screen-reader labels); 200% zoom usable
- [ ] Usability verification: ≥3 cold users complete a verification with zero instructions; findings addressed
- [ ] E2E tests (single, batch, unreadable, provider-down degradation) green in CI; failing CI blocks deploy with rollback
- [ ] Load behavior recorded: 300-item batch wall-clock, provider throttling behavior, Verify Now p95 unaffected during a running batch
- [ ] Cost analysis complete with measured per-label cost and projections
- [ ] Submission package: README (approach, tools, assumptions, trade-offs, limitations), deployed URL, demo script, downloadable sample label set

---

## Performance Requirements

| Metric | Goal | Verified by |
|---|---|---|
| Verify Now latency | **p95 ≤ 5s** upload-to-verdict (hard adoption gate — *"If we can't get results back in about 5 seconds, nobody's going to use it"*) | 20 consecutive timed runs against the deployed URL, recorded in repo |
| First visible feedback | ≤ 1s (progress state, never a dead spinner) | E2E assertion |
| Batch: first result visible | ≤ 10s after job start | Timed 300-item batch |
| Batch: 300 items complete | ≤ 10 minutes wall-clock, UI responsive throughout | Timed 300-item batch on deployed URL |
| Verify Now during active batch | p95 still ≤ 5s (priority lane) | Timed runs concurrent with batch |
| Field-verdict accuracy | ≥ 95% on golden set | Eval harness, final report |
| False pass on warning violations | **0** on golden set (safety-critical asymmetry) | Eval harness gate — release-blocking |
| Fabrication on unreadable fields | 0 — Unreadable, never a guess | Named robustness tests |
| Cost per verification | Measured, documented, and defended | Cost accounting logs |
| Deployed URL first-hit response | ≤ 5s (no cold-start ambush during grading) | Keep-warm + smoke test |

Detection latency will be verified the way the stakeholders would test it: a stopwatch and a stack of labels. If the number on the screen and the number on the stopwatch disagree, the stopwatch wins.

---

## Usability Requirements

The benchmark is Sarah's, verbatim: *"We need something my mother could figure out — she's 73 and just learned to video call her grandkids last year."* Half the team is over 50; Dave prints his emails; Jenny could have built the tool herself. It must serve both ends without a manual. Measurable proxies:

- **Zero-training first run:** a cold user reaches a verdict on the sample label with no instructions, in under a minute. Verified with ≥3 hallway testers.
- **"Clean, obvious, no hunting for buttons":** one primary action per screen; verify flow is upload → enter → verdict — 3 interactions, no navigation maze, no settings required before first value.
- **Legibility floor:** ≥16px body text, WCAG AA contrast, click targets ≥44px. Verdicts are icon + word, never color alone (color-blind agents exist; printers exist — Dave will print the report).
- **Plain language everywhere:** no ML jargon, no "inference failed." Every error states what happened and what to do next, in the vocabulary agents already use (*"request a better image"*).
- **Section 508 / WCAG 2.1 AA:** keyboard-complete, screen-reader labeled, visible focus, 200% zoom. This is a federal agency; accessibility is a legal floor for any production future, so the prototype demonstrates it now.
- **Respect the veteran** *(Dave: "Just don't make my life harder in the process")*: no accounts, no setup wizard, no forced workflow changes. The app mirrors the existing mental model — the printed checklist — and pays rent in its first 10 seconds.
- **Serve the power user** *(Jenny)*: keyboard shortcuts for confirm/override/next, batch triage filters, exportable results. Approachable ≠ shallow.

---

## Test Cases

You own the test cases; the grader (and the stakeholders) verify the app does what the PRD says under conditions the PRD defines. Canonical set — each is a named regression test with fixture data, and the golden set includes at least one instance of every row:

| # | Case | Input state | Expected output |
|---|---|---|---|
| TC-01 | Clean match | Old Tom Distillery sample: application and label agree on all 7 fields | All Match; aggregate Ready to approve; ≤5s |
| TC-02 | STONE'S THROW | Label `STONE'S THROW` (curly apostrophe), application `Stone's Throw` | Brand = Acceptable variation + note; nothing silently passed |
| TC-03 | Title-case warning | Warning header `Government Warning:` in title case | Warning finding (header not all-caps); aggregate Needs review at minimum |
| TC-04 | Bold body | Entire warning paragraph in bold | Finding: body must not be bold |
| TC-05 | Reworded warning | "According to the Surgeon General, pregnant women should not…" | Mismatch with tokenized diff highlighting the substitution |
| TC-06 | Buried warning | Warning present but visibly smaller than surrounding text, low contrast | Needs review: prominence finding with evidence region |
| TC-07 | Missing warning | No warning anywhere on any image | Missing; aggregate Reject candidate, ranked first |
| TC-08 | ABV mismatch | Application 45%, label 40% | Mismatch with delta; tolerance context shown, not used to excuse |
| TC-09 | Proof inconsistency | Label shows `40% Alc./Vol. (90 Proof)` | Internal-consistency finding (90 proof = 45%), independent of application match |
| TC-10 | Non-standard fill | Net contents `733 mL`, matching application | Value matches application AND standards-of-fill finding raised |
| TC-11 | Angled photo | Sharp label shot ~30° off-axis | Correct extraction after correction; verdicts unaffected |
| TC-12 | Glare patch | Glare obscuring warning only | Warning = Unreadable + retake reason; other fields verified |
| TC-13 | Dim lighting | Underexposed but recoverable | Correct extraction after normalization |
| TC-14 | Hopeless blur | Text truly illegible | Per-field Unreadable; zero fabricated values (asserted) |
| TC-15 | Not a label | Cat photo | Graceful "doesn't look like a label" response, no crash, no verdicts |
| TC-16 | Front + back | Warning on back image only, brand on front | Merged extraction; per-field image provenance recorded |
| TC-17 | Table wine | Wine ≤14% labeled "Table Wine," no ABV; none in application | ABV = Not applicable; no false Missing |
| TC-18 | Malt, no ABV | Beer with no ABV statement, none required | Not applicable; no false Missing |
| TC-19 | Import origin | Application marked import; label lacks country of origin | Missing on country of origin |
| TC-20 | Batch of 300 | Mixed manifest: clean, mismatched, unreadable, malformed rows | Job completes ≤10 min; per-item isolation; malformed rows reported with row numbers; triage ordering correct |
| TC-21 | Provider down | AI endpoint unreachable (simulated) | Plain-language degradation, app stays up, no hang, no silent queue |
| TC-22 | Spirits "ABV" abbreviation | Spirits label shows `45% ABV` | Format finding: only `alc./vol.` permitted on spirits |

Document every test case's fixture location and its automated assertion in the repo. The robustness fixtures (TC-11 – TC-14) are generated/sourced label images, not synthetic JSON — the brief encourages AI-generated test labels, and the generation prompts ship with the repo.

---

## Engineering Requirements

Graded alongside functionality; not optional. *(Brief evaluation criteria: code quality and organization; appropriate technical choices; error handling.)*

- **Regression tests with rollback.** Every canonical test case (TC-01 – TC-22) has an automated regression test. A failing CI run blocks deploy; a failed deploy rolls back automatically. Rollback trigger and procedure documented in `CHANGES.md`.
- **End-to-end tests for critical workflows.** At minimum: Verify Now happy path, batch flow with progressive results, the Unreadable path, and provider-down degradation. All run in CI.
- **Mock external services with stable fakes.** Every test that would call the vision/LLM provider uses recorded fixtures or a fake adapter — CI is deterministic and passes with no network. The deployed app runs live; the test suite must not need to.
- **Retries, timeouts, and circuit breakers.** All outbound calls carry explicit timeouts, bounded retries with exponential backoff, and a circuit breaker. The app degrades gracefully when the provider is unreachable — it must not crash or hang. *(This is the firewall lesson: half the vendor's features died behind the agency network. LabelProof's failure mode is a sentence, not a spinner.)*
- **Provider abstraction.** All AI calls go through one server-side interface; the browser never talks to the provider. Swapping providers (e.g., to an Azure-hosted or gov-cloud model) is a config/adapter change, documented — because the customer's production reality is Azure + FedRAMP + a hostile firewall.
- **Enumerated egress.** Every external domain the deployed app contacts is listed in the README. An agency network admin should be able to allowlist the app from one table. *(Marcus: "our network blocks outbound traffic to a lot of domains.")*
- **Developer documentation.** `CHANGES.md` at repo root: what was built, how to run and test locally, how to roll back. Written for the next engineer, not for graders.
- **Code quality bar.** Typed codebase, lint + typecheck green in CI, the verification pipeline isolated from transport/UI so the rules engine is unit-testable in milliseconds.

---

## Security, Privacy & Data Retention

Marcus set the posture: *"there's PII considerations, document retention policies, the usual federal compliance stuff. But for a prototype? Just don't do anything crazy. We're not storing anything sensitive for this exercise."* Translated into requirements — prototype-grade implementation, production-aware documentation:

- **No PII by design.** No accounts, no names, no emails. The app processes label artwork and application field data only, and says so on screen. Demo data is synthetic.
- **Retention policy, implemented.** Uploads and results are ephemeral: auto-purged on a short TTL (default 24h, configurable), with the policy stated in the UI and README. "Document retention policy" is answered with an actual documented retention policy — even in a prototype.
- **EXIF stripped on ingest** (including GPS — phone photos of labels leak location), verified by test.
- **No sensitive content in logs.** Logs carry IDs, timings, token counts, verdict summaries — never label text or images. Tested, not assumed.
- **Upload hardening.** Content-type sniffing (not extension trust), size and dimension caps, image re-encode on ingest, PDFs rendered in a constrained path — uploads are treated as hostile input.
- **Transport & secrets.** HTTPS only; security headers; secrets in the deployment platform's secret store, never in the repo; a secrets scan runs pre-commit.
- **Abuse protection.** Rate limiting on the public prototype URL.
- **No training on user data.** Provider data-handling posture (zero-retention options) documented — a federal customer will ask exactly this question first.
- **Production path, documented not built.** One README section maps prototype → production: FedRAMP-authorized model endpoints (the customer is already on Azure), agency IdP auth, retention aligned to federal records schedules, audit logging. Scope-fenced per the brief, but the thinking is shown.

---

## AI Cost Analysis

### Development and testing costs

Track and report actual spend: provider API costs (input/output token breakdown), number of verification runs during development, total development spend. Recorded from day one via the cost accounting logs (Observability).

### Production cost projections

Estimate monthly costs at three adoption scales, anchored to the customer's real volume (150,000 applications/year ≈ 600 per working day agency-wide):

| | Pilot: 10 agents | Division: all 47 agents | Peak season w/ batch |
|---|---|---|---|
| Verifications/day | ~130 | ~600 | ~1,200 |
| Est. monthly cost | $___/month | $___/month | $___/month |

Fill with measured numbers, and include the assumptions: average images per application (front + back ≈ 2), tokens per extraction call, Tier-3 adjudication trigger rate (target <20% of fields in the gray zone), retry overhead (~10–15%), and re-verification rate. Show the arithmetic from cost-per-label → monthly figure, and state the cost cliffs (image resolution vs. token count is the big one; batch concurrency is the other). A per-label cost target of a few cents makes the ROI story trivial against 5–10 minutes of agent time — make that comparison explicitly.

---

## Deliverables

| Artifact | Contents |
|---|---|
| Source repository (GitHub) | All source; clean history; no secrets |
| `README.md` | Setup + run instructions; approach; tools used; assumptions; trade-offs and limitations; egress table; production-path notes |
| `PRD.md` | This document — the source of truth |
| `TICKETS.md` | The execution plan; every ticket traces to requirement IDs here |
| `CHANGES.md` | Developer log: what was built, how to run/test, how to roll back |
| Deployed application URL | Working prototype, publicly accessible, keep-warm, sample demo one click in |
| Golden label set | ≥25 labels + expected verdicts + generation prompts/scripts |
| Accuracy report | Final eval: field accuracy, confusion matrix, zero-false-pass verification |
| Demo script | 3-minute walkthrough hitting: speed, checklist UI, STONE'S THROW, warning catch, batch triage |

Sections due by checkpoint:

| Section | Due |
|---|---|
| Architecture decisions (stack, pipeline, processing model) | Architecture Defense |
| Verify Now + per-commodity rules + deploy + fixtures + golden v1 | MVP |
| Batch, robustness, warning deep checks, judgment tier, full suite, accessibility, performance verification, accuracy report, cost analysis, submission package | Final |

---

## Constraints

- **Standalone.** No COLA integration, no agency system access — explicit stakeholder scope. Data enters via the UI or a manifest file.
- **Prototype data posture.** Synthetic/sample data only; nothing sensitive stored; short retention.
- **Assistive, never decisive.** The app recommends; the agent decides. No auto-approve/reject anywhere.
- **5 seconds is a constraint, not a target.** Features that can't fit the budget (in Verify Now) don't ship in Verify Now.
- **Warning-statement checks fail closed.** Uncertainty about the warning is Needs review or Unreadable — never Match.
- **Minimal, enumerated egress.** Few external endpoints, all documented; server-brokered AI calls only.
- **Working core beats ambitious incomplete.** Milestone gates enforce the brief's stated preference; scope cuts are documented as trade-offs, not hidden.
- **Fill gaps independently and say so.** Every assumption the brief left open is written down in the README's assumptions log. *(Brief: "we also value how you fill in gaps independently.")*

---

## Appendix A — Requirements Traceability

Every requirement extracted from the brief — overt and between-the-lines — with its source. Quotes are verbatim from the take-home document. **This appendix owns requirement definitions; `TICKETS.md` owns the requirement → ticket mapping.** Nothing is maintained by hand in both places.

Sources: **[SC]** Sarah Chen, Deputy Director · **[MW]** Marcus Williams, IT Systems Admin · **[DM]** Dave Morrison, Senior Agent · **[JP]** Jenny Park, Junior Agent · **[BR]** brief body (requirements/context/deliverables/evaluation sections)

### Performance (PERF)

| ID | Requirement | Source |
|---|---|---|
| PERF-1 | Verify Now p95 ≤ 5s upload-to-verdict; hard adoption gate | [SC] "If we can't get results back in about 5 seconds, nobody's going to use it." |
| PERF-2 | Elapsed time visibly displayed per result — speed must be provable, not claimed | [SC] vendor pilot: "30, 40 seconds… agents just went back to doing it by eye" |
| PERF-3 | Tool+human decision time must beat the manual baseline (5–10 min/application) | [SC] "It takes maybe 5-10 minutes per application" |
| PERF-4 | Batch of 300 completes ≤10 min with first results ≤10s (progressive) | [SC] "200, 300 label applications… at once" + PERF-1 spirit |
| PERF-5 | UI stays responsive during processing; agent can keep working | [DM] "don't make my life harder"; [SC] pilot abandonment |
| PERF-6 | Deployed URL responds ≤5s on first hit (no cold-start ambush) | [BR] "Working prototype we can access and test" + PERF-1 |
| PERF-7 | Over-budget runs show progress/partial results, never a dead spinner | [SC] pilot failure mode |

### Usability (UX)

| ID | Requirement | Source |
|---|---|---|
| UX-1 | Zero-training first-run success — the 73-year-old benchmark | [SC] "something my mother could figure out—she's 73…" |
| UX-2 | One primary action per screen; ≤3 interactions to a verdict | [SC] "Clean, obvious, no hunting for buttons." |
| UX-3 | Legibility floor: ≥16px, WCAG AA contrast, ≥44px targets | [SC] "Half our team is over 50." |
| UX-4 | Section 508 / WCAG 2.1 AA: keyboard, screen reader, focus, 200% zoom | Implied: federal agency; production path requires it |
| UX-5 | Results rendered as a per-field checklist mirroring the paper one | [JP] "I literally have a printed checklist on my desk" |
| UX-6 | Plain-language verdicts/errors; agents' own vocabulary; no jargon | [SC] Dave "still prints his emails"; [JP] "check with my eyes" |
| UX-7 | No accounts/setup/config before first value; zero workflow disruption | [DM] "Just don't make my life harder"; 2008 phone-system story |
| UX-8 | Value visible in first session (adoption is won or lost immediately) | [DM] "I've seen a lot of these 'modernization' projects come and go" |
| UX-9 | Side-by-side evidence: application vs. extracted value + image highlight | [JP] "check with my eyes" preserved as verification affordance |
| UX-10 | Batch triage view: sortable/filterable, worst first | [SC] "drowning in routine stuff" — route attention to issues |

### Batch (BATCH)

| ID | Requirement | Source |
|---|---|---|
| BATCH-1 | Batch upload is a must-have, not a stretch goal | [SC] "If there was some way to handle batch uploads, that would be huge." |
| BATCH-2 | Capacity ≥300 applications per batch | [SC] "big importers who dump 200, 300 label applications on us at once" |
| BATCH-3 | Manifest-based input (structured data file + images) with per-row validation errors | Implied by 300-at-once; hand-entry impossible at that scale |
| BATCH-4 | Per-item status + overall progress (queued/processing/done/failed) | Implied: 10-minute job needs observable progress |
| BATCH-5 | Progressive results — review finished items while the job runs | [SC] "Right now we literally have to process them one at a time" — beat that end-to-end |
| BATCH-6 | Per-item failure isolation — one bad image never kills the batch | Implied: real importer dumps contain junk |
| BATCH-7 | Batch summary + CSV export for the case file | Implied: agents report upward; [DM] will print it |
| BATCH-8 | Retry/resume failed items without reprocessing the batch | Implied: 300 × cost + time |
| BATCH-9 | Worker concurrency tuned to provider rate limits; Verify Now keeps priority | Implied: PERF-1 must hold during batch |
| BATCH-10 | Long-requested feature — treat as first-class, visible in nav | [SC] "Janet from our Seattle office has been asking about this for years." |

### Image robustness (IMG)

| ID | Requirement | Source |
|---|---|---|
| IMG-1 | Handle photos at weird angles (perspective/rotation correction) | [JP] "photographed at weird angles" |
| IMG-2 | Handle bad lighting (exposure/contrast normalization) | [JP] "or the lighting is bad" |
| IMG-3 | Handle glare on bottles | [JP] "or there's glare on the bottle" |
| IMG-4 | Quality gate with specific plain-language retake reasons | [JP] "they just reject it and ask for a better image" — mirror the workflow |
| IMG-5 | Unreadable ≠ Mismatch: per-field honesty; never guess through bad pixels | Implied: false mismatch kills trust, false match kills compliance |
| IMG-6 | Preprocessing: EXIF orient, deskew, normalize before extraction | [JP] "if AI could handle some of that…" |
| IMG-7 | Formats: JPEG/PNG/WebP/HEIC/PDF proofs; caps; graceful non-label handling | Implied: phone photos + print-shop PDFs are the real inputs |
| IMG-8 | Multi-image per application (front + back), merged, with provenance | Implied: warning statement typically on back label |

### Matching & judgment (MATCH)

| ID | Requirement | Source |
|---|---|---|
| MATCH-1 | Fixed verdict taxonomy incl. Acceptable variation + Unreadable + Not applicable | [DM] judgment case + [JP] exactness case require the distinction |
| MATCH-2 | Case/stylization differences resolve as Acceptable variation, visibly | [DM] "'STONE'S THROW' on the label but 'Stone's Throw' in the application" |
| MATCH-3 | Normalization: unicode, quotes/apostrophes, whitespace, diacritics, hyphenation | [DM] same case — curly vs. straight apostrophe is the literal example |
| MATCH-4 | LLM adjudication tier for gray cases, with rationale | [DM] "You can't just pattern match everything… You need judgment." |
| MATCH-5 | Every non-exact verdict carries a one-line human-readable rationale | [DM] trust requires seeing the reasoning |
| MATCH-6 | Confidence per field; low confidence routes to Needs review, never silent | Implied: assistive posture + asymmetry rule |
| MATCH-7 | ABV format equivalence + proof cross-check (90 Proof = 45%) | [BR] sample label "45% Alc./Vol. (90 Proof)" |
| MATCH-8 | Numeric fields: equality after normalization; deltas = Mismatch with delta shown; regulatory tolerances shown as context only | [BR] "ABV is correct? Check." + Appendix B |
| MATCH-9 | Acceptable variation is never silently merged into Match | [DM] the agent must see the judgment call |
| MATCH-10 | Aggregate = worst field verdict; warning findings always ranked first | [JP] warning severity + triage logic |

### Government warning (WARN)

| ID | Requirement | Source |
|---|---|---|
| WARN-1 | Verbatim match against canonical 27 CFR 16.21 text | [JP] "It has to be exact. Like, word-for-word" |
| WARN-2 | `GOVERNMENT WARNING:` all caps AND bold — both checked | [JP] "the 'GOVERNMENT WARNING:' part has to be in all caps and bold" |
| WARN-3 | Title-case header = violation (named regression: Jenny's catch) | [JP] "'Government Warning' in title case instead of all caps. Rejected." |
| WARN-4 | Detect rewording/paraphrase/omission with tokenized diff evidence | [JP] "people try to get creative… different wording" |
| WARN-5 | Prominence heuristics: too-small, low-contrast, buried text → Needs review | [JP] "Smaller font… burying it in tiny text" |
| WARN-6 | Missing warning → Missing + aggregate Reject candidate, ranked first | [BR] "mandatory on all alcohol beverages" |
| WARN-7 | Statement body must NOT be bold (16.22 inverse rule) | Appendix B (regulation behind Jenny's rule) |
| WARN-8 | Word-level diff view as evidence for any warning text finding | [JP] exactness must be provable to the applicant |
| WARN-9 | Type-size honesty: absolute mm not measurable from photo — say so; show applicable minimum as context | Implied: never fake precision (trust) |

### Field verification (FIELD)

| ID | Requirement | Source |
|---|---|---|
| FIELD-1 | Brand name verification | [SC] "Brand name matches? Check." |
| FIELD-2 | Class/type designation verification | [BR] TTB list; sample "Kentucky Straight Bourbon Whiskey" |
| FIELD-3 | Alcohol content verification with commodity format rules | [SC] "ABV is correct? Check." + [BR] |
| FIELD-4 | Net contents verification + standards-of-fill validation | [BR] "Net contents" + sample "750 mL" |
| FIELD-5 | Bottler/producer name & address verification | [BR] "Name and address of bottler/producer" |
| FIELD-6 | Country of origin verification for imports | [BR] "Country of origin for imports" |
| FIELD-7 | Government warning verification (see WARN) | [BR] "mandatory on all alcohol beverages" |
| FIELD-8 | Application data model mirrors COLA-style fields; extensible | [MW] standalone now, "maybe we look at how to incorporate it" later |
| FIELD-9 | Per-commodity required/optional matrix drives Not applicable verdicts | [BR] "requirements vary by beverage type… with some exceptions for certain wine/beer" |

### Commodity types (TYPE)

| ID | Requirement | Source |
|---|---|---|
| TYPE-1 | Support beer/malt, wine, distilled spirits | [BR] "(beer, wine, distilled spirits)" |
| TYPE-2 | Data-driven per-commodity rules (fields, formats, fills, tolerances) | [BR] "exact requirements vary by beverage type" |
| TYPE-3 | ABV exceptions encoded: table/light wine ≤14%; malt optional per state law | [BR] "(with some exceptions for certain wine/beer)" + Appendix B |

### Security, privacy, retention (SEC)

| ID | Requirement | Source |
|---|---|---|
| SEC-1 | No PII collected; synthetic demo data; stated on screen | [MW] "there's PII considerations" / "not storing anything sensitive for this exercise" |
| SEC-2 | Documented + implemented retention: TTL auto-purge of uploads/results | [MW] "document retention policies" |
| SEC-3 | EXIF/GPS stripped on ingest (verified by test) | Implied by PII posture: phone photos leak location |
| SEC-4 | No label content/PII in logs (tested) | [MW] "the usual federal compliance stuff" |
| SEC-5 | Upload hardening: sniffing, caps, re-encode, constrained PDF path | [MW] "Just don't do anything crazy" — hostile-input basics |
| SEC-6 | HTTPS, security headers, secrets in secret store, pre-commit scan | [BR] deployed public URL + federal audience |
| SEC-7 | Provider zero-retention/training posture documented | Implied: first question a federal buyer asks |
| SEC-8 | Production path documented (FedRAMP, Azure, IdP, records schedules) — not built | [MW] Azure/FedRAMP history; "could inform future procurement" |
| SEC-9 | Rate limiting on public URL | [BR] publicly accessible prototype |
| SEC-10 | Dependency audit clean; pinned deps | Federal context + eval criterion "code quality" |

### Network & environment (NET)

| ID | Requirement | Source |
|---|---|---|
| NET-1 | Enumerate every external domain in README (allowlist-ready) | [MW] "our network blocks outbound traffic to a lot of domains" |
| NET-2 | Server-brokered AI calls only; browser never hits provider | [MW] "half their features didn't work because our firewall blocked… their ML endpoints" |
| NET-3 | Graceful degradation when provider unreachable; app stays up | [MW] same pilot failure + engineering bar |
| NET-4 | Provider abstraction; Azure/gov-cloud swap is config, documented | [MW] "We're on Azure now" |
| NET-5 | /health + /ready expose dependency reachability | Ops necessity for the demo + degradation visibility |

### Human-in-the-loop & trust (HITL)

| ID | Requirement | Source |
|---|---|---|
| HITL-1 | Agent makes the final determination; app never auto-decides | [DM] "You need judgment."; assistive scope |
| HITL-2 | Per-field confirm/override; overrides captured in the report | Implied: judgment lives with the agent |
| HITL-3 | Evidence-first UI: extracted value + image region per field | Trust rebuild after failed pilot |
| HITL-4 | "Why" rationale affordance on every verdict | [DM] skepticism is earned; answer it inline |
| HITL-5 | Exportable per-application report: findings + agent decisions | Case-file reality; [DM] prints |
| HITL-6 | Win the skeptic: instant utility, zero setup, no forced change | [DM] 2008 automated-phone-system story |

### Observability & evaluation (OPS)

| ID | Requirement | Source |
|---|---|---|
| OPS-1 | Per-stage latency instrumentation, surfaced in UI + logs | PERF-1 must be provable per run |
| OPS-2 | Golden label set (≥25) with expected verdicts; automated eval harness | [BR] "create or source additional test labels" |
| OPS-3 | Accuracy floors: ≥95% field accuracy; 0 false passes on warning violations | [JP] exactness + asymmetry rule |
| OPS-4 | Token + cost per verification logged from day one | Cost Analysis inputs |
| OPS-5 | Structured logs, request IDs, error taxonomy | Eval criterion "error handling" |
| OPS-6 | Eval runs in CI on fixtures; accuracy regressions block | Engineering bar |

### Engineering (ENG)

| ID | Requirement | Source |
|---|---|---|
| ENG-1 | Regression test per canonical case; CI failure blocks deploy; auto-rollback | Engineering bar (GFA-grade discipline) |
| ENG-2 | E2E: single, batch, unreadable, provider-down | Critical workflows |
| ENG-3 | Stable fakes/recorded fixtures; deterministic CI, no live calls | Engineering bar |
| ENG-4 | Timeouts/retries/backoff/circuit breakers on all outbound | [MW] firewall reality + no-hang rule |
| ENG-5 | CHANGES.md developer log (run, test, roll back) | Engineering bar |
| ENG-6 | IaC deploy, /health + /ready, reproducible, rollback documented | [BR] "Deployed Application URL" done properly |
| ENG-7 | README: setup/run + approach/tools/assumptions | [BR] deliverable #1, verbatim |
| ENG-8 | Typed, linted, modular pipeline (rules engine unit-testable) | [BR] "Code quality and organization" |
| ENG-9 | Milestone gating: working core before ambitious features | [BR] "working core application with clean code is preferred" |

### Deliverables (DEL)

| ID | Requirement | Source |
|---|---|---|
| DEL-1 | Public deployed URL, testable by graders | [BR] "Working prototype we can access and test" |
| DEL-2 | Source repo (GitHub or similar), all source | [BR] deliverable #1 |
| DEL-3 | README setup and run instructions | [BR] verbatim |
| DEL-4 | Documentation of approach, tools, assumptions | [BR] verbatim |
| DEL-5 | Test label set + generation prompts/scripts shipped | [BR] "AI image generation tools work well for this" |
| DEL-6 | Trade-offs and limitations documented | [BR] "Document any trade-offs or limitations" |
| DEL-7 | PRD + TICKETS live in repo as source of truth + plan | This process |

### Scope guardrails (SCOPE)

| ID | Requirement | Source |
|---|---|---|
| SCOPE-1 | Standalone; zero COLA/agency integration | [MW] "we're not looking to integrate with COLA directly" |
| SCOPE-2 | Prototype informing procurement — engineering discipline anyway | [MW] "standalone proof-of-concept that could potentially inform future procurement" |
| SCOPE-3 | Not a legal determination engine; recommendations only | [DM] judgment + assistive posture |
| SCOPE-4 | No real applicant data anywhere in the exercise | [MW] "not storing anything sensitive" |
| SCOPE-5 | Gaps filled independently, assumptions logged | [BR] "we also value how you fill in gaps independently" |

**Count: 114 requirements.** The six the kickoff called out land here as PERF-1, UX-1, BATCH-1, IMG-1–3, MATCH-2/4, SEC-1/SEC-2.

---

## Appendix B — Regulatory Canon

Verified against eCFR / ttb.gov on 2026-08-10. This is the data the rules engine encodes; each item ships as a constant/table with a source comment.

### The government warning statement — canonical text (27 CFR 16.21)

> **GOVERNMENT WARNING:** (1) According to the Surgeon General, women should not drink alcoholic beverages during pregnancy because of the risk of birth defects. (2) Consumption of alcoholic beverages impairs your ability to drive a car or operate machinery, and may cause health problems.

### Warning format rules (27 CFR 16.22)

- "GOVERNMENT WARNING" in capital letters **and bold**; the remaining text **must not** be bold.
- Readily legible, on a contrasting background, separate and apart from other information; characters may not be compressed to impair readability.
- Minimum type size by container volume, with maximum character density:

| Container | Min type size | Max characters/inch |
|---|---|---|
| ≤ 237 mL (8 fl oz) | 1 mm | 40 |
| > 237 mL – 3 L | 2 mm | 25 |
| > 3 L | 3 mm | 12 |

*(App behavior: absolute mm is unverifiable from an unscaled photo — WARN-9 governs.)*

### Alcohol content rules

| Commodity | Status | Format notes | Tolerance (liquid vs. label) |
|---|---|---|---|
| Distilled spirits | Required | % alc. by vol.; only `alc.`/`vol.` abbreviations permitted (no "ABV"); proof optional, same field of vision, in parentheses/brackets; min type 2mm (>200mL) / 1mm (≤200mL) | ±0.3 pp |
| Wine | Required, except optional for "table wine"/"light wine" ≤14% | "Alcohol __% by volume" or range | ±1.5 pp (≤14%); ±1.0 pp (>14%) |
| Malt | Optional unless state law mandates | "% alcohol by volume", `alc/vol` variants OK | ±0.3 pp; special rules <2.5%, "non-alcoholic" <0.5%, alcohol-free = 0.0% |

Proof = 2 × ABV (90 Proof ⇔ 45% Alc./Vol.).

### Standards of fill (containers)

- **Distilled spirits (27 CFR 5.203):** 50, 100, 187, 200, 250, 331, 350, 355, 375, 475, 500, 570, 700, 710, 720, 750, 900, 945 mL; 1, 1.5, 1.75, 1.8, 2, 3, 3.75 L. *(25 sizes; can/non-can distinction eliminated.)*
- **Wine (27 CFR 4.72):** 50, 100, 180, 187, 200, 250, 300, 330, 355, 360, 375, 473, 500, 550, 568, 600, 620, 700, 720, 750 mL; 1, 1.5, 1.8, 2.25, 3 L; plus ≥4 L in even liters.
- **Malt beverages:** no federal standards of fill — net contents must simply be stated accurately.

### Sources

- eCFR, 27 CFR Part 16 Subpart C (warning text & format): ecfr.gov/current/title-27/chapter-I/subchapter-A/part-16/subpart-C
- TTB, distilled spirits alcohol content: ttb.gov/regulated-commodities/beverage-alcohol/distilled-spirits/ds-labeling-home/ds-alcohol-content
- eCFR, 27 CFR 4.36 (wine alcohol content), 27 CFR 7.65 (malt alcohol content)
- eCFR, 27 CFR 5.203 / 4.72 (standards of fill); TTB final rule effective 2025-01-10
- TTB label guidance: ttb.gov (per the brief's pointer)

---

*End of PRD. The execution plan lives in `TICKETS.md`; every ticket there traces to a requirement ID above.*
