"""Hook installation has to work from a clean clone (LP-016).

`git clone` never brings hooks with it — `.git/hooks` is local to every clone — so a
repository whose board integrity and branch protection depend on hooks has to make
installing them one command that actually works. These tests build a repository from
scratch, copy in the tracked `.githooks/` directory and the installer, run it, and then
drive the hooks the way git drives them.

The one that matters most is the executable-bit check. A hook without it is skipped by git
in complete silence: no error, no warning, the commit or push just goes through. That is
the failure this ticket exists to prevent.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / ".githooks"
INSTALLER = REPO_ROOT / "scripts" / "install_hooks.sh"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A fresh repository carrying this repo's hooks and installer, and nothing else."""
    repo = tmp_path / "clone"
    (repo / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")

    shutil.copytree(HOOKS_DIR, repo / ".githooks")
    shutil.copy2(INSTALLER, repo / "scripts" / "install_hooks.sh")
    shutil.copy2(REPO_ROOT / "scripts" / "scan_secrets.py", repo / "scripts")
    shutil.copy2(REPO_ROOT / "scripts" / "sync_board.py", repo / "scripts")

    (repo / "README.md").write_text("clone\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    return repo


def install(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "scripts/install_hooks.sh"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


# --- installation ----------------------------------------------------------------------


def test_the_installer_points_git_at_the_tracked_hooks(clone: Path) -> None:
    assert install(clone).returncode == 0
    assert git(clone, "config", "--get", "core.hooksPath") == ".githooks"


def test_the_hooks_path_is_relative_so_it_survives_a_worktree(clone: Path) -> None:
    """An absolute path pins every worktree to one checkout's hooks. Relative does not."""
    install(clone)
    configured = git(clone, "config", "--get", "core.hooksPath")

    assert not Path(configured).is_absolute()


def test_a_lost_executable_bit_is_restored(clone: Path) -> None:
    """A zip download or a permission-dropping filesystem loses it, and git says nothing."""
    hook = clone / ".githooks" / "pre-commit"
    hook.chmod(0o644)
    assert not hook.stat().st_mode & stat.S_IXUSR

    install(clone)

    assert hook.stat().st_mode & stat.S_IXUSR


def test_running_the_installer_twice_changes_nothing(clone: Path) -> None:
    first = install(clone)
    second = install(clone)

    assert (first.returncode, second.returncode) == (0, 0)
    assert git(clone, "config", "--get", "core.hooksPath") == ".githooks"


def test_the_installer_lists_every_hook_it_wired(clone: Path) -> None:
    result = install(clone)

    for hook in sorted(p.name for p in HOOKS_DIR.iterdir() if p.is_file()):
        assert hook in result.stdout


def test_the_installer_is_itself_executable() -> None:
    """The one command README and CHANGES tell every clone to run.

    `./scripts/install_hooks.sh` is how the hooks get wired at all, so it is one level
    above everything else here: lose its executable bit and the instruction fails, nobody
    installs hooks, and neither the secrets scan nor the main-branch protection is
    running. The CI hooks check covers it for the same reason. Asserted against the mode
    git records, not the working tree's, because that is what a fresh clone gets.
    """
    mode = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-s", "scripts/install_hooks.sh"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[0]

    assert mode == "100755", f"install_hooks.sh is committed as {mode}, not executable"


@pytest.mark.parametrize(
    "hook", ["pre-commit", "pre-push", "post-commit", "commit-msg"]
)
def test_every_hook_is_committed_executable(hook: str) -> None:
    """git skips a non-executable hook in complete silence. No error, no warning."""
    mode = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-s", f".githooks/{hook}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[0]

    assert mode == "100755", f".githooks/{hook} is committed as {mode}, not executable"


# --- pre-push: main stays protected ----------------------------------------------------
#
# This hook predates this branch. These tests exist so it cannot be broken silently by
# someone editing the directory it lives in.


def _run_pre_push(repo: Path, ref: str) -> subprocess.CompletedProcess[str]:
    """Feed the hook the line git feeds it: local-ref local-sha remote-ref remote-sha."""
    sha = git(repo, "rev-parse", "HEAD")
    return subprocess.run(
        ["sh", ".githooks/pre-push", "origin", "https://example.invalid/repo.git"],
        cwd=repo,
        input=f"refs/heads/work {sha} {ref} {sha}\n",
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/master"])
def test_pushing_to_the_default_branch_is_refused(clone: Path, ref: str) -> None:
    result = _run_pre_push(clone, ref)

    assert result.returncode != 0
    assert "BLOCKED" in result.stderr


def test_pushing_a_branch_is_allowed(clone: Path) -> None:
    assert _run_pre_push(clone, "refs/heads/wave/ci").returncode == 0


# --- pre-commit: the secrets scan is actually wired ------------------------------------


def test_the_installed_pre_commit_hook_blocks_a_staged_key(clone: Path) -> None:
    """End to end: install, stage a key, and watch git refuse the commit."""
    install(clone)
    key = "sk-" + "ant-" + "api03-" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2H1g0"
    (clone / "leak.py").write_text(f'KEY = "{key}"\n')
    git(clone, "add", "leak.py")

    result = subprocess.run(
        ["git", "-C", str(clone), "commit", "-m", "add a key by accident"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "add a key by accident" not in git(clone, "log", "--oneline")


def test_a_clean_commit_still_goes_through(clone: Path) -> None:
    """The hook has to be invisible when nothing is wrong, or it gets uninstalled."""
    install(clone)
    (clone / "notes.md").write_text("nothing secret\n")
    git(clone, "add", "notes.md")
    git(clone, "commit", "-qm", "add notes")

    assert "add notes" in git(clone, "log", "--oneline")


# --- post-commit: the board projection runs --------------------------------------------


def test_the_board_projector_survives_a_repo_with_no_board(clone: Path) -> None:
    """TICKETS.md is gitignored, so the hook must no-op rather than fail every commit."""
    install(clone)
    (clone / "thing.txt").write_text("x\n")
    git(clone, "add", "thing.txt")
    git(clone, "commit", "-qm", "work\n\nCloses: LP-999")

    assert "work" in git(clone, "log", "--oneline")
