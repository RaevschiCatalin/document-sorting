from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from docsort.models import AppConfig


def _resolve_path(value: Path, base_dir: Path) -> Path:
    return value if value.is_absolute() else (base_dir / value).resolve()


def load_config(path: Path | None = None) -> AppConfig:
    """Load YAML config and resolve paths relative to the current working directory."""
    data: dict[str, Any] = {}
    if path is not None:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

    config = AppConfig.model_validate(data)
    base_dir = Path.cwd()
    config.input_dir = _resolve_path(config.input_dir, base_dir)
    config.output_dir = _resolve_path(config.output_dir, base_dir)
    config.report_path = _resolve_path(config.report_path, base_dir)
    config.manifest_path = _resolve_path(config.manifest_path, base_dir)
    if config.logging.file_path is not None:
        config.logging.file_path = _resolve_path(config.logging.file_path, base_dir)
    if config.logging.error_file_path is not None:
        config.logging.error_file_path = _resolve_path(config.logging.error_file_path, base_dir)
    return config
