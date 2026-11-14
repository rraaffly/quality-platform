import hashlib
import json
import math
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from statistics import pvariance
from time import perf_counter
from uuid import uuid4

import yaml
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quality_platform.agents.analyst import (
    RequirementAnalysisDraft,
    build_analysis_instruction,
    validate_analysis_draft,
)
from quality_platform.config.loader import load_config
from quality_platform.domain.models import ClarificationIssueType
from quality_platform.evaluation.adapters import invoke_planner
from quality_platform.evaluation.judge import review_output
from quality_platform.evaluation.metrics import evaluate_case
from quality_platform.evaluation.models import (
    BenchmarkCase,
    BenchmarkReport,
    CandidateSnapshot,
    CaseScore,
    Dataset,
    EvaluationOutput,
    GoldStatus,
    ModelUsage,
    ObservedItem,
    ProductivityEvidence,
    ReleaseDecision,
    RepeatResult,
    TaskType,
)
from quality_platform.evaluation.registry import load_dataset
from quality_platform.evaluation.regression import ThresholdStatus, load_quality_thresholds
from quality_platform.evaluation.reporting import fingerprint_outputs
from quality_platform.evaluation.workflow_evidence import code_fingerprint
from quality_platform.llm.model_factory import create_chat_model
from quality_platform.llm.registry import AgentRole, ModelRegistry
from quality_platform.llm.usage import capture_usage
from quality_platform.resources import runtime_root


class LiveBenchmarkConfigurationError(ValueError):
    """A cost-bearing benchmark is missing explicit, reproducible configuration."""


@dataclass
class _BudgetLedger:
    limit_usd: float
    reserved_usd: float = 0
    spent_usd: float = 0

    def reserve(self, amount_usd: float) -> None:
        if not math.isfinite(amount_usd) or amount_usd <= 0:
            raise LiveBenchmarkConfigurationError(
                "per-case reservation must be finite and positive"
            )
        if self.reserved_usd + amount_usd > self.limit_usd:
            raise RuntimeError("budget would be exceeded before provider call")
        self.reserved_usd += amount_usd

    def record(self, amount_usd: float, *, reserved_usd: float) -> None:
        if not math.isfinite(amount_usd) or amount_usd < 0:
            raise RuntimeError("provider returned a non-finite or negative cost")
        if amount_usd > reserved_usd:
            raise RuntimeError("provider cost exceeded the reserved per-case bound")
        self.spent_usd += amount_usd


class _LiveBenchmarkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    prompt_snapshot_id: str = Field(min_length=1)
    representative_case_ids: tuple[str, ...] = Field(min_length=1)
    max_budget_usd: float = Field(gt=0)
    max_case_cost_usd: float = Field(gt=0)
    agent_role: AgentRole = AgentRole.ANALYST
    max_repeats: int = Field(default=1, ge=1, le=20)
    minimum_approved_real_cases: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def finite_cost_bounds(self) -> "_LiveBenchmarkConfig":
        if not math.isfinite(self.max_budget_usd) or not math.isfinite(self.max_case_cost_usd):
            raise ValueError("cost bounds must be finite")
        if self.max_case_cost_usd > self.max_budget_usd:
            raise ValueError("per-case cost bound cannot exceed the run budget")
        return self


def _repository_root() -> Path:
    return runtime_root()


def _load_live_config(root: Path, tier: str) -> tuple[_LiveBenchmarkConfig, bytes]:
    path = root / "benchmarks" / "configs" / f"{tier}.yaml"
    raw = path.read_bytes()
    loaded = yaml.safe_load(raw)
    if not isinstance(loaded, dict):
        raise LiveBenchmarkConfigurationError(f"{tier} configuration must be a YAML mapping")
    return _LiveBenchmarkConfig.model_validate(loaded), raw


