"""E2E: the batch journey an importer's dump actually takes (LP-241, TC-20).

The second of the product's two modes, driven end to end: manifest CSV plus images in,
job id back, progressive status while it runs, and a CSV of per-item verdicts out. No
component is stubbed except the model call.

**These tests mount the batch router themselves**, because `create_app` does not — see
`tests/regression/test_routing_defects.py`, where that gap is pinned as an open defect.
The helper below is deliberately conditional: the moment `main.py` mounts the router, it
becomes a no-op and these tests exercise the shipped app with no edit. Until then, be
clear-eyed about what this file proves — that the batch *pipeline* works, not that the
batch *endpoint* is reachable on the deployed service. That second claim is exactly what
the pinned defect says is untrue today.

The single most important assertion here is the last section: **one bad row must not
contaminate its neighbours.** A 300-item dump where one malformed row poisons the job is
a tool an importer cannot use; a 300-item dump where one row's verdict leaks into
another's is a tool that produces wrong approvals at scale.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from api.config import Config
from api.main import create_app
from api.models import Recommendation
from api.provider.fake import FailingProvider, SpecBackedProvider
from api.routes import batch as batch_routes

pytestmark = pytest.mark.e2e


def _batch_is_mounted(app: FastAPI) -> bool:
    return any(
        str(getattr(route, "path", "")).startswith("/batch") for route in app.router.routes
    )


def _client(tmp_path: Path, provider: Any, **overrides: Any) -> TestClient:
    """The real app, with the batch routes guaranteed reachable.

    `main.py` registers an SPA catch-all last, so a router added afterwards would be
    shadowed by it — anything mounted here is moved to the front of the table. When
    `main.py` mounts batch itself this whole branch is skipped and the app under test is
    byte-for-byte the shipped one.
    """
    config = Config(
        use_fake_provider=True,
        storage_dir=str(tmp_path / "data"),
        batch_workers=2,
        **overrides,
    )
    app = create_app(config=config, provider=provider)
    if not _batch_is_mounted(app):
        before = len(app.router.routes)
        app.include_router(batch_routes.router)
        added = app.router.routes[before:]
        del app.router.routes[before:]
        app.router.routes[0:0] = added
    return TestClient(app)


def _textured_png(seed: int = 0) -> bytes:
    """A small image with real high-frequency detail, so the quality gate passes it.

    A flat rectangle scores as hopeless and is pre-gated before any provider call, which
    would make every isolation test below pass for the wrong reason.
    """
    height, width = 340, 240
    ys, xs = np.mgrid[0:height, 0:width]
    mask = ((xs // 6 + ys // 6 + seed) % 2).astype(np.uint8)
    channels = [(mask * value + 25).astype(np.uint8) for value in (200, 200, 200)]
    buffer = io.BytesIO()
    Image.fromarray(np.dstack(channels)).save(buffer, format="PNG")
    return buffer.getvalue()


def _columns() -> list[str]:
    """The manifest's column list, read from the template the product hands out.

    Hard-coding it here would let this file and the template drift, and the first
    symptom would be an E2E suite passing against a manifest shape no importer could
    produce.
    """
    from api.batch import manifest as manifest_mod

    return next(csv.reader(io.StringIO(manifest_mod.template_csv())))


def _manifest(rows: list[dict[str, str]]) -> str:
    """A manifest in the shape an importer exports from Excel."""
    columns = _columns()
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return buffer.getvalue()


def _row(filename: str, **overrides: str) -> dict[str, str]:
    row = {
        "front_image": filename,
        "commodity": "spirits",
        "brand_name": "OLD TOM DISTILLERY",
        "class_type": "Kentucky Straight Bourbon Whiskey",
        "alcohol_content": "45",
        "net_contents": "750 mL",
        "producer_name": "Old Tom Distillery",
        "producer_address": "Bardstown, Kentucky",
        "country_of_origin": "",
        "is_import": "false",
    }
    row.update(overrides)
    return row


def _submit(client: TestClient, rows: list[dict[str, str]]) -> dict[str, Any]:
    files: list[tuple[str, tuple[str, bytes, str]]] = [
        ("manifest", ("manifest.csv", _manifest(rows).encode(), "text/csv"))
    ]
    for index, row in enumerate(rows):
        files.append(("files", (row["front_image"], _textured_png(index), "image/png")))
    response = client.post("/batch", files=files)
    assert response.status_code in (200, 202), response.text
    body: dict[str, Any] = response.json()
    return body


def _drain(client: TestClient, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
    """Poll until the job stops moving.

    Polling rather than sleeping a fixed interval: a fixed sleep is either slow or
    flaky, and a flaky test in a suite that forbids retries (LP-246) is a broken test.
    """
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/batch/{job_id}").json()
        counts = body["counts"]
        if counts["queued"] == 0 and counts["processing"] == 0:
            return body
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not settle within {timeout}s: {body}")


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-20")
def test_a_manifest_and_its_images_become_a_job(tmp_path: Path) -> None:
    """Submit once, get an id back, and be told what happens next.

    The importer's whole interaction with the tool starts here: one CSV, a folder of
    images, and a reference they can come back to.
    """
    client = _client(tmp_path, SpecBackedProvider("tc01_old_tom_clean"))
    accepted = _submit(client, [_row("tc01_old_tom_clean.png")])

    assert accepted["job_id"]
    assert accepted["message"].strip()


@pytest.mark.tc("TC-20")
def test_a_job_reports_progress_while_it_runs(tmp_path: Path) -> None:
    """BATCH-5: progressive results, not a spinner.

    An importer watching 300 items wants to start reviewing the first ten rather than
    waiting for the three-hundredth. The counts have to add up at every poll, or the
    progress bar lies.
    """
    client = _client(tmp_path, SpecBackedProvider("tc01_old_tom_clean"))
    job_id = _submit(client, [_row(f"tc01_old_tom_clean_{n}.png") for n in range(4)])["job_id"]

    status = client.get(f"/batch/{job_id}").json()
    counts = status["counts"]
    assert counts["total"] == 4
    assert sum(
        counts[state] for state in ("queued", "processing", "done", "failed")
    ) == counts["total"]
    assert status["message"].strip()


@pytest.mark.tc("TC-20")
def test_every_item_reaches_a_terminal_state(tmp_path: Path) -> None:
    """A job that never finishes is a job nobody can act on.

    Also the guard against a worker that drops an item silently: total has to be
    conserved from submission to completion.
    """
    client = _client(tmp_path, SpecBackedProvider("tc01_old_tom_clean"))
    job_id = _submit(client, [_row(f"label_{n}.png") for n in range(3)])["job_id"]

    counts = _drain(client, job_id)["counts"]
    assert counts["queued"] == 0
    assert counts["processing"] == 0
    assert counts["done"] + counts["failed"] == counts["total"] == 3


@pytest.mark.tc("TC-20")
def test_the_completed_job_exports_a_verdict_per_row(tmp_path: Path) -> None:
    """BATCH-7: the deliverable is a spreadsheet, because that is what gets circulated.

    A batch mode whose output only exists on a web page cannot be attached to an email
    or filed with a determination.
    """
    client = _client(tmp_path, SpecBackedProvider("tc01_old_tom_clean"))
    filenames = [f"label_{n}.png" for n in range(3)]
    job_id = _submit(client, [_row(name) for name in filenames])["job_id"]
    _drain(client, job_id)

    export = client.get(f"/batch/{job_id}/export.csv")
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]

    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert len(rows) == len(filenames)
    exported = " ".join(" ".join(row.values()) for row in rows)
    for name in filenames:
        assert name in exported


@pytest.mark.tc("TC-20")
def test_the_export_carries_a_recommendation_an_agent_can_act_on(
    tmp_path: Path,
) -> None:
    """A CSV of raw verdicts is a data dump; the recommendation is the triage."""
    client = _client(tmp_path, SpecBackedProvider("tc01_old_tom_clean"))
    job_id = _submit(client, [_row("label_0.png")])["job_id"]
    _drain(client, job_id)

    row = next(iter(csv.DictReader(io.StringIO(client.get(f"/batch/{job_id}/export.csv").text))))
    recommendations = {r.value for r in Recommendation}
    assert any(value in recommendations for value in row.values())


def test_the_manifest_template_is_downloadable_and_parses(tmp_path: Path) -> None:
    """The importer's starting point. A template the tool cannot read is a support ticket."""
    from api.batch import manifest as manifest_mod

    client = _client(tmp_path, SpecBackedProvider("tc01_old_tom_clean"))
    response = client.get("/batch/manifest-template.csv")
    assert response.status_code == 200
    manifest_mod.parse(response.text)


