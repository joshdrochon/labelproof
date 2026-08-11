"""Regenerate the fixture set and golden/set.json.

Deterministic: same specs in, byte-identical PNGs out (LP-123).

    python -m fixtures.generator.build
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fixtures.generator.catalog import CATALOG, NOT_GENERATED
from fixtures.generator.render import render_to

ROOT = Path(__file__).resolve().parents[2]
LABELS = ROOT / "fixtures" / "labels"
GOLDEN = ROOT / "golden" / "set.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main() -> int:
    entries = []
    for spec in CATALOG:
        written = render_to(spec, LABELS)
        entries.append(
            {
                "name": spec.name,
                "commodity": spec.commodity,
                "images": [p.name for p in written],
                "sha256": {p.name: _sha(p) for p in written},
                "application": {
                    "commodity": spec.commodity,
                    "brand_name": spec.brand_name,
                    "class_type": spec.class_type,
                    "net_contents": spec.net_contents,
                    "producer": spec.producer,
                    "country_of_origin": spec.country_of_origin,
                },
                "expect": spec.expect,
                "notes": spec.notes,
            }
        )

    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(
        json.dumps(
            {
                "tier": "A",
                "description": (
                    "Synthetic fixtures, deterministic and CI-gating. Tier B (real "
                    "bottle photographs) is reported separately and never gates."
                ),
                "not_generated": NOT_GENERATED,
                "fixtures": entries,
            },
            indent=2,
        )
        + "\n"
    )

    print(f"rendered {sum(len(e['images']) for e in entries)} images "
          f"across {len(entries)} fixtures -> {LABELS.relative_to(ROOT)}")
    print(f"wrote {GOLDEN.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
