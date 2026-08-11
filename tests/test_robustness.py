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
import re
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from api.models import Application, BoundingBox, FieldName, Verdict
from api.pipeline import deskew, limitations, preprocess, quality
from api.provider.base import ImageInput
from api.provider.fake import SpecBackedProvider
from api.rules import thresholds as T
from api.verify import verify
from fixtures.generator import degrade
from fixtures.generator.catalog import by_name
from fixtures.generator.layout import FIELD_BANDS
from fixtures.generator.render import render
from fixtures.generator.spec import LabelSpec
from scripts import (
    calibrate_quality,
    compression_sweep,
    crop_before_send,
    robustness_eval,
)

CLEAN = degrade.BASE_FIXTURE
ROBUSTNESS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "robustness"


@pytest.fixture(scope="module")
def spec() -> LabelSpec:
    return by_name(CLEAN)


@pytest.fixture(scope="module")
def clean(spec: LabelSpec) -> np.ndarray:
    return np.array(render(spec))


def bands(
    image: np.ndarray,
    processed: preprocess.Preprocessed,
    condition: degrade.Condition | None = None,
) -> dict[FieldName, BoundingBox]:
    """Field bands carried into the frame the region scorer will read.

    The same two-hop mapping the harness uses, and for the same reason: bands measured on
    the undegraded render do not describe a rotated or rectified image, and reading the
    wrong rows is a way to be green about nothing.
    """
    if condition is not None:
        return robustness_eval.bands_for(condition, image.shape, processed)
    return {name: processed.map_box(band) for name, band in FIELD_BANDS.items()}


def run(  # type: ignore[no-untyped-def]
    spec: LabelSpec,
    image: np.ndarray,
    condition: degrade.Condition | None = None,
):
    """Verify one degraded image, with legibility decided by the pixels themselves.

    `illegible_regions` reads the degraded image and returns the fields nobody could see.
    Handing that set to the spec-backed extractor is what makes this a test of the
    pipeline rather than of a hand-written stub: if the region scorer stops noticing the
    damage, the extractor is told the field is fine and the assertion fails.
    """
    processed = preprocess.preprocess(image)
    illegible = quality.illegible_regions(
        processed.image, bands(image, processed, condition)
    )
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
    return run(spec, condition.apply(clean), condition)


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


# --- LP-194 · the fabrication sweep (TC-14) ------------------------------------------------------
#
# The first version of this sweep could not fail, and it is worth saying exactly how,
# because it looked thorough. It ran a spec-backed extractor that returns the spec's own
# fields, then asserted every extracted value appeared in a string built by joining those
# same fields. `spec.brand_name in " ".join(spec fields)` is true for reasons that have
# nothing to do with the pipeline, and parametrising it over nineteen conditions multiplied
# a tautology by nineteen.
#
# What is actually checkable here, without an OCR engine, is narrower and real: values pass
# through *our* layers unaltered, and a field nobody could see comes back with no value at
# all. So the extractor emits per-field sentinels that appear nowhere else, and anything in
# the output that is not a sentinel was introduced between the provider and the agent.
#
# Whether the *model* fabricates is a different question that needs a model to answer. The
# negative control below is what proves this assertion has teeth.


SENTINELS: dict[FieldName, str] = {
    field: f"SENTINEL-{field.value.upper()}-4f27b1"
    for field in (
        FieldName.BRAND_NAME,
        FieldName.CLASS_TYPE,
        FieldName.ALCOHOL_CONTENT,
        FieldName.NET_CONTENTS,
        FieldName.PRODUCER,
        FieldName.GOVERNMENT_WARNING,
    )
}


class SentinelProvider:
    """Returns tagged values, so anything untagged in the output was invented downstream.

    Deliberately not the spec's real values. If the extractor returned "OLD TOM DISTILLERY"
    and a comparator substituted the *application's* copy of that same string into the
    extracted column, nothing would look wrong — the strings match. With sentinels that
    substitution is visible immediately.
    """

    name = "fake:sentinel"

    def __init__(self, illegible: set[FieldName] | None = None) -> None:
        self.illegible = illegible or set()

    def extract(self, request):  # type: ignore[no-untyped-def]
        from api.models import ExtractedField, Extraction
        from api.provider.base import ExtractionResponse, ProviderUsage

        fields = {}
        for name, value in SENTINELS.items():
            if name in self.illegible:
                fields[name] = ExtractedField(value=None, confidence=0.0, legible=False)
            else:
                fields[name] = ExtractedField(value=value, confidence=0.95, legible=True)

        warning = None if FieldName.GOVERNMENT_WARNING in self.illegible else SENTINELS[
            FieldName.GOVERNMENT_WARNING
        ]
        return ExtractionResponse(
            extractions=[
                Extraction(
                    image_index=image.index,
                    is_label=True,
                    fields=fields,
                    warning_text=warning,
                )
                for image in request.images
            ],
            usage=ProviderUsage(model="fake:sentinel"),
        )


