from __future__ import annotations

import logging
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "dingtalk-live-recorder.log"


class RuntimeDirectoryError(RuntimeError):
    pass


def create_runtime_directories(output_dir: Path, log_dir: Path) -> None:
    for label, directory in (("录制结果", output_dir), ("日志", log_dir)):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RuntimeDirectoryError(f"创建{label}目录失败: {directory}: {error}") from error
        if not directory.is_dir():
            raise RuntimeDirectoryError(f"{label}目录路径不是目录: {directory}")


def _delete_expired_logs(log_dir: Path, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 24 * 60 * 60
    for path in log_dir.glob(f"{LOG_FILE_NAME}.*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            logging.getLogger(__name__).warning("删除过期日志失败: %s", path, exc_info=True)


def configure_logging(log_dir: Path, retention_days: int) -> None:
    _delete_expired_logs(log_dir, retention_days)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = TimedRotatingFileHandler(
        log_dir / LOG_FILE_NAME,
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler],
        force=True,
    )
