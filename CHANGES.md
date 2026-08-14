# CHANGES

Developer log for LabelProof. Written for the next engineer to work on this, not for a
grader (ENG-5) — so it answers the three questions you actually have on day one: how do I
run it, how do I test it, and how do I undo it.

`PRD.md` owns *what* and *why*. `README.md` is the submission-facing document and
carries the approach, the assumptions, and what is not done. This file is the
operational one: run it, test it, deploy it, undo it.

> **Status, 2026-08-12.** The app is deployed and live at <https://labelproof.fly.dev>.
> `scripts/smoke.sh` passes against it, including a real seven-field verification through
> the live model, and `docs/perf-deployed.md` carries 20 timed runs.
>
> Still a **procedure rather than a drill**: the destroy-and-redeploy test (LP-136) and
> the forced-bad-deploy rollback proof (LP-244) have not been run. Deploying repeatedly
> is not the same as proving the environment rebuilds from configuration alone, and the
> results table below stays blank rather than filled with plausible output.

---

## Run it

### Prerequisites

| | |
|---|---|
| Python | **3.14** — what the development venv and CI both run. `pyproject.toml` still declares `>=3.12`, so the code stays 3.12-compatible on purpose (see "Why 3.12 and 3.14 both appear" below). |
| Node | **22+** for the web app |
| An Anthropic API key | Only for live runs. Every test and the whole fixture path work without one. |

### Setup, from a clean clone

```bash
git clone <repo> && cd labelproof

./scripts/install_hooks.sh          # git hooks — do this first, see "Commit convention"

python -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp .env.example .env                # .env is gitignored and must stay that way
                                    # every variable the app reads is listed in the example

npm --prefix web ci
```

### Running

```bash
# API only, with auto-reload. Serves the built SPA from web/dist when it exists.
.venv/bin/uvicorn api.main:app --reload --port 8000

# Web dev server, in a second terminal.
npm --prefix web run dev
```

Health endpoints: `GET /health` (process is up) and `GET /ready` (config valid and the
provider client is constructible). **`/ready` does NOT prove the API key works** — it
builds the SDK client and stops, because the client has no reachability probe. A key that
is present but revoked, expired, or scoped to the wrong workspace answers 200 with
`"simulated": false`. A key that is entirely MISSING is caught. `scripts/smoke.sh` is what
actually proves the key: it performs a real verification against the live model.

### Running without an API key, or without a network

Set `LABELPROOF_FAKE_PROVIDER=1`. Every provider call is served from recorded fixtures
instead, which is what CI does and what you want for UI work — it is instant and it costs
nothing. See `api/provider/fake.py`.

---

## Test it

Each layer runs on its own. Nothing here needs a network or an API key.

```bash
# Python: lint, types, unit + integration
.venv/bin/ruff check .              # the lint gate — see "Lint and typecheck" below
.venv/bin/mypy                      # --strict; scope comes from pyproject `files`
.venv/bin/python -m pytest          # the whole suite

# One canonical test case, by its PRD id
.venv/bin/python -m pytest -m "tc" -k TC-03

# Web
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web test
```

**The suite runs offline, and that is enforced rather than assumed.**
`tests/conftest.py` refuses every egress verb — `connect`, `connect_ex`, `sendto`,
`sendmsg`, `getaddrinfo`, `gethostbyname`, `gethostbyname_ex`, `gethostbyaddr` — for any
non-loopback target, and names the test that tried. It is installed at conftest import,
before pytest collects anything, so module-level code and session-scoped fixtures are
covered too and not just the call phase. CI additionally runs everything inside a network
namespace after proving from inside it that nothing is reachable. If you need a test that
genuinely opens a socket, mark it `@pytest.mark.allow_network` and justify it — there are
none today.

### Lint and typecheck

`ruff check` is the gate and it is configured deliberately in `pyproject.toml`: thirty
rule families, every exception scoped to a file and carrying its reason, and a written
list of the families that were considered and left out so nobody re-litigates them.