class FabricatingProvider(SentinelProvider):
    """Answers every field, including the ones it was told it could not see.

    The negative control. If the sweep below stays green against this, the sweep is not
    checking anything.
    """

    name = "fake:fabricating"

    def extract(self, request):  # type: ignore[no-untyped-def]
        self.illegible = set()
        response = super().extract(request)
        for extraction in response.extractions:
            for name in extraction.fields:
                extraction.fields[name].value = "PLAUSIBLE INVENTED VALUE"
            extraction.warning_text = "GOVERNMENT WARNING: invented but well formed."
        return response


def unsourced_values(result, allowed: set[str]) -> list[str]:  # type: ignore[no-untyped-def]
    """Extracted values that did not come from the extractor."""
    return [
        f.extracted
        for f in result.fields
        if f.extracted is not None and f.extracted not in allowed
    ]


def run_with(spec: LabelSpec, image: np.ndarray, provider, condition=None):  # type: ignore[no-untyped-def]
    processed = preprocess.preprocess(image)
    illegible = quality.illegible_regions(
        processed.image, bands(image, processed, condition)
    )
    provider.illegible = illegible
    return (
        verify(
            Application.model_validate(spec.application()),
            [ImageInput(index=0, data=b"", role="single")],
            provider,
        ),
        illegible,
    )


@pytest.mark.tc("TC-14")
@pytest.mark.parametrize("condition", degrade.CONDITIONS, ids=lambda c: c.name)
def test_no_condition_introduces_a_value_the_extractor_did_not_return(
    spec: LabelSpec, clean: np.ndarray, condition: degrade.Condition
) -> None:
    """Across every degradation, every value the agent sees traces back to the extractor.

    This catches our own layers inventing: a comparator writing the application's expected
    value into the extracted column, a merge filling a gap from another image without
    saying so, an aggregate substituting a default. All of those look like a correct
    answer on screen.
    """
    result, _ = run_with(spec, condition.apply(clean), SentinelProvider(), condition)
    assert unsourced_values(result, set(SENTINELS.values())) == [], condition.name


@pytest.mark.tc("TC-14")
def test_the_sweep_catches_a_fabricated_value(spec: LabelSpec, clean: np.ndarray) -> None:
    """The negative control, and the reason the assertion above is worth running.

    An extractor that answers a field it was told it could not see produces values the
    check does not recognise. If this ever goes green, the sweep has stopped checking.
    """
    result, _ = run_with(spec, degrade.glare_over_warning(clean), FabricatingProvider())
    assert unsourced_values(result, set(SENTINELS.values())) != []


@pytest.mark.tc("TC-14")
@pytest.mark.parametrize("condition", degrade.CONDITIONS, ids=lambda c: c.name)
def test_an_illegible_field_is_never_given_a_value(
    spec: LabelSpec, clean: np.ndarray, condition: degrade.Condition
) -> None:
    """Unreadable and a value are mutually exclusive. There is no channel for a guess and
    this asserts nothing found its way into one anyway."""
    result, _, _ = apply_and_run(spec, clean, condition)
    for field in result.fields:
        if field.verdict is Verdict.UNREADABLE:
            assert field.extracted is None, f"{condition.name}: {field.field.value}"


@pytest.mark.tc("TC-12")
def test_a_region_the_scorer_condemned_comes_back_empty(
    spec: LabelSpec, clean: np.ndarray
) -> None:
    """Ties the region scorer to the output. The glare makes the warning region illegible,
    the extractor is told so, and the field arrives with no value — a chain where every
    link is exercised rather than assumed."""
    result, illegible = run_with(
        spec, degrade.glare_over_warning(clean), SentinelProvider()
    )
    assert FieldName.GOVERNMENT_WARNING in illegible
    assert extracted_of(result, FieldName.GOVERNMENT_WARNING) is None
    assert extracted_of(result, FieldName.BRAND_NAME) == SENTINELS[FieldName.BRAND_NAME]


PREGATED = [c for c in degrade.CONDITIONS if c.expectation == "pregated"]


