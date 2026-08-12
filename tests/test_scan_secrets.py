"""The secrets scan has to catch a real key on a real staged commit (SEC-6, LP-010).

There is a live `ANTHROPIC_API_KEY` in this working tree's gitignored `.env`. The tests
that matter are the ones that build an actual git repository, stage a key into it, and
assert the hook path refuses — not the ones that call a regex directly.

No literal credential appears in this file. Keys are assembled at runtime from fragments,
so the file does not itself trip the scanner it is testing, and so nothing here can ever
be mistaken for a real value.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import scan_secrets

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER = REPO_ROOT / "scripts" / "scan_secrets.py"

# Assembled, never written whole. "sk-ant-" + a plausible body.
ANTHROPIC_KEY = "sk-" + "ant-" + "api03-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPL9"
GITHUB_TOKEN = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyzAB"
PRIVATE_KEY_HEADER = "-----BEGIN" + " RSA PRIVATE KEY-----"

#: A documentation placeholder, assembled the same way for the same reason: written
#: whole it is a key-shaped literal in a test file, which `tests/contract/test_offline.py`
#: forbids outright — a fake long enough to authenticate teaches the next person to
#: paste a real one. The scanner must still decline to fire on it.
PLACEHOLDER_KEY = "sk-" + "ant-" + "api03-EXAMPLE-not-a-real-key"


# --- the headline case -----------------------------------------------------------------


def test_an_anthropic_key_in_any_file_is_caught() -> None:
    findings = scan_secrets.scan_text("api/thing.py", f'KEY = "{ANTHROPIC_KEY}"')

    assert len(findings) == 1
    assert findings[0].rule == "Anthropic API key"


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("api/config.py", f'DEFAULT = "{ANTHROPIC_KEY}"'),
        ("tests/fixture.json", f'{{"key": "{ANTHROPIC_KEY}"}}'),
        ("README.md", f"Export your key: `export ANTHROPIC_API_KEY={ANTHROPIC_KEY}`"),
        ("notes.txt", ANTHROPIC_KEY),
        ("deploy.sh", f"fly secrets set ANTHROPIC_API_KEY={ANTHROPIC_KEY}"),
    ],
    ids=["python", "json", "markdown", "bare", "shell"],
)
def test_an_anthropic_key_is_caught_whatever_it_is_wrapped_in(
    filename: str, content: str
) -> None:
    """A key does not become safe by being in a doc, a fixture, or a deploy script."""
    assert scan_secrets.scan_text(filename, content)


# --- the other credential shapes -------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected_rule"),
    [
        (f'aws_access_key_id = "{AWS_KEY}"', "AWS access key ID"),
        (f"token: {GITHUB_TOKEN}", "GitHub token"),
        (PRIVATE_KEY_HEADER, "Private key block"),
        ("token = " + "xoxb-" + "123456789012-abcdefghijklm", "Slack token"),
        ("key: " + "AIza" + "SyD-1234567890abcdefghijklmnopqrstu", "Google API key"),
        ("stripe = " + "sk_live_" + "51H8vQpLmNoPqRsTuVwXyZ01", "Stripe live key"),
        (
            "FLY_API_TOKEN=" + "fm2_" + "lJPECAAAAAAAAAlxCxBt" * 3,
            "Fly.io API token",
        ),
    ],
    ids=["aws", "github", "pem", "slack", "google", "stripe", "fly"],
)
def test_each_named_credential_shape_is_caught(content: str, expected_rule: str) -> None:
    findings = scan_secrets.scan_text("some/file.txt", content)

    assert [f.rule for f in findings] == [expected_rule]


#: Values live here and are interpolated below, so no source line in this file ever spells
#: out a secret-keyword-and-long-value pair on its own. Writing one would make this module
#: trip the very rule it is testing — which is exactly what used to justify exempting it
#: from the scan, and that exemption was a writable blind spot big enough to park a real
#: key in. In source the cases below read as a keyword followed by `{...}`, which matches
#: nothing; at runtime they produce the real shape.
#:
#: (This comment was itself a finding on the first attempt, for containing an example of
#: the pattern. Left as a note, because it is a good demonstration that the rule works.)
LONG_VALUE = "hunter2Sup3rL0ngV4lue!!x"
ENV_VALUE = "s3cretpasswordvalue123"
HEX_VALUE = "9f8e7d6c5b4a39281706abcdef"


@pytest.mark.parametrize(
    "content",
    [
        f'password = "{LONG_VALUE}"',
        f"DB_PASSWORD={ENV_VALUE}",
        f"client_secret: {HEX_VALUE}",
    ],
    ids=["quoted", "unquoted-env", "unquoted-hex"],
)
def test_a_long_assigned_credential_is_caught_with_no_known_prefix(content: str) -> None:
    assert [f.rule for f in scan_secrets.scan_text("app.conf", content)] == [
        "Assigned credential"
    ]


def test_this_test_module_is_not_exempt_from_the_scanner() -> None:
    """No file gets a free pass, least of all the scanner's own tests.

    `tests/test_scan_secrets.py` and `scripts/scan_secrets.py` were both on a content
    exemption list. Anyone could have parked a live key in either and both the pre-commit
    hook and CI's `--all` sweep would have waved it through.
    """
    assert scan_secrets.CONTENT_EXEMPT == ()

    for path in ("tests/test_scan_secrets.py", "scripts/scan_secrets.py"):
        source = (REPO_ROOT / path).read_bytes()
        assert scan_secrets.reduced_scan_reason(path, source) is None, f"{path} not scanned"
        assert scan_secrets.scan_path(path, source) == [], f"{path} trips its own scanner"


@pytest.mark.parametrize(
    "content",
    [
        "api_key=config.anthropic_api_key, max_retries=0",
        "auth_token: process.env.LABELPROOF_AUTH_TOKEN",
        "api_key = ANTHROPIC_API_KEY_FROM_ENVIRONMENT",
        "client_secret=settings.oauth.client_secret",
    ],
    ids=["python-attr", "node-env", "screaming-snake", "nested-attr"],
)
def test_passing_a_secret_by_reference_is_not_a_leak(content: str) -> None:
    """`api_key=config.anthropic_api_key` is the right way to do it, not a finding.

    This is the false positive that would have made the scanner unusable: the real
    adapter in api/provider/ passes the key exactly this way.
    """
    assert scan_secrets.scan_text("api/provider/adapter.py", content) == []


# --- what must NOT fire ----------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "LABELPROOF_LOG_LEVEL=INFO",
        "ANTHROPIC_API_KEY=",
        "LABELPROOF_TARGET_LONG_EDGE_PX=2576",
        'api_key = "your-key-here"',
        f'api_key = "{PLACEHOLDER_KEY}"',
        "password = ${SECRET_FROM_VAULT}",
        'client_secret = "<paste from the console>"',
        "api_key: {{ ansible_vault_key }}",
        "# password = changeme-before-you-deploy-this-anywhere",
        "sha256:6f4b2c1d9e8a7f3b5c0d1e2a3b4c5d6e7f8091a2",
    ],
    ids=[
        "plain-env-var",
        "empty-assignment",
        "numeric-config",
        "placeholder-your",
        "placeholder-example",
        "shell-interpolation",
        "angle-bracket-placeholder",
        "template-var",
        "commented-placeholder",
        "content-hash",
    ],
)
def test_placeholders_and_ordinary_config_do_not_fire(content: str) -> None:
    """A scanner that cries wolf gets bypassed, and then it protects nothing."""
    assert scan_secrets.scan_text("config.env", content) == []


def test_the_committed_env_example_is_clean() -> None:
    """.env.example documents every variable and must stay safe to commit (LP-011)."""
    example = REPO_ROOT / ".env.example"

    assert scan_secrets.scan_path(".env.example", example.read_bytes()) == []


def test_a_key_hidden_in_a_binary_file_is_still_caught() -> None:
    """Binary files used to return no findings at all.

    A key pasted into a PNG's metadata, or into any file with a NUL byte in its first
    8KB, was simply not looked at. Binary content still cannot be split into lines, so it
    gets the prefix rules over raw bytes instead of nothing.
    """
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + ANTHROPIC_KEY.encode()

    findings = scan_secrets.scan_path("fixtures/labels/whatever.png", png)

    assert [f.rule for f in findings] == ["Anthropic API key"]
    assert ANTHROPIC_KEY not in findings[0].render()


def test_a_key_in_an_oversized_file_is_still_caught() -> None:
    big = b"x" * (scan_secrets._MAX_TEXT_BYTES + 1) + ANTHROPIC_KEY.encode()

    assert [f.rule for f in scan_secrets.scan_path("huge.txt", big)] == ["Anthropic API key"]


@pytest.mark.parametrize(
    ("path", "data", "expected"),
    [
        ("art.png", b"\x89PNG\x00\x00 nothing here", "binary"),
        ("huge.txt", b"y" * (scan_secrets._MAX_TEXT_BYTES + 1), "over the text-scanning"),
        ("fine.py", b"x = 1\n", None),
    ],
    ids=["binary", "oversized", "ordinary"],
)
def test_a_file_that_cannot_be_read_as_text_says_so(
    path: str, data: bytes, expected: str | None
) -> None:
    """Skipping is defensible. Skipping without saying so is not.

    A gap in the check that nobody is told about is indistinguishable from a clean
    result, so the reason is reported up to `main` and printed whether or not anything
    was found.
    """
    reason = scan_secrets.reduced_scan_reason(path, data)

    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected in reason


def test_the_reduced_scan_is_announced_on_stderr(tmp_path: Path) -> None:
    art = tmp_path / "art.png"
    art.write_bytes(b"\x89PNG\x00\x00 harmless")

    result = subprocess.run(
        [sys.executable, str(SCANNER), str(art)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "prefixes only" in result.stderr
    assert "binary" in result.stderr


# --- forbidden files, whatever is inside them ------------------------------------------


@pytest.mark.parametrize(
    "path",
    [".env", ".env.local", ".env.production", "deploy/prod.pem", "keys/id_rsa", ".npmrc"],
)
def test_a_secret_bearing_file_is_refused_on_its_name_alone(path: str) -> None:
    findings = scan_secrets.scan_path(path, b"# nothing sensitive in here, honest\n")

    assert [f.rule for f in findings] == ["Secret-bearing file"]


def test_env_example_is_the_one_dotenv_that_is_allowed() -> None:
    assert scan_secrets.scan_path(".env.example", b"ANTHROPIC_API_KEY=\n") == []


# --- redaction -------------------------------------------------------------------------


def test_a_finding_never_reprints_the_credential() -> None:
    """Echoing the key into CI logs moves the leak; it does not stop it."""
    findings = scan_secrets.scan_text("x.py", f'k = "{ANTHROPIC_KEY}"')
    rendered = findings[0].render()

    assert ANTHROPIC_KEY not in rendered
    assert ANTHROPIC_KEY[8:] not in rendered
    assert str(len(ANTHROPIC_KEY)) in rendered


# --- the real thing: a staged commit in a real repository ------------------------------


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _run_git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sandbox"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-qm", "initial")
    return repo


def _scan_staged(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), "--staged"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_staged_key_exits_nonzero_and_names_the_file(sandbox_repo: Path) -> None:
    (sandbox_repo / "settings.py").write_text(f'ANTHROPIC_KEY = "{ANTHROPIC_KEY}"\n')
    _run_git(sandbox_repo, "add", "settings.py")

    result = _scan_staged(sandbox_repo)

    assert result.returncode == 1
    assert "settings.py" in result.stderr
    assert "Anthropic API key" in result.stderr
    assert ANTHROPIC_KEY not in result.stderr


def test_a_clean_staged_change_passes(sandbox_repo: Path) -> None:
    (sandbox_repo / "notes.md").write_text("Nothing secret here.\n")
    _run_git(sandbox_repo, "add", "notes.md")

    assert _scan_staged(sandbox_repo).returncode == 0


def test_a_key_staged_then_wiped_from_the_worktree_is_still_caught(
    sandbox_repo: Path,
) -> None:
    """The index is the truth. Staging a key and then editing the file must not slip by."""
    target = sandbox_repo / "settings.py"
    target.write_text(f'KEY = "{ANTHROPIC_KEY}"\n')
    _run_git(sandbox_repo, "add", "settings.py")
    target.write_text("KEY = os.environ['ANTHROPIC_API_KEY']\n")  # worktree now looks fine

    assert _scan_staged(sandbox_repo).returncode == 1


def test_force_adding_a_dotenv_is_refused(sandbox_repo: Path) -> None:
    (sandbox_repo / ".gitignore").write_text(".env\n")
    (sandbox_repo / ".env").write_text(f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}\n")
    _run_git(sandbox_repo, "add", "-f", ".env")

    result = _scan_staged(sandbox_repo)

    assert result.returncode == 1
    assert "Secret-bearing file" in result.stderr


def test_the_hook_actually_blocks_a_commit(sandbox_repo: Path) -> None:
    """End to end through git: hooksPath wired the way install_hooks.sh wires it."""
    hooks = sandbox_repo / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" "{SCANNER}" --staged\n'
    )
    hook.chmod(0o755)
    _run_git(sandbox_repo, "config", "core.hooksPath", ".githooks")

    (sandbox_repo / "leak.py").write_text(f'K = "{ANTHROPIC_KEY}"\n')
    _run_git(sandbox_repo, "add", "leak.py")
    result = subprocess.run(
        ["git", "-C", str(sandbox_repo), "commit", "-m", "oops"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, "the commit should have been refused"
    log = subprocess.run(
        ["git", "-C", str(sandbox_repo), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "oops" not in log.stdout


# --- the commit message, which the pre-commit hook cannot see -------------------------


def test_a_key_in_a_commit_message_is_caught(tmp_path: Path) -> None:
    """`git commit -m "temp, key is sk-ant-..."` used to sail straight through.

    pre-commit scans the index and the message is not in the index, so a key pasted into
    a commit message went into history untouched — exactly as permanently as one in a
    file, and considerably harder to notice afterwards.
    """
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(f"wip, remember the key is {ANTHROPIC_KEY}\n")

    result = subprocess.run(
        [sys.executable, str(SCANNER), "--message-file", str(message)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Anthropic API key" in result.stderr
    assert ANTHROPIC_KEY not in result.stderr


def test_an_ordinary_commit_message_passes(tmp_path: Path) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("Add the verdict card\n\nCloses: LP-100\n")

    result = subprocess.run(
        [sys.executable, str(SCANNER), "--message-file", str(message)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0


def test_the_commit_msg_hook_blocks_the_commit(sandbox_repo: Path) -> None:
    """End to end through git, with the real hook file from .githooks/."""
    hooks = sandbox_repo / ".githooks"
    hooks.mkdir()
    (sandbox_repo / "scripts").mkdir()
    shutil.copy2(SCANNER, sandbox_repo / "scripts" / "scan_secrets.py")
    hook = hooks / "commit-msg"
    hook.write_text(
        (REPO_ROOT / ".githooks" / "commit-msg").read_text().replace("python3", sys.executable)
    )
    hook.chmod(0o755)
    _run_git(sandbox_repo, "config", "core.hooksPath", ".githooks")

    (sandbox_repo / "fine.py").write_text("x = 1\n")
    _run_git(sandbox_repo, "add", "fine.py")
    result = subprocess.run(
        ["git", "-C", str(sandbox_repo), "commit", "-m", f"wip key {ANTHROPIC_KEY}"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, "the commit should have been refused"
    assert "wip key" not in _run_git_out(sandbox_repo, "log", "--oneline")


# --- the regression guard --------------------------------------------------------------


def test_the_whole_repository_scans_clean() -> None:
    """Whatever else changes, no tracked file may carry a credential.

    This is the assertion that keeps the CI step honest: it runs the same `--all` sweep
    the workflow does, so a scanner that has been quietly broken shows up here first.
    """
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--all"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