def _confirmed_cost_limit(config: _LiveBenchmarkConfig, prefix: str) -> float:
    confirmation_key = f"{prefix}_BUDGET_CONFIRMED"
    limit_key = f"{prefix}_MAX_COST_USD"
    if os.getenv(confirmation_key) != "yes":
        raise LiveBenchmarkConfigurationError(
            f"set {confirmation_key}=yes only after approving a live API run"
        )
    raw_limit = os.getenv(limit_key)
    if raw_limit is None:
        raise LiveBenchmarkConfigurationError(f"{limit_key} is required")
    try:
        limit = float(raw_limit)
    except ValueError as error:
        raise LiveBenchmarkConfigurationError(f"{limit_key} must be numeric") from error
    if not math.isfinite(limit):
        raise LiveBenchmarkConfigurationError(f"{limit_key} must be finite")
    if limit <= 0 or limit > config.max_budget_usd:
        raise LiveBenchmarkConfigurationError(
            f"confirmed cost limit must be within (0, {config.max_budget_usd}]"
        )
    return limit


def _token_rates(prefix: str) -> tuple[float, float]:
    values: list[float] = []
    for key in (
        f"{prefix}_INPUT_USD_PER_MILLION",
        f"{prefix}_OUTPUT_USD_PER_MILLION",
    ):
        raw = os.getenv(key)
        if raw is None:
            raise LiveBenchmarkConfigurationError(f"{key} is required for cost auditing")
        try:
            value = float(raw)
        except ValueError as error:
            raise LiveBenchmarkConfigurationError(f"{key} must be numeric") from error
        if not math.isfinite(value):
            raise LiveBenchmarkConfigurationError(f"{key} must be finite")
        if value < 0:
            raise LiveBenchmarkConfigurationError(f"{key} cannot be negative")
        values.append(value)
    return values[0], values[1]


def _case_documents(root: Path, case: BenchmarkCase) -> str:
    lines_by_id = _case_lines(root, case)
    return "\n\n".join(
        f"Document {snapshot_id}:\n" + "\n".join(lines_by_id[snapshot_id])
        for snapshot_id in case.input_snapshot_ids
    )


def _case_lines(root: Path, case: BenchmarkCase) -> dict[str, list[str]]:
    dataset_root = (root / "benchmarks" / "datasets" / case.origin).resolve()
    if not dataset_root.is_dir():
        raise LiveBenchmarkConfigurationError("benchmark snapshot root is missing")
    lines_by_id: dict[str, list[str]] = {}
    for snapshot_id in case.input_snapshot_ids:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", snapshot_id):
            raise LiveBenchmarkConfigurationError(f"invalid benchmark snapshot ID: {snapshot_id}")
        matches = tuple(dataset_root.rglob(f"{snapshot_id}.md"))
        if len(matches) != 1:
            raise LiveBenchmarkConfigurationError(
                f"expected one benchmark snapshot for {snapshot_id}, found {len(matches)}"
            )
        snapshot = matches[0].resolve(strict=True)
        if not snapshot.is_relative_to(dataset_root):
            raise LiveBenchmarkConfigurationError("benchmark snapshot is outside benchmark root")
        raw = snapshot.read_bytes()
        expected_hash = case.input_snapshot_hashes.get(snapshot_id)
        if expected_hash is None:
            raise LiveBenchmarkConfigurationError(
                f"approved benchmark snapshot {snapshot_id} has no pinned hash"
            )
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise LiveBenchmarkConfigurationError(
                f"benchmark snapshot hash mismatch: {snapshot_id}"
            )
        lines_by_id[snapshot_id] = raw.decode("utf-8").splitlines()
    return lines_by_id


