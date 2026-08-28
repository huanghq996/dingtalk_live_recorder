from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .config import AppConfig, ConfigError, load_config
from .dingtalk import (
    AllGroupsUnrecognizedError,
    DingTalkSession,
    DingTalkStartupError,
    close_live_summary_windows,
)
from .logging_setup import (
    RuntimeDirectoryError,
    configure_logging,
    create_runtime_directories,
)
from .recorder import LiveRecorder

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
EXIT_UNEXPECTED_ERROR = 1
EXIT_CONFIGURATION_ERROR = 2
EXIT_DINGTALK_STARTUP_ERROR = 3
EXIT_GROUPS_UNRECOGNIZED = 4


def run_monitor(
    config: AppConfig,
    session,
    recorder,
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_scans: int | None = None,
    close_summary: Callable[[], int | None] = close_live_summary_windows,
) -> None:
    scan_count = 0
    while True:
        LOGGER.info(
            "开始扫描群聊直播，优先级=%s, interval=%ss",
            list(config.target_groups),
            config.scan_interval_seconds,
        )
        live_window = session.scan(config.target_groups)
        scan_count += 1
        if live_window is not None:
            LOGGER.info(
                "暂停定时扫描并开始录制: group=%s, hwnd=%s",
                live_window.group_name,
                live_window.hwnd,
            )
            try:
                recorder.record(live_window.hwnd, live_window.group_name)
            finally:
                close_summary()
            LOGGER.info("录制结束，恢复定时扫描: group=%s", live_window.group_name)
        if max_scans is not None and scan_count >= max_scans:
            return
        sleep(config.scan_interval_seconds)


def run_application(
    config_path: Path = CONFIG_PATH,
    *,
    session_factory=DingTalkSession.open,
    recorder_factory=LiveRecorder,
) -> int:
    try:
        config = load_config(config_path)
        create_runtime_directories(config.output_dir, config.log_dir)
    except (ConfigError, RuntimeDirectoryError) as error:
        print(f"启动失败: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR

    try:
        configure_logging(config.log_dir, config.log_retention_days)
    except OSError as error:
        print(f"启动失败: 初始化日志失败: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR

    LOGGER.info(
        "服务启动: output_dir=%s, log_dir=%s, log_retention_days=%s, scan_interval_seconds=%s",
        config.output_dir,
        config.log_dir,
        config.log_retention_days,
        config.scan_interval_seconds,
    )
    session = None
    try:
        session = session_factory()
        recorder = recorder_factory(config.output_dir, config.video_encoder)
        run_monitor(config, session, recorder)
    except DingTalkStartupError:
        LOGGER.exception("启动钉钉失败")
        return EXIT_DINGTALK_STARTUP_ERROR
    except AllGroupsUnrecognizedError:
        LOGGER.exception("配置的群聊全部无法识别，服务退出")
        return EXIT_GROUPS_UNRECOGNIZED
    except KeyboardInterrupt:
        LOGGER.info("收到 Ctrl+C，服务停止")
        return 0
    except Exception:
        LOGGER.exception("服务异常退出")
        return EXIT_UNEXPECTED_ERROR
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                LOGGER.exception("关闭钉钉自动化会话失败")
    return 0


def main() -> None:
    raise SystemExit(run_application())
