from quality_platform.storage.repositories import Grant, check_project_access


def test_same_project_may_read_and_write() -> None:
    assert check_project_access("P1", "P1", "read", None).allowed
    assert check_project_access("P1", "P1", "write", None).allowed


def test_cross_project_read_denied_without_matching_grant() -> None:
    assert not check_project_access("P2", "P1", "read", None).allowed
    wrong_grant = Grant(owner_project_id="P1", grantee_project_id="P3")
    assert not check_project_access("P2", "P1", "read", wrong_grant).allowed


def test_cross_project_grant_is_read_only() -> None:
    grant = Grant(owner_project_id="P1", grantee_project_id="P2")
    assert check_project_access("P2", "P1", "read", grant).allowed
    assert not check_project_access("P2", "P1", "write", grant).allowed
