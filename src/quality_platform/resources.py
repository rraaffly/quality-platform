"""Read-only application resources in a source checkout or an installed wheel."""

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent


def runtime_root() -> Path:
    """Preserve the resource layout without depending on the caller's working directory."""
    if (
        PACKAGE_ROOT.parent.name == "src"
        and (PACKAGE_ROOT.parent.parent / "pyproject.toml").is_file()
    ):
        return PACKAGE_ROOT.parent.parent
    return PACKAGE_ROOT / "_data"
