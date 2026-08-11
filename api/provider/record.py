"""Fixture recorder — capture real provider responses as replayable fixtures (LP-064).

    python -m api.provider.record --commodity spirits fixtures/labels/tc01_old_tom_clean.png
    python -m api.provider.record --commodity wine --key tc17_table_wine \\
        fixtures/labels/tc17_table_wine.png

This is the only place in the repository that deliberately makes a live model call, and
it is a developer tool run by hand — never imported by the app, never touched by a test
(ENG-3). What it writes is exactly what `RecordedProvider` reads back, so a fixture
recorded today is a deterministic, offline test input forever after.

Why this exists at all: `SpecBackedProvider` proves our rules are right, because it
derives its answer from the ground truth that drew the image. It cannot prove the
pipeline survives real model output — the hedged transcription, the odd capitalisation,
the confidence that is not 0.95. Only a recording does that.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from api import logging as lp_logging
from api.config import Config, ConfigError
from api.models import Commodity
from api.provider.anthropic_adapter import AnthropicVisionProvider, estimated_usd
from api.provider.base import ExtractionRequest, ImageInput, ProviderError, ProviderUsage
from api.provider.fake import spec_name_for_image

#: Where `RecordedProvider` looks by default.
DEFAULT_OUTPUT_DIR = Path("fixtures/recorded")

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def media_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in _MEDIA_TYPES:
        raise ValueError(
            f"{path.name} is a {suffix or 'file with no'} extension, which the vision "
            f"API cannot read. Use one of: {', '.join(sorted(_MEDIA_TYPES))}."
        )
    return _MEDIA_TYPES[suffix]


def role_for(path: Path) -> str:
    """Infer front/back/single from the filename, matching the generator's convention."""
    stem = path.stem.lower()
    if stem.endswith("_front"):
        return "front"
    if stem.endswith("_back"):
        return "back"
    return "single"


def default_key(paths: list[Path]) -> str:
    """`tc16_front_back_front.png` records under `tc16_front_back`."""
    name = spec_name_for_image(paths[0].name)
    return name or paths[0].stem


def build_request(commodity: Commodity, paths: list[Path]) -> ExtractionRequest:
    images = [
        ImageInput(
            index=index,
            data=path.read_bytes(),
            media_type=media_type_for(path),
            role=role_for(path),
        )
        for index, path in enumerate(paths)
    ]
    return ExtractionRequest(commodity=commodity, images=images)


def record(
    commodity: Commodity,
    paths: list[Path],
    *,
    out_dir: Path = DEFAULT_OUTPUT_DIR,
    key: str | None = None,
    config: Config | None = None,
) -> Path:
    """Call the real provider once and write the response as a fixture."""
    config = config or Config.from_env()
    provider = AnthropicVisionProvider(config)
    response = provider.extract(build_request(commodity, paths))

    payload = {
        "commodity": commodity.value,
        "sources": [str(path) for path in paths],
        "extractions": [extraction.model_dump(mode="json") for extraction in response.extractions],
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_tokens": response.usage.cache_read_tokens,
            "model": response.usage.model,
        },
        "latency_ms": response.latency_ms,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / f"{key or default_key(paths)}.json"
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m api.provider.record",
        description="Record a real provider response as a replayable fixture (LP-064).",
    )
    parser.add_argument("images", nargs="+", type=Path, help="One or more label images.")
    parser.add_argument(
        "--commodity",
        required=True,
        choices=[c.value for c in Commodity],
        help="Which rule set applies to this label.",
    )
    parser.add_argument(
        "--key",
        default=None,
        help="Fixture name. Defaults to the first image's fixture name.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to write into (default: {DEFAULT_OUTPUT_DIR}).",
    )
    args = parser.parse_args(argv)

    lp_logging.configure()

    missing = [str(path) for path in args.images if not path.exists()]
    if missing:
        print(f"No such image: {', '.join(missing)}", file=sys.stderr)
        return 2

    try:
        destination = record(
            Commodity(args.commodity), list(args.images), out_dir=args.out, key=args.key
        )
    except ConfigError as exc:
        # The overwhelmingly common case: no API key. Say so in one line rather than a
        # traceback, because this is a tool a human runs from a shell.
        print(str(exc), file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"The provider call failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    payload = json.loads(destination.read_text())
    usage = payload["usage"]
    print(f"Wrote {destination}")
    print(
        f"  {len(payload['extractions'])} image(s), {payload['latency_ms']}ms, "
        f"{usage['input_tokens']} in / {usage['output_tokens']} out / "
        f"{usage['cache_read_tokens']} cached, "
        f"${estimated_usd(ProviderUsage(**usage)):.4f}"
    )
    if usage["cache_read_tokens"] == 0:
        print(
            "  Note: 0 cached tokens. Expected on the first call of a session; if it "
            "stays 0 across repeated runs, something in the system prompt is varying "
            "and the prompt cache has stopped paying (BUILD.md §7)."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry point
    raise SystemExit(main())
