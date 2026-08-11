"""Upload ingest. Every uploaded byte is hostile input (SEC-5)."""

import asyncio
import io
import threading
import time
from unittest import mock

import pytest
from PIL import Image

from api import errors
from api.config import Config
from api.pipeline import ingest
from api.pipeline.ingest import MediaType
from fixtures.generator.catalog import by_name
from fixtures.generator.render import render


@pytest.fixture
def config() -> Config:
    return Config(target_long_edge_px=2576, max_images=4, max_pdf_pages=5)


def png_bytes(width: int = 1000, height: int = 1400) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (250, 248, 242)).save(buf, "PNG")
    return buf.getvalue()


def jpeg_with_exif(orientation: int = 6) -> bytes:
    """A portrait photo tagged as needing rotation — what a phone produces."""
    buf = io.BytesIO()
    image = Image.new("RGB", (1600, 1200), (200, 190, 180))
    exif = image.getexif()
    exif[274] = orientation           # Orientation
    exif[34853] = {1: "N", 2: (37.0, 46.0, 30.0)}  # GPSInfo
    exif[271] = "TestPhone"           # Make
    image.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


# --- content sniffing -------------------------------------------------------------------

@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\xff\xd8\xff\xe0rest", MediaType.JPEG),
        (b"\x89PNG\r\n\x1a\nrest", MediaType.PNG),
        (b"%PDF-1.7 rest", MediaType.PDF),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", MediaType.WEBP),
        (b"\x00\x00\x00\x18ftypheic", MediaType.HEIC),
    ],
)
def test_sniffs_from_magic_bytes(data: bytes, expected: MediaType) -> None:
    assert ingest.sniff(data) is expected


def test_extension_is_never_trusted(config: Config) -> None:
    """A renamed executable is still an executable (SEC-5)."""
    with pytest.raises(errors.UserError, match="program"):
        ingest.sniff(b"MZ\x90\x00" + b"\x00" * 100)


@pytest.mark.parametrize(
    ("data", "described"),
    [
        (b"PK\x03\x04payload", "zip"),
        (b"GIF89a", "GIF"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "SVG"),
        (b"<!DOCTYPE html><html>", "web page"),
    ],
)
def test_unsupported_types_are_named_so_the_agent_can_act(data: bytes, described: str) -> None:
    with pytest.raises(errors.UserError) as exc:
        ingest.sniff(data)
    assert described.lower() in exc.value.message.lower()


def test_scripted_svg_is_rejected_outright() -> None:
    """Not re-encoded, not sandboxed — refused."""
    with pytest.raises(errors.UserError):
        ingest.sniff(b"<svg onload='alert(1)'>")


# --- EXIF: orient then strip --------------------------------------------------------------

def test_exif_orientation_is_applied(config: Config) -> None:
    """Orientation 6 means rotate 90 — the output must be portrait."""
    result = ingest.ingest_one(jpeg_with_exif(6), config)[0]
    assert result.height > result.width


def test_all_metadata_is_stripped_including_gps(config: Config) -> None:
    """SEC-3 — phone photos of labels leak location."""
    result = ingest.ingest_one(jpeg_with_exif(), config)[0]
    with Image.open(io.BytesIO(result.data)) as out:
        assert not out.getexif()
        assert "exif" not in out.info
        assert "icc_profile" not in out.info


def test_gps_is_gone_from_the_raw_bytes(config: Config) -> None:
    """Belt and braces: the tag must not survive anywhere in the output."""
    result = ingest.ingest_one(jpeg_with_exif(), config)[0]
    assert b"TestPhone" not in result.data


# --- re-encode: the polyglot defence -------------------------------------------------------

def test_appended_payload_does_not_survive(config: Config) -> None:
    """LP-252 — output is pixels this process drew, not the uploaded container."""
    polyglot = png_bytes() + b"<?php system($_GET['c']); ?>"
    result = ingest.ingest_one(polyglot, config)[0]
    assert b"<?php" not in result.data


def test_output_is_always_reencoded_even_when_unchanged(config: Config) -> None:
    original = png_bytes(800, 600)
    result = ingest.ingest_one(original, config)[0]
    assert result.data != original


# --- downscale ------------------------------------------------------------------------------

def test_oversized_images_are_downscaled_to_the_target(config: Config) -> None:
    result = ingest.ingest_one(png_bytes(5000, 3000), config)[0]
    assert max(result.width, result.height) == config.target_long_edge_px
    assert result.was_downscaled


def test_aspect_ratio_is_preserved(config: Config) -> None:
    result = ingest.ingest_one(png_bytes(4000, 2000), config)[0]
    assert result.width / result.height == pytest.approx(2.0, abs=0.01)


def test_small_images_are_never_upscaled(config: Config) -> None:
    """Upscaling invents detail. A small image stays small and is flagged elsewhere."""
    result = ingest.ingest_one(png_bytes(600, 800), config)[0]
    assert (result.width, result.height) == (600, 800)
    assert not result.was_downscaled