@pytest.mark.tc("TC-14")
@pytest.mark.parametrize("condition", PREGATED, ids=lambda c: c.name)
def test_a_hopeless_image_costs_nothing_and_claims_nothing(
    spec: LabelSpec, clean: np.ndarray, condition: degrade.Condition
) -> None:
    """LP-321. The pre-gate's outcome is "we did not verify this", which is the one thing
    a false pass can never be — and it spends zero tokens saying it."""
    _, processed, _ = apply_and_run(spec, clean, condition)
    assert quality.should_skip_extraction(processed.quality_before)
    assert processed.quality_before.reason


@pytest.mark.tc("TC-14")
@pytest.mark.parametrize("condition", PREGATED, ids=lambda c: c.name)
def test_the_retake_reason_names_a_fixable_problem(
    spec: LabelSpec, clean: np.ndarray, condition: degrade.Condition
) -> None:
    """An agent has to know what to ask for. "Quality score 0.14" is not an instruction."""
    _, processed, _ = apply_and_run(spec, clean, condition)
    reason = (processed.quality_before.reason or "").lower()
    assert any(word in reason for word in ("blurry", "dark", "glare", "flash"))


@pytest.mark.tc("TC-14")
def test_the_sweep_covers_every_kind_of_expectation() -> None:
    """Guards the sweep itself. A condition added without a stated expectation would be
    swept but assert nothing, and the suite would look broader than it is."""
    assert len(degrade.CONDITIONS) >= 19
    assert {c.expectation for c in degrade.CONDITIONS} == {
        "readable",
        "warning_illegible",
        "pregated",
    }


# --- LP-199 · the per-condition harness -----------------------------------------------------
#
# Each of these runs the whole 15-condition set, so they share one computed report. The
# threshold sweeps below are an order of magnitude more expensive again — 40-odd full runs
# — and share theirs for the same reason. CI has a ten-minute budget (LP-247) and a
# harness that eats it is a harness people start skipping.


@pytest.fixture(scope="module")
def report() -> robustness_eval.Report:
    return robustness_eval.evaluate()


def test_the_harness_reports_every_condition(report: robustness_eval.Report) -> None:
    """An aggregate accuracy number hides whether the missing percent is spread evenly or
    is entirely "every photograph taken at an angle"."""
    assert {o.condition for o in report.outcomes} == {c.name for c in degrade.CONDITIONS}


def test_the_whole_robustness_set_currently_passes(report: robustness_eval.Report) -> None:
    """Also the claim thresholds.py records: the shipped values produce no false passes."""
    assert report.false_passes == []
    assert report.passed


def test_false_passes_and_false_flags_are_not_the_same_number() -> None:
    """The distinction the report exists to protect. A field verified from pixels nobody
    could read is silent and dangerous; a usable photo wrongly rejected is a cost."""
    slipped = robustness_eval.Outcome(
        condition="x", tc="TC-14", expectation="pregated", pregated=False
    )
    assert slipped.false_pass and not slipped.false_flag

    over_eager = robustness_eval.Outcome(
        condition="y", tc="TC-11", expectation="readable", pregated=True
    )
    assert over_eager.false_flag and not over_eager.false_pass


def test_a_warning_read_through_glare_counts_as_a_false_pass() -> None:
    outcome = robustness_eval.Outcome(
        condition="z", tc="TC-12", expectation="warning_illegible", pregated=False
    )
    assert outcome.false_pass


def test_a_report_with_false_passes_does_not_pass() -> None:
    """False passes fail the run on their own, regardless of anything else in it."""
    broken = robustness_eval.Report(tier="A")
    broken.outcomes.append(
        robustness_eval.Outcome(
            condition="x", tc="TC-14", expectation="pregated", pregated=False
        )
    )
    assert not broken.passed


def test_false_flags_alone_do_not_fail_the_run() -> None:
    noisy = robustness_eval.Report(tier="A")
    noisy.outcomes.append(
        robustness_eval.Outcome(
            condition="y", tc="TC-11", expectation="readable", pregated=True
        )
    )
    assert noisy.passed


def test_the_condition_report_states_the_regression_rule(
    report: robustness_eval.Report,
) -> None:
    """Printed every run so the trade cannot be made by accident."""
    text = robustness_eval.render_table(report)
    assert "reduces flags by increasing false passes is a regression" in text
    assert "PASS" in text


def test_the_condition_report_serializes(report: robustness_eval.Report) -> None:
    payload = robustness_eval.as_dict(report)
    assert payload["false_passes"] == 0
    assert len(payload["conditions"]) == len(degrade.CONDITIONS)