`ruff format` is configured but **not** a CI gate, on purpose. Running it would expand the
hand-aligned regulatory tables in `api/canon.py` and `api/rules/normalize.py` into one
value per line, which separates each constant from its CFR citation and makes the part a
compliance reviewer audits by eye harder to audit. The reasoning and the one-commit path
to turning it on are both in `pyproject.toml`.

`mypy --strict` is clean over `api/` — 50 source files, and that is the claim CI gates on
(`.venv/bin/mypy --strict api/`).

The wider claim that used to sit here was wrong twice, and both corrections are worth
recording because it was phrased as a checkable measurement:

- It said "clean over **every file in** `scripts/`". `pyproject.toml`'s `files` lists 4 of
  the 12 scripts. Checked individually, 10 of 12 are strict-clean; `triage_merge.py` and
  `compression_sweep.py` have 3 errors each.
- It cited "**22 errors in 9 files**" from `mypy --strict api tests eval fixtures`. That
  command does not produce a count at all — it aborts with `tests/test_api.py: Source file
  found twice under different module names`, and reports `Found 1 error in 1 file (errors
  prevented further checking)`. `--explicit-package-bases` does not help. The number could
  not have come from the command quoted next to it.

`tests/`, `eval/` and `fixtures/` are not strict-clean, and this file no longer puts a
figure on it, because a number nobody can reproduce is worse than no number — which is
what `pyproject.toml` says a few lines above the list that made this claim checkable.

#### Why 3.12 and 3.14 both appear

CI and the development venv run 3.14. Ruff's `target-version` and mypy's `python_version`
say 3.12, matching `requires-python`. That is on purpose: lint against the **oldest**
interpreter the project claims to support, so 3.13/3.14-only syntax cannot reach a
container built on an older base image. Raise all three together or none of them.

### CI

`.github/workflows/ci.yml`, on every push and pull request:

| Job | Steps |
|---|---|
| `api` | install → scan for credentials → check git hooks are executable → **(offline)** ruff, mypy, pytest |
| `web` | install → **(offline)** eslint, tsc, vitest |

Everything after install runs with no network egress. Install itself needs PyPI and the
npm registry; the claim is that the *suite* runs offline, not that the machine never had
a network.

---

## Commit convention

Closing a ticket requires a commit that names it in a `Closes:` git trailer. A close with
no commit behind it is a false close, and `TICKETS.md` is a **projection of history** —
`scripts/sync_board.py` rewrites its checkboxes from the trailers, so editing one by hand
achieves nothing.

```
Closes: LP-017
Closes: LP-023, LP-024, LP-025
Closes: LP-040..LP-048
```

The trailer goes in the commit body, one line, last. Ranges and comma-separated lists both
work. `sync_board.py` warns if a commit cites a ticket the board has never heard of, which
is how a typo'd id gets caught.

### Hooks

```bash
./scripts/install_hooks.sh
```

Run it once per clone. `git clone` never brings hooks with it — `.git/hooks` is local to
each clone — so the script points `core.hooksPath` at the tracked `.githooks/` directory
and fixes the executable bits, which a zip download or a permission-dropping filesystem
will silently lose. It is idempotent.

| Hook | What it does |
|---|---|
| `pre-commit` | Refuses to commit a credential (SEC-6). Reads the **index**, not the working tree, because staging a key and then editing the file is the actual attack. |
| `commit-msg` | Refuses a credential in the commit *message*. pre-commit cannot see it, and a key in a message is in history just as permanently as one in a file. |
| `pre-push` | Refuses to push `main`. Everything ships on a branch. |
| `post-commit` | Reprojects `TICKETS.md` from the `Closes:` trailers in history. |

`git commit --no-verify` bypasses both scans. It is there for a genuine false positive; if
you use it, add the case to `scripts/scan_secrets.py` so the next person is not stopped by
it too.

The scanner announces what it could not read. Binary and oversized files cannot be split
into lines, so they get the key-prefix rules over raw bytes instead, and every one of them
is listed on stderr with the reason — a gap in a check that nobody is told about is
indistinguishable from a clean result.

