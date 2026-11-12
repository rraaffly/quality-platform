"""CLI entry points for durable agent work and explicit human decisions."""

import asyncio
import difflib
import json
from pathlib import Path
from typing import Annotated

import typer

from quality_platform.cli.commands import _repository, _selected_project
from quality_platform.config.loader import load_config
from quality_platform.rendering.test_cases import (
    RenderingStyle,
    _atomic_write,
    write_test_case_artifacts,
)
from quality_platform.services.approvals import BaselineSelection
from quality_platform.services.revisions import RevisionApproval, RevisionDraft, RevisionService
from quality_platform.services.scopes import ScopeEdit, ScopeService, propose_scope_edit
from quality_platform.storage.checkpoints import create_checkpoint_saver
from quality_platform.workflows.execution import WorkflowExecution, configure_execution
from quality_platform.workflows.gates import ReviewTarget
from quality_platform.workflows.graph import WorkflowRuntime, WorkflowServices


def register_workflow_commands(
    workflow: typer.Typer, scope: typer.Typer, artifact: typer.Typer
) -> None:
    workflow.command("run")(run_workflow)
    workflow.command("import")(import_sources)
    workflow.command("inspect")(inspect_workflow)
    workflow.command("impact")(list_impact)
    scope.command("list")(list_scopes)
    scope.command("baseline")(approve_baseline)
    scope.command("propose-edit")(propose_edit)
    scope.command("apply-edit")(apply_edit)
    artifact.command("submit")(submit_review)
    artifact.command("revision")(propose_revision)
    artifact.command("commit-revision")(commit_revision)


