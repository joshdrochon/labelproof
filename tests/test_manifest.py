"""Manifest parsing (LP-148, LP-149, LP-150, BATCH-3, TC-20).

The load-bearing assertion in this file is that every problem carries the row number an
agent sees in a spreadsheet. A 300-row manifest with a message that does not say *which*
row is a message that costs more time than it saves.
"""

from __future__ import annotations

import csv
import io

import pytest

from api.batch import manifest as manifest_mod
from api.models import Commodity

HEADER = ",".join(manifest_mod.COLUMNS)

GOOD_ROW = (
    "spirits,Old Tom Distillery,Kentucky Straight Bourbon Whiskey,45,750 mL,"
    "Old Tom Distillery,Bardstown Kentucky,,false,front.png,back.png"
)


def manifest(*rows: str, header: str = HEADER) -> str:
    return "\n".join([header, *rows]) + "\n"


# --- the template ---------------------------------------------------------------------

def test_template_columns_are_exactly_what_the_parser_reads() -> None:
    """LP-148 — a template that drifts from the parser is worse than no template."""
    reader = csv.reader(io.StringIO(manifest_mod.template_csv()))
    assert tuple(next(reader)) == manifest_mod.COLUMNS


def test_the_template_example_row_parses_without_errors() -> None:
    """The worked example must actually work, or it teaches the wrong format."""
    parsed = manifest_mod.parse(manifest_mod.template_csv())
    assert parsed.errors == []
    assert len(parsed.rows) == 1
    assert parsed.rows[0].application.commodity is Commodity.SPIRITS
    assert parsed.rows[0].front_image == "old_tom_front.png"


# --- happy path -----------------------------------------------------------------------

def test_parses_application_fields() -> None:
    parsed = manifest_mod.parse(manifest(GOOD_ROW))
    assert parsed.errors == []
    application = parsed.rows[0].application
    assert application.brand_name == "Old Tom Distillery"
    assert application.alcohol_content == 45.0
    assert application.net_contents == "750 mL"
    assert application.country_of_origin is None
    assert application.is_import is False


def test_blank_alcohol_content_is_none_not_an_error() -> None:
    """A malt beverage with no stated ABV is a real application, not a bad row."""
    row = GOOD_ROW.replace(",45,", ",,")
    parsed = manifest_mod.parse(manifest(row))
    assert parsed.errors == []
    assert parsed.rows[0].application.alcohol_content is None


def test_percent_sign_in_alcohol_content_is_tolerated() -> None:
    parsed = manifest_mod.parse(manifest(GOOD_ROW.replace(",45,", ",45%,")))
    assert parsed.errors == []
    assert parsed.rows[0].application.alcohol_content == 45.0


@pytest.mark.parametrize("written,expected", [
    ("true", True), ("TRUE", True), ("Yes", True), ("1", True),
    ("false", False), ("no", False), ("", False),
])
def test_import_flag_accepts_what_people_actually_type(written: str, expected: bool) -> None:
    row = GOOD_ROW.replace(",false,", f",{written},")
    parsed = manifest_mod.parse(manifest(row))
    assert parsed.errors == []
    assert parsed.rows[0].application.is_import is expected


def test_blank_lines_between_blocks_are_not_errors() -> None:
    parsed = manifest_mod.parse(manifest(GOOD_ROW, "", GOOD_ROW))
    assert parsed.errors == []
    assert len(parsed.rows) == 2


def test_a_bom_from_excel_does_not_break_the_first_column() -> None:
    parsed = manifest_mod.parse("﻿" + manifest(GOOD_ROW))
    assert parsed.errors == []
    assert parsed.rows[0].application.commodity is Commodity.SPIRITS


# --- row-numbered errors (TC-20) ------------------------------------------------------

def test_row_numbers_are_the_ones_a_spreadsheet_shows() -> None:
    """The header is row 1, so the first application is row 2 — TC-20."""
    parsed = manifest_mod.parse(manifest(GOOD_ROW, GOOD_ROW.replace("spirits", "vodka")))
    assert [e.row for e in parsed.errors] == [3]


def test_an_unknown_commodity_names_the_row_and_the_column() -> None:
    parsed = manifest_mod.parse(manifest(GOOD_ROW.replace("spirits", "cider")))
    error = parsed.errors[0]
    assert error.row == 2
    assert error.column == "commodity"
    assert "spirits, wine or malt" in error.message


def test_a_missing_required_value_names_the_row_and_the_column() -> None:
    row = GOOD_ROW.replace("Old Tom Distillery,Kentucky", ",Kentucky", 1)
    parsed = manifest_mod.parse(manifest(row))
    assert [(e.row, e.column) for e in parsed.errors] == [(2, "brand_name")]
    assert "brand name is empty" in parsed.errors[0].message