def test_downscale_target_keeps_the_high_resolution_tier(config: Config) -> None:
    """Below 1568 loses the capability the model was chosen for."""
    result = ingest.ingest_one(png_bytes(9000, 6000), config)[0]
    assert max(result.width, result.height) >= 1568


# --- caps -------------------------------------------------------------------------------------

def test_oversized_file_is_rejected_with_the_limit(config: Config) -> None:
    small = Config(max_image_bytes=1024)
    with pytest.raises(errors.UserError, match="MB"):
        ingest.ingest_one(png_bytes(), small)


def test_empty_file_is_rejected(config: Config) -> None:
    with pytest.raises(errors.UserError, match="empty"):
        ingest.ingest_one(b"", config)


def test_no_images_is_rejected(config: Config) -> None:
    with pytest.raises(errors.UserError, match="No images"):
        ingest.ingest([], config)


def test_too_many_images_is_rejected(config: Config) -> None:
    with pytest.raises(errors.UserError, match="more than"):
        ingest.ingest([png_bytes() for _ in range(6)], config)


def test_corrupt_image_reports_an_image_error_not_a_crash(config: Config) -> None:
    corrupt = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    with pytest.raises(errors.ImageError, match="damaged|could not be opened"):
        ingest.ingest_one(corrupt, config)


# --- multi-file ---------------------------------------------------------------------------------

def test_indices_are_assigned_in_order(config: Config) -> None:
    results = ingest.ingest([png_bytes(), png_bytes()], config)
    assert [r.index for r in results] == [0, 1]


def test_real_fixture_ingests_cleanly(config: Config) -> None:
    buf = io.BytesIO()
    render(by_name("tc01_old_tom_clean")).save(buf, "PNG")
    result = ingest.ingest_one(buf.getvalue(), config)[0]
    assert result.width > 0 and result.metadata_stripped


def test_ingested_image_decodes_for_quality_scoring(config: Config) -> None:
    from api.pipeline import quality

    buf = io.BytesIO()
    render(by_name("tc01_old_tom_clean")).save(buf, "PNG")
    result = ingest.ingest_one(buf.getvalue(), config)[0]
    assert quality.assess(ingest.to_array(result)).verdict == "ok"


# --- PDF label proofs (LP-057) -------------------------------------------------------------------

