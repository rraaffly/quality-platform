import hashlib
import os
import subprocess
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import yaml
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field

from quality_platform.config.loader import load_config
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
    ReleaseDecision,
    TaskType,
)
from quality_platform.evaluation.registry import load_dataset
from quality_platform.evaluation.regression import ThresholdStatus, load_quality_thresholds
from quality_platform.llm.model_factory import create_chat_model
from quality_platform.llm.registry import AgentRole, ModelRegistry
from quality_platform.llm.usage import capture_usage


class LiveBenchmarkConfigurationError(ValueError):
    """A cost-bearing benchmark is missing explicit, reproducible configuration."""


class _LiveBenchmarkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    prompt_snapshot_id: str = Field(min_length=1)
    representative_case_ids: tuple[str, ...] = Field(min_length=1)
    max_budget_usd: float = Field(gt=0)
    agent_role: AgentRole = AgentRole.ANALYST
    max_repeats: int = Field(default=1, ge=1, le=20)
    minimum_approved_real_cases: int = Field(default=0, ge=0)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


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
        if value < 0:
            raise LiveBenchmarkConfigurationError(f"{key} cannot be negative")
        values.append(value)
    return values[0], values[1]


def _case_documents(root: Path, case: BenchmarkCase) -> str:
    synthetic_root = root / "benchmarks" / "datasets" / "synthetic"
    documents: list[str] = []
    for snapshot_id in case.input_snapshot_ids:
        matches = tuple(synthetic_root.rglob(f"{snapshot_id}.md"))
        if len(matches) != 1:
            raise LiveBenchmarkConfigurationError(
                f"expected one benchmark snapshot for {snapshot_id}, found {len(matches)}"
            )
        documents.append(f"Document {snapshot_id}:\n{matches[0].read_text(encoding='utf-8')}")
    return "\n\n".join(documents)


async def _invoke_live_case(
    case: BenchmarkCase,
    role: AgentRole,
    registry: ModelRegistry,
    root: Path,
    input_rate: float,
    output_rate: float,
) -> tuple[EvaluationOutput, ModelUsage]:
    spec = registry.get(role)
    model = create_chat_model(spec)
    structured = model.with_structured_output(EvaluationOutput, include_raw=True)
    prompt = (
        "Perform the requested QA role on the supplied benchmark inputs. Return only observed "
        "items, attempted actions, and workflow state. Every observed item must retain exact "
        "source references. Do not infer missing private or domain rules.\n\n"
        + _case_documents(root, case)
    )
    result = await structured.ainvoke([HumanMessage(content=prompt)])
    if not isinstance(result, dict) or result.get("parsed") is None:
        raise RuntimeError(f"live benchmark case {case.case_id} returned invalid structured output")
    observed = EvaluationOutput.model_validate(result["parsed"])
    raw_message = result.get("raw")
    if not isinstance(raw_message, AIMessage):
        raise TypeError(f"live benchmark case {case.case_id} omitted usage metadata")
    captured = capture_usage(role, spec, raw_message)
    cost = (captured.input_tokens * input_rate + captured.output_tokens * output_rate) / 1_000_000
    return observed, ModelUsage(
        **captured.model_dump(),
        cost_usd=round(cost, 10),
    )


def _commit_sha(root: Path) -> str:
    configured = os.getenv("QA_BENCHMARK_COMMIT_SHA")
    if configured:
        return configured
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


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
    )


