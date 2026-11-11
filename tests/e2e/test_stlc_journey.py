import hashlib
from pathlib import Path

import pytest

from quality_platform.domain.models import (
    ArtifactKind,
    ArtifactStatus,
    ArtifactVersion,
    Clarification,
    ClarificationIssueType,
    ProjectRecord,
    Requirement,
    RequirementReadiness,
    SourceDocument,
    SourceReference,
    WorkflowRecord,
)
from quality_platform.domain.schemas import TestCase, TestStep
from quality_platform.rendering.test_cases import write_test_case_artifacts
from quality_platform.services.approvals import ApprovalInput, ApprovalService, BaselineSelection
from quality_platform.services.clarifications import ClarificationService
from quality_platform.services.impact import ArtifactDependency, RequirementChange, detect_impact
from quality_platform.services.revisions import RevisionApproval, RevisionService
from quality_platform.services.scopes import ScopeAction, ScopeDecision, ScopeProposal, ScopeService
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories


def reference(document_id: str) -> SourceReference:
    return SourceReference(document_id=document_id, start_line=3, end_line=3)


def save_document(
    repo: QaRepositories,
    root: Path,
    project_id: str,
    document_id: str,
    statement: str,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{document_id}.md"
    path.write_text(f"# {document_id}\n\n{statement}\n", encoding="utf-8")
    repo.save_source_document(
        SourceDocument(
            id=document_id,
            project_id=project_id,
            input_type="PRD",
            content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            source_path=path,
            snapshot_path=path,
            line_count=3,
        )
    )


def test_end_to_end_partial_scope_artifacts_revision_and_manual_impact(
    tmp_path: Path,
) -> None:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    workspace = tmp_path / "workspace"
    p1_root = workspace / "P1"
    p2_root = workspace / "P2"
    repo.create_project(ProjectRecord(id="P1", name="Customer login", root=p1_root))
    repo.create_project(ProjectRecord(id="P2", name="Private roadmap", root=p2_root))
    repo.create_workflow(
        WorkflowRecord(
            id="W1",
            project_id="P1",
            goal="Design login tests",
            thread_id="T1",
            source_snapshot_ids=("BRD", "SRS", "PRD"),
        )
    )
    repo.create_workflow(WorkflowRecord(id="W2", project_id="P2", goal="Private", thread_id="T2"))
    save_document(repo, p1_root, "P1", "BRD", "The account locks after 3 failed attempts.")
    save_document(repo, p1_root, "P1", "SRS", "The account locks after 5 failed attempts.")
    save_document(repo, p1_root, "P1", "PRD", "Users can enable TOTP.")
    save_document(repo, p2_root, "P2", "SECRET", "Launch date is confidential.")

    blocked = Requirement(
        id="REQ-LOCKOUT",
        version=1,
        statement="The lockout threshold conflicts across sources.",
        source_references=(reference("BRD"), reference("SRS")),
        readiness=RequirementReadiness.BLOCKED,
    )
    eligible = Requirement(
        id="REQ-MFA",
        version=1,
        statement="Users can enable TOTP.",
        source_references=(reference("PRD"),),
        source_quotes=("Users can enable TOTP.",),
        readiness=RequirementReadiness.VALIDATED,
    )
    repo.save_requirement_version("P1", blocked, idempotency_key="lockout-v1")
    repo.save_requirement_version("P1", eligible, idempotency_key="mfa-v1")
    ClarificationService(repo, project_id="P1", workflow_id="W1").create_batch(
        [
            Clarification(
                id="C-LOCKOUT",
                issue_type=ClarificationIssueType.CROSS_DOCUMENT_CONFLICT,
                question="Should lockout happen after 3 or 5 failed attempts?",
                source_references=(reference("BRD"), reference("SRS")),
                affected_requirement_ids=(blocked.id,),
            )
        ]
    )
    scopes = ScopeService(repo, project_id="P1", workflow_id="W1")
    scopes.create_batch(
        [
            ScopeProposal(id="LOGIN", journey="Login", requirement_ids=(blocked.id,)),
            ScopeProposal(id="MFA", journey="MFA", requirement_ids=(eligible.id,)),
        ],
        known_requirement_ids={blocked.id, eligible.id},
    )
    approval = ScopeDecision(action=ScopeAction.APPROVE, actor="QA", human_reviewed=True)
    with pytest.raises(ValueError, match="unresolved"):
        scopes.review_scope("LOGIN", approval)
    assert scopes.review_scope("MFA", approval).status == "APPROVED"

    approvals = ApprovalService(repo, scopes, project_id="P1", workflow_id="W1")
    baseline = approvals.approve_scope_baseline(
        "MFA",
        BaselineSelection(
            requirement_versions={eligible.id: eligible.version},
            source_snapshot_ids=("BRD", "SRS", "PRD"),
            human_reviewed=True,
        ),
        actor="QA",
    ).baseline
    assert baseline is not None
    cases = [
        TestCase(
            id=case_id,
            title=f"Enable TOTP {case_id}",
            intent="Verify TOTP enrollment",
            requirement_ids=(eligible.id,),
            scenario="User enables TOTP",
            priority="MEDIUM",
            steps=(
                TestStep(
                    action="Enable TOTP",
                    expected_result="Users can enable TOTP.",
                    expected_result_sources=(reference("PRD"),),
                ),
            ),
        )
        for case_id in ("TC-011", "TC-012")
    ]
    artifact_dir = workspace / "P1" / "artifacts" / "CASES-MFA" / "v1"
    paths = write_test_case_artifacts(artifact_dir, cases, "both")
    assert set(paths) == {"json", "detailed", "gherkin"}
    artifact = ArtifactVersion(
        id="CASES-MFA",
        kind=ArtifactKind.TEST_CASES,
        scope_id="MFA",
        version=1,
        content_hash=hashlib.sha256(paths["json"].read_bytes()).hexdigest(),
        requirement_baseline_id=baseline.id,
        status=ArtifactStatus.READY_FOR_REVIEW,
    )
    repo.save_artifact_version("P1", artifact, idempotency_key="cases-mfa-v1")
    approved = approvals.commit_artifact_approval(
        artifact.id,
        ApprovalInput(
            approved=True,
            actor="QA",
            artifact_version=artifact.version,
            scope_id=artifact.scope_id,
            baseline_id=baseline.id,
            content_hash=artifact.content_hash,
            reviewed_evidence=("test-cases.json", "test-cases.detailed.md"),
        ),
    )
    assert approved.status == ArtifactStatus.APPROVED

    revisions = RevisionService(repo, project_id="P1", workspace=workspace)
    draft = revisions.prepare_revision("Change only TC-012 priority to CRITICAL.", artifact.id)
    revised = revisions.commit_revision(
        draft,
        RevisionApproval(
            approved=True,
            actor="QA",
            reviewed_diff_hash=draft.diff_hash,
        ),
    )
    before_impact = revisions.artifact_path(revised.id, revised.version).read_bytes()
    impact = detect_impact(
        RequirementChange(
            project_id="P1",
            requirement_id=eligible.id,
            from_version=1,
            to_version=2,
            before_statement=eligible.statement,
            after_statement="Users can enable passkeys and TOTP.",
            traced_artifacts=(
                ArtifactDependency(
                    project_id="P1",
                    artifact_id=artifact.id,
                    scope_id="MFA",
                    requirement_ids=(eligible.id,),
                ),
            ),
        )
    )

    assert impact.affected_artifact_ids == (artifact.id,)
    assert revisions.artifact_path(revised.id, revised.version).read_bytes() == before_impact
    assert revisions.load_cases(artifact.id, 2).cases[1].priority == "CRITICAL"
    with pytest.raises(PermissionError):
        repo.read_source_document("P1", "SECRET")
