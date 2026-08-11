# LabelProof

AI-powered alcohol label verification for TTB compliance review. `PRD.md` is the source of
truth for requirements.

<!-- Setup, approach, tools, assumptions, egress table and the rest of the README are owned
     elsewhere. The section below is the security, privacy and retention documentation
     (SEC-1..SEC-10). Append around it. -->

## Security, privacy, and data retention

Marcus set the posture: *"there's PII considerations, document retention policies, the usual
federal compliance stuff. But for a prototype? Just don't do anything crazy."* This section is
the literal answer — prototype-grade implementation, production-aware documentation. Every
claim here has a test behind it, named at the end of its paragraph.

### No PII, by design

There are no accounts, no names, no email addresses, and no login. The app processes label
artwork and the application field data an agent types in, and nothing else. All demo and
fixture data is synthetic. Nothing in this repository, in its fixtures, or in its golden set
came from a real applicant.

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
all — to assert the brand name, the producer address and the artwork are gone. Tests:
`tests/test_retention.py`.

### Uploads are treated as hostile

Content type is sniffed from magic bytes, never from the filename. Size, count and page caps
are enforced before decode. **All metadata is stripped, including GPS** — phone photos of
bottles carry the location they were taken. Every image is re-encoded on ingest, which
neutralises polyglot files, and PDFs are rendered through a page-capped path. Tests:
`tests/test_ingest.py`.

### Nothing from a label reaches the logs

`api/logging.py` accepts an **allowlist of field names** and raises `ContentInLogError` on
anything else. It is not a convention that erodes — there is no channel through which a brand
name can be logged deliberately.

The allowlist governs `applog.log` and nothing else, so a second layer covers the way it would
actually happen: a **traceback**. Starlette's `ServerErrorMiddleware` re-raises after the app's
error handler runs, uvicorn formats the traceback to stdout, and exception messages in this
codebase carry label text for real — a pydantic `ValidationError` renders `input_value=...`,
which on the extraction path is the label the model just read. So:

- an exception-containment middleware catches every unhandled exception before it can escape
  to the server, logs one scrubbed line naming only the exception class, and returns the
  taxonomy 500;
- process-wide containment replaces the log record factory, `sys.excepthook` and
  `threading.excepthook`, so a traceback logged by *any* library, on *any* thread, is reduced
  to `<ExceptionType> suppressed: traceback withheld (SEC-4)`. Ordinary log lines are
  untouched, so uvicorn's startup and bind lines still read normally.

Set `LABELPROOF_DEBUG_TRACEBACKS=1` to turn the second layer off while debugging locally. It is
off by default in every other configuration. Tests: `tests/test_security.py`, including one
that demonstrates the leak is real with containment removed.

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

**Two deliberate notes on the CSP**, because a policy with an undocumented relaxation is worse
than a looser one:

1. `style-src` carries `'unsafe-inline'`. The evidence overlay positions each highlight box
   with a React inline `style` attribute, which CSP3 blocks under a bare `style-src 'self'` —
   silently, leaving every box stacked in the corner. A highlight pointing at the wrong region
   is worse than no highlight in a product whose argument is honest evidence. `script-src` has
   no relaxation of any kind.
2. **`/docs` (Swagger UI) does not render under this policy.** It loads from `cdn.jsdelivr.net`
   with an inline bootstrap script. Adding a CDN to the egress table so a developer convenience
   page works is the opposite of what NET-1 is for, and an agency firewall would block it
   anyway. `/openapi.json` is unaffected.

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
plain-language body in the same error taxonomy as everything else.

**Client identity** comes from `LABELPROOF_CLIENT_IP_HEADER` (default `fly-client-ip`), falling
back to the socket peer when the header is absent. On Fly every request arrives from the proxy,
so keying on the socket peer would put every user in one bucket; Fly's proxy sets and
overwrites that header, so there it is authoritative. Off Fly, behind no proxy, set the
variable to `""` — otherwise the header is client-supplied and the limit is evadable.

**Limitation:** buckets are in-process. On the single machine this deploys to that is exact; at
N machines the effective ceiling becomes N times the limit. Redis would fix it and would add a
host to the egress table that a prototype with one machine should not carry.

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

### The production path — documented, not built (SEC-8)

Scope-fenced per the brief. This is what changes between this prototype and something a federal
agency could actually run:

| Area | Prototype today | Production |
|---|---|---|
| Model endpoint | Anthropic API | FedRAMP-authorized endpoint; the customer is already on Azure, so Azure-hosted models with a signed zero-retention term |
| Identity | None. No accounts by design. | Agency IdP via SAML/OIDC, PIV/CAC where required, role separation between agent and supervisor |
| Retention | 24h TTL on local disk | Aligned to the applicable NARA records schedule, not to a convenient number. Verification artefacts likely become part of the COLA case record, which changes the answer from "delete in 24h" to "retain per schedule, then dispose on schedule" |
| Audit logging | Structured logs, no content | Tamper-evident audit trail of who verified what and when, retained per schedule, with the same no-content rule |
| Rate limiting | In-process buckets | Shared store or an API gateway policy, per-identity rather than per-IP |
| Network | One public URL | Behind the agency perimeter; egress allowlisted from the table in this README |
| Data classification | Synthetic only | A review before any real applicant data touches it — this prototype has never held any and its retention story assumes it never will |

### Environment variables this section refers to

| Variable | Default | Effect |
|---|---|---|
| `LABELPROOF_RETENTION_HOURS` | `24` | TTL for batch uploads and results |
| `LABELPROOF_RETENTION_SWEEP_SECONDS` | `900` | How often the retention timer runs |
| `LABELPROOF_RATE_LIMIT_PER_MINUTE` | `30` | The `/verify` lane's budget |
| `LABELPROOF_CLIENT_IP_HEADER` | `fly-client-ip` | Header naming the real client; `""` uses the socket peer |
| `LABELPROOF_ALLOWED_ORIGINS` | *(empty)* | Comma-separated cross-origin allowlist; empty means same-origin only |
| `LABELPROOF_HSTS` | `1` | Emit HSTS on HTTPS requests |
| `LABELPROOF_DEBUG_TRACEBACKS` | `0` | Set to `1` to allow tracebacks on stdout while debugging locally |