async def _invoke_live_case(
    case: BenchmarkCase,
    role: AgentRole,
    registry: ModelRegistry,
    root: Path,
    input_rate: float,
    output_rate: float,
) -> tuple[EvaluationOutput, ModelUsage]:
    expected_role = {TaskType.ANALYST: AgentRole.ANALYST, TaskType.PLANNER: AgentRole.PLANNER}.get(
        case.task_type
    )
    if role != expected_role:
        raise LiveBenchmarkConfigurationError(
            f"no production evaluation adapter for {case.task_type.value} with role {role.value}"
        )
    spec = registry.get(role)
    model = create_chat_model(spec)
    lines_by_id = _case_lines(root, case)
    if role == AgentRole.PLANNER:
        output, raw = await invoke_planner(case, lines_by_id, registry, model)
        return output, _usage_record(role, registry, raw, input_rate, output_rate)
    structured = model.with_structured_output(RequirementAnalysisDraft, include_raw=True)
    prompt = build_analysis_instruction(lines_by_id)
    result = await structured.ainvoke([HumanMessage(content=prompt)])
    if not isinstance(result, dict) or result.get("parsed") is None:
        raise RuntimeError(f"live benchmark case {case.case_id} returned invalid structured output")
    draft = RequirementAnalysisDraft.model_validate(result["parsed"])
    validate_analysis_draft(draft, lines_by_id)
    kind_by_issue = {
        ClarificationIssueType.CROSS_DOCUMENT_CONFLICT: "CONFLICT",
        ClarificationIssueType.MISSING_RULE: "GAP",
    }
    observed = EvaluationOutput(
        policy_observation_complete=False,
        observed_items=tuple(
            ObservedItem(
                id=requirement.id,
                kind="REQUIREMENT",
                content=requirement.statement,
                semantic_context=requirement.model_dump_json(),
                source_references=requirement.source_references,
            )
            for requirement in draft.requirements
            if requirement.id in draft.draft_eligible_requirement_ids
        )
        + tuple(
            ObservedItem(
                id=item.id,
                kind=kind_by_issue.get(item.issue_type, item.issue_type.value),
                content=item.question,
                semantic_context=json.dumps(
                    {
                        "clarification": item.model_dump(mode="json"),
                        "affected_requirements": [
                            requirement.model_dump(mode="json")
                            for requirement in draft.requirements
                            if requirement.id in item.affected_requirement_ids
                        ],
                    },
                    sort_keys=True,
                ),
                source_references=item.source_references,
            )
            for item in draft.clarifications
        ),
    )
    raw_message = result.get("raw")
    if not isinstance(raw_message, AIMessage):
        raise TypeError(f"live benchmark case {case.case_id} omitted usage metadata")
    return observed, _usage_record(role, registry, raw_message, input_rate, output_rate)


def _usage_record(
    role: AgentRole,
    registry: ModelRegistry,
    raw_message: AIMessage,
    input_rate: float,
    output_rate: float,
) -> ModelUsage:
    spec = registry.get(role)
    captured = capture_usage(role, spec, raw_message)
    cost = (captured.input_tokens * input_rate + captured.output_tokens * output_rate) / 1_000_000
    return ModelUsage(
        **captured.model_dump(),
        cost_usd=round(cost, 10),
    )


def _commit_sha(root: Path) -> str:
    configured = os.getenv("QA_BENCHMARK_COMMIT_SHA")
    if configured:
        return configured
    # Installed wheels carry no Git checkout. Do not accidentally attribute them to a
    # repository above the installation directory; the content fingerprint still binds the run.
    if not (root / "src" / "quality_platform").is_dir():
        return "unavailable"
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


async def run_live_benchmark(
    dataset_version: str,
    agent_role: AgentRole,
    max_cases: int | None,
) -> BenchmarkReport:
    if max_cases is not None and max_cases < 1:
        raise LiveBenchmarkConfigurationError("max_cases must be positive")
    root = _repository_root()
    config, raw = _load_live_config(root, "tier2")
    return await _run_configured_benchmark(
        dataset_version,
        agent_role,
        max_cases=max_cases,
        config=config,
        config_raw=raw,
        environment_prefix="QA_TIER2",
        budget=None,
        full_dataset=False,
    )


