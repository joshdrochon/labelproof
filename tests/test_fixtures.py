"""The fixture generator. Determinism is the requirement (LP-123)."""

import hashlib
import json
from pathlib import Path

import pytest

from api import canon
from fixtures.generator.catalog import CATALOG, NOT_GENERATED, by_name
from fixtures.generator.render import render
from fixtures.generator.spec import LabelSpec

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "golden" / "set.json"


def _png_bytes(spec: LabelSpec) -> bytes:
    import io
    buf = io.BytesIO()
    render(spec).save(buf, "PNG", optimize=True)
    return buf.getvalue()


# --- determinism ----------------------------------------------------------------------

def test_rendering_is_byte_identical_across_runs() -> None:
    """LP-123: two CI runs must produce identical eval output."""
    spec = by_name("tc01_old_tom_clean")
    assert hashlib.sha256(_png_bytes(spec)).digest() == hashlib.sha256(_png_bytes(spec)).digest()


def test_different_specs_render_differently() -> None:
    clean = _png_bytes(by_name("tc01_old_tom_clean"))
    titled = _png_bytes(by_name("tc03_title_case_warning"))
    assert clean != titled


def test_bold_body_changes_the_pixels() -> None:
    """If this fails, warning_body_bold is not reaching the renderer and TC-04 is fake."""
    base = by_name("tc01_old_tom_clean").with_(face="back")
    assert _png_bytes(base) != _png_bytes(base.with_(warning_body_bold=True))


def test_warning_scale_changes_the_pixels() -> None:
    base = by_name("tc01_old_tom_clean").with_(face="back")
    assert _png_bytes(base) != _png_bytes(base.with_(warning_scale=0.45))


# --- the specs say what they claim ------------------------------------------------------

def test_clean_spec_renders_the_canonical_warning_verbatim() -> None:
    assert by_name("tc01_old_tom_clean").rendered_warning() == canon.CANONICAL_WARNING


@pytest.mark.tc("TC-03")
def test_title_case_spec_actually_produces_title_case() -> None:
    rendered = by_name("tc03_title_case_warning").rendered_warning()
    assert rendered.startswith("Government Warning:")
    assert not rendered.startswith("GOVERNMENT WARNING:")


@pytest.mark.tc("TC-03")
def test_title_case_spec_differs_from_canonical_only_in_the_header() -> None:
    """The fixture must isolate the defect — body text identical, header wrong."""
    rendered = by_name("tc03_title_case_warning").rendered_warning()
    assert rendered.split(": ", 1)[1] == canon.CANONICAL_WARNING.split(": ", 1)[1]


@pytest.mark.tc("TC-05")
def test_reworded_spec_differs_from_canonical() -> None:
    assert by_name("tc05_reworded_warning").rendered_warning() != canon.CANONICAL_WARNING


@pytest.mark.tc("TC-07")
def test_missing_warning_spec_omits_it() -> None:
    assert not by_name("tc07_missing_warning").include_warning


@pytest.mark.tc("TC-09")
def test_proof_inconsistent_spec_is_actually_inconsistent() -> None:
    from api.rules import abv
    parsed = abv.parse(by_name("tc09_proof_inconsistent").alcohol_text)
    assert abv.check_internal_consistency(parsed)


@pytest.mark.tc("TC-08")
def test_abv_mismatch_spec_is_internally_consistent() -> None:
    """TC-08 must isolate the comparison — 80 proof is correct for 40%."""
    from api.rules import abv
    parsed = abv.parse(by_name("tc08_abv_mismatch").alcohol_text)
    assert not abv.check_internal_consistency(parsed)


@pytest.mark.tc("TC-10")
def test_non_standard_fill_spec_uses_an_unauthorized_size() -> None:
    from api.models import Commodity
    from api.rules import fills
    parsed = fills.parse(by_name("tc10_non_standard_fill").net_contents)
    assert not fills.is_authorized(parsed.ml, Commodity.SPIRITS)


# --- catalog integrity ------------------------------------------------------------------

def test_fixture_names_are_unique() -> None:
    names = [s.name for s in CATALOG]
    assert len(names) == len(set(names))


def test_every_generatable_tc_has_a_fixture() -> None:
    """A TC with no fixture and no entry in NOT_GENERATED is a silent hole."""
    all_tcs = {f"TC-{n:02d}" for n in range(1, 23)}
    covered = {s.name.split("_")[0].upper().replace("TC", "TC-") for s in CATALOG}
    accounted = covered | set(NOT_GENERATED)
    assert all_tcs - accounted == set(), f"unaccounted test cases: {sorted(all_tcs - accounted)}"


def test_every_expected_verdict_is_a_real_verdict() -> None:
    from api.models import Verdict
    valid = {v.value for v in Verdict}
    for spec in CATALOG:
        for field, verdict in spec.expect.items():
            assert verdict in valid, f"{spec.name}: {field} -> unknown verdict {verdict!r}"


def test_every_expected_field_is_a_real_field() -> None:
    from api.models import FieldName
    valid = {f.value for f in FieldName}
    for spec in CATALOG:
        for field in spec.expect:
            assert field in valid, f"{spec.name}: unknown field {field!r}"


def test_every_fixture_explains_why_it_exists() -> None:
    for spec in CATALOG:
        assert spec.notes.strip(), f"{spec.name} has no notes"


# --- golden manifest --------------------------------------------------------------------

def test_golden_manifest_exists_and_parses() -> None:
    assert GOLDEN.exists(), "run: python -m fixtures.generator.build"
    json.loads(GOLDEN.read_text())


def test_golden_manifest_covers_every_fixture() -> None:
    data = json.loads(GOLDEN.read_text())
    assert {e["name"] for e in data["fixtures"]} == {s.name for s in CATALOG}


def test_golden_manifest_records_image_hashes() -> None:
    data = json.loads(GOLDEN.read_text())
    for entry in data["fixtures"]:
        assert entry["sha256"], f"{entry['name']} has no hashes"


def test_golden_manifest_declares_its_tier() -> None:
    """Tier A gates CI; Tier B never does. Conflating them would hide the real number."""
    assert json.loads(GOLDEN.read_text())["tier"] == "A"
