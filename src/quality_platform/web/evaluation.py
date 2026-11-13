"""Private immutable evaluation records for the local Web worker."""

import hashlib
import json
import os
from pathlib import Path
from typing import Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quality_platform.evaluation.models import BenchmarkReport, EvalRecord
from quality_platform.evaluation.reporting import (
    _atomic_write,
    _restrict_private_path,
    write_report,
)
from quality_platform.evaluation.runner import run_full_benchmark, run_live_benchmark
from quality_platform.llm.registry import AgentRole
from quality_platform.web.control import Control, digest
from quality_platform.web.models import SafeID

T = TypeVar("T", bound=EvalRecord)


class EvaluationImportCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["evaluation_import_report"]
    project_id: SafeID
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelRates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_usd_per_million: float = Field(ge=0, allow_inf_nan=False)
    output_usd_per_million: float = Field(ge=0, allow_inf_nan=False)


class EvaluationRunCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["run_benchmark"]
    project_id: SafeID
    settings_id: SafeID
    tier: Literal[2, 3]
    dataset_version: str = Field(pattern=r"^v[0-9]+(?:\.[0-9]+)*$")
    agent_role: Literal["analyst", "planner"] = "analyst"
    repeats: int = Field(default=1, ge=1, le=5)
    max_cost_usd: float = Field(gt=0, allow_inf_nan=False)
    rates: dict[Literal["analyst", "planner", "evaluator"], ModelRates]
    confirm_cost: bool

    @model_validator(mode="after")
    def validate_confirmation(self) -> "EvaluationRunCommand":
        if not self.confirm_cost:
            raise ValueError("Explicit paid benchmark confirmation is required")
        if set(self.rates) != {"analyst", "planner", "evaluator"}:
            raise ValueError("Rates for analyst, planner and evaluator are required")
        if self.tier == 2 and self.repeats != 1:
            raise ValueError("Tier 2 runs once")
        return self


class EvaluationStore:
    def __init__(self, state: Path) -> None:
        self.root = state.resolve() / "evaluation" / "records"

    def save(self, kind: str, record: EvalRecord) -> str:
        if not kind or not kind.replace("-", "").isalnum():
            raise ValueError("Invalid evaluation record kind")
        payload = json.dumps(
            {"kind": kind, "record": record.model_dump(mode="json")},
            sort_keys=True,
            separators=(",", ":"),
        )
        identity = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        _restrict_private_path(self.root.parent, 0o700)
        _restrict_private_path(self.root, 0o700)
        target = self.root / f"{identity}.json"
        if target.exists():
            if target.read_text(encoding="utf-8") != payload:
                raise ValueError("Evaluation record changed")
        else:
            _atomic_write(target, payload)
        _restrict_private_path(target, 0o600)
        return identity

    def load(self, identity: str, schema: type[T]) -> T:
        if len(identity) != 64 or any(char not in "0123456789abcdef" for char in identity):
            raise ValueError("Invalid evaluation record ID")
        raw = (self.root / f"{identity}.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != identity:
            raise ValueError("Evaluation record checksum mismatch")
        envelope = json.loads(raw)
        return schema.model_validate(envelope["record"])


def register_report(control: Control, project_id: str, report: BenchmarkReport) -> dict[str, str]:
    record_id = EvaluationStore(control.state).save("benchmark-report", report)
    summary = report.model_dump(
        mode="json",
        include={
            "run_id",
            "dataset_version",
            "decision",
            "readiness_labels",
            "candidate",
            "rubric_id",
            "rubric_version",
            "overall_precision",
            "overall_recall",
            "eligible_count",
            "excluded_count",
            "case_scores",
            "metric_variance",
            "total_cost_usd",
            "input_fingerprint",
            "output_fingerprint",
        },
    )
    entry_id = digest(f"{project_id}:{record_id}")
    with control.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO reports VALUES (?,?,?)",
            (entry_id, project_id, json.dumps(summary, sort_keys=True)),
        )
    return {"report_id": record_id, "run_id": report.run_id, "decision": report.decision.value}


async def execute_run(
    control: Control, command: EvaluationRunCommand, job_id: str
) -> dict[str, str]:
    base = control.state / "evaluation"
    receipts = base / "jobs"
    receipts.mkdir(parents=True, exist_ok=True)
    _restrict_private_path(base, 0o700)
    _restrict_private_path(receipts, 0o700)
    receipt = receipts / f"{job_id}.json"
    if receipt.exists():
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        if saved["project_id"] != command.project_id:
            raise ValueError("Benchmark receipt belongs to another project")
        EvaluationStore(control.state).load(saved["result"]["report_id"], BenchmarkReport)
        return saved["result"]

    config = control.config(command.settings_id)
    configs = base / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    _restrict_private_path(configs, 0o700)
    config_path = configs / f"{command.settings_id}.yaml"
    config_yaml = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True)
    if config_path.exists():
        if config_path.read_text(encoding="utf-8") != config_yaml:
            raise ValueError("Pinned benchmark settings changed")
    else:
        _atomic_write(config_path, config_yaml)
    _restrict_private_path(config_path, 0o600)

    prefix = f"QA_TIER{command.tier}"
    selected = command.rates[command.agent_role]
    updates = {
        "QUALITY_PLATFORM_CONFIG": str(config_path),
        f"{prefix}_BUDGET_CONFIRMED": "yes",
        f"{prefix}_MAX_COST_USD": str(command.max_cost_usd),
        f"{prefix}_INPUT_USD_PER_MILLION": str(selected.input_usd_per_million),
        f"{prefix}_OUTPUT_USD_PER_MILLION": str(selected.output_usd_per_million),
    }
    for role, rates in command.rates.items():
        key = prefix + "_" + role.upper()
        updates[f"{key}_INPUT_USD_PER_MILLION"] = str(rates.input_usd_per_million)
        updates[f"{key}_OUTPUT_USD_PER_MILLION"] = str(rates.output_usd_per_million)
    previous = {key: os.environ.get(key) for key in updates}
    try:
        os.environ.update(updates)
        report = (
            await run_live_benchmark(command.dataset_version, AgentRole(command.agent_role), None)
            if command.tier == 2
            else await run_full_benchmark(command.dataset_version, repeats=command.repeats)
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    runs = base / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    _restrict_private_path(runs, 0o700)
    report_path, _ = write_report(report, runs)
    persisted = BenchmarkReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    result = register_report(control, command.project_id, persisted)
    _atomic_write(
        receipt,
        json.dumps({"project_id": command.project_id, "result": result}, sort_keys=True),
    )
    _restrict_private_path(receipt, 0o600)
    return result
