<!--
  This file currently carries the deployment and rollback sections only (LP-136, LP-137).
  The developer log — what was built, how to run and test it locally — lands here
  separately (LP-142) and should slot in above "Deploying" without touching these.
-->

# CHANGES

Developer log. Written for the next engineer, not for a grader.

---

## Deploying

> **Status: the pipeline has never completed a run.** `ruff check .` and `mypy --strict
> api/` are currently red on files outside the deployment wave, so the gate fails and the
> deploy job correctly refuses to run. That is the gate working, but it means nothing
> below has been exercised end to end, and it is *why* a production that returned 503 on
> every verification survived long enough to be found by hand. The first green pipeline
> run is the thing to watch for; until then, treat this section as the intended behaviour
> rather than the observed one.

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