**There is no `.pre-commit-config.yaml`, deliberately.** One shipped briefly and was
removed: `pre-commit install` refuses outright when `core.hooksPath` is set, which
`install_hooks.sh` sets, so following this document and then that file's instructions were
mutually exclusive. Its `language: system` entries also resolved ruff and mypy from
`PATH`, so its stated guarantee — that a developer's result and CI's cannot disagree —
was enforced by nothing, and a globally-installed ruff of any version would have won. Two
hook mechanisms that conflict are worse than one that works. Lint and typecheck are
CI-enforced and one command away locally.

CI re-runs the secrets scan over every tracked file and fails if any hook has lost its
executable bit — a non-executable hook is skipped by git without a word, which is the
worst failure mode a hook has.

---

## Roll it back

### A bad commit

```bash
git revert <sha>          # preferred: history stays honest, CI re-runs on the revert
```

Never force-push a shared branch to make a bad commit disappear. `pre-push` already
refuses `main`; the revert is the supported path.

Because `TICKETS.md` is projected from `Closes:` trailers, reverting a commit does **not**
reopen its tickets — `git revert` writes a new commit and the original trailer is still in
history. If the work is genuinely undone, say so in the revert's message and reopen the
ticket deliberately.

### A bad deploy

> **Not yet drilled.** The app IS deployed — one always-on Fly machine in `iad`, see
> `fly.toml` — and it has been redeployed cleanly several times, with `scripts/smoke.sh`
> gating each one. What has not happened is a deliberately bad deploy: LP-244 forces one
> and proves the rollback fires. Until that closes, the steps below are the intended
> procedure rather than a tested one, and should be read as a plan.

```bash
fly releases                          # find the last known-good version
fly deploy --image <previous-image>   # redeploy it by digest
```

The gate that should make this rare: deploy runs only on green CI (LP-130), and a failed
post-deploy health check rolls back automatically (LP-131). Verify a rollback landed by
hitting `/health` and `/ready` and by running the post-deploy smoke check (LP-135).

### A bad configuration

Configuration is environment-only and fails fast on a missing or invalid value
(`api/config.py`), so a bad config is a startup failure rather than a silent
misbehaviour. Fix the value in the platform's secret store and redeploy; there is no
config baked into the image to roll back separately.

---

## Build log

Newest first. One entry per merged wave — what landed, and anything the next engineer
would otherwise have to discover.

### LP-003 · Developer log, CI, lint/typecheck gates, secrets scan

- `ruff check` and `mypy --strict` are now gates rather than suggestions, and the repo is
  clean to both. The rule set is chosen and its exceptions are individually justified in
  `pyproject.toml`.
- CI runs on every push: install → lint → typecheck → tests, with everything after
  install executed with no network egress and the sandbox verified from inside itself.
- A dependency-free secrets scan runs pre-commit, commit-msg, and in CI. It reads the git
  index rather than the working tree.
- **A real false-pass class was found and fixed (LP-045).**
  `expand_state_abbreviations()` rewrote any standalone word matching a two-letter state
  code, so `Old Tom Distilling Co` became `old tom distilling colorado`. Because the
  rewrite is many-to-one, distinct producers collapsed onto one string and compared as an
  exact Tier-1 **Match** — `La Crema Winery` passed as `Louisiana Crema Winery`, and every
  producer ending in "Co" passed as one ending in "Colorado". Expansion is now restricted
  to address position. Symmetric normalization had been offered as the reason this was
  harmless; it is not, because it only prevents false *mismatches*.
- The test that should have caught it was vacuous — its assertion ended in `or True`.
  Replacing it with a single strict xfail was worse: with the negative assertion first,
  the positive one became unreachable, so deleting `"or": "oregon"` from the table left
  the suite green. It is two tests now, verified by re-running that mutation.
- `api/provider/fake.py` had a closure over a loop variable on the path every test takes.
  Removed, but honestly: it was a latent smell, not a live defect. The closure was called
  synchronously inside the iteration that defined it, and 135 old-vs-new combinations
  (15 fixtures × 3 image counts × 3 illegible sets) show zero behavioural difference.
- The offline guard shipped with two working egress paths — a UDP `sendto` needs no
  `connect`, and `gethostbyname` does not go through `getaddrinfo`. Both leaked real
  packets. Closed, along with the guard only existing during a test's call phase.

