import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
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
    Dataset,
    EvaluationOutput,
    GoldStatus,
    ModelUsage,
    ReleaseDecision,
    TaskType,
)
from quality_platform.evaluation.registry import load_dataset
from quality_platform.llm.model_factory import create_chat_model
from quality_platform.llm.registry import AgentRole, ModelRegistry
from quality_platform.llm.usage import capture_usage


class LiveBenchmarkConfigurationError(ValueError):
    """A cost-bearing benchmark is missing explicit, reproducible configuration."""


class _Tier2Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    prompt_snapshot_id: str = Field(min_length=1)
    representative_case_ids: tuple[str, ...] = Field(min_length=1)
    max_budget_usd: float = Field(gt=0)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_tier2_config(root: Path) -> tuple[_Tier2Config, bytes]:
    path = root / "benchmarks" / "configs" / "tier2.yaml"
    raw = path.read_bytes()
    loaded = yaml.safe_load(raw)
    if not isinstance(loaded, dict):
        raise LiveBenchmarkConfigurationError("Tier 2 configuration must be a YAML mapping")
    return _Tier2Config.model_validate(loaded), raw


def _confirmed_cost_limit(config: _Tier2Config) -> float:
    if os.getenv("QA_TIER2_BUDGET_CONFIRMED") != "yes":
        raise LiveBenchmarkConfigurationError(
            "set QA_TIER2_BUDGET_CONFIRMED=yes only after approving a live API run"
        )
    raw_limit = os.getenv("QA_TIER2_MAX_COST_USD")
    if raw_limit is None:
        raise LiveBenchmarkConfigurationError("QA_TIER2_MAX_COST_USD is required")
    try:
        limit = float(raw_limit)
    except ValueError as error:
        raise LiveBenchmarkConfigurationError("QA_TIER2_MAX_COST_USD must be numeric") from error
    if limit <= 0 or limit > config.max_budget_usd:
        raise LiveBenchmarkConfigurationError(
            f"confirmed cost limit must be within (0, {config.max_budget_usd}]"
        )
    return limit


def _token_rates() -> tuple[float, float]:
    values: list[float] = []
    for key in (
        "QA_TIER2_INPUT_USD_PER_MILLION",
        "QA_TIER2_OUTPUT_USD_PER_MILLION",
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
    tier2, tier2_raw = _load_tier2_config(root)
    cost_limit = _confirmed_cost_limit(tier2)
    input_rate, output_rate = _token_rates()
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
        if case.case_id in tier2.representative_case_ids
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
            raise RuntimeError("Tier 2 live run exceeded the confirmed cost limit")

    rates = f"{input_rate:.10f}:{output_rate:.10f}".encode()
    config_hash = hashlib.sha256(tier2_raw + app_config_raw + rates).hexdigest()
    candidate = CandidateSnapshot(
        candidate_id=tier2.candidate_id,
        commit_sha=_commit_sha(root),
        config_hash=config_hash,
        model_snapshot_id=registry.snapshot.id,
        prompt_snapshot_id=tier2.prompt_snapshot_id,
    )
    subset = dataset.model_copy(update={"cases": selected})
    return run_offline_suite(
        subset,
        lambda case: outputs[case.case_id],
        candidate=candidate,
        usage=tuple(usage),
    )


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
