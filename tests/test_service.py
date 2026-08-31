from __future__ import annotations

import os
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from dingtalk_live_recorder.app import (
    EXIT_DINGTALK_STARTUP_ERROR,
    EXIT_GROUPS_UNRECOGNIZED,
    run_application,
    run_monitor,
)
from dingtalk_live_recorder.config import AppConfig, ConfigError, load_config
from dingtalk_live_recorder.dingtalk import (
    AllGroupsUnrecognizedError,
    DingTalkStartupError,
    GroupInspection,
    LiveWindow,
    close_live_summary_windows,
    select_prioritized_live,
)
from dingtalk_live_recorder.logging_setup import (
    LOG_FILE_NAME,
    RuntimeDirectoryError,
    configure_logging,
    create_runtime_directories,
)


def write_config(path: Path, *, output_dir: str = "recordings") -> None:
    path.write_text(
        f"output_dir: {output_dir}\n"
        "target_groups:\n"
        "  - 第一群\n"
        "  - 第二群\n"
        "log:\n"
        "  directory: logs\n"
        "  retention_days: 7\n",
        encoding="utf-8",
    )


def test_config_defaults_and_group_order(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "target_groups:\n  - 第一群\n  - 第二群\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.output_dir == (tmp_path / "recordings").resolve()
    assert config.log_dir == (tmp_path / "logs").resolve()
    assert config.log_retention_days == 30
    assert config.video_encoder == "auto"
    assert config.scan_interval_seconds == 15
    assert config.target_groups == ("第一群", "第二群")


def test_config_accepts_gpu_video_encoder(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "target_groups:\n  - 第一群\nscan_interval_seconds: 7\nrecording:\n  video_encoder: h264_nvenc\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    assert config.scan_interval_seconds == 7
    assert config.video_encoder == "h264_nvenc"



@pytest.mark.parametrize("interval", ["0", "-1", "true", "\"15\""])
def test_config_rejects_invalid_scan_interval(tmp_path: Path, interval: str) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"target_groups:\n  - 第一群\nscan_interval_seconds: {interval}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="scan_interval_seconds 必须是正整数"):
        load_config(config_path)

def test_application_passes_configured_video_encoder_to_recorder(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "target_groups:\n  - 第一群\nrecording:\n  video_encoder: h264_nvenc\n",
        encoding="utf-8",
    )
    initialized_with: list[tuple[Path, str]] = []

    class Session:
        def scan(self, groups):
            raise KeyboardInterrupt

        def close(self):
            pass

    class Recorder:
        def __init__(self, output_dir: Path, video_encoder: str) -> None:
            initialized_with.append((output_dir, video_encoder))

    assert run_application(config_path, session_factory=Session, recorder_factory=Recorder) == 0
    assert initialized_with == [(tmp_path / "recordings", "h264_nvenc")]


def test_selects_first_live_group_by_configured_priority() -> None:
    inspected: list[str] = []

    def inspect(group_name: str) -> GroupInspection:
        inspected.append(group_name)
        if group_name == "第一群":
            return GroupInspection(recognized=True)
        return GroupInspection(
            recognized=True,
            live_window=LiveWindow(group_name=group_name, hwnd=42),
        )

    recognized, selected = select_prioritized_live(
        ("第一群", "第二群", "第三群"),
        inspect,
    )

    assert recognized == 2
    assert selected == LiveWindow(group_name="第二群", hwnd=42)
    assert inspected == ["第一群", "第二群"]


def test_recording_blocks_later_scans() -> None:
    events: list[str] = []

    class Session:
        def scan(self, groups):
            events.append("scan")
            if events.count("scan") == 1:
                return LiveWindow(group_name=groups[0], hwnd=7)
            return None

    class Recorder:
        def record(self, hwnd, group_name):
            events.append(f"record:{group_name}:{hwnd}")
            assert events == ["scan", "record:第一群:7"]

    config = AppConfig(Path("recordings"), ("第一群",), Path("logs"), 7)
    sleeps: list[float] = []

    run_monitor(
        config,
        Session(),
        Recorder(),
        sleep=sleeps.append,
        max_scans=2,
        close_summary=lambda: events.append("close-summary"),
    )

    assert events == ["scan", "record:第一群:7", "close-summary", "scan"]
    assert sleeps == [10]


def test_monitor_retries_after_live_window_timeout() -> None:
    scans = 0
    sleeps: list[float] = []

    class Session:
        def scan(self, groups):
            nonlocal scans
            scans += 1
            if scans == 1:
                raise TimeoutError("10 秒内未确认进入直播界面")
            return None

    config = AppConfig(
        Path("recordings"),
        ("第一群",),
        Path("logs"),
        7,
        scan_interval_seconds=7,
    )

    run_monitor(
        config,
        Session(),
        object(),
        sleep=sleeps.append,
        max_scans=2,
    )

    assert scans == 2
    assert sleeps == [7]


def test_closes_only_visible_dingtalk_summary_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    import dingtalk_live_recorder.dingtalk as dingtalk_module

    class User32:
        def __init__(self) -> None:
            self.closed: list[int] = []

        def IsWindowVisible(self, hwnd: int) -> bool:
            return hwnd != 3

        def PostMessageW(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
            assert message == 0x0010
            self.closed.append(hwnd)
            return True

    class Window:
        def __init__(self, hwnd: int, title: str) -> None:
            self.hwnd = hwnd
            self.window_name = title
            self.class_name = "StandardFrame"

    user32 = User32()
    windows = [Window(1, "统计"), Window(2, "钉钉"), Window(3, "统计"), Window(4, "统计")]
    paths = {1: r"C:\DingTalk\DingTalk.exe", 3: r"C:\DingTalk\DingTalk.exe", 4: r"C:\Other\Other.exe"}
    monkeypatch.setattr(dingtalk_module, "USER32", user32)
    monkeypatch.setattr(dingtalk_module.Toolkit, "find_desktop_windows", lambda: windows)
    monkeypatch.setattr(dingtalk_module, "_window_process_path", paths.__getitem__)

    assert close_live_summary_windows() == 1
    assert user32.closed == [1]


def test_waits_for_delayed_live_summary_window(monkeypatch: pytest.MonkeyPatch) -> None:
    import dingtalk_live_recorder.dingtalk as dingtalk_module

    windows = iter([[], [SimpleNamespace(hwnd=7, window_name="统计")]])
    closed: list[int] = []
    clock = [0.0]
    user32 = SimpleNamespace(
        IsWindowVisible=lambda _: True,
        PostMessageW=lambda hwnd, *_: closed.append(hwnd) or True,
    )
    monkeypatch.setattr(dingtalk_module, "USER32", user32)
    monkeypatch.setattr(dingtalk_module.Toolkit, "find_desktop_windows", lambda: next(windows))
    monkeypatch.setattr(dingtalk_module, "_window_process_path", lambda _: r"C:\DingTalk.exe")

    assert close_live_summary_windows(
        timeout=1.0,
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        monotonic=lambda: clock[0],
    ) == 1
    assert closed == [7]


def test_runtime_directories_are_created_and_old_logs_removed(tmp_path: Path) -> None:
    output_dir = tmp_path / "nested" / "recordings"
    log_dir = tmp_path / "nested" / "logs"
    create_runtime_directories(output_dir, log_dir)
    old_log = log_dir / f"{LOG_FILE_NAME}.old"
    old_log.write_text("old", encoding="utf-8")
    old_timestamp = time.time() - 10 * 24 * 60 * 60
    os.utime(old_log, (old_timestamp, old_timestamp))

    configure_logging(log_dir, retention_days=7)

    assert output_dir.is_dir()
    assert log_dir.is_dir()
    assert not old_log.exists()


def test_directory_creation_failure_is_fatal(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("x", encoding="utf-8")

    with pytest.raises(RuntimeDirectoryError, match="创建录制结果目录失败"):
        create_runtime_directories(parent_file / "recordings", tmp_path / "logs")


def test_dingtalk_startup_failure_returns_dedicated_exit_code(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path)

    def fail_startup():
        raise DingTalkStartupError("cannot start")

    assert run_application(config_path, session_factory=fail_startup) == EXIT_DINGTALK_STARTUP_ERROR


def test_all_groups_unrecognized_returns_dedicated_exit_code(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path)

    class Session:
        def scan(self, groups):
            raise AllGroupsUnrecognizedError(",".join(groups))

        def close(self):
            pass

    assert (
        run_application(config_path, session_factory=Session)
        == EXIT_GROUPS_UNRECOGNIZED
    )