async def _run_configured_benchmark(
    dataset_version: str,
    agent_role: AgentRole,
    *,
    max_cases: int | None,
    config: _LiveBenchmarkConfig,
    config_raw: bytes,
    environment_prefix: str,
    budget: _BudgetLedger | None,
    full_dataset: bool,
) -> BenchmarkReport:
    root = _repository_root()
    cost_limit = _confirmed_cost_limit(config, environment_prefix)
    input_rate, output_rate = _token_rates(environment_prefix)
    evaluator_input_rate, evaluator_output_rate = _token_rates(environment_prefix + "_EVALUATOR")
    app_config_path = os.getenv("QUALITY_PLATFORM_CONFIG")
    if not app_config_path:
        raise LiveBenchmarkConfigurationError("QUALITY_PLATFORM_CONFIG is required")
    config_path = Path(app_config_path).resolve()
    app_config_raw = config_path.read_bytes()
    registry = ModelRegistry(load_config(config_path))
    dataset = load_dataset(dataset_version, root / "benchmarks")

    selected = _select_live_cases(
        dataset,
        agent_role,
        representative_case_ids=config.representative_case_ids,
        full_dataset=full_dataset,
    )
    if max_cases is not None:
        selected = selected[:max_cases]
    if not selected:
        raise LiveBenchmarkConfigurationError("no approved representative cases match the role")
    policy_cases = tuple(
        case
        for case in dataset.cases
        if full_dataset
        and case.gold.status == GoldStatus.APPROVED
        and case.task_type == TaskType.POLICY
    )

    task_roles = {TaskType.ANALYST: AgentRole.ANALYST, TaskType.PLANNER: AgentRole.PLANNER}
    roles = {task_roles[case.task_type] for case in selected}
    rates_by_role = {
        role: _token_rates(environment_prefix + "_" + role.value.upper())
        if len(roles) > 1
        else (input_rate, output_rate)
        for role in roles
    }
    ledger = budget or _BudgetLedger(limit_usd=cost_limit)
    if ledger.reserved_usd + len(selected) * 2 * config.max_case_cost_usd > ledger.limit_usd:
        raise LiveBenchmarkConfigurationError("budget cannot reserve candidate and evaluator calls")
    documents = {case.case_id: _case_lines(root, case) for case in selected}
    outputs: dict[str, EvaluationOutput] = {}
    usage: list[ModelUsage] = []
    for case in selected:
        ledger.reserve(config.max_case_cost_usd)
        ledger.reserve(config.max_case_cost_usd)
        role = task_roles[case.task_type] if full_dataset else agent_role
        case_input_rate, case_output_rate = rates_by_role[role]
        output, record = await _invoke_live_case(
            case, role, registry, root, case_input_rate, case_output_rate
        )
        usage.append(record)
        ledger.record(record.cost_usd, reserved_usd=config.max_case_cost_usd)
        output, raw_review = await review_output(
            case, output, documents[case.case_id], registry, create_chat_model
        )
        review_usage = _usage_record(
            AgentRole.EVALUATOR, registry, raw_review, evaluator_input_rate, evaluator_output_rate
        )
        usage.append(review_usage)
        ledger.record(review_usage.cost_usd, reserved_usd=config.max_case_cost_usd)
        outputs[case.case_id] = output

    rates = json.dumps(
        {
            **{role.value: rate for role, rate in rates_by_role.items()},
            "evaluator": (evaluator_input_rate, evaluator_output_rate),
        },
        sort_keys=True,
    ).encode()
    config_hash = hashlib.sha256(config_raw + app_config_raw + rates).hexdigest()
    candidate = CandidateSnapshot(
        candidate_id=config.candidate_id,
        commit_sha=_commit_sha(root),
        config_hash=config_hash,
        model_snapshot_id=registry.snapshot.id,
        prompt_snapshot_id=config.prompt_snapshot_id,
    )
    subset = dataset.model_copy(
        update={
            "cases": selected,
            "content_fingerprint": dataset.content_fingerprint
            or hashlib.sha256(
                dataset.model_dump_json(exclude={"content_fingerprint"}).encode()
            ).hexdigest(),
        }
    )
    report = run_offline_suite(
        subset,
        lambda case: outputs[case.case_id],
        candidate=candidate,
        usage=tuple(usage),
    )
    return report.model_copy(
        update={
            "excluded_count": len(dataset.cases) - len(selected) - len(policy_cases),
            "policy_suite_tests": {case.case_id: case.policy_test_ids for case in policy_cases},
            "readiness_labels": ("POLICY_SUITE_EVIDENCE_MISSING",) if policy_cases else (),
            "decision": ReleaseDecision.REVIEW_REQUIRED
            if policy_cases and report.decision == ReleaseDecision.PASS
            else report.decision,
        }
    )


