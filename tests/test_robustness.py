"""The named robustness cases, end to end — pixels in, verdicts out (F3).

The tests in `test_quality.py` and `test_preprocess.py` check one measurement at a time.
These check the thing an agent would actually see: a degraded photograph goes in one end
and a set of per-field verdicts comes out the other.

The chain is real rather than stubbed at the interesting step. Region readability is
computed from the actual degraded pixels, and that result is what tells the extractor
which fields it cannot see — so the test fails if the region scorer stops noticing the
glare, which is exactly the regression worth catching.
"""

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

CLEAN = "tc01_old_tom_clean"


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