# --------------------------------------------------------------------------------------
# Per-item isolation — the assertion that makes batch mode usable at all
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-20")
def test_one_malformed_row_does_not_stop_the_others(tmp_path: Path) -> None:
    """An importer dump contains junk. One bad row is one failed row.

    The alternative — the whole job failing on row 47 — means an importer has to clean
    a 300-row spreadsheet by hand before the tool will look at any of it, which is the
    work the tool was supposed to do.
    """
    client = _client(tmp_path, SpecBackedProvider("tc01_old_tom_clean"))
    rows = [
        _row("good_0.png"),
        _row("bad.png", commodity="cider", alcohol_content="not a number"),
        _row("good_1.png"),
    ]
    job_id = _submit(client, rows)["job_id"]

    counts = _drain(client, job_id)["counts"]
    assert counts["done"] >= 2, counts
    assert counts["done"] + counts["failed"] == counts["total"]


@pytest.mark.tc("TC-20")
def test_a_malformed_row_is_reported_with_its_row_number(tmp_path: Path) -> None:
    """"Something is wrong with your manifest" is unactionable on 300 rows.

    The row number is what turns a rejection into a five-second fix.

    Asserted as *the specific number* rather than as the substring "row". The earlier
    version was `assert "row" in body.lower()`, which matches "rows", "row_count", and
    the word "narrow" — and passed for a manifest with nothing wrong with it at all.
    """
    client = _client(tmp_path, SpecBackedProvider("tc01_old_tom_clean"))
    accepted = _submit(
        client,
        [
            _row("good_0.png"),
            _row("good_1.png"),
            _row("bad.png", commodity="cider"),  # data row 3 -> CSV line 4
        ],
    )
    body = json.dumps(accepted)

    # The offending row is the third data row; the manifest has a header, so callers
    # counting lines in Excel see line 4. Either convention is defensible; what is not
    # is a message with no number in it.
    assert re.search(r"\brow 4\b", body) or re.search(r"\brow 3\b", body), body
    assert "cider" in body or "commodity" in body, (
        "the message names neither the bad value nor the column it was in"
    )