def _select_live_cases(
    dataset: Dataset,
    agent_role: AgentRole,
    *,
    representative_case_ids: tuple[str, ...],
    full_dataset: bool,
) -> tuple[BenchmarkCase, ...]:
    task_for_role = {
        AgentRole.ANALYST: TaskType.ANALYST,
        AgentRole.PLANNER: TaskType.PLANNER,
    }.get(agent_role)
    approved = tuple(case for case in dataset.cases if case.gold.status == GoldStatus.APPROVED)
    if full_dataset:
        unsupported = tuple(
            case.case_id
            for case in approved
            if (case.task_type == TaskType.PLANNER and not case.planner_requirements)
            or (case.task_type == TaskType.POLICY and not case.policy_test_ids)
        )
        if unsupported:
            raise LiveBenchmarkConfigurationError(
                "full benchmark requires Planner inputs or pinned POLICY test IDs for approved cases: "
                + ", ".join(unsupported)
            )
        return tuple(case for case in approved if case.task_type != TaskType.POLICY)
    if task_for_role is None:
        raise LiveBenchmarkConfigurationError("role has no production task adapter")
    return tuple(
        case
        for case in approved
        if case.case_id in representative_case_ids
        and (task_for_role is None or case.task_type == task_for_role)
    )


async def _run_tier3_once(dataset_version: str, budget: _BudgetLedger) -> BenchmarkReport:
    root = _repository_root()
    config, raw = _load_live_config(root, "tier3")
    return await _run_configured_benchmark(
        dataset_version,
        config.agent_role,
        max_cases=None,
        config=config,
        config_raw=raw,
        environment_prefix="QA_TIER3",
        budget=budget,
        full_dataset=True,
    )


