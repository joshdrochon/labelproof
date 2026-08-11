"""End-to-end verification against generated fixtures, with no model in the loop."""

import pytest

from api.models import Application, Commodity, FieldName, Recommendation, Verdict
from api.provider.base import ImageInput, ProviderError
from api.provider.fake import (
    FailingProvider,
    NonLabelProvider,
    SpecBackedProvider,
    spec_name_for_image,
)
from api.verify import verify
from fixtures.generator.catalog import by_name


def images(n: int = 1, roles: list[str] | None = None) -> list[ImageInput]:
    roles = roles or ["single"] * n
    return [ImageInput(index=i, data=b"", role=r) for i, r in enumerate(roles)]


def application_for(name: str, **overrides: object) -> Application:
    spec = by_name(name)
    producer_name, _, producer_address = spec.producer.partition(", ")
    base = {
        "commodity": Commodity(spec.commodity),
        "brand_name": spec.brand_name,
        "class_type": spec.class_type,
        "alcohol_content": 45.0 if spec.commodity == "spirits" else None,
        "net_contents": spec.net_contents,
        "producer_name": producer_name,
        "producer_address": producer_address,
        "country_of_origin": spec.country_of_origin,
        "is_import": False,
    }
    base.update(overrides)
    return Application(**base)  # type: ignore[arg-type]


def run(name: str, **overrides: object):
    spec = by_name(name)
    return verify(application_for(name, **overrides), images(1), SpecBackedProvider(spec))


# --- TC-01 ------------------------------------------------------------------------------

@pytest.mark.tc("TC-01")
def test_clean_label_is_ready_to_approve() -> None:
    result = run("tc01_old_tom_clean")
    assert result.aggregate.recommendation is Recommendation.READY_TO_APPROVE
    assert all(f.verdict in (Verdict.MATCH, Verdict.NOT_APPLICABLE) for f in result.fields)


@pytest.mark.tc("TC-01")
def test_clean_label_returns_all_seven_fields() -> None:
    assert len(run("tc01_old_tom_clean").fields) == 7


def test_warning_is_listed_first() -> None:
    assert run("tc01_old_tom_clean").fields[0].field is FieldName.GOVERNMENT_WARNING


# --- defects ------------------------------------------------------------------------------

@pytest.mark.tc("TC-02")
def test_stones_throw_is_acceptable_variation() -> None:
    result = run("tc02_stones_throw", brand_name="Stone's Throw")
    brand = next(f for f in result.fields if f.field is FieldName.BRAND_NAME)
    assert brand.verdict is Verdict.ACCEPTABLE_VARIATION
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


@pytest.mark.tc("TC-03")
def test_title_case_warning_returns_for_correction() -> None:
    result = run("tc03_title_case_warning")
    assert result.aggregate.recommendation is Recommendation.RETURN_FOR_CORRECTION
    assert result.aggregate.driving_field is FieldName.GOVERNMENT_WARNING


@pytest.mark.tc("TC-04")
def test_bold_warning_body_returns_for_correction() -> None:
    assert run("tc04_bold_warning_body").aggregate.recommendation is (
        Recommendation.RETURN_FOR_CORRECTION
    )


@pytest.mark.tc("TC-05")
def test_reworded_warning_returns_for_correction() -> None:
    assert run("tc05_reworded_warning").aggregate.recommendation is (
        Recommendation.RETURN_FOR_CORRECTION
    )


@pytest.mark.tc("TC-07")
def test_missing_warning_returns_for_correction() -> None:
    result = run("tc07_missing_warning")
    warning = next(f for f in result.fields if f.field is FieldName.GOVERNMENT_WARNING)
    assert warning.verdict is Verdict.MISSING
    assert result.aggregate.recommendation is Recommendation.RETURN_FOR_CORRECTION


@pytest.mark.tc("TC-08")
def test_abv_mismatch_needs_review() -> None:
    result = run("tc08_abv_mismatch")
    abv = next(f for f in result.fields if f.field is FieldName.ALCOHOL_CONTENT)
    assert abv.verdict is Verdict.MISMATCH
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


@pytest.mark.tc("TC-10")
def test_non_standard_fill_matches_and_still_flags() -> None:
    result = run("tc10_non_standard_fill", net_contents="733 mL")
    net = next(f for f in result.fields if f.field is FieldName.NET_CONTENTS)
    assert net.verdict is Verdict.MATCH
    assert any(f.code == "non_standard_fill" for f in net.findings)