# --- Tier B ------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def photo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A stand-in Tier B directory. Real photographs replace it; the path does not care."""
    from PIL import Image

    directory = tmp_path_factory.mktemp("photos")
    Image.open(ROBUSTNESS_DIR / "tc13_dim.png").save(directory / "bottle.png")
    Image.open(ROBUSTNESS_DIR / "tc14_blur_hopeless.png").save(directory / "shaky.png")
    return directory


def test_tier_b_without_ground_truth_claims_no_accuracy(photo_dir: Path) -> None:
    """A number computed against ground truth nobody wrote down is not a number."""
    tier_b = robustness_eval.evaluate_photos(photo_dir)
    assert tier_b.tier == "B"
    assert {o.expectation for o in tier_b.outcomes} == {"unstated"}
    assert tier_b.false_passes == []


def test_tier_b_reads_expectations_when_they_exist(photo_dir: Path, tmp_path: Path) -> None:
    from PIL import Image

    Image.open(ROBUSTNESS_DIR / "tc14_blur_hopeless.png").save(tmp_path / "shaky.png")
    (tmp_path / "expectations.json").write_text(json.dumps({"shaky.png": "pregated"}))

    tier_b = robustness_eval.evaluate_photos(tmp_path)
    assert tier_b.outcomes[0].pregated
    assert tier_b.passed


def test_tier_b_is_never_averaged_into_tier_a(photo_dir: Path) -> None:
    """BUILD.md §5. A set generated by our own renderer only proves the pipeline can read
    our own renderer; blending the two would hide the number that matters."""
    text = robustness_eval.render_table(robustness_eval.evaluate_photos(photo_dir))
    assert "never averaged with Tier A" in text


# --- LP-200 · the calibration harness -------------------------------------------------------

# Every sweep level re-runs all nineteen conditions, so a full seven-level grid is
# half a minute of CI for logic that three levels exercise just as well. The grids below
# keep the shape that matters — a level that produces a false pass, the shipped value, and
# a level that over-flags — and the script keeps the full range for when someone is
# actually choosing a threshold (LP-247).
def _narrow(name: str, values: tuple[float, ...]) -> calibrate_quality.Knob:
    original = calibrate_quality.SWEEPS[name]
    return calibrate_quality.Knob(
        values, stricter=original.stricter, effect=original.effect
    )


@pytest.fixture(scope="module")
def blur_sweep() -> calibrate_quality.Sweep:
    return calibrate_quality.sweep_one(
        "BLUR_HOPELESS_VARIANCE", _narrow("BLUR_HOPELESS_VARIANCE", (30, 60, 110, 140))
    )


@pytest.fixture(scope="module")
def glare_sweep() -> calibrate_quality.Sweep:
    return calibrate_quality.sweep_one(
        "GLARE_SATURATION_FRACTION",
        _narrow("GLARE_SATURATION_FRACTION", (0.05, 0.25, 0.5)),
    )


def test_the_sweep_moves_the_real_constant_not_a_copy() -> None:
    """A sweep measuring a copy of the thresholds would be measuring something the
    pipeline does not use."""
    with calibrate_quality.threshold("HOPELESS", 0.99):
        assert T.HOPELESS == 0.99
    assert T.HOPELESS != 0.99


def test_a_sweep_puts_the_threshold_back(blur_sweep: calibrate_quality.Sweep) -> None:
    """A sweep that leaked its last value into the process would silently retune the
    pipeline for every test that ran after it."""
    assert blur_sweep.current == T.BLUR_HOPELESS_VARIANCE


def test_loosening_the_gate_shows_up_as_false_passes(
    blur_sweep: calibrate_quality.Sweep,
) -> None:
    """The whole point. Dropping the blur floor lets images through that nobody can read,
    and the table has to say so rather than just showing a lower flag count."""
    loosest = blur_sweep.levels[0]
    assert loosest.false_passes > 0
    assert loosest.regression
    assert loosest.false_flags == 0  # cheaper on flags, and worse — the trap


def test_a_level_that_gates_nothing_is_outside_the_clean_band(
    blur_sweep: calibrate_quality.Sweep,
) -> None:
    """A level with zero flags because it gates nothing is the failure this exists to
    prevent, so it must not be reported as clean."""
    band = blur_sweep.safe_band
    assert band is not None
    assert band[0] > blur_sweep.levels[0].value