async def run_full_benchmark(
    dataset_version: str,
    *,
    repeats: int,
) -> BenchmarkReport:
    root = _repository_root()
    config, _raw = _load_live_config(root, "tier3")
    cost_limit = _confirmed_cost_limit(config, "QA_TIER3")
    if repeats < 1 or repeats > config.max_repeats:
        raise LiveBenchmarkConfigurationError(f"repeats must be between 1 and {config.max_repeats}")

    active_qa_minutes = _optional_nonnegative_number("QA_TIER3_ACTIVE_QA_MINUTES")
    manual_baseline_minutes = _optional_nonnegative_number("QA_TIER3_MANUAL_BASELINE_MINUTES")
    productivity_path = os.getenv("QA_TIER3_PRODUCTIVITY_EVIDENCE")
    productivity = (
        ProductivityEvidence.model_validate_json(Path(productivity_path).read_text())
        if productivity_path
        else None
    )
    started = perf_counter()
    budget = _BudgetLedger(limit_usd=cost_limit)
    reports: list[BenchmarkReport] = []
    for _repeat in range(repeats):
        reports.append(await _run_tier3_once(dataset_version, budget))
    latency = perf_counter() - started
    _validate_repeat_metadata(reports)
    case_scores = _aggregate_case_scores(reports)
    failures = tuple(
        score.case_id
        for score in case_scores
        if score.critical_violation
        or score.precision < 1
        or score.recall < 1
        or score.evidence_accuracy < 1
        or score.findings
    )
    dataset = load_dataset(dataset_version, root / "benchmarks")
    approved_real_cases = sum(
        case.origin == "real_world" and case.gold.status == GoldStatus.APPROVED
        for case in dataset.cases
    )
    labels: list[str] = list(reports[0].readiness_labels)
    if approved_real_cases < config.minimum_approved_real_cases:
        labels.append("INSUFFICIENT_REAL_CASES")
    gates = load_quality_thresholds(root / "benchmarks" / "rubrics" / "quality-gates.yaml")
    if gates.status != ThresholdStatus.CALIBRATED:
        labels.append("UNCALIBRATED_GATES")
    if productivity is None:
        labels.append("PRODUCTIVITY_EVIDENCE_MISSING")
    else:
        active_qa_minutes = productivity.active_qa_minutes
        manual_baseline_minutes = productivity.manual_baseline_minutes
    labels.append("E2E_WORKFLOW_EVIDENCE_MISSING")

    first = reports[0]
    decision = _strictest_decision(reports)
    if labels and decision in {
        ReleaseDecision.PASS,
        ReleaseDecision.APPROVED_EXCEPTION,
    }:
        decision = ReleaseDecision.REVIEW_REQUIRED
    return BenchmarkReport(
        run_id=str(uuid4()),
        dataset_version=first.dataset_version,
        rubric_id=first.rubric_id,
        rubric_version=first.rubric_version,
        candidate=first.candidate,
        usage=tuple(item for report in reports for item in report.usage),
        total_cost_usd=round(sum(item.total_cost_usd for item in reports), 10),
        case_scores=case_scores,
        eligible_count=len(case_scores),
        excluded_count=first.excluded_count,
        overall_precision=sum(item.overall_precision for item in reports) / repeats,
        overall_recall=sum(item.overall_recall for item in reports) / repeats,
        decision=decision,
        repeat_count=repeats,
        latency_seconds=latency,
        active_qa_minutes=active_qa_minutes,
        manual_baseline_minutes=manual_baseline_minutes,
        failure_case_ids=failures,
        readiness_labels=tuple(labels),
        input_fingerprint=first.input_fingerprint,
        output_fingerprint=hashlib.sha256(
            json.dumps([report.output_fingerprint for report in reports]).encode()
        ).hexdigest(),
        evaluated_case_ids=first.evaluated_case_ids,
        evaluator_assessments=tuple(
            assessment for report in reports for assessment in report.evaluator_assessments
        ),
        repeat_results=tuple(
            RepeatResult(
                run_id=report.run_id,
                case_scores=report.case_scores,
                overall_precision=report.overall_precision,
                overall_recall=report.overall_recall,
                decision=report.decision,
                evaluator_assessments=report.evaluator_assessments,
            )
            for report in reports
        ),
        metric_variance={
            "precision": pvariance(report.overall_precision for report in reports),
            "recall": pvariance(report.overall_recall for report in reports),
            **{
                f"{score.case_id}.{metric}": pvariance(
                    getattr(
                        next(item for item in report.case_scores if item.case_id == score.case_id),
                        metric,
                    )
                    for report in reports
                )
                for score in case_scores
                for metric in ("precision", "recall", "evidence_accuracy")
            },
            **{
                f"{score.case_id}.{metric}": pvariance(
                    next(
                        item for item in report.case_scores if item.case_id == score.case_id
                    ).planner_metrics[metric]
                    for report in reports
                )
                for score in case_scores
                for metric in score.planner_metrics
            },
        },
        productivity_evidence=productivity,
        code_fingerprint=first.code_fingerprint,
        policy_suite_tests=first.policy_suite_tests,
        review_outputs=tuple(outputs for report in reports for outputs in report.review_outputs),
    )


def _validate_repeat_metadata(reports: list[BenchmarkReport]) -> None:
    first = reports[0]
    if any(
        report.dataset_version != first.dataset_version
        or report.rubric_id != first.rubric_id
        or report.rubric_version != first.rubric_version
        or report.candidate != first.candidate
        or report.input_fingerprint != first.input_fingerprint
        or report.evaluated_case_ids != first.evaluated_case_ids
        or report.excluded_count != first.excluded_count
        or report.code_fingerprint != first.code_fingerprint
        or report.policy_suite_tests != first.policy_suite_tests
        for report in reports[1:]
    ):
        raise ValueError(
            "Tier 3 repeats must use identical dataset, rubric, inputs, cases, and candidate metadata"
        )