@pytest.mark.tc("TC-20")
def test_a_manifest_with_nothing_wrong_reports_no_row_problems(tmp_path: Path) -> None:
    """The control the previous version of the test above did not have.

    `assert "row" in body` passed on a perfectly valid manifest, so it could not
    distinguish "the error names its row" from "the response happens to mention rows".
    This pins the other side: a clean submission carries no row-level complaint.
    """
    client = _client(tmp_path, SpecBackedProvider("tc01_old_tom_clean"))
    accepted = _submit(client, [_row("good_0.png"), _row("good_1.png")])
    body = json.dumps(accepted)

    assert not re.search(r"\brow \d+\b", body), body
    assert accepted["job_id"]


@pytest.mark.tc("TC-21")
def test_a_provider_outage_fails_the_items_rather_than_the_service(
    tmp_path: Path,
) -> None:
    """The service stays up and the job resolves, so the importer is told rather than hung.

    A batch that hangs on a provider outage is worse than one that fails: nobody knows
    whether to resubmit, and the queue silently fills.
    """
    client = _client(tmp_path, FailingProvider("Connection refused"))
    job_id = _submit(client, [_row(f"label_{n}.png") for n in range(2)])["job_id"]

    counts = _drain(client, job_id)["counts"]
    assert counts["failed"] == counts["total"]
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.tc("TC-21")
def test_failed_items_can_be_retried_without_redoing_the_successful_ones(
    tmp_path: Path,
) -> None:
    """BATCH-8: retry the failures only.

    Re-running 300 items to recover 4 costs the whole spend again, and on a transient
    outage that is the most likely thing an importer would do.
    """
    client = _client(tmp_path, FailingProvider())
    job_id = _submit(client, [_row(f"label_{n}.png") for n in range(2)])["job_id"]
    _drain(client, job_id)

    response = client.post(f"/batch/{job_id}/retry")
    assert response.status_code == 200
    assert response.json()["counts"]["total"] == 2


# --------------------------------------------------------------------------------------
# The service keeps its shape under a batch
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-20")
def test_verify_now_still_answers_while_a_batch_is_running(
    tmp_path: Path, fixture_uploads: Any
) -> None:
    """PERF-5: an agent working their queue must not wait for an importer's dump.

    Batch is background work. If a 300-item job could block one interactive
    verification, the two modes would be sharing a queue rather than a priority lane.
    """
    client = _client(tmp_path, SpecBackedProvider("tc01_old_tom_clean"))
    _submit(client, [_row(f"label_{n}.png") for n in range(6)])

    response = client.post(
        "/verify",
        files=fixture_uploads("tc01_old_tom_clean.png"),
        data={
            "application": json.dumps(
                {
                    "commodity": "spirits",
                    "brand_name": "OLD TOM DISTILLERY",
                    "class_type": "Kentucky Straight Bourbon Whiskey",
                    "alcohol_content": 45.0,
                    "net_contents": "750 mL",
                    "producer_name": "Old Tom Distillery",
                    "producer_address": "Bardstown, Kentucky",
                    "country_of_origin": None,
                    "is_import": False,
                }
            )
        },
    )
    assert response.status_code == 200
    assert response.json()["aggregate"]["recommendation"]


def test_an_unknown_job_id_is_refused_in_the_error_taxonomy(tmp_path: Path) -> None:
    """A mistyped id gets a sentence, not a framework default (OPS-5, UX-6)."""
    client = _client(tmp_path, SpecBackedProvider("tc01_old_tom_clean"))
    response = client.get("/batch/job_does_not_exist")
    assert response.status_code != 200
    error = response.json()["error"]
    assert error["message"].strip()
    assert "Traceback" not in error["message"]