def run_workflow(
    database: Annotated[Path, typer.Option()],
    workflow_id: Annotated[str, typer.Option()],
    instruction: Annotated[
        str, typer.Option(help="Natural-language intent; invokes configured model APIs")
    ],
    request_id: Annotated[
        str, typer.Option(help="Stable ID: reuse after a crash, new ID for new intent")
    ],
    checkpoint_database: Annotated[Path, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
    style: Annotated[
        RenderingStyle | None, typer.Option(help="Rendering only; never changes the model")
    ] = None,
) -> None:
    with _repository(database) as repo:
        project_id = _selected_project(database, repo, project_id)

        async def run() -> list[dict[str, object]]:
            async with create_checkpoint_saver(checkpoint_database) as saver:
                execution = WorkflowExecution(repo, project_id, workflow_id, saver)
                results = await execution.run(instruction, request_id, style=style)
                return [item.model_dump(mode="json") for item in results]

        results = asyncio.run(run())
    typer.echo(json.dumps(results, indent=2))


def import_sources(
    database: Annotated[Path, typer.Option()],
    workflow_id: Annotated[str, typer.Option()],
    source: Annotated[list[Path], typer.Option()],
    config: Annotated[Path, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        project_id = _selected_project(database, repo, project_id)
        configure_execution(repo, project_id, workflow_id, load_config(config), source)
        typer.echo(repo.read_workflow(project_id, workflow_id).model_dump_json(indent=2))


def inspect_workflow(
    database: Annotated[Path, typer.Option()],
    workflow_id: Annotated[str, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        project_id = _selected_project(database, repo, project_id)
        item = repo.read_workflow(project_id, workflow_id)
        if item.project_id != project_id:
            raise PermissionError("cross-project workflow inspection denied")
        typer.echo(item.model_dump_json(indent=2))
        for event in repo.list_decisions(project_id):
            if event.record_id == workflow_id and event.event_type in {
                "task_plan",
                "task_result",
                "revision_draft",
            }:
                typer.echo(f"{event.event_type}: {event.payload}")


def list_impact(
    database: Annotated[Path, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        project_id = _selected_project(database, repo, project_id)
        for event in repo.list_decisions(project_id):
            if event.event_type == "impact":
                typer.echo(event.payload)


def list_scopes(
    database: Annotated[Path, typer.Option()],
    workflow_id: Annotated[str, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        project_id = _selected_project(database, repo, project_id)
        for item in ScopeService(
            repo, project_id=project_id, workflow_id=workflow_id
        ).list_scopes():
            typer.echo(item.model_dump_json(indent=2))


def approve_baseline(
    database: Annotated[Path, typer.Option()],
    workflow_id: Annotated[str, typer.Option()],
    scope_id: Annotated[str, typer.Option()],
    actor: Annotated[str, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        project_id = _selected_project(database, repo, project_id)

        async def approve() -> str:
            async with create_checkpoint_saver(
                database.with_suffix(".checkpoints.sqlite")
            ) as saver:
                execution = WorkflowExecution(repo, project_id, workflow_id, saver)
                versions = execution.scope_versions(scope_id)
                sources = repo.read_workflow(project_id, workflow_id).source_snapshot_ids
                result = execution.approvals.approve_scope_baseline(
                    scope_id,
                    BaselineSelection(
                        requirement_versions=versions,
                        source_snapshot_ids=sources,
                        human_reviewed=True,
                    ),
                    actor,
                )
                if not result.approved:
                    raise typer.BadParameter("; ".join(result.reasons))
                return result.model_dump_json(indent=2)

        typer.echo(asyncio.run(approve()))


def submit_review(
    database: Annotated[Path, typer.Option()],
    workflow_id: Annotated[str, typer.Option()],
    artifact_id: Annotated[str, typer.Option()],
    baseline_id: Annotated[str, typer.Option()],
    checkpoint_database: Annotated[Path, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        project_id = _selected_project(database, repo, project_id)

        async def submit() -> str:
            async with create_checkpoint_saver(checkpoint_database) as saver:
                execution = WorkflowExecution(repo, project_id, workflow_id, saver)
                item = execution.prepare_review(artifact_id, baseline_id)
                runtime = WorkflowRuntime(
                    WorkflowServices(
                        repo,
                        project_id,
                        ReviewTarget(
                            id=item.id,
                            version=item.version,
                            scope_id=item.scope_id,
                            baseline_id=item.requirement_baseline_id,
                        ),
                        execution.approvals,
                    ),
                    saver,
                )
                result = await runtime.run_workflow(workflow_id)
                return result.model_dump_json(indent=2)

        typer.echo(asyncio.run(submit()))


def propose_edit(
    database: Annotated[Path, typer.Option()],
    workflow_id: Annotated[str, typer.Option()],
    instruction: Annotated[str, typer.Option()],
    output: Annotated[Path, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        project_id = _selected_project(database, repo, project_id)

        async def propose() -> ScopeEdit:
            async with create_checkpoint_saver(
                database.with_suffix(".checkpoints.sqlite")
            ) as saver:
                execution = WorkflowExecution(repo, project_id, workflow_id, saver)
                return await propose_scope_edit(execution.agent_context(), instruction)

        edit = asyncio.run(propose())
        _atomic_write(output, edit.model_dump_json(indent=2))
        typer.echo(edit.model_dump_json(indent=2))
        typer.echo(
            "Proposal only. Review the JSON, then use scope apply-edit with your actor identity."
        )


def apply_edit(
    database: Annotated[Path, typer.Option()],
    workflow_id: Annotated[str, typer.Option()],
    proposal: Annotated[Path, typer.Option()],
    actor: Annotated[str, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    edit = ScopeEdit.model_validate_json(proposal.read_text())
    with _repository(database) as repo:
        project_id = _selected_project(database, repo, project_id)
        result = ScopeService(repo, project_id=project_id, workflow_id=workflow_id).review_scope(
            edit.scope_id, edit.decision.model_copy(update={"human_reviewed": True, "actor": actor})
        )
        typer.echo(result.model_dump_json(indent=2))


def propose_revision(
    database: Annotated[Path, typer.Option()],
    workflow_id: Annotated[str, typer.Option()],
    artifact_id: Annotated[str, typer.Option()],
    instruction: Annotated[str, typer.Option()],
    output: Annotated[Path, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        project_id = _selected_project(database, repo, project_id)

        async def propose() -> RevisionDraft:
            async with create_checkpoint_saver(
                database.with_suffix(".checkpoints.sqlite")
            ) as saver:
                execution = WorkflowExecution(repo, project_id, workflow_id, saver)
                return await execution.prepare_revision(instruction, artifact_id)

        draft = asyncio.run(propose())
        _atomic_write(output, draft.model_dump_json(indent=2))
        typer.echo(
            "".join(
                difflib.unified_diff(
                    draft.before.model_dump_json(indent=2).splitlines(True),
                    draft.after.model_dump_json(indent=2).splitlines(True),
                    fromfile="before",
                    tofile="proposed",
                )
            )
        )
        typer.echo(
            f"Reviewed diff hash: {draft.diff_hash}\nDraft saved to {output}; no case was changed."
        )


def commit_revision(
    database: Annotated[Path, typer.Option()],
    workflow_id: Annotated[str, typer.Option()],
    draft: Annotated[Path, typer.Option()],
    reviewed_diff_hash: Annotated[str, typer.Option()],
    actor: Annotated[str, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    from quality_platform.agents.planner import planning_requirements, validate_test_cases

    item = RevisionDraft.model_validate_json(draft.read_text())
    with _repository(database) as repo:
        project_id = _selected_project(database, repo, project_id)

        async def commit() -> str:
            async with create_checkpoint_saver(
                database.with_suffix(".checkpoints.sqlite")
            ) as saver:
                execution = WorkflowExecution(repo, project_id, workflow_id, saver)
                artifact = execution.artifact(item.artifact_id)
                versions = execution.scope_versions(artifact.scope_id)
                if (
                    item.requirement_versions != versions
                    or item.source_snapshot_ids
                    != repo.read_workflow(project_id, workflow_id).source_snapshot_ids
                ):
                    raise ValueError(
                        "revision source baseline changed; prepare and review a new diff"
                    )
                eligible, _ = planning_requirements(execution.agent_context(), artifact.scope_id)
                selected = tuple(
                    requirement
                    for requirement in eligible
                    if requirement.id
                    in {key for case in item.after.cases for key in case.requirement_ids}
                )
                validate_test_cases(list(item.after.cases), selected)
                service = RevisionService(
                    repo, project_id=project_id, workspace=execution.config.workspace_root
                )
                result = service.commit_revision(
                    item,
                    RevisionApproval(
                        approved=True, actor=actor, reviewed_diff_hash=reviewed_diff_hash
                    ),
                )
                repo.save_artifact_trace(project_id, result, versions, workflow_id=workflow_id)
                write_test_case_artifacts(
                    execution.artifact_directory(result.id, result.version),
                    list(item.after.cases),
                    execution.config.test_case_style,
                )
                return result.model_dump_json(indent=2)

        typer.echo(asyncio.run(commit()))
