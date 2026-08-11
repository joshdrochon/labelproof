<!--
  This file currently carries the deployment and rollback sections only (LP-136, LP-137).
  The developer log — what was built, how to run and test it locally — lands here
  separately (LP-142) and should slot in above "Deploying" without touching these.
-->

# CHANGES

Developer log. Written for the next engineer, not for a grader.

---

## Deploying

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

You do not normally do this by hand. The deploy job records the digest of the currently
live image *before* touching anything, and if either the deploy or the smoke test fails it
puts that digest straight back and re-runs the smoke test to confirm service is restored.

Rollback fires on:

- `fly deploy` failing or timing out (a half-applied release needs reverting too)
- any smoke-test failure — a missing key, a broken web build, a field dropped out of the
  pipeline, or the service answering in sample mode

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

### Recorded result

**Not yet executed.** This procedure ships with the configuration it tests; the run
requires a live Fly account and an API key, and the results below are deliberately blank
rather than filled with plausible output.

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

1. **The prompt-cache pre-warm only pays on models whose minimum cacheable prefix is below
   ~1.7k tokens.** The extraction system prompt measures around that; the minimum varies by
   model and a prefix under it caches silently not at all — no error, just a full-price
   bill. `keepwarm_cache_not_engaging` in the logs is how you find out. Verify after any
   model change.
2. **The 5-second budget assumes the adopted model's measured latency.** The machine is
   sized for roughly 300 ms of non-provider work (`performance-2x`, two dedicated cores).
   If a faster model widens the budget, `shared-cpu-2x` at 2 GB is a one-line change in
   `fly.toml` and about an eighth of the cost.

**The deploy gate deliberately duplicates CI.** A deploy that depends on another workflow's
name to know it is safe stops being gated the day someone renames that workflow. Keep the
duplication.

**Do not add a volume without re-reading the retention policy.** Uploads and results are
supposed to be destroyed, and a volume also breaks the drill above by adding a manual
`fly volumes create` before `fly deploy`.