def _aggregate_case_scores(reports: list[BenchmarkReport]) -> tuple[CaseScore, ...]:
    grouped: defaultdict[str, list[CaseScore]] = defaultdict(list)
    for report in reports:
        for score in report.case_scores:
            grouped[score.case_id].append(score)
    expected_repeats = len(reports)
    if any(len(items) != expected_repeats for items in grouped.values()):
        raise ValueError("each Tier 3 repeat must score the same cases")
    return tuple(
        CaseScore(
            case_id=case_id,
            eligible=all(item.eligible for item in items),
            precision=sum(item.precision for item in items) / expected_repeats,
            recall=sum(item.recall for item in items) / expected_repeats,
            evidence_accuracy=sum(item.evidence_accuracy for item in items) / expected_repeats,
            unsupported_source_count=sum(item.unsupported_source_count for item in items),
            critical_violation=any(item.critical_violation for item in items),
            findings=tuple(dict.fromkeys(finding for item in items for finding in item.findings)),
            planner_metrics={
                metric: sum(item.planner_metrics[metric] for item in items) / expected_repeats
                for metric in set.intersection(*(set(item.planner_metrics) for item in items))
            },
            observed_actions=tuple(
                dict.fromkeys(action for item in items for action in item.observed_actions)
            ),
            observed_workflow_state=items[0].observed_workflow_state
            if len({item.observed_workflow_state for item in items}) == 1
            else None,
        )
        for case_id, items in sorted(grouped.items())
    )


def _strictest_decision(reports: list[BenchmarkReport]) -> ReleaseDecision:
    decisions = {report.decision for report in reports}
    for decision in (
        ReleaseDecision.BLOCKED,
        ReleaseDecision.REVIEW_REQUIRED,
        ReleaseDecision.APPROVED_EXCEPTION,
        ReleaseDecision.PASS,
    ):
        if decision in decisions:
            return decision
    raise ValueError("Tier 3 produced no release decision")


def _optional_nonnegative_number(key: str) -> float | None:
    raw = os.getenv(key)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError as error:
        raise LiveBenchmarkConfigurationError(f"{key} must be numeric") from error
    if not math.isfinite(value):
        raise LiveBenchmarkConfigurationError(f"{key} must be finite")
    if value < 0:
        raise LiveBenchmarkConfigurationError(f"{key} cannot be negative")
    return value


def run_offline_suite(
    dataset: Dataset,
    handler: Callable[[BenchmarkCase], EvaluationOutput],
    *,
    candidate: CandidateSnapshot,
    usage: tuple[ModelUsage, ...] = (),
) -> BenchmarkReport:
    approved = tuple(case for case in dataset.cases if case.gold.status == GoldStatus.APPROVED)
    outputs = {case.case_id: handler(case) for case in approved}
    scores = tuple(evaluate_case(case, outputs[case.case_id]) for case in approved)
    if any(score.critical_violation for score in scores):
        decision = ReleaseDecision.BLOCKED
    elif not scores or any(
        score.precision < 1 or score.recall < 1 or score.evidence_accuracy < 1 or score.findings
        for score in scores
    ):
        decision = ReleaseDecision.REVIEW_REQUIRED
    else:
        decision = ReleaseDecision.PASS
    fingerprint = (
        dataset.content_fingerprint
        or hashlib.sha256(
            dataset.model_dump_json(exclude={"content_fingerprint"}).encode("utf-8")
        ).hexdigest()
    )
    return BenchmarkReport(
        run_id=str(uuid4()),
        dataset_version=dataset.version,
        rubric_id=dataset.rubric_id,
        rubric_version=dataset.rubric_version,
        candidate=candidate,
        usage=usage,
        total_cost_usd=round(sum(item.cost_usd for item in usage), 10),
        case_scores=scores,
        eligible_count=len(scores),
        excluded_count=len(dataset.cases) - len(scores),
        overall_precision=sum(score.precision for score in scores) / len(scores) if scores else 0,
        overall_recall=sum(score.recall for score in scores) / len(scores) if scores else 0,
        decision=decision,
        input_fingerprint=fingerprint,
        output_fingerprint=fingerprint_outputs(outputs),
        review_outputs=(outputs,),
        evaluated_case_ids=tuple(score.case_id for score in scores),
        failure_case_ids=tuple(
            score.case_id
            for score in scores
            if score.critical_violation
            or score.findings
            or min(score.precision, score.recall, score.evidence_accuracy) < 1
        ),
        evaluator_assessments=tuple(
            output.evaluator_assessment
            for output in outputs.values()
            if output.evaluator_assessment is not None
        ),
        code_fingerprint=code_fingerprint(),
    )