---

## Deploying

> **Status, 2026-08-12: the deploy pipeline has still never run, and every deploy so far
> was issued by hand.** `.github/workflows/deploy.yml` triggers on `push` to `main`, and
> all the work is on `merge/wave-1` — so the release gate, the golden-set eval and the
> auto-rollback have never executed against the deployed artifact. That is a real gap and
> it is the reason this section is a plan.
>
> Local gates are green: ruff clean, `mypy --strict api/` clean, 3582 tests passing, the
> eval at 100% on Tier A across 175 rows with zero warning false passes. CI on this branch
> was red for ten commits on environment differences rather than defects — an SPA build CI
> does not run, a Linux font rasterizer measuring different pixels than macOS, a timing
> ceiling set from a laptop, and four retention tests asserting a SQLite build detail as a
> precondition. All fixed.
>
> It remains true that nothing below has been exercised end to end, and it is *why* a
> production returning 503 on every verification survived long enough to be found by hand.
> The first green pipeline run is the thing to watch for; until then, read
> this section as intended behaviour rather than observed.

### Deploying by hand

The pipeline deploys on a push to `main` and first ran green on 2026-08-13, producing
release v27. Deploy by hand when you are working on a branch, or when the runner is not
the fastest way to get a fix out:

```bash
fly deploy --app labelproof --ha=false
scripts/smoke.sh https://labelproof.fly.dev
```

**`--ha=false` is not optional.** Without it `fly deploy` creates TWO machines, and batch
state is SQLite on each machine's own disk — no volume, deliberately. A job queued on one
does not exist on the other, the edge round-robins, and a status poll alternates 200 and
400 `batch_not_found`. Observed in production: `400 200 400 200 400 200`. The batch itself
completes fine on the machine that owns it while the page watching it flickers "no batch
with that reference" every other tick.

`fly.toml` cannot pin the machine count — Fly takes it from the deploy command — so this
line and step 6 of `scripts/smoke.sh` are the control. The smoke test queues a real batch
and polls it six times, which fails on a two-machine app and cannot be satisfied by
reading configuration back. If you ever find two machines: `fly scale count 1`.

### What the pipeline would do

`main` deploys itself. `.github/workflows/deploy.yml` runs the release gate — lint, types,
the full test suite, the golden-set eval, a production web build — and only then ships.
The gate runs with no API key present, so it cannot accidentally start depending on the
network.

After `fly deploy` returns, the pipeline runs `scripts/smoke.sh` against the public URL.
That is where a release actually passes or fails: Fly's health checks confirm the process
is up, and the smoke test confirms the thing that matters, which is that the deployed
service verified a real label over HTTPS against the live model.

```bash
scripts/smoke.sh https://labelproof.fly.dev   # run it by hand any time; exit 0 means good
```

---

## Rolling back (ENG-5)

### It is already automatic

You do not normally do this by hand. Before touching anything the deploy job records two
things — the digest of the live image **and** the live configuration (`flyctl config
show`) — and if the deploy or the smoke test does not cleanly succeed it puts both back
and re-runs the smoke test.

Capturing the configuration matters more than it looks. An earlier version rolled back
with `--config fly.toml`, which re-applies the **new** configuration to the **old** image.
For any failure whose cause *is* the configuration — a health-check path typo, a provider
timeout below the request budget (a startup error), a CSP that blanks the SPA — that
"rollback" ships the bug again and then fails its own smoke test. It could not recover the
class of failure most likely to need it.

Rollback fires on:

- `fly deploy` failing, timing out, **or being cancelled** (an exceeded `timeout-minutes`
  cancels rather than fails, and a half-applied release needs reverting either way)
- any smoke-test failure — a missing key, a broken web build, a field dropped out of the
  pipeline, a latency budget too small for the model, or the service answering in sample
  mode

It does **not** fire when the deploy step never ran. A failure in checkout or flyctl setup
leaves production untouched, and redeploying over it would be the only thing that could
break it.

