#!/usr/bin/env python3
"""Refuse to commit a credential (SEC-6, LP-010).

There is a real `.env` in this working tree holding a live `ANTHROPIC_API_KEY`. It is
gitignored, which protects it right up until someone runs `git add -f .env`, renames it,
or pastes the key into a test fixture "just to check something". This script is the
thing that catches that.

It runs three ways, all of which execute the same checks:

    .githooks/pre-commit         from a clean clone, no installs (see install_hooks.sh)
    .pre-commit-config.yaml      for anyone who already uses the pre-commit framework
    .github/workflows/ci.yml     over every tracked file, on every push

Two deliberate design choices, because both will be questioned:

**No downloaded rule packs.** gitleaks and detect-secrets carry far more patterns than
this file does, and both fetch a repo or a binary at install time. This project's entire
test posture is offline-and-deterministic, and the customer's network blocks outbound
traffic to most of the internet (NET-1). A security control that only works when the
network is up is the wrong shape here. So: named patterns, in one auditable file, that
cover the credential shapes this repository can actually leak.

**No entropy heuristic and no allowlist file.** This repo is full of base64 image
fixtures and 40-character SHAs; entropy scanning would fire on them constantly, and a
scanner people learn to bypass is worse than no scanner. Every hit here is explainable in
one sentence. The escape hatch is `git commit --no-verify`, which is loud and deliberate.

Matches are printed redacted. A scanner that echoes the secret into terminal scrollback
and CI logs has moved the leak, not stopped it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# --- what a credential looks like ------------------------------------------------------
#
# Ordered most-specific first, so a match reports the tightest rule that explains it.


@dataclass(frozen=True)
class Rule:
    """One credential shape, its pattern, and what to tell the person who tripped it."""

    name: str
    pattern: re.Pattern[str]
    advice: str
    #: Which capture group holds the credential itself. 0 means the whole match.
    value_group: int = 0
    #: When the value arrived unquoted and reads as an identifier, treat it as a
    #: reference rather than a literal — `api_key=config.anthropic_api_key` is the
    #: correct way to pass a secret, not a leak of one.
    unquoted_identifier_is_a_reference: bool = False


_ANTHROPIC_ADVICE = (
    "Rotate it at console.anthropic.com, then put the new one in .env (gitignored) "
    "or the deployment platform's secret store."
)

RULES: tuple[Rule, ...] = (
    Rule(
        "Anthropic API key",
        # The shape this repo is most likely to leak: `sk-ant-api03-...` and any future
        # `sk-ant-` variant. Deliberately loose after the prefix.
        re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
        _ANTHROPIC_ADVICE,
    ),
    Rule(
        "OpenAI-style API key",
        re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{32,}\b"),
        "Rotate it with the provider and move it to the secret store.",
    ),
    Rule(
        "Private key block",
        re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
        "Private keys never belong in a repository. Remove it and reissue the key pair.",
    ),
    Rule(
        "AWS access key ID",
        re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
        "Disable the key in IAM and issue a new one.",
    ),
    Rule(
        "GitHub token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
        "Revoke it in GitHub developer settings.",
    ),
    Rule(
        "Slack token",
        re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b"),
        "Revoke it in the Slack app configuration.",
    ),
    Rule(
        "Google API key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        "Revoke it in the Google Cloud console.",
    ),
    Rule(
        "Stripe live key",
        re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}\b"),
        "Roll it in the Stripe dashboard immediately.",
    ),
    Rule(
        # The deploy target for this project (pinned build decision), so worth its own rule.
        "Fly.io API token",
        re.compile(r"\b(?:FlyV1\s+fm\d|fm\d[a-z]?_[A-Za-z0-9+/=_-]{40,})"),
        "Run `fly tokens revoke` and issue a new deploy token.",
    ),
    Rule(
        "JSON Web Token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "If it is a real session or service token, revoke it; if it is test data, "
        "shorten it so it no longer parses as a JWT.",
    ),
    Rule(
        # The catch-all. Requires a secret-ish name, an assignment, and a long value, so
        # it does not fire on `LABELPROOF_LOG_LEVEL=INFO`. Placeholders are filtered
        # below rather than being written into this pattern, which keeps it readable.
        "Assigned credential",
        re.compile(
            # No leading \b: `DB_PASSWORD=` must match, and `_` is a word character, so
            # a boundary before PASSWORD would never fire there.
            r"(?i)(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token"
            r"|client[_-]?secret|password|passwd)\b\s*[:=]\s*"
            r"([\"']?)([A-Za-z0-9/+=_.-]{20,})"
        ),
        "If it is real, rotate it and read it from the environment instead.",
        value_group=2,
        unquoted_identifier_is_a_reference=True,
    ),
)

#: Shapes that are a *reference* to a credential rather than the credential:
#: `config.anthropic_api_key`, `process.env.TOKEN`, `SOME_LONG_ENV_VAR_NAME`. A bare
#: lowercase run like `s3cretpasswordvalue` is NOT here — that is what a leaked value
#: looks like, and exempting it would be the hole this scanner exists to close.
_REFERENCE_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"),  # dotted attribute
    re.compile(r"[A-Z][A-Z0-9_]*"),  # SCREAMING_SNAKE env var name
)


def _is_reference(value: str) -> bool:
    return any(shape.fullmatch(value) for shape in _REFERENCE_SHAPES)

#: Substrings that mark a value as a placeholder rather than a credential. Checked
#: case-insensitively against the matched text.
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "example",
    "placeholder",
    "your-",
    "your_",
    "changeme",
    "change-me",
    "redacted",
    "dummy",
    "fake",
    "sample",
    "xxxxx",
    "notreal",
    "not-real",
    "test-key",
    "<",
    "${",
    "{{",
)

#: Files that are a leak by their very presence, regardless of content. `.gitignore`
#: already lists these; this catches `git add -f` and anything renamed past the ignore.
FORBIDDEN_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|/)\.env$"),
    re.compile(r"(?:^|/)\.env\.(?!example$).+$"),
    re.compile(r"\.pem$"),
    re.compile(r"\.p12$"),
    re.compile(r"\.pfx$"),
    re.compile(r"(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r"(?:^|/)\.npmrc$"),
    re.compile(r"(?:^|/)\.pypirc$"),
)

#: Paths whose *content* is not scanned. **Deliberately empty.**
#:
#: It used to hold this file, its test module, and the lock files. Every one of those was
#: a permanent, writable blind spot: anyone could park a live key in
#: `tests/test_scan_secrets.py` and both the hook and CI's `--all` sweep would wave it
#: through. Removing all five entries produces exactly zero false positives across the
#: repository, because the test module now assembles its fixture values at runtime rather
#: than writing them as literals — which is the right way to keep a scanner's own tests
#: from tripping it. Left in place as a named extension point so that if an exemption ever
#: becomes genuinely necessary, it lands here with a reason attached instead of being
#: hidden in a regex somewhere.
CONTENT_EXEMPT: tuple[re.Pattern[str], ...] = ()

#: Above this, a file is scanned for prefix patterns over raw bytes rather than parsed
#: into lines. Not skipped — see `scan_path`.
_MAX_TEXT_BYTES = 2_000_000

#: The rules whose patterns are unambiguous enough to run against raw bytes, where there
#: are no line numbers and no surrounding syntax to reason about. "Assigned credential" is
#: excluded: it needs `key = value` context, and without it the false-positive rate on
#: compressed data would be absurd.
_BYTE_RULE_NAMES = frozenset(
    {
        "Anthropic API key",
        "OpenAI-style API key",
        "Private key block",
        "AWS access key ID",
        "GitHub token",
        "Slack token",
        "Google API key",
        "Stripe live key",
        "Fly.io API token",
    }
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    redacted: str
    advice: str

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"  {where}\n      {self.rule}: {self.redacted}\n      {self.advice}"


def redact(secret: str) -> str:
    """Show enough to identify the credential, never enough to use it."""
    keep = 4 if len(secret) > 12 else 1
    return f"{secret[:keep]}{'*' * min(len(secret) - keep, 12)} ({len(secret)} chars)"


def _is_placeholder(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _exempt(path: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(path) for pattern in patterns)


def scan_text(path: str, text: str) -> list[Finding]:
    """Every credential-shaped string in one file's content."""
    if _exempt(path, CONTENT_EXEMPT):
        return []

    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule in RULES:
            match = rule.pattern.search(line)
            if match is None:
                continue
            secret = match.group(rule.value_group)
            if _is_placeholder(match.group(0)):
                continue
            quoted = bool(rule.value_group and match.group(rule.value_group - 1))
            if rule.unquoted_identifier_is_a_reference and not quoted and _is_reference(secret):
                continue
            findings.append(Finding(path, lineno, rule.name, redact(secret), rule.advice))
            break  # one finding per line is enough to block the commit
    return findings


