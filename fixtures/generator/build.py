"""Regenerate the fixture set and golden/set.json.

Deterministic: the same specs in, byte-identical PNGs and byte-identical manifest out
(LP-123). Determinism is what makes the accuracy number comparable run over run — if a
regeneration produces a diff without a spec change, the renderer has become
non-reproducible and every historical eval figure is suspect.

**The scope of that guarantee, stated rather than implied.** Byte-identity holds for any
two runs *on the same machine*. It does not hold across machines with different fonts:
`render.py` picks the first available regular/bold family, so a macOS build (Arial) and a
Linux build (DejaVu) rasterise different pixels from the same spec. The manifest records
which family produced its hashes so a mismatch is diagnosable rather than mysterious.

The eval *report* is unaffected either way — it is scored from the specs through the
spec-backed provider, never from the pixels — so `python -m eval.run` is byte-identical
across machines as well as across runs.

    python -m fixtures.generator.build
    python -m fixtures.generator.build --labels-dir /tmp/a --golden /tmp/a/set.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from fixtures.generator.catalog import CATALOG, NOT_GENERATED
from fixtures.generator.render import font_family, render_to
from fixtures.generator.spec import LabelSpec

ROOT = Path(__file__).resolve().parents[2]
LABELS = ROOT / "fixtures" / "labels"
GOLDEN = ROOT / "golden" / "set.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def manifest(specs: list[LabelSpec], labels_dir: Path) -> dict[str, Any]:
    """Render every spec into `labels_dir` and describe the result."""
    entries = []
    for spec in specs:
        written = render_to(spec, labels_dir)
        entries.append(
            {
                "name": spec.name,
                "commodity": spec.commodity,
                "images": [p.name for p in written],
                "sha256": {p.name: _sha(p) for p in written},
                "application": spec.application(),
                "expect": spec.expect,
                "expect_findings": spec.expect_findings,
                "pending": spec.pending,
                "notes": spec.notes,
            }
        )

    return {
        "tier": "A",
        "description": (
            "Synthetic fixtures, deterministic and CI-gating. Tier B (real bottle "
            "photographs) lives in golden/tier_b/ and is reported separately, never gating."
        ),
        # Recorded so a hash mismatch between machines is diagnosable: different font
        # family, same specs. It is not a defect, and it does not move the eval number.
        "rendered_with": {"font_family": Path(font_family()).name},
        "not_generated": NOT_GENERATED,
        "fixtures": entries,
    }


def serialise(body: dict[str, Any]) -> str:
    """One serialisation, so two builds cannot differ in whitespace or key order."""
    return json.dumps(body, indent=2) + "\n"


def build(
    specs: list[LabelSpec] | None = None,
    labels_dir: Path | None = None,
    golden_path: Path | None = None,
) -> dict[str, Any]:
    """Render the fixtures and write the manifest. Returns the manifest body."""
    specs = list(CATALOG) if specs is None else specs
    labels_dir = LABELS if labels_dir is None else labels_dir
    golden_path = GOLDEN if golden_path is None else golden_path

    body = manifest(specs, labels_dir)
    golden_path.parent.mkdir(parents=True, exist_ok=True)
    golden_path.write_text(serialise(body))
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fixtures.generator.build",
        description="Regenerate the Tier A fixture images and golden/set.json.",
    )
    parser.add_argument("--labels-dir", type=Path, default=LABELS)
    parser.add_argument("--golden", type=Path, default=GOLDEN)
    args = parser.parse_args(argv)

    body = build(labels_dir=args.labels_dir, golden_path=args.golden)
    images = sum(len(e["images"]) for e in body["fixtures"])
    print(f"rendered {images} images across {len(body['fixtures'])} fixtures "
          f"-> {args.labels_dir}")
    print(f"wrote {args.golden}  (font: {body['rendered_with']['font_family']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