def test_the_shipped_value_sits_inside_its_clean_band(
    blur_sweep: calibrate_quality.Sweep, glare_sweep: calibrate_quality.Sweep
) -> None:
    for sweep in (blur_sweep, glare_sweep):
        band = sweep.safe_band
        assert band is not None and band[0] <= sweep.current <= band[1], sweep.name


def test_the_report_names_the_margin_to_the_nearest_false_pass(
    glare_sweep: calibrate_quality.Sweep,
) -> None:
    """A threshold one step from a false pass is lucky, not calibrated, and real optics
    will spend that luck."""
    assert glare_sweep.margin >= 1
    assert "margin" in glare_sweep.verdict or "step" in glare_sweep.verdict


def test_tightening_too_far_shows_up_as_false_flags(
    glare_sweep: calibrate_quality.Sweep,
) -> None:
    """The other end of the trade. Over-tight and readable labels get rejected."""
    assert glare_sweep.levels[0].false_flags > 0


def test_every_swept_threshold_exists_and_declares_its_direction() -> None:
    """Without a direction the table shows a number moving and not what moving it costs."""
    for name, knob in calibrate_quality.SWEEPS.items():
        assert hasattr(T, name), name
        assert knob.stricter in ("higher", "lower"), name
        assert knob.effect


def test_the_calibration_report_states_the_regression_rule(
    blur_sweep: calibrate_quality.Sweep,
) -> None:
    text = calibrate_quality.render([blur_sweep])
    assert "reduces flags by letting a bad label through is a" in text
    assert "Clean band" in text


def test_the_calibration_report_serializes(blur_sweep: calibrate_quality.Sweep) -> None:
    payload = calibrate_quality.as_dict([blur_sweep])
    assert payload["sweeps"][0]["threshold"] == "BLUR_HOPELESS_VARIANCE"
    assert payload["sweeps"][0]["clean_band"]


def test_calibrating_against_photos_uses_the_photo_set(photo_dir: Path) -> None:
    """The Tier B path — the one that decides these values once real photographs land."""
    knob = _narrow("HOPELESS", (0.15, 0.2, 0.25))
    sweep = calibrate_quality.sweep_one("HOPELESS", knob, photos=photo_dir)
    assert sweep.tier == "B"
    assert len(sweep.levels) == len(knob.values)


def test_tier_b_calibration_says_it_is_the_honest_number(photo_dir: Path) -> None:
    with mock.patch.dict(
        calibrate_quality.SWEEPS, {"HOPELESS": _narrow("HOPELESS", (0.2,))}
    ):
        text = calibrate_quality.render(
            calibrate_quality.sweep_all(["HOPELESS"], photos=photo_dir)
        )
    assert "never averaged with Tier A" in text


# --- LP-322 · client encode quality --------------------------------------------------------

@pytest.fixture(scope="module")
def compression() -> compression_sweep.Sweep:
    """WebP only, and the four levels these assertions actually read.

    The full sweep is six levels across two formats over fifteen conditions, which is
    thirty-odd seconds — worth paying when someone is deciding the encode setting, not
    worth paying on every CI run (LP-247). The computation per level is identical either
    way, so what is asserted here is what the full run reports.
    """
    with mock.patch.object(compression_sweep, "QUALITIES", (100, 95, 90, 85)), \
         mock.patch.object(compression_sweep, "FORMATS", ("WEBP",)):
        return compression_sweep.sweep()


def test_the_sweep_covers_the_qualities_the_ticket_names() -> None:
    """q95/q85/q75, plus the shipped setting and a lossless control. Without the control
    there is nothing for a fidelity number to be measured against."""
    for level in (95, 85, 75, 100):
        assert level in compression_sweep.QUALITIES


def test_fidelity_is_measured_against_the_original_not_the_compressed_image(
    clean: np.ndarray,
) -> None:
    """The error the previous proxy made. Scoring the compressed image alone with a
    gradient measure reads encoder ringing as detail, which rated JPEG q60 above its own
    uncompressed source and produced a 'JPEG is gentler' conclusion that was an artefact.
    SSIM against the original cannot do that: invented edges count against it."""
    crushed, _ = compression_sweep.encode_roundtrip(clean, "JPEG", 20)
    assert compression_sweep.ssim(clean, crushed) < 1.0
    assert compression_sweep.ssim(clean, clean) == pytest.approx(1.0, abs=1e-6)