def _pdf_bytes(pages: int = 1) -> bytes:
    """A minimal multi-page PDF, built without a writer library."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + b" ".join(
            f"{3 + i} 0 R".encode() for i in range(pages)
        ) + f"] /Count {pages} >>".encode(),
    ]
    for _ in range(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> >>"
        )

    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n".encode()
        + b"%%EOF\n"
    )
    return bytes(out)


def test_pdf_renders_one_image_per_page(config: Config) -> None:
    results = ingest.ingest_one(_pdf_bytes(pages=2), config)
    assert len(results) == 2
    assert [r.page for r in results] == [1, 2]


def test_pdf_pages_are_indexed_continuously(config: Config) -> None:
    results = ingest.ingest_one(_pdf_bytes(pages=3), config, index=5)
    assert [r.index for r in results] == [5, 6, 7]


def test_pdf_page_cap_is_enforced_before_rendering(config: Config) -> None:
    """A 500-page PDF must be refused, not rendered (SEC-5)."""
    capped = Config(max_pdf_pages=2)
    with pytest.raises(errors.UserError, match="pages"):
        ingest.ingest_one(_pdf_bytes(pages=5), capped)


def test_malformed_pdf_reports_a_user_error(config: Config) -> None:
    with pytest.raises(errors.UserError, match="PDF"):
        ingest.ingest_one(b"%PDF-1.7\nthis is not a pdf", config)


def test_pdf_output_carries_its_source_type(config: Config) -> None:
    result = ingest.ingest_one(_pdf_bytes(), config)[0]
    assert result.source_media_type is MediaType.PDF


# --- off the event loop -------------------------------------------------------------------
#
# Ingest and quality scoring measured ~700ms for a two-image upload of 2400x3360 PNGs.
# Run inline on an async server that is 700ms during which every other request in the
# process is frozen, so two agents submitting at once take 700ms and 1400ms rather than
# 700ms each. Invisible in single-user testing, which is where it would have stayed.


def test_ingest_does_not_run_on_the_event_loop(config: Config) -> None:
    """Asked directly: is there a running loop in the thread doing the work? If there is,
    the work is on the loop and every other request is waiting for it."""
    where: dict[str, bool] = {}
    real = ingest.ingest

    def spy(files: list[bytes], cfg: Config) -> list[ingest.IngestedImage]:
        try:
            asyncio.get_running_loop()
            where["on_loop"] = True
        except RuntimeError:
            where["on_loop"] = False
        return real(files, cfg)

    with mock.patch.object(ingest, "ingest", spy):
        asyncio.run(ingest.ingest_async([png_bytes()], config))

    assert where["on_loop"] is False


def test_quality_scoring_does_not_run_on_the_event_loop(config: Config) -> None:
    where: dict[str, bool] = {}
    real = ingest.assess

    def spy(images: list[ingest.IngestedImage]) -> list[object]:
        try:
            asyncio.get_running_loop()
            where["on_loop"] = True
        except RuntimeError:
            where["on_loop"] = False
        return real(images)

    with mock.patch.object(ingest, "assess", spy):
        asyncio.run(ingest.assess_async(ingest.ingest([png_bytes()], config)))

    assert where["on_loop"] is False


def test_two_uploads_overlap_instead_of_queueing(config: Config) -> None:
    """The claim, proved without a stopwatch.

    A barrier of two only releases when two threads are inside the work at the same
    moment. Serialized, the first call waits for a partner that cannot arrive until it
    returns, the barrier times out, and this fails. No timing assertion, so no flake.
    """
    barrier = threading.Barrier(2, timeout=15)
    real = ingest.ingest

    def gated(files: list[bytes], cfg: Config) -> list[ingest.IngestedImage]:
        barrier.wait()
        return real(files, cfg)

    async def both() -> list[list[ingest.IngestedImage]]:
        return list(
            await asyncio.gather(
                ingest.ingest_async([png_bytes()], config),
                ingest.ingest_async([png_bytes()], config),
            )
        )

    with mock.patch.object(ingest, "ingest", gated):
        results = asyncio.run(both())

    assert len(results) == 2


def test_the_event_loop_keeps_serving_while_ingest_runs(config: Config) -> None:
    """The other half of the same claim: the loop is free to do work meanwhile.

    Driven by a sleeping stand-in rather than a real decode, so the tick count does not
    depend on how fast the machine is. `time.sleep` releases the GIL, which is exactly
    what the real OpenCV and Pillow calls do.
    """
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    def slow(files: list[bytes], cfg: Config) -> list[ingest.IngestedImage]:
        time.sleep(0.25)
        return ingest.ingest_one(files[0], cfg)

    async def main() -> None:
        beat = asyncio.create_task(heartbeat())
        await ingest.ingest_async([png_bytes()], config)
        beat.cancel()

    with mock.patch.object(ingest, "ingest", slow):
        asyncio.run(main())

    assert ticks >= 5


def test_the_async_wrappers_return_what_the_sync_ones_do(config: Config) -> None:
    """Moving work to a thread must not change the answer."""
    data = [png_bytes(1200, 900)]
    sync = ingest.ingest(data, config)
    from_thread = asyncio.run(ingest.ingest_async(data, config))
    assert [i.data for i in sync] == [i.data for i in from_thread]


def test_errors_still_surface_through_the_wrapper(config: Config) -> None:
    """A UserError raised on a worker thread has to arrive as a UserError, not as a
    concurrent.futures wrapper the error taxonomy has never heard of."""
    with pytest.raises(errors.UserError, match="empty"):
        asyncio.run(ingest.ingest_async([b""], config))


# --- the route actually uses the wrappers ---------------------------------------------
#
# The tests above prove ingest_async and assess_async behave. They say nothing about
# whether anything calls them, and the two lines in api/routes/verify.py that do sit in a
# block several other branches rewrite. A conflict resolved the other way would delete the
# fix and leave every test above green, because they exercise the wrappers directly.
#
# So this drives a real request over HTTP and asserts the route went through them.


def test_the_verify_route_ingests_off_the_event_loop() -> None:
    """Goes red if `verify_endpoint` calls `ingest_mod.ingest` synchronously again."""
    from tests.test_api import label_files, make_client, post_verify

    calls: list[str] = []
    real_ingest = ingest.ingest_async
    real_assess = ingest.assess_async

    async def spy_ingest(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append("ingest_async")
        return await real_ingest(*args, **kwargs)  # type: ignore[arg-type]

    async def spy_assess(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append("assess_async")
        return await real_assess(*args, **kwargs)  # type: ignore[arg-type]

    with (
        mock.patch.object(ingest, "ingest_async", spy_ingest),
        mock.patch.object(ingest, "assess_async", spy_assess),
    ):
        response = post_verify(
            make_client(), files=label_files("tc01_old_tom_clean.png")
        )

    assert response.status_code == 200
    assert calls == ["ingest_async", "assess_async"], (
        "api/routes/verify.py is not going through the off-loop wrappers — a merge has "
        "reverted the event-loop fix"
    )


def test_the_verify_route_does_not_call_the_blocking_ingest_directly() -> None:
    """The same claim from the other side, so neither a revert nor a partial one passes."""
    from tests.test_api import label_files, make_client, post_verify

    on_loop: list[bool] = []
    real = ingest.ingest

    def watch(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    with mock.patch.object(ingest, "ingest", watch):
        response = post_verify(
            make_client(), files=label_files("tc01_old_tom_clean.png")
        )

    assert response.status_code == 200
    assert on_loop == [False], "ingest ran on the event loop during a real request"