@pytest.mark.tc("TC-17")
def test_table_wine_alcohol_content_is_not_applicable() -> None:
    result = run("tc17_table_wine", alcohol_content=None)
    abv = next(f for f in result.fields if f.field is FieldName.ALCOHOL_CONTENT)
    assert abv.verdict is Verdict.NOT_APPLICABLE


@pytest.mark.tc("TC-18")
def test_malt_alcohol_content_is_not_applicable() -> None:
    result = run("tc18_malt_no_abv", alcohol_content=None)
    abv = next(f for f in result.fields if f.field is FieldName.ALCOHOL_CONTENT)
    assert abv.verdict is Verdict.NOT_APPLICABLE


@pytest.mark.tc("TC-19")
def test_import_without_origin_is_missing() -> None:
    result = run("tc19_import_missing_origin", is_import=True, country_of_origin="France")
    origin = next(f for f in result.fields if f.field is FieldName.COUNTRY_OF_ORIGIN)
    assert origin.verdict is Verdict.MISSING


# --- TC-16: multi-image ------------------------------------------------------------------

@pytest.mark.tc("TC-16")
def test_warning_on_the_back_is_found() -> None:
    """Declaring Missing without searching every image is a false finding."""
    spec = by_name("tc16_front_back")
    result = verify(
        application_for("tc16_front_back"),
        images(2, roles=["front", "back"]),
        SpecBackedProvider(spec),
    )
    warning = next(f for f in result.fields if f.field is FieldName.GOVERNMENT_WARNING)
    assert warning.verdict is not Verdict.MISSING


@pytest.mark.tc("TC-16")
def test_front_only_cannot_see_the_warning() -> None:
    """The complement: with only the front, the warning genuinely is not there."""
    spec = by_name("tc16_front_back")
    result = verify(application_for("tc16_front_back"), images(1, roles=["front"]),
                    SpecBackedProvider(spec))
    warning = next(f for f in result.fields if f.field is FieldName.GOVERNMENT_WARNING)
    assert warning.verdict is Verdict.MISSING


# --- TC-12 / TC-15 / TC-21 ---------------------------------------------------------------

@pytest.mark.tc("TC-12")
def test_glare_over_the_warning_is_unreadable_not_missing() -> None:
    spec = by_name("tc01_old_tom_clean")
    provider = SpecBackedProvider(spec, illegible={FieldName.GOVERNMENT_WARNING})
    result = verify(application_for("tc01_old_tom_clean"), images(1), provider)
    warning = next(f for f in result.fields if f.field is FieldName.GOVERNMENT_WARNING)
    assert warning.verdict is Verdict.UNREADABLE
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


@pytest.mark.tc("TC-12")
def test_glare_on_the_warning_leaves_other_fields_verified() -> None:
    """Per-field honesty: one bad region does not condemn the whole label."""
    spec = by_name("tc01_old_tom_clean")
    provider = SpecBackedProvider(spec, illegible={FieldName.GOVERNMENT_WARNING})
    result = verify(application_for("tc01_old_tom_clean"), images(1), provider)
    brand = next(f for f in result.fields if f.field is FieldName.BRAND_NAME)
    assert brand.verdict is Verdict.MATCH


@pytest.mark.tc("TC-15")
def test_non_label_image_is_handled_gracefully() -> None:
    result = verify(application_for("tc01_old_tom_clean"), images(1), NonLabelProvider())
    assert result.fields == []
    assert "does not look like a label" in result.aggregate.rationale


@pytest.mark.tc("TC-21")
def test_provider_down_raises_a_typed_error_not_a_crash() -> None:
    with pytest.raises(ProviderError) as exc:
        verify(application_for("tc01_old_tom_clean"), images(1), FailingProvider())
    assert exc.value.retryable


# --- invariants ---------------------------------------------------------------------------

def test_no_verification_ever_fabricates_a_value() -> None:
    """LP-067 across the whole pipeline, not just the comparators."""
    spec = by_name("tc01_old_tom_clean")
    provider = SpecBackedProvider(spec, illegible=set(FieldName))
    result = verify(application_for("tc01_old_tom_clean"), images(1), provider)
    for field in result.fields:
        if field.verdict is Verdict.UNREADABLE:
            assert field.extracted is None


def test_every_result_carries_timings() -> None:
    assert run("tc01_old_tom_clean").timings_ms.total >= 0


def test_image_filename_maps_back_to_its_fixture() -> None:
    assert spec_name_for_image("tc03_title_case_warning_back.png") == "tc03_title_case_warning"
    assert spec_name_for_image("tc01_old_tom_clean.png") == "tc01_old_tom_clean"
