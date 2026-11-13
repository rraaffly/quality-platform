"""Private immutable evaluation records for the local Web worker."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quality_platform.evaluation.calibration import Review
from quality_platform.evaluation.models import (
    BenchmarkReport,
    EvalRecord,
    ProductivityEvidence,
)
from quality_platform.evaluation.release import (
    IndependentReviews,
    create_calibration,
    validate_independent_reviews,
)
from quality_platform.evaluation.reporting import (
    _atomic_write,
    _restrict_private_path,
    validate_review_packet,
    write_report,
)
from quality_platform.evaluation.runner import run_full_benchmark, run_live_benchmark
from quality_platform.evaluation.workflow_evidence import (
    attach_workflow_evidence,
    capture_workflow_suite,
)
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


class EvaluationActionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["evaluation_action"]
    action: Literal["capture_workflow", "record_productivity", "capture_review", "calibrate"]
    project_id: SafeID
    report_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    human_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviews: tuple[Review, ...] = ()
    productivity: ProductivityEvidence | None = None
    confirm_independent: bool = False
    confirm_all_repeats_reviewed: bool = False

    @model_validator(mode="after")
    def required_evidence(self) -> "EvaluationActionCommand":
        if self.action == "capture_review" and (not self.confirm_independent or not self.reviews):
            raise ValueError("Independent human ratings are required")
        if self.action == "calibrate" and not self.human_id:
            raise ValueError("Human review ID is required")
        if self.action == "record_productivity" and self.productivity is None:
            raise ValueError("Equivalent-task productivity evidence is required")
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


def register_report(
    control: Control, project_id: str, report: BenchmarkReport, *, source: str = "import"
) -> dict[str, str]:
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
    summary["source"] = source
    summary["record_id"] = record_id
    entry_id = digest(f"{project_id}:{record_id}")
    with control.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO reports VALUES (?,?,?)",
            (entry_id, project_id, json.dumps(summary, sort_keys=True)),
        )
    return {"report_id": record_id, "run_id": report.run_id, "decision": report.decision.value}


def web_report(control: Control, project_id: str, report_id: str) -> BenchmarkReport:
    entry_id = digest(f"{project_id}:{report_id}")
    with control.connect() as db:
        row = db.execute(
            "SELECT payload FROM reports WHERE id=? AND project_id=?", (entry_id, project_id)
        ).fetchone()
    if row is None or json.loads(row["payload"]).get("source") != "web":
        raise PermissionError("Release evidence requires a Web-generated benchmark")
    return EvaluationStore(control.state).load(report_id, BenchmarkReport)


def review_packet(control: Control, project_id: str, report_id: str) -> dict:
    report = web_report(control, project_id, report_id)
    path = Path(report.review_packet_path or "").resolve()
    if not path.is_relative_to((control.state / "evaluation").resolve()):
        raise PermissionError("Review packet is outside the local evaluation state")
    packet = validate_review_packet(report)
    return packet.model_dump(mode="json", exclude_none=True)


async def execute_action(
    control: Control, command: EvaluationActionCommand, job_id: str
) -> dict[str, str | int]:
    base = control.state / "evaluation"
    receipts = base / "jobs"
    receipts.mkdir(parents=True, exist_ok=True)
    _restrict_private_path(base, 0o700)
    _restrict_private_path(receipts, 0o700)
    receipt = receipts / f"{job_id}.json"
    if receipt.exists():
        return json.loads(receipt.read_text(encoding="utf-8"))
    report = web_report(control, command.project_id, command.report_id)
    store = EvaluationStore(control.state)
    if command.action == "capture_workflow":
        evidence_path = base / "evidence" / f"{job_id}.junit.xml"
        evidence = await asyncio.to_thread(capture_workflow_suite, report, evidence_path)
        store.save("workflow-suite", evidence)
        updated = attach_workflow_evidence(report, evidence)
        result: dict[str, str | int] = {}
        result.update(register_report(control, command.project_id, updated, source="web"))
    elif command.action == "record_productivity":
        assert command.productivity is not None
        updated = report.model_copy(
            update={
                "productivity_evidence": command.productivity,
                "active_qa_minutes": command.productivity.active_qa_minutes,
                "manual_baseline_minutes": command.productivity.manual_baseline_minutes,
                "readiness_labels": tuple(
                    label
                    for label in report.readiness_labels
                    if label != "PRODUCTIVITY_EVIDENCE_MISSING"
                ),
            }
        )
        result = {}
        result.update(register_report(control, command.project_id, updated, source="web"))
    elif command.action == "capture_review":
        review_packet(control, command.project_id, command.report_id)
        if report.repeat_count > 1 and not command.confirm_all_repeats_reviewed:
            raise ValueError("All repeats must be reviewed")
        human = IndependentReviews(
            candidate_run_id=report.run_id,
            input_fingerprint=report.input_fingerprint,
            output_fingerprint=report.output_fingerprint,
            reviews=command.reviews,
            reviewed_repeat_run_ids=tuple(item.run_id for item in report.repeat_results),
        )
        validate_independent_reviews(report, human)
        result = {"human_id": store.save("human-review", human)}
    else:
        assert command.human_id is not None
        human = store.load(command.human_id, IndependentReviews)
        calibration = create_calibration(report, human)
        result = {
            "calibration_id": store.save("calibration", calibration),
            "sample_size": calibration.calibration.sample_size,
        }
    _atomic_write(receipt, json.dumps(result, sort_keys=True))
    _restrict_private_path(receipt, 0o600)
    return result


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
    result = register_report(control, command.project_id, persisted, source="web")
    _atomic_write(
        receipt,
        json.dumps({"project_id": command.project_id, "result": result}, sort_keys=True),
    )
    _restrict_private_path(receipt, 0o600)
    return result