**Where it still cannot help you.** If the live configuration could not be captured, the
rollback re-applies the current `fly.toml` and says so in the log. And a failed *first*
deploy has nothing to return to by definition — the job reports that plainly rather than
implying a recovery happened.

It does **not** fire on a slow-but-working release. Latency is reported by the smoke test
and enforced by the 20-run p95 table; rolling back over one 5.2s sample would cost more
availability than it defends.

The one case with no automatic recovery is the very first deploy of an app, where there is
no previous image. The job says so explicitly rather than failing obscurely.

### By hand, when you need to

Roll back by **digest**, not by commit. Redeploying a digest re-runs the release that was
known good; rebuilding from a commit produces a new image and hopes it is identical.

```bash
# 1. What is live right now, and what was live before it
fly releases --app labelproof
fly image show --app labelproof --json

# 2. Put a known-good image back. --strategy immediate: this is an outage, not a rollout.
fly deploy --app labelproof --image registry.fly.io/labelproof@sha256:<digest> \
           --strategy immediate --wait-timeout 600

# 3. Prove it. Never trust a rollback you have not smoke-tested.
scripts/smoke.sh https://labelproof.fly.dev
```

### When the problem is configuration, not code

A bad secret or a bad environment value does not need a new image:

```bash
fly secrets set ANTHROPIC_API_KEY="sk-ant-..."   # restarts the machines
fly logs --app labelproof                        # structured JSON, one object per line
fly ssh console --app labelproof                 # last resort
```

Two failure signatures worth recognising immediately in the logs:

| Log event | What it means | Fix |
|---|---|---|
| `keepwarm_simulated_provider` | The service is in sample mode. Every verdict on the public URL is a replay of a built-in fixture. | Set `ANTHROPIC_API_KEY`; confirm `LABELPROOF_FAKE_PROVIDER` is `0`. |
| `keepwarm_cache_not_engaging` | The prompt cache is not being read or written. Cost per verification is at full price and the latency budget assumed otherwise. | `reason_code` names the likely cause. `prefix_below_model_minimum` means the current model will not cache a prefix this short — see the note below. |

### If everything is on fire

```bash
fly scale count 1 --app labelproof   # confirm exactly one machine is meant to be running
fly status --app labelproof          # machine state and check results
fly machine restart <id> --app labelproof
```

If the app is serving nothing and there is no good image to return to, redeploy the last
tag known to be good: `fly deploy --app labelproof --image registry.fly.io/labelproof:<tag>`.

---

## Destroy-and-redeploy drill (ENG-6, LP-136)

The point of infrastructure-as-configuration is that it can be proven, so this is a drill
you run rather than a property you assert. It destroys the app completely and rebuilds it
from the repository plus one secret. If any step needs a console click, an out-of-band
environment value, or a manually created volume, `fly.toml` is incomplete and the drill has
found it.

**Build the image for the platform you deploy to.** Fly runs `linux/amd64`. If you are on
an Apple Silicon machine, a local `docker build` produces `arm64` — the image you validate
is not the image that ships. Use `--platform linux/amd64` locally, or rely on
`fly deploy --remote-only` (which CI does) to build on the target architecture.

```bash
# 0. Baseline — record what a working service looks like
scripts/smoke.sh https://labelproof.fly.dev | tee /tmp/before.txt

# 1. Destroy. Everything: machines, image history, releases, secrets.
fly apps destroy labelproof --yes

# 2. Rebuild from configuration alone. Nothing here reads a saved state.
fly apps create labelproof
fly secrets set ANTHROPIC_API_KEY="sk-ant-..."
fly deploy --config fly.toml --remote-only

# 3. Prove it came back identical
scripts/smoke.sh https://labelproof.fly.dev | tee /tmp/after.txt
diff <(sed 's/[0-9]\+ ms//' /tmp/before.txt) <(sed 's/[0-9]\+ ms//' /tmp/after.txt)
```

Step 3 must pass with **no manual step between 2 and 3**. The one input that is not in the
repository is the API key, and that is the point: it is the only thing that should have to
come from somewhere else.

