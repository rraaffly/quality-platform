import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
    input_snapshot_hashes: dict[str, str] = Field(default_factory=dict)
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
    dataset_raw = dataset_path.read_bytes()
    gold_raw = gold_path.read_bytes()
    rubric_raw = rubric_path.read_bytes()
    manifests: list[tuple[Literal["synthetic", "real_world"], _DatasetManifest, _GoldManifest]] = [
        (
            "synthetic",
            _DatasetManifest.model_validate(json.loads(dataset_raw)),
            _GoldManifest.model_validate(json.loads(gold_raw)),
        )
    ]
    fingerprint_parts = [dataset_raw, gold_raw, rubric_raw]
    real_dataset_path = benchmark_root / "datasets" / "real_world" / f"{version}.json"
    real_gold_path = benchmark_root / "ground_truth" / "real_world" / f"{version}.json"
    if real_dataset_path.exists() != real_gold_path.exists():
        raise ValueError("real-world dataset and gold manifests must be provided together")
    if real_dataset_path.exists():
        real_dataset_raw = real_dataset_path.read_bytes()
        real_gold_raw = real_gold_path.read_bytes()
        manifests.append(
            (
                "real_world",
                _DatasetManifest.model_validate(json.loads(real_dataset_raw)),
                _GoldManifest.model_validate(json.loads(real_gold_raw)),
            )
        )
        fingerprint_parts.extend((real_dataset_raw, real_gold_raw))
    rubric = Rubric.model_validate(json.loads(rubric_raw))
    benchmark_cases: list[BenchmarkCase] = []
    seen_case_ids: set[str] = set()
    fingerprint = hashlib.sha256(b"".join(fingerprint_parts))
    for origin, manifest, gold_manifest in manifests:
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
        if seen_case_ids.intersection(input_ids):
            raise ValueError("duplicate benchmark case ID across dataset origins")
        seen_case_ids.update(input_ids)
        for case in manifest.cases:
            for snapshot_id in sorted(case.input_snapshot_hashes):
                fingerprint.update(snapshot_id.encode("utf-8"))
                fingerprint.update(case.input_snapshot_hashes[snapshot_id].encode("ascii"))
            benchmark_cases.append(
                BenchmarkCase(
                    case_id=case.case_id,
                    task_type=case.task_type,
                    input_snapshot_ids=case.input_snapshot_ids,
                    input_snapshot_hashes=case.input_snapshot_hashes,
                    severity=case.severity,
                    origin=origin,
                    gold=gold_by_id[case.case_id],
                )
            )
    return Dataset(
        version=version,
        rubric_id=rubric.id,
        rubric_version=rubric.version,
        cases=tuple(benchmark_cases),
        content_fingerprint=fingerprint.hexdigest(),
    )
