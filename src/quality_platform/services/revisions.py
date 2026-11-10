import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from pydantic import Field

from quality_platform.domain.models import ArtifactKind, ArtifactStatus, ArtifactVersion, Record
from quality_platform.domain.schemas import TestCase, TestCaseSet
from quality_platform.rendering.test_cases import (
    ChangeManifest,
    build_change_manifest,
    render_test_cases,
)
from quality_platform.storage.repositories import QaRepositories


class RevisionDraft(Record):
    artifact_id: str = Field(min_length=1)
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=2)
    instruction: str = Field(min_length=1)
    target_case_ids: tuple[str, ...] = Field(min_length=1)
    before: TestCaseSet
    after: TestCaseSet
    manifest: ChangeManifest
    diff_hash: str = Field(min_length=1)


class RevisionApproval(Record):
    approved: bool
    actor: str = Field(min_length=1)
    reviewed_diff_hash: str = Field(min_length=1)


CaseReviser = Callable[[TestCase, str], TestCase]


def _default_reviser(case: TestCase, instruction: str) -> TestCase:
    priority = re.search(
        r"\bpriority\s+(?:to\s+)?([A-Za-z][A-Za-z_-]*)",
        instruction,
        re.IGNORECASE,
    )
    if priority is not None:
        return case.model_copy(update={"priority": priority.group(1).upper()})
    raise ValueError("revision instruction needs a configured deterministic reviser")


class RevisionService:
    def __init__(
        self,
        repo: QaRepositories,
        *,
        project_id: str,
        workspace: Path,
        reviser: CaseReviser = _default_reviser,
    ) -> None:
        self.repo = repo
        self.project_id = project_id
        self.workspace = workspace.resolve()
        self.reviser = reviser
        repo.read_project_record(project_id, project_id)

    def artifact_path(self, artifact_id: str, version: int) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", artifact_id):
            raise ValueError("invalid artifact ID")
        path = (
            self.workspace
            / self.project_id
            / "artifacts"
            / artifact_id
            / f"v{version}"
            / "test-cases.json"
        ).resolve()
        if not path.is_relative_to(self.workspace):
            raise ValueError("artifact path escapes workspace")
        return path

    def load_cases(self, artifact_id: str, version: int) -> TestCaseSet:
        path = self.artifact_path(artifact_id, version)
        return TestCaseSet.model_validate_json(path.read_text(encoding="utf-8"))

    def _verified_cases(self, artifact: ArtifactVersion) -> TestCaseSet:
        path = self.artifact_path(artifact.id, artifact.version)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != artifact.content_hash:
            raise ValueError("canonical test case artifact hash mismatch")
        return TestCaseSet.model_validate_json(content)

    def prepare_revision(self, instruction: str, artifact_id: str) -> RevisionDraft:
        if not instruction.strip():
            raise ValueError("revision instruction is required")
        artifact = self.repo.read_artifact_version(self.project_id, artifact_id)
        if artifact.kind != ArtifactKind.TEST_CASES:
            raise ValueError("targeted test case revision requires a TEST_CASES artifact")
        before = self._verified_cases(artifact)
        mentioned = tuple(dict.fromkeys(re.findall(r"\bTC-[A-Za-z0-9-]+\b", instruction)))
        if len(mentioned) != 1:
            raise ValueError("revision must target exactly one test case ID")
        by_id = {case.id: case for case in before.cases}
        target_id = mentioned[0]
        target = by_id.get(target_id)
        if target is None:
            raise ValueError(f"revision target does not exist: {target_id}")
        revised = TestCase.model_validate(self.reviser(target, instruction))
        if revised.id != target.id:
            raise ValueError("revision cannot change the stable test case ID")
        after = TestCaseSet(
            cases=tuple(revised if case.id == target_id else case for case in before.cases)
        )
        manifest = build_change_manifest(list(before.cases), list(after.cases))
        if manifest.changed_ids != (target_id,) or manifest.added_ids or manifest.removed_ids:
            raise ValueError("revision changed content outside the authorized target")
        canonical_diff = json.dumps(
            {
                "artifact_id": artifact_id,
                "from_version": artifact.version,
                "to_version": artifact.version + 1,
                "instruction": instruction,
                "target_case_ids": mentioned,
                "before_hash": manifest.before_hash,
                "after_hash": manifest.after_hash,
            },
            sort_keys=True,
        )
        return RevisionDraft(
            artifact_id=artifact_id,
            from_version=artifact.version,
            to_version=artifact.version + 1,
            instruction=instruction,
            target_case_ids=mentioned,
            before=before,
            after=after,
            manifest=manifest,
            diff_hash=hashlib.sha256(canonical_diff.encode("utf-8")).hexdigest(),
        )

    def commit_revision(self, draft: RevisionDraft, approval: RevisionApproval) -> ArtifactVersion:
        if not approval.approved or approval.reviewed_diff_hash != draft.diff_hash:
            raise PermissionError("human approval of the exact reviewed diff is required")
        current = self.repo.read_artifact_version(self.project_id, draft.artifact_id)
        content = render_test_cases(list(draft.after.cases), "detailed")["json"]
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if current.version == draft.to_version:
            persisted = self.artifact_path(current.id, current.version).read_bytes()
            if (
                current.kind == ArtifactKind.TEST_CASES
                and current.status == ArtifactStatus.DRAFT
                and current.content_hash == content_hash
                and hashlib.sha256(persisted).hexdigest() == content_hash
            ):
                return current
            raise ValueError("revision replay differs from the committed artifact")
        if current.version != draft.from_version:
            raise ValueError("artifact changed after revision preparation")
        before = self._verified_cases(current)
        if before != draft.before:
            raise ValueError("artifact content changed after revision preparation")
        manifest = build_change_manifest(list(draft.before.cases), list(draft.after.cases))
        if manifest != draft.manifest or manifest.changed_ids != draft.target_case_ids:
            raise ValueError("revision draft no longer matches its reviewed diff")

        revised = ArtifactVersion(
            id=current.id,
            kind=current.kind,
            scope_id=current.scope_id,
            version=draft.to_version,
            content_hash=content_hash,
            requirement_baseline_id=current.requirement_baseline_id,
            status=ArtifactStatus.DRAFT,
        )
        target = self.artifact_path(current.id, draft.to_version)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=target.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            self.repo.save_artifact_version(
                self.project_id,
                revised,
                idempotency_key=(
                    f"revision:{self.project_id}:{current.id}:{draft.to_version}:{draft.diff_hash}"
                ),
            )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return revised