def reduced_scan_reason(path: str, data: bytes) -> str | None:
    """Why this file cannot be parsed as lines of text, if it cannot.

    Returned rather than swallowed, because "the scanner skipped 47 files" is something
    the operator has to be able to find out. Silence here is how a key inside a PNG's
    metadata gets committed under a green check.
    """
    if _exempt(path, CONTENT_EXEMPT):
        return "exempted by configuration"
    if b"\x00" in data[:8192]:
        return "binary"
    if len(data) > _MAX_TEXT_BYTES:
        return f"{len(data) // 1_000_000}MB, over the text-scanning limit"
    return None


def scan_bytes(path: str, data: bytes) -> list[Finding]:
    """Prefix-pattern scan over raw bytes, for content that has no lines.

    Binary and oversized files used to return `[]`. A key pasted into a document's
    metadata, or into a file that happens to be 3MB, was simply not looked at. This does
    not recover line numbers or the assignment-shaped rule, but it does catch every
    credential family with an unmistakable prefix, which is the ones that matter most.
    """
    findings: list[Finding] = []
    text = data.decode("latin-1")  # total, lossless byte->char mapping; never raises
    for rule in RULES:
        if rule.name not in _BYTE_RULE_NAMES:
            continue
        match = rule.pattern.search(text)
        if match is None or _is_placeholder(match.group(0)):
            continue
        findings.append(
            Finding(path, 0, rule.name, redact(match.group(rule.value_group)), rule.advice)
        )
    return findings


