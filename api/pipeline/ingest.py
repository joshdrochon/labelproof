"""Upload ingest — treat every uploaded byte as hostile (SEC-5).

Order is fixed and each step exists for a reason:

1. **Sniff the type from magic bytes**, never the filename. An extension is a claim by
   whoever uploaded the file.
2. **Enforce caps** before decoding. A decompression bomb is a small file until you open it.
3. **Auto-orient from EXIF**, then **strip all metadata** — including GPS. Phone photos of
   labels leak location (SEC-3).
4. **Always re-encode.** This is what neutralizes a polyglot: whatever else was in the
   container, the output is pixels this process drew.
5. **Downscale to the target long edge, never upscale.**

That last one carries a constraint worth stating: the target is 2576px because that is the
high-resolution vision tier, and dropping below it is what makes small warning text
illegible. Downscaling further would save upload bytes and cost the one field that must
never be misread.

**All of this is CPU-bound, and none of it may run on the event loop.** Measured on two
2400x3360 PNGs: 535ms to decode, resize and re-encode, then 173ms of quality scoring on
top. On an async server that is ~700ms during which *every other request in the process is
frozen*, so two agents submitting at once do not take 700ms each, they take 700 and 1400.
Against a five-second budget that is not a rounding error, and it is invisible in
single-user testing, which is where it would have stayed. `ingest_async` and `assess_async`
move the work to a worker thread; the synchronous functions stay exactly as they were,
because the batch worker is already off the loop and does not need a second hop.
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from enum import StrEnum

import pillow_heif
import pypdfium2
from PIL import Image, ImageOps

from api import errors
from api.config import Config
from api.models import ImageQuality

pillow_heif.register_heif_opener()

#: Guard against decompression bombs. Pillow warns above this and errors at 2x.
Image.MAX_IMAGE_PIXELS = 80_000_000


class MediaType(StrEnum):
    JPEG = "image/jpeg"
    PNG = "image/png"
    WEBP = "image/webp"
    HEIC = "image/heic"
    PDF = "application/pdf"


#: Magic-byte signatures. Order matters — check longer prefixes first.
_SIGNATURES: list[tuple[bytes, MediaType]] = [
    (b"\xff\xd8\xff", MediaType.JPEG),
    (b"\x89PNG\r\n\x1a\n", MediaType.PNG),
    (b"%PDF-", MediaType.PDF),
]


def sniff(data: bytes) -> MediaType:
    """Identify content from its bytes. Raises UserError on anything unsupported."""
    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            return media_type

    # RIFF containers: WEBP sits at offset 8.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return MediaType.WEBP

    # ISO-BMFF: HEIC/HEIF brand sits in the ftyp box at offset 4.
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return MediaType.HEIC

    raise errors.unsupported_file_type(_describe(data))


def _describe(data: bytes) -> str:
    """Name what the file appears to be, for a message an agent can act on."""
    if data.startswith(b"PK\x03\x04"):
        return "Word, Excel or zip file"
    if data.startswith(b"GIF8"):
        return "GIF image"
    if data.startswith((b"\x00\x00\x01\x00", b"\x00\x00\x02\x00")):
        return "Windows icon"
    if data.lstrip()[:5].lower() in (b"<html", b"<!doc"):
        return "web page"
    if data.lstrip().startswith(b"<svg") or b"<svg" in data[:512].lower():
        return "SVG image"
    if data.startswith(b"\x7fELF") or data.startswith(b"MZ"):
        return "program"
    return "file of an unrecognised type"


@dataclass(frozen=True)
class IngestedImage:
    """One decoded, sanitized, re-encoded image ready for quality scoring."""

    index: int
    data: bytes
    width: int
    height: int
    source_media_type: MediaType
    page: int | None = None
    was_downscaled: bool = False
    metadata_stripped: bool = True
    media_type: str = "image/png"


def _sanitize(image: Image.Image, config: Config) -> tuple[Image.Image, bool]:
    """Orient, strip, convert, and downscale. Returns the image and whether it shrank."""
    # EXIF orientation first — after this the pixels are upright and the tag is moot.
    image = ImageOps.exif_transpose(image) or image

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    long_edge = max(image.size)
    downscaled = False
    if long_edge > config.target_long_edge_px:
        ratio = config.target_long_edge_px / long_edge
        new_size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
        downscaled = True

    # Rebuild from raw pixel data so nothing from the source container survives —
    # no EXIF, no GPS, no ICC profile, no appended payload.
    clean = Image.frombytes(image.mode, image.size, image.tobytes())
    return clean, downscaled


def _encode(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _from_pdf(data: bytes, config: Config, start_index: int) -> list[IngestedImage]:
    """Render a PDF label proof, one image per page, capped (LP-057).

    Rendered in a constrained path: page cap enforced before rendering, no external
    resource loading, and the output is raster only.
    """
    try:
        document = pypdfium2.PdfDocument(data)
    except Exception as exc:  # noqa: BLE001 — any parse failure is a user-facing error
        raise errors.UserError(
            "That PDF could not be opened. Save it again, or upload the label as an "
            "image instead.",
            next_step="replace",
            code="unreadable_pdf",
        ) from exc

    page_count = len(document)
    if page_count > config.max_pdf_pages:
        raise errors.UserError(
            f"That PDF has {page_count} pages and this tool reads at most "
            f"{config.max_pdf_pages}. Upload just the label pages.",
            next_step="replace",
            code="pdf_too_many_pages",
        )

    out: list[IngestedImage] = []
    for page_number in range(page_count):
        page = document[page_number]
        scale = config.target_long_edge_px / max(page.get_width(), page.get_height())
        rendered = page.render(scale=min(scale, 4.0)).to_pil()
        clean, downscaled = _sanitize(rendered, config)
        out.append(
            IngestedImage(
                index=start_index + page_number,
                data=_encode(clean),
                width=clean.width,
                height=clean.height,
                source_media_type=MediaType.PDF,
                page=page_number + 1,
                was_downscaled=downscaled,
            )
        )
    return out


def ingest_one(data: bytes, config: Config, index: int = 0) -> list[IngestedImage]:
    """Sanitize one uploaded file. A PDF yields one image per page; others yield one."""
    if not data:
        raise errors.UserError(
            "That file is empty. Upload the label artwork for this application.",
            next_step="replace",
            code="empty_file",
        )

    if len(data) > config.max_image_bytes:
        raise errors.file_too_large(config.max_image_bytes // (1024 * 1024))

    media_type = sniff(data)

    if media_type is MediaType.PDF:
        return _from_pdf(data, config, index)

    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            clean, downscaled = _sanitize(opened, config)
    except errors.LabelProofError:
        raise
    except Exception as exc:  # noqa: BLE001 — a corrupt image is a user-facing error
        raise errors.ImageError(
            "That image could not be opened. It may be damaged — try saving it again, "
            "or request a new image.",
            next_step="replace",
            code="corrupt_image",
        ) from exc

    return [
        IngestedImage(
            index=index,
            data=_encode(clean),
            width=clean.width,
            height=clean.height,
            source_media_type=media_type,
            was_downscaled=downscaled,
        )
    ]


def ingest(files: list[bytes], config: Config) -> list[IngestedImage]:
    """Sanitize an upload set, enforcing the image-count cap across expanded PDF pages."""
    if not files:
        raise errors.UserError(
            "No images were uploaded. Add the label artwork for this application.",
            next_step="upload",
            code="no_images",
        )

    out: list[IngestedImage] = []
    for raw in files:
        out.extend(ingest_one(raw, config, index=len(out)))
        if len(out) > config.max_images:
            raise errors.UserError(
                f"That is more than {config.max_images} images. Upload the front and "
                f"back of the label.",
                next_step="reduce",
                code="too_many_images",
            )
    return out


def to_array(image: IngestedImage):  # type: ignore[no-untyped-def]
    """Decode a sanitized image for quality scoring."""
    import numpy as np

    with Image.open(io.BytesIO(image.data)) as opened:
        return np.array(opened.convert("RGB"))


def assess(images: list[IngestedImage]) -> list[ImageQuality]:
    """Decode and quality-score a sanitized upload set."""
    from api.pipeline import quality

    return [quality.assess(to_array(image)) for image in images]


# --------------------------------------------------------------------------------------
# Off the event loop
# --------------------------------------------------------------------------------------
#
# Both wrappers hand the whole batch to one worker thread rather than one thread per
# image. The work is already vectorised inside OpenCV and Pillow, which release the GIL,
# so the win being chased here is *concurrent requests overlapping*, not one request
# getting faster. Fanning a single upload across four threads would burn four workers of a
# shared pool to shave milliseconds off one agent's request while the next agent waits.


async def ingest_async(files: list[bytes], config: Config) -> list[IngestedImage]:
    """`ingest`, on a worker thread. Same errors, same order, same output."""
    return await asyncio.to_thread(ingest, files, config)


async def assess_async(images: list[IngestedImage]) -> list[ImageQuality]:
    """`assess`, on a worker thread."""
    return await asyncio.to_thread(assess, images)