def test_harder_compression_costs_the_warning_more(
    compression: compression_sweep.Sweep,
) -> None:
    """A sweep whose numbers did not move with the setting would be measuring nothing."""
    webp = {level.quality: level for level in compression.for_format("WEBP")}
    assert webp[85].worst_fidelity < webp[100].worst_fidelity


def test_the_shipped_client_setting_matches_the_sweep(
    compression: compression_sweep.Sweep,
) -> None:
    """The guard that survives a merge. Nothing else ties web/src/api.ts to the evidence
    it cites, so a resolution that reverts the constant would leave the suite green while
    the shipped client quietly went back to damaging the warning."""
    source = (Path(__file__).resolve().parents[1] / "web" / "src" / "api.ts").read_text()
    match = re.search(r"const WEBP_Q = ([0-9.]+);", source)
    assert match, "WEBP_Q is not where this test expects it"

    shipped = round(float(match.group(1)) * 100)
    recommended = compression.recommended("WEBP")
    assert recommended is not None
    assert shipped == recommended.quality, (
        f"web/src/api.ts ships WebP q{shipped} but the sweep recommends "
        f"q{recommended.quality}"
    )


def test_the_previous_setting_was_measurably_worse(
    compression: compression_sweep.Sweep,
) -> None:
    """0.90 shipped before anyone measured it. Recording why it moved matters more than
    the move."""
    previous = next(
        level for level in compression.for_format("WEBP") if level.quality == 90
    )
    assert previous.lossy_for_the_warning


def test_a_level_is_judged_on_its_worst_condition_not_its_average() -> None:
    """An encoder kind to thirteen fixtures and ruinous on the fourteenth has destroyed a
    government warning, and an average hides exactly that."""
    level = compression_sweep.Level(
        fmt="WEBP",
        quality=50,
        median_bytes=1,
        warning_fidelity=0.999,
        worst_fidelity=0.5,
        false_passes=0,
        false_flags=0,
    )
    assert level.lossy_for_the_warning and not level.safe


def test_a_level_with_a_false_pass_is_never_safe() -> None:
    level = compression_sweep.Level(
        fmt="WEBP",
        quality=10,
        median_bytes=1,
        warning_fidelity=1.0,
        worst_fidelity=1.0,
        false_passes=1,
        false_flags=0,
    )
    assert not level.safe


def test_the_recommendation_survives_a_non_monotone_column() -> None:
    """Real encoders are not monotone. The previous version assumed the safe levels ran
    contiguously from the top and used a bare next(), which raised StopIteration out of
    main the moment they did not."""
    sweep = compression_sweep.Sweep()
    for quality_level, worst in ((100, 0.999), (95, 0.90), (90, 0.999), (85, 0.5)):
        sweep.levels.append(
            compression_sweep.Level(
                fmt="WEBP",
                quality=quality_level,
                median_bytes=1,
                warning_fidelity=worst,
                worst_fidelity=worst,
                false_passes=0,
                false_flags=0,
            )
        )
    assert sweep.recommended("WEBP") is not None


def test_nothing_safe_returns_none_rather_than_raising() -> None:
    sweep = compression_sweep.Sweep()
    sweep.levels.append(
        compression_sweep.Level(
            fmt="WEBP",
            quality=90,
            median_bytes=1,
            warning_fidelity=0.1,
            worst_fidelity=0.1,
            false_passes=0,
            false_flags=0,
        )
    )
    assert sweep.recommended("WEBP") is None


def test_formats_are_compared_at_equal_bytes(
    compression: compression_sweep.Sweep,
) -> None:
    """WebP q90 and JPEG q90 are different scales sharing a name. The only comparison that
    answers "which format should we ship" is at the same cost."""
    text = compression_sweep.render(compression)
    assert "equal budget" in text
    assert "different scales sharing a name" in text


def test_the_report_does_not_call_the_proxy_accuracy(
    compression: compression_sweep.Sweep,
) -> None:
    """A fidelity proxy named as one is useful. Reported as accuracy it is a lie with a
    number attached — there is no model in this harness."""
    text = compression_sweep.render(compression)
    assert "not character accuracy" in text or "not character accuracy" in (
        compression_sweep.__doc__ or ""
    )
    assert "fidelity" in text.lower()


def test_the_sweep_runs_as_a_command() -> None:
    with mock.patch.object(compression_sweep, "QUALITIES", (100, 85)), \
         mock.patch.object(compression_sweep, "FORMATS", ("WEBP",)):
        assert compression_sweep.main(["--json"]) == 0


# --- LP-326 · crop before send, measured rather than assumed --------------------------------

