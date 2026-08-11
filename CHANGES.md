# CHANGES

Developer log for LabelProof. Written for the next engineer to work on this, not for a
grader (ENG-5) — so it answers the three questions you actually have on day one: how do I
run it, how do I test it, and how do I undo it.

`PRD.md` owns *what* and *why*. `BUILD.md` owns the pinned architecture decisions.
`README.md` is the submission-facing document. This file is the operational one.

> **Status.** Seeded at LP-003. The run and test sections are live and verified. The
> rollback section is a **procedure, not yet a drill** — nothing has been deployed as of
> this writing, so the deploy-level steps are marked accordingly rather than presented as
> tested. LP-137 makes them real; LP-244 breaks a deploy on purpose to prove they work.

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
provider is reachable). `/ready` is the one that tells you whether the API key works.

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

`mypy --strict` is clean over `api/` and over every file in `scripts/`, each of which is
listed individually in `pyproject.toml`'s `files` so the claim can be checked. `tests/`,
`eval/`, and `fixtures/` are **not** yet strict-clean: `mypy --strict api tests eval
fixtures` reports **22 errors in 9 files**, broken down per file in `pyproject.toml`.
That is a measured number with the command that produces it, not an estimate.

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

> **Not yet drilled.** Nothing is deployed as of LP-003. The procedure below is the
> intended one from `BUILD.md` §1 (host: Fly.io, `min_machines_running = 1`). LP-137
> documents it against a real deployment; LP-244 forces a bad deploy and proves the
> rollback works. Until those close, treat this as a plan, not a runbook.

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
