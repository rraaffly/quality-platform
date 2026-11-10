import hashlib
import os
import tempfile
from pathlib import Path


def publish_source_snapshot(project_root: Path, content: bytes) -> tuple[Path, str]:
    digest = hashlib.sha256(content).hexdigest()
    directory = project_root / ".quality-platform" / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(project_root.resolve()):
        raise ValueError("snapshot directory is outside project root")
    target = directory / f"{digest}.md"
    if target.exists():
        if target.is_symlink() or not target.resolve().is_relative_to(project_root.resolve()):
            raise ValueError("snapshot target is outside project root")
        if target.read_bytes() != content:
            raise ValueError("existing source snapshot does not match its content hash")
        return target, digest

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".tmp-", delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target, digest