def test_unparseable_alcohol_content_says_what_to_type_instead() -> None:
    parsed = manifest_mod.parse(manifest(GOOD_ROW.replace(",45,", ",strong,")))
    error = parsed.errors[0]
    assert error.column == "alcohol_content"
    assert "45" in error.message


def test_a_row_with_no_image_reference_is_an_error() -> None:
    parsed = manifest_mod.parse(manifest(GOOD_ROW.replace(",front.png,back.png", ",,")))
    assert parsed.errors[0].row == 2
    assert "front_image" in parsed.errors[0].message


def test_bad_rows_do_not_take_the_good_ones_down() -> None:
    """TC-20 — a mixed manifest queues what it can and reports the rest."""
    parsed = manifest_mod.parse(
        manifest(GOOD_ROW, GOOD_ROW.replace("spirits", "brandywine"), GOOD_ROW)
    )
    assert len(parsed.rows) == 2
    assert [e.row for e in parsed.errors] == [3]


def test_every_error_message_says_what_to_do_next() -> None:
    """UX-6 — plain language, and an instruction, on every single one."""
    parsed = manifest_mod.parse(
        manifest(
            GOOD_ROW.replace("spirits", "cider"),
            GOOD_ROW.replace(",45,", ",strong,"),
            GOOD_ROW.replace(",false,", ",maybe,"),
            GOOD_ROW.replace(",front.png,back.png", ",,"),
        )
    )
    assert parsed.errors
    for error in parsed.errors:
        assert error.message[0].isupper() or error.message[0].islower()
        assert error.message.endswith(".")
        assert not any(
            jargon in error.message.lower()
            for jargon in ("valueerror", "none", "traceback", "pydantic", "null")
        )


# --- whole-file problems --------------------------------------------------------------

def test_an_empty_file_is_a_file_problem_not_a_row_problem() -> None:
    with pytest.raises(manifest_mod.ManifestError) as caught:
        manifest_mod.parse("")
    assert "template" in str(caught.value)


def test_missing_columns_are_named() -> None:
    with pytest.raises(manifest_mod.ManifestError) as caught:
        manifest_mod.parse("brand_name,front_image\nOld Tom,front.png\n")
    message = str(caught.value)
    assert "commodity" in message and "net_contents" in message


def test_a_manifest_with_headings_and_no_rows_says_so() -> None:
    with pytest.raises(manifest_mod.ManifestError) as caught:
        manifest_mod.parse(HEADER + "\n")
    assert "one row per application" in str(caught.value)


def test_headings_are_matched_case_insensitively() -> None:
    parsed = manifest_mod.parse(manifest(GOOD_ROW, header=HEADER.upper()))
    assert parsed.errors == []
    assert len(parsed.rows) == 1


# --- pairing (LP-150, LP-151) ---------------------------------------------------------

def test_pairing_resolves_front_and_back_in_order() -> None:
    parsed = manifest_mod.parse(manifest(GOOD_ROW))
    pairing = manifest_mod.pair(parsed.rows, ["back.png", "front.png"])
    assert pairing.resolved[2] == ["front.png", "back.png"]
    assert pairing.errors == []
    assert pairing.unmatched == []


def test_pairing_ignores_the_folder_a_zip_put_the_file_in() -> None:
    parsed = manifest_mod.parse(manifest(GOOD_ROW))
    pairing = manifest_mod.pair(parsed.rows, ["labels/front.png", "labels\\back.PNG"])
    assert pairing.resolved[2] == ["labels/front.png", "labels\\back.PNG"]


def test_a_reference_to_a_file_nobody_uploaded_is_a_row_error() -> None:
    parsed = manifest_mod.parse(manifest(GOOD_ROW))
    pairing = manifest_mod.pair(parsed.rows, ["front.png"])
    assert [(e.row, e.column) for e in pairing.errors] == [(2, "back_image")]
    assert "back.png" in pairing.errors[0].message


def test_unmatched_files_are_reported_by_name() -> None:
    """LP-150 — "3 files unused" makes the agent diff two lists of 300 by hand."""
    parsed = manifest_mod.parse(manifest(GOOD_ROW))
    pairing = manifest_mod.pair(
        parsed.rows, ["front.png", "back.png", "stray.png", "notes.png"]
    )
    assert pairing.unmatched == ["notes.png", "stray.png"]


def test_one_image_shared_by_two_rows_is_not_reported_as_unmatched() -> None:
    parsed = manifest_mod.parse(manifest(GOOD_ROW, GOOD_ROW))
    pairing = manifest_mod.pair(parsed.rows, ["front.png", "back.png"])
    assert pairing.unmatched == []
    assert pairing.resolved[2] == pairing.resolved[3]
