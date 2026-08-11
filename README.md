<!--
  This file currently carries the deployment sections only (LP-127 – LP-138).
  Setup/run instructions, approach, tools and the assumptions log land here separately
  (LP-139 – LP-141) and should slot in above "Deployment" without touching it.
-->

# LabelProof

AI-assisted label verification for TTB compliance review. Compares alcohol label artwork
against the filed application, field by field, with evidence — and recommends. The agent
decides.

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

Two claims that were here have been removed because we cannot currently support them:

- *"the shortest hop to the provider"* — plausible, unmeasured. Nobody has timed
  `iad → api.anthropic.com` against another region from inside this app. It stays out
  until someone does.
- *"the service and its only outbound dependency both sit inside the continental US, so
  the data-residency question has a one-word answer"* — **not true as shipped.** The
  adapter does not set `inference_geo`, so requests follow the workspace default, which
  is global unless configured otherwise. The app's *compute* is in Virginia; where the
  model runs is a separate setting nobody had set. Told to a federal agency, that was a
  claim the code did not back, which is worse than making no claim at all.

Pinning `inference_geo="us"` in the adapter is tracked separately. **Until that ships and
the workspace allowlist is confirmed, do not tell anyone this deployment guarantees US
data residency.** When it does ship, this section can say so — and `/ready` reporting the
geo would make it checkable rather than asserted.

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
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains; preload` | Two years, preload-eligible |
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
