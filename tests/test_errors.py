"""Error taxonomy. Every message must be actionable by a compliance agent (UX-6)."""

import pytest

from api import errors
from api.errors import ErrorKind


def test_four_kinds_split_by_who_can_act() -> None:
    assert {k.value for k in ErrorKind} == {"user", "image", "provider", "internal"}


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (errors.UserError("x"), 400),
        (errors.ImageError("x"), 422),
        (errors.ProviderUnavailable(), 503),
        (errors.InternalError(), 500),
    ],
)
def test_status_codes(error: errors.LabelProofError, status: int) -> None:
    assert error.status_code == status


def test_provider_trouble_is_503_not_500() -> None:
    """Not our bug. The distinction matters to anyone reading a status page."""
    assert errors.ProviderUnavailable().status_code == 503


def test_payload_shape_matches_the_api_contract() -> None:
    payload = errors.file_too_large(10).to_payload()["error"]
    assert set(payload) == {"kind", "code", "message", "next_step"}


# --- plain language -------------------------------------------------------------------

@pytest.mark.parametrize(
    "error",
    [
        errors.file_too_large(10),
        errors.unsupported_file_type("Word document"),
        errors.not_a_label(),
        errors.unreadable("Glare covers the lower third of the label."),
        errors.ProviderUnavailable(),
        errors.InternalError(),
    ],
)
def test_messages_are_written_for_an_agent(error: errors.LabelProofError) -> None:
    message = error.message
    assert message[0].isupper() and message.rstrip().endswith(".")
    for jargon in ("exception", "traceback", "null", "inference", "500", "stack"):
        assert jargon not in message.lower()


@pytest.mark.parametrize(
    "error",
    [errors.file_too_large(10), errors.not_a_label(), errors.ProviderUnavailable()],
)
def test_every_error_says_what_to_do_next(error: errors.LabelProofError) -> None:
    assert error.next_step


def test_provider_failure_reassures_that_nothing_changed() -> None:
    """Marcus watched a vendor pilot die behind a firewall. Say what did not happen."""
    assert "has been changed" in errors.ProviderUnavailable().message


@pytest.mark.tc("TC-15")
def test_not_a_label_is_graceful_and_specific() -> None:
    error = errors.not_a_label()
    assert error.kind is ErrorKind.IMAGE
    assert "does not look like a label" in error.message
