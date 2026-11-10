from typing import NotRequired, TypedDict


class WorkflowState(TypedDict):
    workflow_id: str
    project_id: str
    scope_id: str
    target_id: str
    target_version: int
    baseline_id: str
    content_hash: str
    requested_at: str
    status: str
    actor: NotRequired[str]
    approval_id: NotRequired[str]
    reasons: NotRequired[list[str]]
