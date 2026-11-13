"""Private immutable evaluation records for the local Web worker."""

import hashlib
import json
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from quality_platform.evaluation.models import EvalRecord
from quality_platform.evaluation.reporting import _atomic_write, _restrict_private_path
from quality_platform.web.models import SafeID

T = TypeVar("T", bound=EvalRecord)


class EvaluationImportCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["evaluation_import_report"]
    project_id: SafeID
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


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
