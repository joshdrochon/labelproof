"""Upload ingest. Every uploaded byte is hostile input (SEC-5)."""

import io

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
