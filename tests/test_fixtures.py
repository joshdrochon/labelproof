"""The fixture generator. Determinism is the requirement (LP-123).

Byte-identity is guaranteed between two runs *on one machine*. It is deliberately not
claimed across machines: `render.py` picks the first available regular/bold family, so
macOS (Arial) and Linux (DejaVu) rasterise different pixels from the same spec, and
`golden/set.json` records which family produced its hashes so that difference reads as
what it is. The eval report is unaffected — it is scored from the specs through the
spec-backed provider, never from the pixels.
"""

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from api import canon
from fixtures.generator import build as build_module
from fixtures.generator.catalog import CATALOG, NOT_GENERATED, by_name
from fixtures.generator.render import render
from fixtures.generator.spec import LabelSpec

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "golden" / "set.json"


def _png_bytes(spec: LabelSpec) -> bytes:
    buf = io.BytesIO()
    render(spec).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _without_hashes(body: dict[str, Any]) -> dict[str, Any]:
    """The parts of the manifest that must match on every machine.

    Image hashes and the font name are machine-dependent by design; everything else —
    names, applications, expectations, notes — is a property of the catalog alone.
    """
    return {
        "tier": body["tier"],
        "not_generated": body["not_generated"],
        "fixtures": [
            {k: v for k, v in entry.items() if k != "sha256"} for entry in body["fixtures"]
        ],
    }


# --- determinism ----------------------------------------------------------------------

def test_rendering_is_byte_identical_across_runs() -> None:
    """LP-123: two CI runs must produce identical eval output."""
    spec = by_name("tc01_old_tom_clean")
    assert hashlib.sha256(_png_bytes(spec)).digest() == hashlib.sha256(_png_bytes(spec)).digest()


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_every_fixture_renders_byte_identically_twice(spec: LabelSpec) -> None:
    """One stable fixture proves nothing about the other fourteen."""
    faces = ["front", "back"] if spec.face != "single" else ["single"]
    for face in faces:
        variant = spec.with_(face=face)
        assert _png_bytes(variant) == _png_bytes(variant), f"{spec.name} [{face}]"


def test_two_full_regenerations_are_byte_identical(tmp_path: Path) -> None:
    """The whole pipeline, not just the renderer: images and manifest alike."""
    first, second = tmp_path / "first", tmp_path / "second"
    body_a = build_module.build(labels_dir=first, golden_path=first / "set.json")
    body_b = build_module.build(labels_dir=second, golden_path=second / "set.json")

    assert build_module.serialise(body_a) == build_module.serialise(body_b)
    assert (first / "set.json").read_bytes() == (second / "set.json").read_bytes()

    names = sorted(p.name for p in first.glob("*.png"))
    assert names == sorted(p.name for p in second.glob("*.png"))
    assert names, "the build produced no images at all"
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_a_regeneration_does_not_drift_from_the_committed_manifest(tmp_path: Path) -> None:
    """A stale manifest would silently decouple the golden set from the catalog."""
    fresh = build_module.build(labels_dir=tmp_path, golden_path=tmp_path / "set.json")
    committed = json.loads(GOLDEN.read_text())
    assert _without_hashes(fresh) == _without_hashes(committed)


def test_the_manifest_records_the_font_that_produced_its_hashes() -> None:
    """So a cross-machine hash mismatch reads as a font change, not as flakiness."""
    body = json.loads(GOLDEN.read_text())
    assert body["rendered_with"]["font_family"]


def test_the_manifest_serialisation_is_stable(tmp_path: Path) -> None:
    body = build_module.build(labels_dir=tmp_path, golden_path=tmp_path / "set.json")
    assert build_module.serialise(body) == build_module.serialise(body)
    assert build_module.serialise(body).endswith("\n")


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