@pytest.fixture(scope="module")
def crop_report() -> crop_before_send.Report:
    return crop_before_send.measure()


def test_the_measurement_covers_the_whole_robustness_set(
    crop_report: crop_before_send.Report,
) -> None:
    assert len(crop_report.measurements) == len(degrade.CONDITIONS)


def test_no_crop_this_detector_proposes_would_cut_text(
    crop_report: crop_before_send.Report,
) -> None:
    """A crop that takes the bottom off a back label takes the government warning with it,
    and the pipeline then reports Missing on a compliant label — a false finding this
    system manufactured, indistinguishable from a real one."""
    assert crop_report.unsafe == []


def test_the_feature_does_not_ship_on_this_evidence(
    crop_report: crop_before_send.Report,
) -> None:
    """The ticket's condition, with the reasoning corrected.

    The first version of this failed the feature on a detection rate of 27%, which was
    measuring the fixture set rather than the detector: most of these are labels rendered
    edge to edge, with no boundary to find by construction, so the 80% gate was one this
    set could never pass however good the detector was. Where a boundary does exist it is
    found every time and cuts nothing.

    It still does not ship, for the reason that actually holds: four fixtures is no
    evidence either way, and the cost of being wrong is a government warning removed by
    our own preprocessing and reported Missing on a compliant label.
    """
    assert not crop_report.ships
    assert len(crop_report.testable) < 8, "sample is still too small to conclude from"
    assert crop_report.detection_rate_where_testable == 1.0


def test_the_detection_rate_is_not_computed_over_undetectable_fixtures(
    crop_report: crop_before_send.Report,
) -> None:
    """A label rendered edge to edge has no boundary, so a miss on it says nothing. If
    those were counted, the rate would be a fact about the fixtures."""
    assert len(crop_report.structurally_undetectable) > len(crop_report.testable)
    assert all(not m.detected for m in crop_report.structurally_undetectable)


def test_the_saving_is_real_where_detection_fires(
    crop_report: crop_before_send.Report,
) -> None:
    """Not shipping it is a judgment about reliability, not a claim that it would not have
    helped. Recording the size of what is being declined keeps the decision reviewable."""
    assert crop_report.median_saving > 0.3


def test_an_undetected_boundary_counts_as_safe() -> None:
    """Not cropping is always safe. Only an attempted crop can cut anything."""
    nothing = crop_before_send.Measurement(condition="x", tc="TC-11", detected=False)
    assert nothing.safe and nothing.saving == 0.0


def test_a_crop_that_loses_detail_is_unsafe() -> None:
    bad = crop_before_send.Measurement(
        condition="y", tc="TC-11", detected=True, detail_lost=0.4,
        pixels_before=100, pixels_after=50,
    )
    assert not bad.safe


def test_no_measurement_on_this_set_can_make_it_ship() -> None:
    """The decision does not rest on a number this set can produce, and encoding that is
    the point — a later change that happened to raise the rate must not flip it silently."""
    report = crop_before_send.Report()
    report.measurements = [
        crop_before_send.Measurement(
            condition=f"c{i}", tc="TC-11", detected=True, detail_lost=0.0,
            pixels_before=100, pixels_after=50,
        )
        for i in range(50)
    ]
    assert report.unsafe == []
    assert report.detection_rate_where_testable == 1.0
    assert not report.ships


def test_the_report_says_why_not_just_no(crop_report: crop_before_send.Report) -> None:
    text = crop_before_send.render(crop_report)
    assert "DOES NOT SHIP" in text
    assert "no boundary by construction" in text
    assert "not because the detector looks weak" in text.lower()


def test_the_report_flags_that_tier_b_could_change_the_answer(
    crop_report: crop_before_send.Report,
) -> None:
    """Most of this set is rendered edge to edge and genuinely has no boundary. Real
    photographs mostly do. Reporting 27% without that caveat would read as "the detector
    is weak", which is not what was measured."""
    assert "Tier B is what would change this" in crop_before_send.render(crop_report)


def test_the_measurement_runs_as_a_command() -> None:
    assert crop_before_send.main(["--json"]) == 0


def test_the_crop_bar_is_the_same_one_deskew_uses() -> None:
    """One number. A crop the preprocessing pass would refuse must not be a crop the
    upload path accepts."""
    assert crop_before_send.MAX_DETAIL_LOST == deskew._MAX_INK_OUTSIDE_QUAD


# --- LP-202 · the limitations, kept honest by a test ---------------------------------------

