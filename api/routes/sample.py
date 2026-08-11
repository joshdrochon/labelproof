"""`GET /sample` — the one-click demo's payload (LP-088, UX-1).

A grader who has to type nine fields before seeing anything has already formed an opinion
about the product. This endpoint hands the front end a complete, real application: the
Old Tom details from `assets/samples/old_tom.json` and the generated front/back pair of
its label, which together reach a verdict in one click.

The images are served as URLs rather than inlined as base64. The response stays small
enough to be instant, the browser can put them straight in an `<img>`, and fetching them
back as blobs to post to `/verify` is one line. Only the two files named in the manifest
are servable — the path is never assembled from what the caller sent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse

from api import errors

router = APIRouter()

_ROOT = Path(__file__).resolve().parents[2]
_SAMPLE_JSON = _ROOT / "assets" / "samples" / "old_tom.json"
_LABELS = _ROOT / "fixtures" / "labels"

#: The demo pair, in the order an agent would upload them. The warning lives on the back,
#: which is what makes this pair worth demoing rather than a single face (TC-16).
_IMAGES: list[tuple[str, str]] = [
    ("tc16_front_back_front.png", "front"),
    ("tc16_front_back_back.png", "back"),
]

_SERVABLE: frozenset[str] = frozenset(name for name, _ in _IMAGES)


def _load_application() -> tuple[dict[str, Any], str | None]:
    if not _SAMPLE_JSON.exists():
        raise errors.InternalError(
            "The sample application is missing from this build, so the demo cannot "
            "load. Enter an application by hand, or ask whoever runs this service."
        )
    raw: dict[str, Any] = json.loads(_SAMPLE_JSON.read_text())
    note = raw.pop("_source", None)
    application = {k: v for k, v in raw.items() if not k.startswith("_")}
    return application, note


@router.get("/sample")
def sample() -> dict[str, Any]:
    """The Old Tom application plus its label pair, ready to submit to `/verify`."""
    application, note = _load_application()
    missing = [name for name, _ in _IMAGES if not (_LABELS / name).exists()]
    if missing:
        raise errors.InternalError(
            "The sample label images are missing from this build, so the demo cannot "
            "load. Upload a label of your own to try the tool."
        )

    return {
        "application": application,
        "note": note,
        "images": [
            {
                "index": index,
                "role": role,
                "filename": name,
                "media_type": "image/png",
                "url": f"/sample/images/{name}",
            }
            for index, (name, role) in enumerate(_IMAGES)
        ],
    }


@router.get("/sample/images/{name}")
def sample_image(name: str) -> FileResponse:
    """Serve one demo image. Allowlisted by name — never a path built from input."""
    if name not in _SERVABLE:
        raise errors.UserError(
            "That sample image does not exist. Load the sample again from the start.",
            next_step="reload",
            code="unknown_sample_image",
        )
    path = _LABELS / name
    if not path.exists():
        raise errors.InternalError(
            "That sample image is missing from this build. Upload a label of your own "
            "to try the tool."
        )
    return FileResponse(path, media_type="image/png")
