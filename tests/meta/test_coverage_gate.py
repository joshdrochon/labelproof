"""META: the coverage gate fires, and cannot be turned off by how you spell the path.

A release gate nobody tests is a release gate that stops working and says nothing. This
one already did.

`_ran_the_whole_suite` compared the literal strings the user typed against a small
allowlist. With a rules module dropped below its floor:

    pytest tests   -> exit 1, gate fired
    pytest tests/  -> exit 0, SILENT
    pytest ./tests -> exit 0, SILENT

A trailing slash — which is how most people and most CI files write a path — turned the
gate off, and the coverage table still printed on the way past, so the run looked
measured. Same shape as an eval gate bypassed by one word.

These tests drive `_ran_the_whole_suite` and the floor arithmetic directly rather than
shelling out to a nested pytest: a subprocess run of the whole suite per case would add
minutes to every CI run, and the thing that was broken is the path comparison, which is
a pure function.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import (
    PROJECT_COVERAGE_FLOOR,
    RULES_COVERAGE_FLOORS,
    _ran_the_whole_suite,
)

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]


class _Config:
    """Just enough of `pytest.Config` for the selection check."""

    def __init__(self, selection: list[str]) -> None:
        self.option = type("_Option", (), {"file_or_dir": selection})()

    def getini(self, name: str) -> Any:
        assert name == "testpaths"
        return ["tests"]


class _Session:
    def __init__(self, selection: list[str]) -> None:
        self.config = _Config(selection)


def _selection(paths: list[str]) -> Any:
    """A stand-in `pytest.Session` carrying just a selection.

    Cast at this one boundary rather than sprinkling ignores at every call site: the
    function under test reads two attributes, and a real `Session` cannot be built
    outside a running pytest.
    """
    return _Session(paths)


# --------------------------------------------------------------------------------------
# The spellings that must all count as a full run
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selection",
    [
        [],
        ["tests"],
        ["tests/"],
        ["./tests"],
        ["./tests/"],
        [str(ROOT / "tests")],
        [str(ROOT / "tests") + "/"],
    ],
    ids=[
        "bare-pytest", "tests", "tests-slash", "dot-tests", "dot-tests-slash",
        "absolute", "absolute-slash",
    ],
)
def test_every_spelling_of_the_full_suite_arms_the_gate(selection: list[str]) -> None:
    """REGRESSION: `tests/` and `./tests` silently disabled the gate.

    Enumerated rather than reasoned about, because the failure mode was somebody
    reasoning that "tests" and "tests/" were obviously the same thing while the code
    compared them as strings.
    """
    assert _ran_the_whole_suite(_selection(selection)), selection


@pytest.mark.parametrize(
    "selection",
    [
        ["tests/properties"],
        ["tests/properties/"],
        ["tests/contract/test_offline.py"],
        [str(ROOT / "tests" / "regression")],
    ],
)
def test_a_partial_selection_does_not_arm_the_gate(selection: list[str]) -> None:
    """The gate stays off for a hand-picked run, which is the point of having the check.

    A floor that fires on `pytest tests/properties` tells the developer only that they
    ran a subset — which they know — and the lesson learned is to pass a bypass flag,
    which then gets pasted into CI. Both halves have to hold or the check is either
    useless or hostile.
    """
    assert not _ran_the_whole_suite(_selection(selection)), selection


def test_a_selection_that_is_not_a_path_does_not_arm_the_gate() -> None:
    """`pytest tests::something` and other odd spellings fail closed on the gate side.

    Failing *open* here would be wrong in the dangerous direction: it would enforce a
    floor against a partial run and produce a spurious red build.
    """
    assert not _ran_the_whole_suite(_selection(["tests::nonsense", "-k", "foo"]))


# --------------------------------------------------------------------------------------
# The floors themselves are meaningful
# --------------------------------------------------------------------------------------


def test_every_rules_module_has_a_floor() -> None:
    """A module added to the rules engine without a floor is a module with no gate.

    Computed from the directory rather than restated, so the omission is impossible
    rather than merely discouraged.
    """
    present = {
        f"api/rules/{path.name}"
        for path in (ROOT / "api" / "rules").glob("*.py")
        if path.name != "__init__.py"
    }
    assert present == set(RULES_COVERAGE_FLOORS), sorted(present ^ set(RULES_COVERAGE_FLOORS))


def test_no_rules_floor_has_been_quietly_lowered() -> None:
    """The floors are 100% but for one line that is documented as unreachable.

    Written as a bound rather than an exact dict so that raising a floor is free and
    lowering one is a visible edit to this assertion — which is the direction the
    comment in `conftest.py` insists on.
    """
    for module, floor in RULES_COVERAGE_FLOORS.items():
        assert floor >= 98.0, f"{module} floor has been lowered to {floor}"
    assert sum(1 for floor in RULES_COVERAGE_FLOORS.values() if floor < 100.0) <= 1


def test_the_project_floor_is_a_backstop_rather_than_a_target() -> None:
    """High enough to notice a deleted test file, low enough not to fire on drift."""
    assert 80.0 <= PROJECT_COVERAGE_FLOOR <= 95.0


def test_no_coverage_omit_entry_is_a_directory_wide_glob() -> None:
    """An omit glob is a claim about files that do not exist yet.

    `*/__init__.py` was the one entry in the list with no justification beside it, and
    it was not omitting nothing: it removed `api/routes/__init__.py` — 118 lines
    containing `provider_for` and `_fixture_provider`, the sample-mode fail-closed logic
    that is one of this suite's motivating incidents — plus `api/batch/__init__.py`.
    Neither is an empty package marker, and nobody had looked since the glob was
    written.

    Named files only. A file added later that ought to be omitted is then a deliberate
    edit with a reason next to it, which is the same standard every other entry meets.
    """
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    omitted = config["tool"]["coverage"]["run"]["omit"]

    globbed = [entry for entry in omitted if "*" in entry]
    assert globbed == [], f"coverage omit uses globs: {globbed}"


def test_every_omitted_file_exists_and_is_named_in_the_config() -> None:
    """A stale omit silently stops omitting, or silently omits a renamed file.

    Both directions are quiet failures: the first inflates the number, the second hides
    a module. Neither shows up anywhere except in this test.
    """
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    missing = [
        entry
        for entry in config["tool"]["coverage"]["run"]["omit"]
        if not (ROOT / entry).exists()
    ]
    assert missing == [], f"coverage omits files that do not exist: {missing}"


def test_the_bypass_flag_is_not_used_anywhere_in_the_repository() -> None:
    """`--no-cov-gate` exists for a local debugging session and nowhere else.

    It prints a loud banner when used, but a banner in a CI log nobody reads is not
    much of a guard — so the flag appearing in any *executable* checked-in surface is a
    failure here. Prose is exempt: `tests/README.md` documents the flag and says it is
    local-only, and a check that punished the documentation would push it out of the
    documentation and into somebody's shell history.
    """
    flag = "--no-cov" + "-gate"
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".yml", ".yaml", ".sh", ".toml", ".cfg"}:
            continue
        if any(part in {".git", ".venv", "node_modules", ".hypothesis"} for part in path.parts):
            continue
        if flag in path.read_text(errors="ignore"):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], offenders
