"""Everything `api/` imports must be in the runtime image (LP-136, ENG-6).

The failure this exists for, in the order it happened:

1. `api/provider/fake.py` grew `from fixtures.generator.layout import FIELD_BANDS`.
2. The `Dockerfile` copies `fixtures/generator/` file by file — deliberately, so the
   image carries the demo and not the whole fixture corpus — and nobody added `layout.py`.
3. The image built, booted, served every screen, and answered `500 ModuleNotFoundError`
   on the first sample click. `/health` stayed green because it touches nothing;
   `/ready` went `503` because it constructs the provider.
4. Nothing in CI saw it. The suite imports from the source tree, where `layout.py` is
   right there, and the deployed app runs with a real key and never imports the fake
   provider at all.

So the gap is specifically between *what shipped code imports* and *what the image
contains*, and no test that runs against the source tree can see it. This one parses the
`Dockerfile`'s COPY instructions for what lands in the image, walks the import closure of
`api/` for what is needed, and fails on the difference.

**What it does not cover, stated rather than assumed.** It follows `import` statements
only. A file the application opens by path at runtime — a font, a JSON fixture, an
image — is invisible to it, and those stay guarded by `tests/test_deploy_config.py` and
by the smoke test. It also only looks at first-party packages; third-party ones are
`pip install`'s problem and a missing one fails the build loudly.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"

#: First-party top-level packages. Anything else in an import is third-party and is
#: installed by pip rather than copied, so it is out of scope here.
FIRST_PARTY = ("api", "fixtures", "golden", "eval", "scripts")

#: Packages the runtime image copies wholesale rather than file by file.
_WHOLESALE = ("api",)


def _copied_paths() -> set[str]:
    """Repo-relative paths the Dockerfile copies into the runtime stage.

    Line continuations are joined first: the `fixtures/generator/` COPY spans four lines
    and reading them one at a time is how a multi-line list gets half-parsed.
    """
    text = DOCKERFILE.read_text()
    text = re.sub(r"\\\s*\n\s*", " ", text)

    copied: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        if "--from=web" in stripped or "--from=deps" in stripped:
            continue  # build stages, not repository files
        parts = [p for p in stripped.split() if not p.startswith("--") and p != "COPY"]
        for source in parts[:-1]:  # the last token is the destination
            copied.add(source.rstrip("/"))
    return copied


def _module_file(module: str) -> Path | None:
    """`fixtures.generator.layout` -> the file that would satisfy it, if it exists."""
    candidate = ROOT / Path(*module.split("."))
    if candidate.with_suffix(".py").is_file():
        return candidate.with_suffix(".py")
    if (candidate / "__init__.py").is_file():
        return candidate / "__init__.py"
    return None


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return {name for name in found if name.split(".")[0] in FIRST_PARTY}


def _closure() -> set[str]:
    """Every first-party module reachable from `api/`, transitively."""
    seen: set[str] = set()
    queue = [
        ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for path in sorted((ROOT / "api").rglob("*.py"))
        if "__pycache__" not in path.parts
    ]
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_file(module)
        if path is None:
            continue
        for imported in _imports_of(path):
            if imported not in seen:
                queue.append(imported)
            # `from fixtures.generator.layout import X` names the module; `from
            # fixtures.generator import layout` names its parent, so walk up too.
            parts = imported.split(".")
            for depth in range(1, len(parts)):
                parent = ".".join(parts[:depth])
                if parent not in seen:
                    queue.append(parent)
    return seen


def _is_shipped(module: str, copied: set[str]) -> bool:
    if module.split(".")[0] in _WHOLESALE:
        return True
    path = _module_file(module)
    if path is None:
        return True  # not a real module in this tree; nothing to ship
    relative = path.relative_to(ROOT).as_posix()
    if relative in copied:
        return True
    # A copied directory covers everything under it.
    return any(
        relative.startswith(f"{entry}/") for entry in copied if not entry.endswith(".py")
    )


def test_every_module_api_imports_is_in_the_runtime_image() -> None:
    """The regression that shipped: a `from fixtures.…` with no matching COPY."""
    copied = _copied_paths()
    missing = sorted(
        module for module in _closure() if not _is_shipped(module, copied)
    )
    assert not missing, (
        "These modules are imported from `api/` but are not copied into the runtime "
        "image, so the container will answer 500 the first time the code path runs:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd them to the COPY block in the Dockerfile, or stop importing them from "
        "shipped code."
    )


def test_the_layout_module_is_the_case_this_guards() -> None:
    """The specific file that shipped broken, pinned so the guard cannot rot silently.

    If `api/provider/fake.py` ever stops importing it this test should be deleted along
    with the COPY line, rather than left asserting a coupling nobody has.
    """
    fake = ROOT / "api" / "provider" / "fake.py"
    if "fixtures.generator.layout" not in fake.read_text():
        pytest.skip("api/provider/fake.py no longer imports fixtures.generator.layout")
    assert "fixtures/generator/layout.py" in _copied_paths()
