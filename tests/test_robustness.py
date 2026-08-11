"""The named robustness cases, end to end — pixels in, verdicts out (F3).

The tests in `test_quality.py` and `test_preprocess.py` check one measurement at a time.
These check the thing an agent would actually see: a degraded photograph goes in one end
and a set of per-field verdicts comes out the other.

The chain is real rather than stubbed at the interesting step. Region readability is
computed from the actual degraded pixels, and that result is what tells the extractor
which fields it cannot see — so the test fails if the region scorer stops noticing the
glare, which is exactly the regression worth catching.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from api.models import Application, FieldName, Verdict
from api.pipeline import preprocess, quality
from api.provider.base import ImageInput
from api.provider.fake import SpecBackedProvider
from api.verify import verify
from fixtures.generator import degrade
from fixtures.generator.catalog import by_name
from fixtures.generator.layout import FIELD_BANDS
from fixtures.generator.render import render
from fixtures.generator.spec import LabelSpec

CLEAN = degrade.BASE_FIXTURE
ROBUSTNESS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "robustness"


@pytest.fixture(scope="module")
def spec() -> LabelSpec:
    return by_name(CLEAN)


@pytest.fixture(scope="module")
def clean(spec: LabelSpec) -> np.ndarray:
    return np.array(render(spec))


def run(spec: LabelSpec, image: np.ndarray):  # type: ignore[no-untyped-def]
    """Verify one degraded image, with legibility decided by the pixels themselves.

    `illegible_regions` reads the degraded image and returns the fields nobody could see.
    Handing that set to the spec-backed extractor is what makes this a test of the
    pipeline rather than of a hand-written stub: if the region scorer stops noticing the
    damage, the extractor is told the field is fine and the assertion fails.
    """
    processed = preprocess.preprocess(image)
    illegible = quality.illegible_regions(processed.image, FIELD_BANDS)
    provider = SpecBackedProvider(spec, illegible=illegible)
    application = Application.model_validate(spec.application())
    images = [ImageInput(index=0, data=b"", role="single")]
    return verify(application, images, provider), processed, illegible


def verdict_of(result, field: FieldName) -> Verdict:  # type: ignore[no-untyped-def]
    return next(f.verdict for f in result.fields if f.field is field)


def extracted_of(result, field: FieldName) -> str | None:  # type: ignore[no-untyped-def]
    return next(f.extracted for f in result.fields if f.field is field)


# --- TC-12 · glare over the warning, and only the warning ---------------------------------

@pytest.mark.tc("TC-12")
def test_glare_over_the_warning_makes_it_unreadable(spec: LabelSpec, clean: np.ndarray) -> None:
    """The headline case. Unreadable, never Missing and never Match — "we could not read
    it" and "it is not there" are different findings, and confusing them is the false
    pass this product exists to avoid."""
    result, _, _ = run(spec, degrade.glare_over_warning(clean))
    assert verdict_of(result, FieldName.GOVERNMENT_WARNING) is Verdict.UNREADABLE


@pytest.mark.tc("TC-12")
def test_the_rest_of_the_label_is_still_verified(spec: LabelSpec, clean: np.ndarray) -> None:
    """Rejecting the whole image would throw away five perfectly readable fields to
    protect the one that is not. The agent still gets their work done."""
    result, _, _ = run(spec, degrade.glare_over_warning(clean))
    assert verdict_of(result, FieldName.BRAND_NAME) is Verdict.MATCH
    assert verdict_of(result, FieldName.CLASS_TYPE) is Verdict.MATCH
    assert verdict_of(result, FieldName.ALCOHOL_CONTENT) is Verdict.MATCH


@pytest.mark.tc("TC-12")
def test_no_warning_text_is_invented_under_the_glare(
    spec: LabelSpec, clean: np.ndarray
) -> None:
    """The most dangerous possible output here is a plausible warning string. There is no
    channel for a guess and this asserts there is no value in it either."""
    result, _, _ = run(spec, degrade.glare_over_warning(clean))
    assert extracted_of(result, FieldName.GOVERNMENT_WARNING) is None


@pytest.mark.tc("TC-12")
def test_the_rationale_says_it_was_not_checked(spec: LabelSpec, clean: np.ndarray) -> None:
    result, _, _ = run(spec, degrade.glare_over_warning(clean))
    rationale = next(
        f.rationale for f in result.fields if f.field is FieldName.GOVERNMENT_WARNING
    )
    assert "could not be read" in rationale.lower()


@pytest.mark.tc("TC-12")
def test_only_the_warning_is_lost(spec: LabelSpec, clean: np.ndarray) -> None:
    _, _, illegible = run(spec, degrade.glare_over_warning(clean))
    assert illegible == {FieldName.GOVERNMENT_WARNING}


@pytest.mark.tc("TC-12")
def test_the_recommendation_is_not_ready_to_approve(
    spec: LabelSpec, clean: np.ndarray
) -> None:
    """An unread warning can never end in an approval recommendation, whatever else on
    the label checked out (WARN-6)."""
    result, _, _ = run(spec, degrade.glare_over_warning(clean))
    assert result.aggregate.recommendation.value != "ready_to_approve"


@pytest.mark.tc("TC-12")
def test_a_clean_label_is_the_control(spec: LabelSpec, clean: np.ndarray) -> None:
    """Without the glare the same chain verifies the warning. Otherwise the case above
    would pass on a pipeline that simply never reads warnings."""
    result, _, illegible = run(spec, clean)
    assert illegible == set()
    assert verdict_of(result, FieldName.GOVERNMENT_WARNING) is Verdict.MATCH


# --- the robustness set, condition by condition -------------------------------------------

def apply_and_run(spec: LabelSpec, clean: np.ndarray, condition: degrade.Condition):  # type: ignore[no-untyped-def]
    return run(spec, condition.apply(clean))


@pytest.mark.parametrize("condition", degrade.CONDITIONS, ids=lambda c: c.name)
def test_each_condition_does_what_it_says_it_does(
    spec: LabelSpec, clean: np.ndarray, condition: degrade.Condition
) -> None:
    """A robustness fixture with no stated expectation only proves the code did not crash.

    Each condition declares one of three obligations and this checks that exact one, so a
    change that turns a recoverable photo into a rejected one fails here rather than
    quietly halving the set's value.
    """
    result, processed, illegible = apply_and_run(spec, clean, condition)

    match condition.expectation:
        case "readable":
            assert illegible == set(), condition.name
            assert (
                verdict_of(result, FieldName.GOVERNMENT_WARNING) is Verdict.MATCH
            ), condition.name
        case "warning_illegible":
            assert illegible == {FieldName.GOVERNMENT_WARNING}, condition.name
            assert verdict_of(result, FieldName.BRAND_NAME) is Verdict.MATCH
        case "pregated":
            assert quality.should_skip_extraction(processed.quality_before), condition.name


@pytest.mark.tc("TC-11")
@pytest.mark.parametrize("condition", degrade.by_tc("TC-11"), ids=lambda c: c.name)
def test_an_angled_photo_is_corrected_not_rejected(
    spec: LabelSpec, clean: np.ndarray, condition: degrade.Condition
) -> None:
    """Jenny's "photographed at weird angles". The product answer is to straighten it,
    not to hand the agent back a retake request they cannot act on."""
    _, processed, _ = apply_and_run(spec, clean, condition)
    assert processed.quality_after.verdict != "hopeless"
    assert abs(processed.quality_after.skew_deg) < 2.0


# --- the fixtures on disk ---------------------------------------------------------------------

@pytest.mark.parametrize("condition", degrade.CONDITIONS, ids=lambda c: c.name)
def test_every_condition_has_a_committed_fixture(condition: degrade.Condition) -> None:
    """Committed as files so the set survives a change to the generator — a regression in
    the renderer would otherwise silently rewrite the evidence and the tests would follow
    it rather than catch it."""
    assert (ROBUSTNESS_DIR / f"{condition.name}.png").exists()


def test_the_manifest_matches_the_files_on_disk() -> None:
    """LP-123. A regeneration that changed a fixture must show up as a diff, not as a
    test result that moved for no visible reason."""
    manifest = json.loads((ROBUSTNESS_DIR / "manifest.json").read_text())
    for entry in manifest["conditions"]:
        data = (ROBUSTNESS_DIR / f"{entry['name']}.png").read_bytes()
        assert hashlib.sha256(data).hexdigest()[:16] == entry["sha256"], entry["name"]


def test_regenerating_is_byte_identical(tmp_path: Path) -> None:
    assert degrade.build(tmp_path) == degrade.build(tmp_path)


def test_the_manifest_states_the_simulation_limit() -> None:
    """The set simulates optics, not physics. Saying so in the artefact itself means the
    caveat travels with the fixtures rather than living only in a document."""
    manifest = json.loads((ROBUSTNESS_DIR / "manifest.json").read_text())
    assert "not physics" in manifest["note"]


def test_every_condition_explains_why_it_exists() -> None:
    for condition in degrade.CONDITIONS:
        assert condition.why and condition.tc and condition.description


# --- LP-201 · curved surfaces -----------------------------------------------------------------

def test_a_bottle_curve_is_not_claimed_to_be_flattened(
    spec: LabelSpec, clean: np.ndarray
) -> None:
    """Curvature is not projective, so a four-point transform cannot undo it. The report
    saying so is the deliverable — an audit trail of what did not happen is worth more
    than a correction that never worked."""
    _, processed, _ = run(spec, degrade.cylinder(clean))
    assert not processed.perspective_applied


def test_a_curved_label_still_reads(spec: LabelSpec, clean: np.ndarray) -> None:
    result, processed, illegible = run(spec, degrade.cylinder(clean))
    assert processed.quality_after.verdict != "hopeless"
    assert illegible == set()
    assert verdict_of(result, FieldName.GOVERNMENT_WARNING) is Verdict.MATCH


def test_correcting_an_angled_bottle_does_not_make_the_curve_worse(
    spec: LabelSpec, clean: np.ndarray
) -> None:
    """Nobody photographs a bottle both curved and perfectly square to the camera. The
    rectification has a boundary to find here, and the guard is that it leaves the label
    at least as readable as it found it."""
    photo = degrade.on_surface(degrade.cylinder(clean), degrees=20.0)
    _, processed, _ = run(spec, photo)
    assert processed.quality_after.blur >= processed.quality_before.blur - 0.1


def test_the_centre_of_a_curved_label_stays_sharpest(clean: np.ndarray) -> None:
    """What a cylinder actually does: the middle stays legible while the edges crowd. If
    this ever inverts, the warp is modelling something other than a bottle."""
    warped = degrade.cylinder(clean)
    width = warped.shape[1]
    centre = warped[:, int(width * 0.35) : int(width * 0.65)]
    edge = warped[:, : int(width * 0.15)]
    assert quality.blur_score(centre) > quality.blur_score(edge)
