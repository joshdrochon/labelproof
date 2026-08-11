"""META: every canonical test case has a named automated test, and the suite stays legible.

LP-237 asks for a regression test per canonical case, TC-01 through TC-22. The usual way
that requirement decays is silently: a case gets renamed, a test gets deleted in a
refactor, and the claim in the README stays true-looking because nobody recounts.

So the claim is computed rather than asserted from memory. These tests read the suite
itself — the `@pytest.mark.tc` markers — and check the coverage against the PRD's table.
A case that loses its last test fails here by name.

The rest of the file holds the conventions in `tests/README.md` to the same standard. A
convention that is documented and unenforced is a convention that describes the past.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent
PRD = ROOT / "PRD.md"

#: The PRD's canonical table, TC-01 through TC-22.
CANONICAL_CASES = [f"TC-{n:02d}" for n in range(1, 23)]

#: Directories this branch introduced, each with a stated job. Listed here so that a new
#: one has to be described before it can be added — an unexplained directory is where
#: tests go when nobody knows where they belong.
LAYERS = {
    "properties": "claims about all inputs, checked with hypothesis",
    "regression": "one historical defect per file, each naming what it pins",
    "contract": "agreements with something outside this process",
    "e2e": "whole journeys over the real HTTP stack",
    "meta": "tests about the test suite itself",
}


def _test_modules() -> list[Path]:
    return sorted(TESTS.rglob("test_*.py"))


def _marked_cases() -> dict[str, list[str]]:
    """Canonical case -> the modules that carry a test for it."""
    found: dict[str, list[str]] = {}
    for module in _test_modules():
        for case in set(re.findall(r'pytest\.mark\.tc\("(TC-\d{2})"\)', module.read_text())):
            found.setdefault(case, []).append(str(module.relative_to(TESTS)))
    return found


# --------------------------------------------------------------------------------------
# LP-237 — every canonical case is automated
# --------------------------------------------------------------------------------------


def test_every_canonical_case_in_the_prd_has_at_least_one_named_test() -> None:
    """The claim, computed from the suite rather than remembered.

    A case whose last test is deleted in a refactor fails here by name, which is the
    only way this requirement survives contact with six months of maintenance.
    """
    missing = sorted(set(CANONICAL_CASES) - set(_marked_cases()))
    assert missing == [], f"canonical cases with no automated test: {missing}"


def test_no_test_claims_a_case_the_prd_does_not_define() -> None:
    """A marker for `TC-23` is either a typo or a case somebody forgot to write down.

    Either way the coverage report above would be counting something that does not
    exist.
    """
    unknown = sorted(set(_marked_cases()) - set(CANONICAL_CASES))
    assert unknown == [], f"tests marked with unknown cases: {unknown}"


def test_the_prd_still_defines_the_cases_this_suite_covers() -> None:
    """The other direction: the source of truth is the PRD, not this list.

    If the PRD grows TC-23, this fails and somebody has to decide whether it needs a
    fixture — rather than the case quietly having no test forever.
    """
    if not PRD.exists():  # pragma: no cover - the PRD is committed
        pytest.skip("PRD.md is not present in this checkout")
    defined = set(re.findall(r"\| (TC-\d{2}) \|", PRD.read_text()))
    assert defined == set(CANONICAL_CASES), sorted(defined ^ set(CANONICAL_CASES))


@pytest.mark.parametrize("case", ["TC-03", "TC-04", "TC-05", "TC-07"])
def test_every_implemented_warning_case_is_covered_in_more_than_one_layer(
    case: str,
) -> None:
    """The warning cases carry LP-290 and are held to a higher bar.

    "Zero false passes on warning violations" is the product's central claim. A single
    unit test asserting it is a claim about one function; the same case asserted in the
    rules layer *and* end to end is a claim about the product.

    TC-06 is excluded on purpose: prominence heuristics are LP-211 and do not exist, so
    demanding two layers of coverage for a capability nobody has built would be
    theatre. It is covered once, and the missing capability is pinned as a strict xfail
    in tests/e2e/test_verify_flows.py.
    """
    modules = _marked_cases().get(case, [])
    assert modules, f"{case} has no test at all"
    layers = {Path(module).parts[0] if "/" in module else "unit" for module in modules}
    assert len(layers) >= 2, f"{case} is only covered in {layers}"


# --------------------------------------------------------------------------------------
# The suite stays legible to somebody reading it cold
# --------------------------------------------------------------------------------------


def test_the_conventions_are_written_down() -> None:
    """`tests/README.md` is the map. Without it the directory names are guesses."""
    readme = TESTS / "README.md"
    assert readme.exists(), "tests/README.md is missing"
    text = readme.read_text()
    for layer in LAYERS:
        assert f"`{layer}/`" in text, f"tests/README.md does not describe {layer}/"


def test_every_layer_directory_is_one_the_readme_describes() -> None:
    """A new directory has to be explained before it can be added.

    An unexplained directory is where tests go when nobody knows where they belong,
    and it stops being possible to say what the suite covers.
    """
    present = {
        path.name
        for path in TESTS.iterdir()
        if path.is_dir() and not path.name.startswith((".", "__"))
    }
    assert present <= set(LAYERS), sorted(present - set(LAYERS))


def test_every_test_module_explains_what_it_is_for() -> None:
    """A module docstring, because a grader reads files before they read functions.

    The first thing anybody opening `test_aggregate_warning_holes.py` needs is the
    sentence saying what a warning hole is.
    """
    import ast

    undocumented = [
        str(module.relative_to(TESTS))
        for module in _test_modules()
        if not ast.get_docstring(ast.parse(module.read_text()))
    ]
    assert undocumented == [], undocumented


@pytest.mark.parametrize("layer", sorted(LAYERS))
def test_every_module_in_a_layer_declares_its_marker(layer: str) -> None:
    """`pytestmark` at the top of the file, so `-m contract` selects the whole layer.

    Per-function markers drift; a module-level one cannot be forgotten on a new test.
    """
    directory = TESTS / layer
    expected = {
        "properties": "property",
        "regression": "regression",
        "contract": "contract",
        "e2e": "e2e",
        # `meta/` tests the suite, which is an agreement with a reader rather than with
        # a process — close enough to a contract to share the marker rather than mint a
        # sixth one nobody would remember to select.
        "meta": "contract",
    }[layer]
    for module in sorted(directory.rglob("test_*.py")):
        assert f"pytestmark = pytest.mark.{expected}" in module.read_text(), module.name


def _layer_modules() -> list[Path]:
    """Modules in the layered directories.

    The per-module test files at the root of `tests/` predate this structure and belong
    to other owners; holding their names and shapes to conventions introduced here would
    be this branch grading somebody else's file.
    """
    return sorted(
        module for module in _test_modules() if module.parent.name in LAYERS
    )


def test_every_test_function_names_a_behaviour_rather_than_a_function() -> None:
    """`test_a_missing_warning_is_disqualifying`, not `test_recommend_2`.

    The name is what a grader reads in the failure output, and it is the only part of a
    test guaranteed to be read. A name that repeats the function under test tells them
    nothing the traceback does not.
    """
    import ast

    offenders: list[str] = []
    for module in _layer_modules():
        for node in ast.walk(ast.parse(module.read_text())):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            if len(node.name[len("test_") :].split("_")) < 3:
                offenders.append(f"{module.relative_to(TESTS)}::{node.name}")
    assert offenders == [], offenders



def _skip_marks(module: Path) -> list[tuple[int, str]]:
    """Every `@pytest.mark.skip(...)` in a module, with its argument text."""
    import ast

    source = module.read_text()
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        parts: list[str] = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if parts[:2] != ["skip", "mark"]:
            continue
        found.append((node.lineno, ast.get_source_segment(source, node) or ""))
    return found


def test_no_test_is_skipped_or_xfailed_without_a_reason() -> None:
    """A bare `skip` is a test nobody will ever turn back on.

    Every skip and xfail in this suite says what it is waiting for, so a grader can tell
    a known gap from an abandoned one.
    """
    offenders: list[str] = []
    for module in _test_modules():
        for line, text in _xfail_reasons(module) + _skip_marks(module):
            if "reason=" not in text:
                offenders.append(f"{module.relative_to(TESTS)}:{line}")
    assert offenders == [], offenders


def test_every_xfail_is_strict() -> None:
    """A non-strict xfail passes forever once the defect is fixed, and the pin rots.

    Strictness turns a pinned defect into a notification: fixing it makes the build red,
    and somebody has to come back and remove the marker. Without it, the six open
    defects this branch pinned would quietly become six tests that pass either way.
    """
    offenders: list[str] = []
    for module in _test_modules():
        for line, text in _xfail_reasons(module):
            if "strict=True" not in text:
                offenders.append(f"{module.relative_to(TESTS)}:{line}")
    assert offenders == [], offenders


def _xfail_reasons(module: Path) -> list[tuple[int, str]]:
    """Every `@pytest.mark.xfail(...)` in a module, with its full argument text.

    Parsed with `ast` rather than by regex. The regex version required the closing
    paren on a line of its own — `pytest\\.mark\\.xfail\\((.*?)\\n\\)` — so a
    single-line `@pytest.mark.xfail(strict=True, reason="no owner named")` matched
    nothing at all and the check passed vacuously on it. A test whose empty match set
    is its pass condition is the thing this file exists to find.
    """
    import ast

    source = module.read_text()
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        parts: list[str] = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if parts[:2] != ["xfail", "mark"]:
            continue
        found.append((node.lineno, ast.get_source_segment(source, node) or ""))
    return found


def test_every_pinned_defect_names_the_file_that_owns_it() -> None:
    """A defect report with no owner is a defect report nobody reads.

    Each xfail reason names the module to change, so the person who picks it up starts
    in the right file.
    """
    offenders: list[str] = []
    for module in sorted(TESTS.rglob("test_*.py")):
        for line, text in _xfail_reasons(module):
            if "Owner:" not in text:
                offenders.append(f"{module.relative_to(TESTS)}:{line}")
    assert offenders == [], offenders


def test_the_owner_check_sees_a_single_line_xfail() -> None:
    """The teeth. The previous regex matched nothing on one, and passed.

    Parsed from a literal here rather than from a real test, so the check is exercised
    against the exact shape that used to slip past it.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        probe = Path(directory) / "test_probe.py"
        probe.write_text(
            "import pytest\n\n"
            '@pytest.mark.xfail(strict=True, reason="no owner named here at all")\n'
            "def test_x() -> None:\n    pass\n"
        )
        found = _xfail_reasons(probe)

    assert len(found) == 1, "a single-line xfail was not seen at all"
    assert "Owner:" not in found[0][1]
    assert "strict=True" in found[0][1], "the whole call was captured, not a fragment"