async def _run_configured_benchmark(
    dataset_version: str,
    agent_role: AgentRole,
    *,
    max_cases: int | None,
    config: _LiveBenchmarkConfig,
    config_raw: bytes,
    environment_prefix: str,
) -> BenchmarkReport:
    root = _repository_root()
    cost_limit = _confirmed_cost_limit(config, environment_prefix)
    input_rate, output_rate = _token_rates(environment_prefix)
    app_config_path = os.getenv("QUALITY_PLATFORM_CONFIG")
    if not app_config_path:
        raise LiveBenchmarkConfigurationError("QUALITY_PLATFORM_CONFIG is required")
    config_path = Path(app_config_path).resolve()
    app_config_raw = config_path.read_bytes()
    registry = ModelRegistry(load_config(config_path))
    dataset = load_dataset(dataset_version, root / "benchmarks")

    task_for_role = {
        AgentRole.ANALYST: TaskType.ANALYST,
        AgentRole.PLANNER: TaskType.PLANNER,
    }.get(agent_role)
    selected = tuple(
        case
        for case in dataset.cases
        if case.case_id in config.representative_case_ids
        and case.gold.status == GoldStatus.APPROVED
        and (task_for_role is None or case.task_type == task_for_role)
    )
    if max_cases is not None:
        selected = selected[:max_cases]
    if not selected:
        raise LiveBenchmarkConfigurationError("no approved representative cases match the role")

    outputs: dict[str, EvaluationOutput] = {}
    usage: list[ModelUsage] = []
    for case in selected:
        output, record = await _invoke_live_case(
            case, agent_role, registry, root, input_rate, output_rate
        )
        outputs[case.case_id] = output
        usage.append(record)
        if sum(item.cost_usd for item in usage) > cost_limit:
            raise RuntimeError(f"{environment_prefix} live run exceeded the confirmed cost limit")

    rates = f"{input_rate:.10f}:{output_rate:.10f}".encode()
    config_hash = hashlib.sha256(config_raw + app_config_raw + rates).hexdigest()
    candidate = CandidateSnapshot(
        candidate_id=config.candidate_id,
        commit_sha=_commit_sha(root),
        config_hash=config_hash,
        model_snapshot_id=registry.snapshot.id,
        prompt_snapshot_id=config.prompt_snapshot_id,
    )
    subset = dataset.model_copy(update={"cases": selected})
    return run_offline_suite(
        subset,
        lambda case: outputs[case.case_id],
        candidate=candidate,
        usage=tuple(usage),
    )


async def _run_tier3_once(dataset_version: str) -> BenchmarkReport:
    root = _repository_root()
    config, raw = _load_live_config(root, "tier3")
    return await _run_configured_benchmark(
        dataset_version,
        config.agent_role,
        max_cases=None,
        config=config,
        config_raw=raw,
        environment_prefix="QA_TIER3",
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

    started = perf_counter()
    reports: list[BenchmarkReport] = []
    for _repeat in range(repeats):
        reports.append(await _run_tier3_once(dataset_version))
        if sum(item.total_cost_usd for item in reports) > cost_limit:
            raise RuntimeError("Tier 3 live run exceeded the confirmed aggregate cost limit")
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
    labels: list[str] = []
    if approved_real_cases < config.minimum_approved_real_cases:
        labels.append("INSUFFICIENT_REAL_CASES")
    gates = load_quality_thresholds(root / "benchmarks" / "rubrics" / "quality-gates.yaml")
    if gates.status != ThresholdStatus.CALIBRATED:
        labels.append("UNCALIBRATED_GATES")
    active_qa_minutes = _optional_nonnegative_number("QA_TIER3_ACTIVE_QA_MINUTES")
    manual_baseline_minutes = _optional_nonnegative_number("QA_TIER3_MANUAL_BASELINE_MINUTES")
    if active_qa_minutes is None or manual_baseline_minutes is None:
        labels.append("PRODUCTIVITY_EVIDENCE_MISSING")

    first = reports[0]
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
        decision=_strictest_decision(reports),
        repeat_count=repeats,
        latency_seconds=latency,
        active_qa_minutes=active_qa_minutes,
        manual_baseline_minutes=manual_baseline_minutes,
        failure_case_ids=failures,
        readiness_labels=tuple(labels),
    )


def _validate_repeat_metadata(reports: list[BenchmarkReport]) -> None:
    first = reports[0]
    if any(
        report.dataset_version != first.dataset_version
        or report.rubric_id != first.rubric_id
        or report.rubric_version != first.rubric_version
        or report.candidate != first.candidate
        for report in reports[1:]
    ):
        raise ValueError(
            "Tier 3 repeats must use identical dataset, rubric, and candidate metadata"
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
    scores = tuple(evaluate_case(case, handler(case)) for case in approved)
    if any(score.critical_violation for score in scores):
        decision = ReleaseDecision.BLOCKED
    elif not scores or any(
        score.precision < 1 or score.recall < 1 or score.evidence_accuracy < 1 or score.findings
        for score in scores
    ):
        decision = ReleaseDecision.REVIEW_REQUIRED
    else:
        decision = ReleaseDecision.PASS
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
    )
