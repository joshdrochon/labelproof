"""The manifest — 300 applications as one spreadsheet (BATCH-3, LP-148, LP-149).

CSV, because the person assembling an importer dump has the data in Excel and every
alternative asks them to convert it first. The columns are the `Application` fields plus
`front_image` and `back_image` (pinned build decision).

Three rules govern this module.

**Every error names a row and a column.** An agent with a 300-row manifest and a message
that says "invalid input" has to find the bad row by hand, which is the manual labour
batch mode exists to remove. Row numbers are the ones Excel shows: the header is row 1,
so the first application is row 2 (TC-20).

**One bad row never rejects the file.** Rows that parse are queued; rows that do not are
reported. Rejecting 300 applications over one typo would send the agent back to
processing them one at a time, which is the problem, not the fix.

**The template is generated from the same tuple the parser reads.** A committed template
file would be a second copy of the schema and would drift the first time a column is
added. `template_csv()` renders it, so the example an agent downloads is by construction
the format the parser accepts.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from api import entry
from api.batch.models import RowError
from api.models import Application, Commodity

#: The application half of the manifest, in the order the template writes them.
APPLICATION_COLUMNS: tuple[str, ...] = (
    "commodity",
    "brand_name",
    "class_type",
    "alcohol_content",
    "net_contents",
    "producer_name",
    "producer_address",
    "country_of_origin",
    "is_import",
)

#: The image references. At least one must be filled — an application with no artwork
#: cannot be verified, and queueing it would produce a failure the agent could have been
#: told about at upload time.
IMAGE_COLUMNS: tuple[str, ...] = ("front_image", "back_image")

COLUMNS: tuple[str, ...] = APPLICATION_COLUMNS + IMAGE_COLUMNS

#: Columns that must be present in the header and non-empty in every row.
REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "commodity",
        "brand_name",
        "class_type",
        "net_contents",
        "producer_name",
        "producer_address",
    }
)

#: Column names in the agents' words, for messages. The schema's names are for the schema.
_LABELS: dict[str, str] = {
    "commodity": "commodity",
    "brand_name": "brand name",
    "class_type": "class or type designation",
    "alcohol_content": "alcohol content",
    "net_contents": "net contents",
    "producer_name": "producer name",
    "producer_address": "producer address",
    "country_of_origin": "country of origin",
    "is_import": "imported",
    "front_image": "front image",
    "back_image": "back image",
}

_TRUE = frozenset({"true", "t", "yes", "y", "1", "import", "imported"})
_FALSE = frozenset({"false", "f", "no", "n", "0", "", "domestic"})

#: The first application sits on line 2 of the file, because line 1 is the header. Every
#: row number this module emits is a line number in the file the agent opened.
_FIRST_DATA_ROW = 2


@dataclass(frozen=True)
class ManifestRow:
    """One application, parsed and ready to queue."""

    row: int
    application: Application
    front_image: str | None = None
    back_image: str | None = None

    @property
    def image_names(self) -> list[str]:
        return [name for name in (self.front_image, self.back_image) if name]


@dataclass
class ParsedManifest:
    rows: list[ManifestRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)


class ManifestError(Exception):
    """The file is not a manifest at all — no header, no columns, nothing to parse.

    Distinct from a row error: a row error is one application to fix, this is a wrong
    file. The caller turns it into a `UserError` with the message unchanged.
    """


def template_csv() -> str:
    """The downloadable template: the header plus one filled example row (LP-168).

    A header-only template leaves an agent guessing what `is_import` wants and whether
    `750 mL` needs the space. One worked example answers both without documentation.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerow(
        [
            "spirits",
            "Old Tom Distillery",
            "Kentucky Straight Bourbon Whiskey",
            "45",
            "750 mL",
            "Old Tom Distillery",
            "Bardstown, Kentucky",
            "",
            "false",
            "old_tom_front.png",
            "old_tom_back.png",
        ]
    )
    return buffer.getvalue()


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _parse_bool(raw: str) -> bool | None:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return None


def _validation_errors(row: int, exc: ValidationError) -> list[RowError]:
    out: list[RowError] = []
    for detail in exc.errors():
        column = str(detail["loc"][0]) if detail["loc"] else ""
        label = _LABELS.get(column, column.replace("_", " ") or "this row")
        kind = str(detail["type"])
        if kind == "missing":
            message = f"{label} is required. Fill it in and upload the manifest again."
        elif kind.startswith("enum"):
            message = (
                f"{label} must be spirits, wine or malt. Correct it and upload the "
                f"manifest again."
            )
        elif kind == "value_error":
            # `api/entry.py` wrote a sentence naming the field and the fix. Replacing it
            # with the generic line below would drop the only text that says WHICH of the
            # two sizes in the cell the tool refused to choose between.
            detail_msg = str(detail.get("msg", "")).removeprefix("Value error, ")
            message = f"{detail_msg}. Correct it and upload the manifest again."
        else:
            message = (
                f"{label} is not in a form this tool can read. Correct it and upload "
                f"the manifest again."
            )
        out.append(RowError(row=row, column=column or None, message=message))
    return out


