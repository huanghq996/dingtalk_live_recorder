from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_OUTPUT_DIRECTORY = "recordings"
DEFAULT_LOG_DIRECTORY = "logs"
DEFAULT_LOG_RETENTION_DAYS = 30


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppConfig:
    output_dir: Path
    target_groups: tuple[str, ...]
    log_dir: Path
    log_retention_days: int


def _resolve_directory(base_dir: Path, value: Any, default: str, field: str) -> Path:
    selected = default if value is None or value == "" else value
    if not isinstance(selected, str):
        raise ConfigError(f"{field} 必须是字符串路径")
    path = Path(selected).expanduser()
    return (base_dir / path).resolve() if not path.is_absolute() else path.resolve()


def load_config(path: Path) -> AppConfig:
    config_path = path.resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"配置文件不存在: {config_path}") from error
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"读取配置文件失败: {config_path}: {error}") from error

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 顶层必须是映射")

    groups = raw.get("target_groups")
    if not isinstance(groups, list) or not groups:
        raise ConfigError("target_groups 必须是非空字符串列表")
    if any(not isinstance(group, str) or not group.strip() for group in groups):
        raise ConfigError("target_groups 必须是非空字符串列表")
    normalized_groups = tuple(group.strip() for group in groups)

    log_config = raw.get("log") or {}
    if not isinstance(log_config, dict):
        raise ConfigError("log 必须是映射")
    retention_days = log_config.get("retention_days", DEFAULT_LOG_RETENTION_DAYS)
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 1:
        raise ConfigError("log.retention_days 必须是正整数")

    base_dir = config_path.parent
    return AppConfig(
        output_dir=_resolve_directory(
            base_dir,
            raw.get("output_dir"),
            DEFAULT_OUTPUT_DIRECTORY,
            "output_dir",
        ),
        target_groups=normalized_groups,
        log_dir=_resolve_directory(
            base_dir,
            log_config.get("directory"),
            DEFAULT_LOG_DIRECTORY,
            "log.directory",
        ),
        log_retention_days=retention_days,
    )
