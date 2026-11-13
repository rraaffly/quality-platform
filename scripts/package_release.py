"""Create a source release with the prebuilt frontend and offline evidence."""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT_FILES = ("pyproject.toml", "uv.lock", "config.yaml", "README.md")
TREES = ("src", "tests", "benchmarks", "docs", "web/dist")
SCRIPTS = ("install.ps1", "install.sh", "package_release.py", "smoke_release.py")
EXCLUDED_PARTS = {
    ".venv", ".caches", ".pytest_cache", "__pycache__", "node_modules",
    "real_world", "reports", "tmp_run", ".superpowers",
}


def build_archive(root: Path, destination: Path) -> None:
    """Write only explicit release inputs, rejecting a missing Web build."""
    root = root.resolve()
    if not (root / "web/dist/index.html").is_file():
        raise FileNotFoundError("web/dist/index.html is missing; build the frontend first")
    files = [root / name for name in ROOT_FILES]
    files += [root / "scripts" / name for name in SCRIPTS]
    for tree in TREES:
        files.extend((root / tree).rglob("*"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for file in sorted(set(files)):
            if not file.is_file() or file.is_symlink():
                continue
            relative = file.relative_to(root)
            if any(part in EXCLUDED_PARTS or part.startswith(".env") for part in relative.parts):
                continue
            archive.write(file, Path("quality-platform") / relative)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Destination ZIP path")
    args = parser.parse_args()
    build_archive(Path(__file__).resolve().parents[1], args.output)
    print(args.output.resolve())