def _parse_row(row: int, raw: dict[str, str | None]) -> tuple[ManifestRow | None, list[RowError]]:
    errors: list[RowError] = []
    values = {column: _clean(raw.get(column)) for column in COLUMNS}

    for column in sorted(REQUIRED_COLUMNS):
        if not values[column]:
            errors.append(
                RowError(
                    row=row,
                    column=column,
                    message=(
                        f"{_LABELS[column]} is empty. Fill it in and upload the manifest "
                        f"again — this application was not checked."
                    ),
                )
            )

    commodity = values["commodity"].lower()
    if commodity and commodity not in {c.value for c in Commodity}:
        errors.append(
            RowError(
                row=row,
                column="commodity",
                message=(
                    f"commodity reads “{values['commodity']}”. It must be "
                    f"spirits, wine or malt."
                ),
            )
        )
        commodity = ""

    # ONE rule for reading a typed alcohol entry, shared with the form and the JSON API
    # (LP-336). This used to be `_parse_float`, a second implementation that stripped a
    # trailing "%" and called `float()` — so a manifest rejected `alc. 45% by vol.` and
    # `90 proof`, which the single-check screen accepted. Two doors into one product
    # disagreeing about what a valid entry is.
    abv: float | None = None
    try:
        abv = entry.read_alcohol_content(values["alcohol_content"])
    except entry.EntryError as problem:
        errors.append(
            RowError(
                row=row,
                column="alcohol_content",
                message=f"{problem}. Correct it and upload the manifest again.",
            )
        )

    is_import = _parse_bool(values["is_import"])
    if is_import is None:
        errors.append(
            RowError(
                row=row,
                column="is_import",
                message=(
                    f"imported reads “{values['is_import']}”. Enter true or "
                    f"false, or leave it empty for a domestic product."
                ),
            )
        )
        is_import = False

    front = values["front_image"] or None
    back = values["back_image"] or None
    if not front and not back:
        errors.append(
            RowError(
                row=row,
                column="front_image",
                message=(
                    "No label image is named for this application. Put the image file "
                    "name in front_image (and back_image if there are two)."
                ),
            )
        )

    if errors:
        return None, errors

    try:
        application = Application(
            commodity=Commodity(commodity),
            brand_name=values["brand_name"],
            class_type=values["class_type"],
            alcohol_content=abv,
            net_contents=values["net_contents"],
            producer_name=values["producer_name"],
            producer_address=values["producer_address"],
            country_of_origin=values["country_of_origin"] or None,
            is_import=is_import,
        )
    except ValidationError as exc:
        return None, _validation_errors(row, exc)

    return ManifestRow(row=row, application=application, front_image=front, back_image=back), []


def parse(text: str) -> ParsedManifest:
    """Parse a manifest into queueable rows plus row-numbered problems (LP-149)."""
    # utf-8-sig upstream strips the BOM Excel writes; a stray one here would make the
    # first column name unrecognisable and report every row as missing a commodity.
    stripped = text.lstrip("﻿")
    if not stripped.strip():
        raise ManifestError(
            "That manifest file is empty. Download the template, fill in one row per "
            "application, and upload it again."
        )

    reader = csv.DictReader(io.StringIO(stripped))
    header = [name.strip().lower() for name in (reader.fieldnames or [])]
    if not header:
        raise ManifestError(
            "That file has no column headings, so there is no way to tell which value "
            "is which. Download the template and use its first line as the heading row."
        )

    missing = sorted(REQUIRED_COLUMNS - set(header))
    if not set(IMAGE_COLUMNS) & set(header):
        missing.append("front_image")
    if missing:
        names = ", ".join(sorted(missing))
        raise ManifestError(
            f"The manifest is missing these columns: {names}. Download the template, "
            f"copy your data into it, and upload it again."
        )

    parsed = ParsedManifest()
    for offset, raw in enumerate(reader):
        line = _FIRST_DATA_ROW + offset
        normalized = {
            (key or "").strip().lower(): value
            for key, value in raw.items()
            if isinstance(key, str)
        }
        if not any(_clean(value) for value in normalized.values()):
            continue  # a blank line between blocks is not an error
        row, errors = _parse_row(line, normalized)
        if row is not None:
            parsed.rows.append(row)
        parsed.errors.extend(errors)

    if not parsed.rows and not parsed.errors:
        raise ManifestError(
            "That manifest has column headings but no applications under them. Add one "
            "row per application and upload it again."
        )

    return parsed


def _key(name: str) -> str:
    """Match on the bare file name, case-insensitively.

    A zip built on Windows carries `images\\front.PNG` and one built on a Mac carries
    `Batch/front.png`; the manifest says `front.png` in both cases. Matching on the
    basename is what makes the same manifest work with either archive.
    """
    return Path(name.replace("\\", "/")).name.strip().lower()


@dataclass
class Pairing:
    """The result of matching manifest rows to the files that were uploaded."""

    resolved: dict[int, list[str]] = field(default_factory=dict)
    """row number -> supplied file names, front first."""

    errors: list[RowError] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


def pair(rows: list[ManifestRow], filenames: list[str]) -> Pairing:
    """Match each row's image references against the uploaded files (LP-150, LP-151).

    Files nobody referenced are reported **by name**. "3 files were not used" makes the
    agent diff two lists of 300 by hand; naming them makes the typo obvious.
    """
    index: dict[str, str] = {}
    for name in filenames:
        index.setdefault(_key(name), name)

    pairing = Pairing()
    used: set[str] = set()

    for row in rows:
        supplied: list[str] = []
        for column in IMAGE_COLUMNS:
            referenced = getattr(row, column)
            if not referenced:
                continue
            actual = index.get(_key(referenced))
            if actual is None:
                pairing.errors.append(
                    RowError(
                        row=row.row,
                        column=column,
                        message=(
                            f"No uploaded file is named “{referenced}”. Check "
                            f"the spelling, or add that image to the upload."
                        ),
                    )
                )
                continue
            used.add(_key(referenced))
            supplied.append(actual)

        if supplied:
            pairing.resolved[row.row] = supplied

    pairing.unmatched = sorted(
        name for key, name in index.items() if key not in used
    )
    return pairing