def scan_path(path: str, data: bytes) -> list[Finding]:
    """Content and filename checks for one file."""
    for pattern in FORBIDDEN_PATHS:
        if pattern.search(path):
            return [
                Finding(
                    path,
                    0,
                    "Secret-bearing file",
                    "this file must never be committed",
                    "It is listed in .gitignore. If you meant to share its shape, "
                    "edit .env.example instead.",
                )
            ]

    if reduced_scan_reason(path, data) is not None:
        return scan_bytes(path, data)

    return scan_text(path, data.decode("utf-8", errors="replace"))


# --- git plumbing ----------------------------------------------------------------------


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], capture_output=True, check=True
    ).stdout


def staged_files() -> list[str]:
    """Paths added, copied, modified, or renamed in the index."""
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [name for name in out.decode().split("\0") if name]


def tracked_files() -> list[str]:
    out = _git("ls-files", "-z")
    return [name for name in out.decode().split("\0") if name]


def staged_content(path: str) -> bytes:
    """Read from the index, not the working tree.

    These differ, and the difference is exactly the attack: stage a key, delete it from
    the file, commit. A hook that reads the working tree sees nothing.
    """
    try:
        return _git("show", f":{path}")
    except subprocess.CalledProcessError:
        return b""


def worktree_content(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


# --- entry point -----------------------------------------------------------------------


@dataclass(frozen=True)
class Scan:
    findings: list[Finding]
    #: `path -> why it got the reduced byte-level scan`. Reported, never silent.
    reduced: dict[str, str]


def _collect(sources: list[tuple[str, bytes]]) -> Scan:
    findings: list[Finding] = []
    reduced: dict[str, str] = {}
    for path, data in sources:
        reason = reduced_scan_reason(path, data)
        if reason is not None and not any(p.search(path) for p in FORBIDDEN_PATHS):
            reduced[path] = reason
        findings.extend(scan_path(path, data))
    return Scan(findings, reduced)


def run(mode: str, paths: list[str]) -> Scan:
    if mode == "staged":
        return _collect([(p, staged_content(p)) for p in staged_files()])
    if mode == "all":
        return _collect([(p, worktree_content(p)) for p in tracked_files()])
    if mode == "message":
        return _collect([(p, worktree_content(p)) for p in paths])
    return _collect([(p, worktree_content(p)) for p in paths])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scan_secrets",
        description="Refuse to commit a credential (SEC-6).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--staged",
        action="store_true",
        help="scan the git index — what the pre-commit hook uses (default)",
    )
    group.add_argument(
        "--all", action="store_true", help="scan every tracked file — what CI uses"
    )
    group.add_argument(
        "--message-file",
        metavar="PATH",
        help=(
            "scan a commit message — what the commit-msg hook uses. A key pasted into "
            "a commit message is in history forever, exactly like one in a file."
        ),
    )
    parser.add_argument("paths", nargs="*", help="specific files to scan")
    args = parser.parse_args(argv)

    if args.all:
        mode, targets = "all", []
    elif args.message_file:
        mode, targets = "message", [args.message_file]
    elif args.paths and not args.staged:
        mode, targets = "paths", args.paths
    else:
        mode, targets = "staged", []

    scan = run(mode, targets)
    findings = scan.findings

    # Announced whether or not anything was found. A file the scanner could not read as
    # text is a gap in the check, and a gap nobody is told about is indistinguishable
    # from a clean result.
    if scan.reduced:
        print(
            f"note: {len(scan.reduced)} file(s) scanned for key prefixes only, not line "
            f"by line:",
            file=sys.stderr,
        )
        for path, reason in sorted(scan.reduced.items()):
            print(f"  {path} ({reason})", file=sys.stderr)

    if not findings:
        return 0

    plural = "" if len(findings) == 1 else "s"
    print(f"\nBLOCKED: {len(findings)} possible credential{plural} found.\n", file=sys.stderr)
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    print(
        "\nNothing has been committed. Remove the value, rotate it if it was ever real,\n"
        "and read it from the environment (.env is gitignored; see .env.example).\n"
        "If this is a false positive, `git commit --no-verify` bypasses the hook —\n"
        "and please add the case to scripts/scan_secrets.py so the next person is not\n"
        "stopped by it too.\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
