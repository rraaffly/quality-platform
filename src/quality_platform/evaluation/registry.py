import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from quality_platform.evaluation.models import (
    BenchmarkCase,
    Dataset,
    GoldRecord,
    Rubric,
    Severity,
    TaskType,
)


class _InputCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    task_type: TaskType
    input_snapshot_ids: tuple[str, ...]
    severity: Severity


class _DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    rubric_version: str
    cases: tuple[_InputCase, ...]


class _GoldCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    gold: GoldRecord


class _GoldManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    cases: tuple[_GoldCase, ...]


def load_dataset(version: str, root: Path) -> Dataset:
    if not re.fullmatch(r"v[0-9]+(?:\.[0-9]+)*", version):
        raise ValueError("invalid dataset version")
    benchmark_root = root.resolve()
    dataset_path = benchmark_root / "datasets" / "synthetic" / f"{version}.json"
    gold_path = benchmark_root / "ground_truth" / f"{version}.json"
    rubric_path = benchmark_root / "rubrics" / f"{version}.json"
    manifest = _DatasetManifest.model_validate(json.loads(dataset_path.read_text(encoding="utf-8")))
    gold_manifest = _GoldManifest.model_validate(json.loads(gold_path.read_text(encoding="utf-8")))
    rubric = Rubric.model_validate(json.loads(rubric_path.read_text(encoding="utf-8")))
    if manifest.version != version or gold_manifest.version != version:
        raise ValueError("dataset and gold versions must match the requested version")
    if rubric.version != manifest.rubric_version:
        raise ValueError("rubric version mismatch")
    gold_by_id = {case.case_id: case.gold for case in gold_manifest.cases}
    if len(gold_by_id) != len(gold_manifest.cases):
        raise ValueError("duplicate gold case ID")
    input_ids = {case.case_id for case in manifest.cases}
    if input_ids != set(gold_by_id):
        raise ValueError("dataset and gold case IDs differ")
    return Dataset(
        version=version,
        rubric_id=rubric.id,
        rubric_version=rubric.version,
        cases=tuple(
            BenchmarkCase(
                case_id=case.case_id,
                task_type=case.task_type,
                input_snapshot_ids=case.input_snapshot_ids,
                severity=case.severity,
                gold=gold_by_id[case.case_id],
            )
            for case in manifest.cases
        ),
    )