def test_the_committed_document_matches_the_code(report: robustness_eval.Report) -> None:
    """A hand-written limitations list is a limitations list as of the day someone wrote
    it. This one is generated, and this test is what stops it drifting away from the code
    it describes."""
    committed = robustness_eval.DOCS_PATH.read_text()
    assert committed == robustness_eval.render_docs(report), (
        "docs/robustness.md is stale — run `python -m scripts.robustness_eval --docs`"
    )


def test_every_limitation_says_what_is_not_handled() -> None:
    """The second column is the one worth reading. An entry with an empty 'not handled'
    is marketing wearing a limitations list's clothes."""
    for limitation in limitations.LIMITATIONS:
        assert limitation.handled and limitation.not_handled
        assert limitation.why and limitation.evidence


def test_the_limitations_cover_every_condition_family() -> None:
    """Anything the robustness set exercises has to appear in the honest account, or the
    account is describing a different pipeline."""
    text = " ".join(limitation.area.lower() for limitation in limitations.LIMITATIONS)
    for topic in ("angle", "curved", "lighting", "glare", "blur", "threshold"):
        assert topic in text, topic


def test_the_hardest_admissions_are_actually_in_there(
    report: robustness_eval.Report,
) -> None:
    """The three a reviewer would find on their own: a cat photo passes the gate, glare
    is never painted over, and the thresholds have never seen a photograph."""
    text = robustness_eval.render_docs(report).lower()
    assert "photograph of a cat" in text
    assert "no inpainting" in text
    assert "calibrated against generated labels, not photographs" in text


def test_the_document_says_how_to_reproduce_every_number(
    report: robustness_eval.Report,
) -> None:
    text = robustness_eval.render_docs(report)
    for command in (
        "python -m scripts.robustness_eval",
        "python -m scripts.calibrate_quality",
        "python -m scripts.compression_sweep",
        "python -m scripts.crop_before_send",
    ):
        assert command in text


def test_the_document_names_the_tier_a_gap(report: robustness_eval.Report) -> None:
    """It proves the pipeline can read our own renderer. Saying only that would be the
    misleading number BUILD.md §5 warns about."""
    text = robustness_eval.render_docs(report)
    assert "read our own renderer" in text
    assert "never averaged" in text


def test_an_unswept_value_is_reported_as_unmeasured() -> None:
    """If someone edits a threshold without widening the sweep, every level was measured
    and none of them was the value in use. The summary must not then report a clean bill
    of health for a number nothing here covers."""
    original = T.HOPELESS
    try:
        T.HOPELESS = 0.99
        sweep = calibrate_quality.sweep_one("HOPELESS", _narrow("HOPELESS", (0.15, 0.2)))
    finally:
        T.HOPELESS = original

    assert sweep.unproven
    assert sweep.margin == -1
    assert "never measured" in sweep.verdict
    assert "never swept at their shipped value" in calibrate_quality.render([sweep])


def test_a_swept_value_is_not_reported_as_unmeasured(
    blur_sweep: calibrate_quality.Sweep,
) -> None:
    assert blur_sweep.on_grid and not blur_sweep.unproven


def test_the_document_separates_what_ships_from_what_does_not(
    report: robustness_eval.Report,
) -> None:
    """The claim that started this: per-region readability was listed under Handled in a
    document whose whole premise is not overclaiming, while `verify_endpoint` has never
    called it."""
    text = robustness_eval.render_docs(report)
    assert "## Running in the product" in text
    assert "## Built and tested, but NOT wired into the product" in text
    assert "currently unreachable from the API" in text


def test_the_unwired_half_is_not_empty_and_names_the_region_check() -> None:
    """If someone wires it up, they should move the entry rather than delete this test."""
    unwired = [x.area for x in limitations.LIMITATIONS if not x.runs_in_production]
    assert "Region readability" in unwired
    assert "Glare" in unwired


def test_the_pre_gate_is_listed_as_running() -> None:
    """It genuinely does run — `verify_endpoint` calls quality.assess and pre-gates on it.
    Understating that would be the opposite error."""
    live = [x.area for x in limitations.LIMITATIONS if x.runs_in_production]
    assert "Blur" in live
    assert "Thresholds" in live


def test_the_document_carries_the_exact_wiring_change(
    report: robustness_eval.Report,
) -> None:
    """"Wire up preprocessing" is not an instruction anyone can act on."""
    text = robustness_eval.render_docs(report)
    assert "preprocess_mod.preprocess" in text
    assert "assess_region" in text
    assert "Order matters" in text
