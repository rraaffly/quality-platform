from pathlib import Path

import yaml

from quality_platform.config.models import AppConfig, LocalConfig


def load_config(path: Path) -> AppConfig:
    return AppConfig.model_validate(load_local_config(path).model_dump())


def load_local_config(path: Path) -> LocalConfig:
    config_path = path.resolve()
    with config_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise TypeError("configuration must be a YAML mapping")
    config = LocalConfig.model_validate(raw)
    base = config_path.parent
    return config.model_copy(
        update={
            "database_path": (base / config.database_path).resolve(),
            "workspace_root": (base / config.workspace_root).resolve(),
        }
    )
