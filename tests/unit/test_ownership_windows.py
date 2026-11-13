import sys
from pathlib import Path

import pytest

from quality_platform.storage.database import open_database
from quality_platform.storage.ownership import writer_lock


def test_writer_lock_uses_one_identity_for_windows_uri_characters(tmp_path: Path) -> None:
    database = tmp_path / "domain ?# space.sqlite"

    with writer_lock(database), pytest.raises(RuntimeError, match="writer already active"), writer_lock(database):
        pass

    with writer_lock(database):
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="Windows filename restriction")
def test_open_database_rejects_windows_reserved_characters(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid Windows database filename"):
        open_database(tmp_path / "domain ?# space.sqlite")