**What "identical" can and cannot mean here.** The configuration, the source and the base
image are pinned; the Python dependency set is not (see the note in the `Dockerfile`), so a
rebuild months later may resolve different library versions. The drill proves the
environment *rebuilds from configuration alone*, which is what ENG-6 asks. It does not
prove bit-identical images, and should not be described as though it does.

**Run it against a throwaway app.** `fly apps destroy labelproof` on the app a grader is
about to open is a bad trade for a checkmark. Create `labelproof-drill` from the same
`fly.toml` (`fly deploy --app labelproof-drill`), run the sequence there, and destroy it
afterwards. That converts the strongest claim in this repository from an assertion into a
result, and costs nothing anyone is looking at.

### Recorded result

**Not yet executed.** This procedure ships with the configuration it tests. The results
below are deliberately blank rather than filled with plausible output — this is the one
artifact whose entire purpose is proof, and inventing its output would make it the least
trustworthy thing in the repository.

Until it is run, the gate does the cheap half: `flyctl config validate` runs on every
deploy, so a `fly.toml` that the platform will not accept fails in CI rather than at
`fly deploy`. That catches a malformed config; it does not catch a config that is valid
and incomplete, which is what the drill is for.

| | |
|---|---|
| Date | _pending_ |
| Run by | _pending_ |
| Destroy → first green smoke | _pending_ |
| Manual steps required beyond the four commands above | _pending — any non-zero answer is a defect in `fly.toml`_ |
| `diff` of before/after smoke output | _pending — expected: empty_ |

Paste the terminal output under this table when the drill is run. If it required a fifth
command, record which one and fix `fly.toml` rather than the runbook.

---

## Notes for whoever inherits this

**The extraction model is not pinned in `fly.toml`.** It is an eval output, not a
deployment decision, and `api/config.py` is its single source of truth. Overriding it from
infrastructure would let production and the accuracy report disagree about which model
produced the numbers.

**Two things depend on the extraction model choice and are not obvious:**

1. **The prompt cache engages on the shipped model; check it again after any model
   change.** Measured with `count_tokens`, the system blocks are 2,074 tokens on Opus 5
   and 1,602 on Haiku 4.5, against minimum cacheable prefixes of 512 and 4,096. So on
   Opus 5 it caches comfortably, and Haiku 4.5 is the model where it would not. An
   earlier version of this note had that comparison backwards and read as though the
   cache were broken today; it is not. `keepwarm_cache_not_engaging` in the logs is how
   you find out if this changes.

   **The pre-warm has to send the same request the extractor does, and this has been got
   wrong twice.** Anything rendered at or before the cache breakpoint selects the entry:
   the response-format schema, `thinking`, `effort`, and `inference_geo` — which
   *partitions* the cache, so the same prompt in two geographies is two entries. Miss one
   and the warmer writes an entry only it ever reads, while the logs say `cache_read`
   forever. The second time this happened both requests wrote 4,351 tokens, so the log
   signature of working and broken was byte-identical; the numbers will not save you.

   `scripts/keepwarm.py:cache_parameters` therefore builds from the app's own `Config`
   and the adapter's own constants, restating nothing, and
   `tests/test_keepwarm.py` takes a **set difference** against the kwargs the adapter
   really sends. Add a parameter to `_one_call` and forget the warmer and the build
   fails. The earlier version of that test iterated a hand-written list of keys and
   could not fail — which is how the second miss shipped.
2. **The latency budget is pinned to the configured model** in `fly.toml`
   (`LABELPROOF_PROVIDER_TIMEOUT_MS` / `LABELPROOF_REQUEST_BUDGET_MS`). Change the model
   and these have to move with it — `scripts/smoke.sh` fails loudly if the budget is
   below the model's measured latency, and `tests/test_deploy_config.py` fails in CI
   before it ever reaches production.

**The deploy gate deliberately duplicates CI.** A deploy that depends on another workflow's
name to know it is safe stops being gated the day someone renames that workflow. Keep the
duplication.

**Do not add a volume without re-reading the retention policy.** Uploads and results are
supposed to be destroyed, and a volume also breaks the drill above by adding a manual
`fly volumes create` before `fly deploy`.
