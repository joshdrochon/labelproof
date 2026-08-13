"""Reading a typed application entry (LP-336).

The defect this exists for: the browser read the alcohol box with
`raw.match(/-?\\d+(\\.\\d+)?/)` — first number wins — so

    45% BY VOL. (Front label) / 43% BY VOL. (Back label)

was filed as 45.0 and checked against the label as though the applicant had declared it.
No message, no flag. A tool whose whole argument is that it never guesses, guessing in
the first box on the first screen.

Two properties are asserted throughout, and they pull in opposite directions on purpose:
generous about decoration, strict about ambiguity.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api import entry
from api.batch import manifest as manifest_mod
from api.config import Config
from api.main import create_app
from api.models import Application

# --- generous about decoration --------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("45", 45.0),
        ("45.0", 45.0),
        ("45%", 45.0),
        ("45 %", 45.0),
        ("45% ABV", 45.0),
        ("45% Alc./Vol.", 45.0),
        ("alc. 45% by vol.", 45.0),
        ("Alcohol 45 percent by volume", 45.0),
        ("90 proof", 45.0),
        ("90°", 45.0),
        ("45% Alc./Vol. (90 Proof)", 45.0),  # one value stated twice, not two values
        ("45,5%", 45.5),  # European decimal comma
        ("  45  ", 45.0),
        ("", None),
        ("   ", None),
    ],
)
def test_a_decorated_entry_is_read_rather_than_refused(typed: str, expected: float | None) -> None:
    """The agent is pasting out of a COLA screen. Punishing them for the punctuation that
    came with it is the tool making a person slower, which is the complaint this product
    answers rather than repeats.
    """
    assert entry.read_alcohol_content(typed) == expected


# --- strict about ambiguity -----------------------------------------------------------


@pytest.mark.parametrize(
    "typed",
    [
        "45% BY VOL. (Front label) / 43% BY VOL. (Back label)",  # the reported entry
        "45% or 43%",
        "40%-45%",
        "45% / 43%",
    ],
)
def test_two_different_values_are_refused_rather_than_guessed(typed: str) -> None:
    with pytest.raises(entry.EntryError) as caught:
        entry.read_alcohol_content(typed)

    assert "more than one value" in str(caught.value)


def test_the_refusal_names_both_numbers_it_saw() -> None:
    """"Enter a number" would be false — they did. The message has to say what made the
    entry unreadable, or the agent retypes the same thing.
    """
    with pytest.raises(entry.EntryError) as caught:
        entry.read_alcohol_content("45% (Front) / 43% (Back)")

    message = str(caught.value)
    assert "43" in message and "45" in message
    assert "will not choose" in message


@pytest.mark.parametrize("typed", ["about forty-five", "n/a", "see label", "%"])
def test_an_entry_with_no_number_is_refused(typed: str) -> None:
    with pytest.raises(entry.EntryError):
        entry.read_alcohol_content(typed)


@pytest.mark.parametrize("typed", ["145%", "999"])
def test_a_value_outside_nought_to_a_hundred_is_refused(typed: str) -> None:
    with pytest.raises(entry.EntryError, match="between 0 and 100"):
        entry.read_alcohol_content(typed)


# --- net contents: same unit twice is ambiguous, two units is not ---------------------


def test_two_sizes_in_the_same_unit_are_refused() -> None:
    with pytest.raises(entry.EntryError, match="more than one ml value"):
        entry.check_net_contents("750mL (Front label) / 700 mL (Back label)")


@pytest.mark.parametrize(
    "typed",
    [
        "750 mL (25.4 fl oz)",  # one quantity, two units — ordinary on a real label
        "750 mL",
        "1.75 L",
        "1,75 L",
        "",
    ],
)
def test_a_dual_unit_declaration_is_not_ambiguous(typed: str) -> None:
    """Refusing this would be the gate firing on correct, common input — which is how a
    validation rule teaches people to work around it.
    """
    assert entry.check_net_contents(typed) == typed.strip()


def test_the_entry_is_returned_exactly_as_typed() -> None:
    """Not normalised. The comparison downstream already normalises, and rewriting the
    agent's text would make the row they read differ from the row they filed.
    """
    assert entry.check_net_contents("  750 ML  ") == "750 ML"


# --- one rule, every door -------------------------------------------------------------


def test_the_model_reads_a_decorated_entry() -> None:
    """On `Application`, not in the route — so the form, the JSON API and a manifest row
    cannot disagree about what a valid entry is."""
    application = Application(
        commodity="spirits",
        brand_name="b",
        class_type="c",
        alcohol_content="alc. 45% by vol.",  # type: ignore[arg-type]
        net_contents="750 mL",
        producer_name="p",
        producer_address="a",
    )
    assert application.alcohol_content == 45.0


def test_the_model_refuses_an_ambiguous_entry() -> None:
    with pytest.raises(ValidationError):
        Application(
            commodity="spirits",
            brand_name="b",
            class_type="c",
            alcohol_content="45% / 43%",  # type: ignore[arg-type]
            net_contents="750 mL",
            producer_name="p",
            producer_address="a",
        )


def test_the_http_route_returns_the_sentence_entry_wrote() -> None:
    """The 400 must carry the message naming both numbers, not the generic "not in a form
    this tool can read" — which is what `_validation_message` said before it learned to
    pass `value_error` through.
    """
    client = TestClient(create_app(config=Config(use_fake_provider=True)))

    response = client.post(
        "/verify",
        data={
            "application": json.dumps(
                {
                    "commodity": "spirits",
                    "brand_name": "OLD OAK",
                    "class_type": "Bourbon",
                    "alcohol_content": "45% BY VOL. (Front) / 43% BY VOL. (Back)",
                    "net_contents": "750 mL",
                    "producer_name": "p",
                    "producer_address": "a",
                    "is_import": False,
                }
            )
        },
        files=[("images", ("a.png", b"not-an-image", "image/png"))],
    )

    assert response.status_code == 400, response.text
    message = response.json()["error"]["message"]
    assert "more than one value" in message
    assert "43" in message and "45" in message
    assert "not in a form this tool can read" not in message


def test_a_manifest_row_reads_the_same_entries_the_form_does() -> None:
    """The manifest had its OWN parser: strip a trailing "%", call float(). So a batch
    rejected `alc. 45% by vol.` while the single-check screen accepted it — two doors into
    one product disagreeing about what a valid application is.
    """
    manifest = (
        "commodity,brand_name,class_type,alcohol_content,net_contents,producer_name,"
        "producer_address,country_of_origin,is_import,front_image,back_image\r\n"
        "spirits,Old Tom,Bourbon,alc. 45% by vol.,750 mL,Old Tom,Bardstown KY,,false,f.png,\r\n"
    )

    parsed = manifest_mod.parse(manifest)

    assert parsed.errors == [], parsed.errors
    assert parsed.rows[0].application.alcohol_content == 45.0


def test_a_manifest_row_with_two_values_is_refused_by_row_and_column() -> None:
    manifest = (
        "commodity,brand_name,class_type,alcohol_content,net_contents,producer_name,"
        "producer_address,country_of_origin,is_import,front_image,back_image\r\n"
        "spirits,Old Tom,Bourbon,45% / 43%,750 mL,Old Tom,Bardstown KY,,false,f.png,\r\n"
    )

    parsed = manifest_mod.parse(manifest)

    assert parsed.rows == []
    assert [(e.row, e.column) for e in parsed.errors] == [(2, "alcohol_content")]
    assert "more than one value" in parsed.errors[0].message
