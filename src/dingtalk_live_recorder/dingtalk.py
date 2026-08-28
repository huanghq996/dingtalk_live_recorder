from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from maa.controller import (
    MaaWin32InputMethodEnum,
    MaaWin32ScreencapMethodEnum,
    Win32Controller,
)
from maa.pipeline import JOCR, JRecognitionType
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = PROJECT_ROOT / "assets"
DINGTALK_INSTALL_ROOTS = tuple(
    Path(value)
    for value in (
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        r"D:\install",
    )
    if value
)
DINGTALK_PROCESS = re.compile(r"dingtalk\.exe$", re.IGNORECASE)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SHADOW_WINDOW_CLASSES = {"DuiShadowWnd"}
ANCHOR_TEXTS = {"消息", "未读"}
LIVE_BANNER_TEXT = "正在直播"
LIVE_WINDOW_CLASS = "StandardFrame"
LIVE_SUMMARY_WINDOW_TITLES = {"统计"}
WM_CLOSE = 0x0010

USER32 = ctypes.windll.user32
USER32.IsWindowVisible.argtypes = [ctypes.c_void_p]
USER32.IsWindowVisible.restype = ctypes.c_bool
USER32.PostMessageW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
USER32.PostMessageW.restype = ctypes.c_bool


class DingTalkStartupError(RuntimeError):
    pass


class AllGroupsUnrecognizedError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveWindow:
    group_name: str
    hwnd: int


@dataclass(frozen=True)
class GroupInspection:
    recognized: bool
    live_window: LiveWindow | None = None


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def box_tuple(result) -> tuple[int, int, int, int]:
    x, y, width, height = result.box
    return x, y, width, height


def _window_process_path(hwnd: int) -> str:
    process_id = ctypes.c_ulong()
    USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    process_handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        process_id.value,
    )
    if not process_handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buffer))
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
            process_handle,
            0,
            buffer,
            ctypes.byref(size),
        ):
            return ""
        return buffer.value
    finally:
        ctypes.windll.kernel32.CloseHandle(process_handle)


def find_dingtalk_window():
    candidates = []
    for window in Toolkit.find_desktop_windows():
        hwnd = window.hwnd.value if hasattr(window.hwnd, "value") else int(window.hwnd)
        process_path = _window_process_path(hwnd)
        if not DINGTALK_PROCESS.search(process_path):
            continue
        if not USER32.IsWindowVisible(hwnd):
            continue
        if window.class_name in SHADOW_WINDOW_CLASSES or not window.window_name.strip():
            continue
        if window.window_name.strip() in LIVE_SUMMARY_WINDOW_TITLES:
            continue
        is_main_window = (
            window.class_name == "Qt51511QWindowIcon"
            and window.window_name.strip() == "钉钉"
        )
        candidates.append((not is_main_window, window, process_path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])

    _, window, process_path = candidates[0]
    return window, process_path


def close_live_summary_windows() -> int:
    try:
        windows = Toolkit.find_desktop_windows()
    except Exception:
        LOGGER.exception("查找直播结束统计窗口失败")
        return 0

    closed_count = 0
    for window in windows:
        title = window.window_name.strip()
        if title not in LIVE_SUMMARY_WINDOW_TITLES:
            continue
        if hasattr(window.hwnd, "value"):
            hwnd = window.hwnd.value
        else:
            hwnd = int(window.hwnd)
        if not USER32.IsWindowVisible(hwnd):
            continue
        if not DINGTALK_PROCESS.search(_window_process_path(hwnd)):
            continue
        if USER32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            closed_count += 1
            LOGGER.info("已关闭直播结束统计窗口: hwnd=%s, title=%r", hwnd, title)
        else:
            LOGGER.warning("关闭直播结束统计窗口失败: hwnd=%s, title=%r", hwnd, title)
    return closed_count


def find_dingtalk_executable() -> Path | None:
    path_from_environment = shutil.which("DingTalk.exe")
    if path_from_environment:
        return Path(path_from_environment)
    for install_root in DINGTALK_INSTALL_ROOTS:
        for version_directory in ("current", "current_new"):
            candidate = install_root / "DingDing" / "main" / version_directory / "DingTalk.exe"
            if candidate.is_file():
                return candidate
    return None


def ensure_dingtalk_window():
    candidate = find_dingtalk_window()
    if candidate:
        return candidate
    executable = find_dingtalk_executable()
    if executable is None:
        searched = ", ".join(str(root) for root in DINGTALK_INSTALL_ROOTS)
        raise DingTalkStartupError(f"未找到钉钉程序；已检查 PATH 和安装目录: {searched}")
    try:
        subprocess.Popen([str(executable)], close_fds=True)
    except OSError as error:
        raise DingTalkStartupError(f"启动钉钉失败: {executable}: {error}") from error

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        candidate = find_dingtalk_window()
        if candidate:
            return candidate
        time.sleep(0.5)
    raise DingTalkStartupError("钉钉已启动，但 30 秒内没有找到可控制的顶层窗口")


def create_controller(window) -> Win32Controller:
    controller = Win32Controller(
        hWnd=window.hwnd,
        screencap_method=MaaWin32ScreencapMethodEnum.FramePool,
        mouse_method=MaaWin32InputMethodEnum.Seize,
        keyboard_method=MaaWin32InputMethodEnum.PostMessage,
    )
    if not controller.post_connection().wait().status.succeeded:
        raise DingTalkStartupError("钉钉 Win32 Controller 连接失败")
    return controller


def screencap(controller: Win32Controller):
    image = controller.post_screencap().wait().get()
    if image is None or image.size == 0:
        raise RuntimeError("钉钉截图为空")
    return image


def recognize_ocr(
    tasker: Tasker,
    image,
    roi: tuple[int, int, int, int],
    expected: list[str],
):
    job = tasker.post_recognition(
        JRecognitionType.OCR,
        JOCR(expected=expected, roi=roi, threshold=0.3, order_by="Horizontal"),
        image,
    ).wait()
    task_detail = job.get()
    if task_detail is None:
        raise RuntimeError("OCR 识别任务结果为空")
    for node in task_detail.nodes:
        if node.recognition is not None:
            return node.recognition
    raise RuntimeError("OCR 未返回识别结果")


def find_anchor_pair(recognition) -> tuple[Any, Any]:
    results = [
        result
        for result in recognition.all_results
        if normalize_text(result.text) in ANCHOR_TEXTS
    ]
    messages = [result for result in results if normalize_text(result.text) == "消息"]
    unread = [result for result in results if normalize_text(result.text) == "未读"]
    candidates = []
    for message in messages:
        message_x, message_y, message_width, message_height = box_tuple(message)
        message_center_y = message_y + message_height // 2
        message_center_x = message_x + message_width // 2
        for unread_button in unread:
            unread_x, unread_y, unread_width, unread_height = box_tuple(unread_button)
            unread_center_y = unread_y + unread_height // 2
            unread_center_x = unread_x + unread_width // 2
            if unread_center_x <= message_center_x:
                continue
            vertical_distance = abs(message_center_y - unread_center_y)
            if vertical_distance <= max(message_height, unread_height):
                candidates.append((vertical_distance, message_y, message, unread_button))
    if not candidates:
        found = ", ".join(
            f"{result.text!r}@{tuple(result.box)}" for result in recognition.all_results
        )
        raise RuntimeError(f"未识别到同一行的“消息/未读”按钮；OCR 结果: {found}")
    _, _, message, unread_button = min(candidates, key=lambda item: item[:2])
    return message, unread_button


def build_conversation_roi(
    message,
    unread_button,
    image_width: int,
    image_height: int,
) -> tuple[int, int, int, int]:
    message_x, message_y, message_width, message_height = box_tuple(message)
    unread_x, unread_y, unread_width, unread_height = box_tuple(unread_button)
    horizontal_gap = (
        unread_x + unread_width // 2 - (message_x + message_width // 2)
    )
    button_height = max(message_height, unread_height)
    left = max(0, message_x - horizontal_gap)
    right = min(image_width, unread_x + unread_width + horizontal_gap * 2)
    top = min(
        image_height,
        max(message_y + message_height, unread_y + unread_height) + button_height,
    )
    if right <= left or top >= image_height:
        raise RuntimeError("根据消息锚点计算出的会话列表区域无效")
    return left, top, right - left, image_height - top


def build_banner_roi(
    conversation_roi: tuple[int, int, int, int],
    image_width: int,
    image_height: int,
) -> tuple[int, int, int, int]:
    left = conversation_roi[0] + conversation_roi[2]
    if left >= image_width:
        raise RuntimeError("会话主面板区域无效")
    return left, 0, image_width - left, min(image_height, max(180, image_height // 3))


def find_target(recognition, group_name: str):
    normalized_name = normalize_text(group_name)
    candidates = [
        result
        for result in recognition.filtered_results
        if normalize_text(result.text).startswith(normalized_name)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda result: box_tuple(result)[0])


def find_live_banner(recognition):
    candidates = [
        result
        for result in recognition.filtered_results
        if LIVE_BANNER_TEXT in normalize_text(result.text)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda result: box_tuple(result)[1])


def find_live_marker(recognition):
    candidates = [
        result
        for result in recognition.filtered_results
        if normalize_text(result.text).startswith("直播")
        and LIVE_BANNER_TEXT not in normalize_text(result.text)
    ]
    if not candidates:
        raise RuntimeError("未识别到直播界面标记")
    return min(candidates, key=lambda result: box_tuple(result)[1])


def click_result(controller: Win32Controller, result, label: str) -> None:
    x, y, width, height = box_tuple(result)
    center = (x + width // 2, y + height // 2)
    if not controller.post_click(*center).wait().status.succeeded:
        raise RuntimeError(f"点击{label}失败")
    LOGGER.info("已点击%s: box=%s", label, tuple(result.box))


def focus_dingtalk_main_window() -> None:
    window, _ = ensure_dingtalk_window()
    hwnd = window.hwnd.value if hasattr(window.hwnd, "value") else int(window.hwnd)
    USER32.ShowWindow(hwnd, 9)
    if not USER32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040):
        raise RuntimeError("无法将钉钉主窗口置顶执行点击")
    USER32.SetForegroundWindow(hwnd)
    time.sleep(0.2)


def wait_for_live_window(resource: Resource, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    last_error = "未找到钉钉直播窗口"
    while time.monotonic() < deadline:
        windows = [
            window
            for window in Toolkit.find_desktop_windows()
            if window.class_name == LIVE_WINDOW_CLASS
            and window.window_name.strip() == "钉钉"
        ]
        for window in windows:
            live_tasker = None
            try:
                live_controller = create_controller(window)
                live_tasker = Tasker()
                if not live_tasker.bind(resource, live_controller) or not live_tasker.inited:
                    raise RuntimeError("直播窗口 Tasker 初始化失败")
                image = screencap(live_controller)
                height, width = image.shape[:2]
                recognition = recognize_ocr(
                    live_tasker,
                    image,
                    (0, 0, width, min(height, 90)),
                    [r"直播"],
                )
                marker = find_live_marker(recognition)
                hwnd = window.hwnd.value if hasattr(window.hwnd, "value") else int(window.hwnd)
                LOGGER.info(
                    "已进入直播窗口: hwnd=%s, marker=%r@%s",
                    hwnd,
                    marker.text,
                    tuple(marker.box),
                )
                return hwnd
            except RuntimeError as error:
                last_error = str(error)
            finally:
                if live_tasker is not None:
                    live_tasker.post_stop().wait()
        time.sleep(0.2)
    raise TimeoutError(f"10 秒内未确认进入直播界面: {last_error}")


def select_prioritized_live(
    groups: tuple[str, ...],
    inspect: Callable[[str], GroupInspection],
) -> tuple[int, LiveWindow | None]:
    recognized_count = 0
    for group_name in groups:
        result = inspect(group_name)
        if not result.recognized:
            continue
        recognized_count += 1
        if result.live_window is not None:
            return recognized_count, result.live_window
    return recognized_count, None


class DingTalkSession:
    def __init__(self, tasker: Tasker, controller: Win32Controller, resource: Resource):
        self._tasker = tasker
        self._controller = controller
        self._resource = resource
        self._conversation_roi: tuple[int, int, int, int] | None = None

    @classmethod
    def open(cls) -> "DingTalkSession":
        try:
            Toolkit.init_option(ASSET_ROOT)
            window, process_path = ensure_dingtalk_window()
            close_live_summary_windows()
            LOGGER.info(
                "使用钉钉窗口: hwnd=%s, class=%r, title=%r, process=%r",
                window.hwnd,
                window.class_name,
                window.window_name,
                process_path,
            )
            controller = create_controller(window)
            resource = Resource()
            if not resource.post_bundle(ASSET_ROOT / "resource").wait().status.succeeded:
                raise DingTalkStartupError("MaaFramework 资源加载失败")
            tasker = Tasker()
            if not tasker.bind(resource, controller) or not tasker.inited:
                raise DingTalkStartupError("Tasker 初始化失败")
            return cls(tasker, controller, resource)
        except DingTalkStartupError:
            raise
        except Exception as error:
            raise DingTalkStartupError(f"初始化钉钉失败: {error}") from error

    def close(self) -> None:
        self._tasker.post_stop().wait()

    def _locate_conversation_roi(self):
        image = screencap(self._controller)
        image_height, image_width = image.shape[:2]
        anchor_roi = (
            0,
            0,
            min(image_width, max(500, image_width // 2)),
            min(image_height, max(180, image_height // 3)),
        )
        recognition = recognize_ocr(self._tasker, image, anchor_roi, [])
        message, unread_button = find_anchor_pair(recognition)
        self._conversation_roi = build_conversation_roi(
            message,
            unread_button,
            image_width,
            image_height,
        )
        return image

    def _inspect_group(self, group_name: str) -> GroupInspection:
        image = screencap(self._controller)
        image_height, _ = image.shape[:2]
        if self._conversation_roi is None:
            raise RuntimeError("会话列表区域尚未定位")
        target_recognition = recognize_ocr(
            self._tasker,
            image,
            self._conversation_roi,
            [re.escape(group_name)],
        )
        target = find_target(target_recognition, group_name)
        if target is None:
            LOGGER.warning("未识别到目标群聊: %s", group_name)
            return GroupInspection(recognized=False)

        focus_dingtalk_main_window()
        click_result(self._controller, target, f"“{group_name}”群聊")
        time.sleep(0.8)
        group_image = screencap(self._controller)
        group_height, group_width = group_image.shape[:2]
        if group_height != image_height:
            self._conversation_roi = None
            raise RuntimeError("钉钉窗口尺寸在扫描期间改变")
        banner_roi = build_banner_roi(
            self._conversation_roi,
            group_width,
            group_height,
        )
        banner_recognition = recognize_ocr(
            self._tasker,
            group_image,
            banner_roi,
            [r"正在\s*直播"],
        )
        banner = find_live_banner(banner_recognition)
        if banner is None:
            LOGGER.info("群聊当前未直播: %s", group_name)
            return GroupInspection(recognized=True)

        LOGGER.info("检测到群聊直播: %s", group_name)
        click_result(self._controller, banner, "“正在直播”横幅")
        time.sleep(2.0)
        hwnd = wait_for_live_window(self._resource)
        return GroupInspection(
            recognized=True,
            live_window=LiveWindow(group_name=group_name, hwnd=hwnd),
        )

    def scan(self, groups: tuple[str, ...]) -> LiveWindow | None:
        focus_dingtalk_main_window()
        self._locate_conversation_roi()
        recognized_count, live_window = select_prioritized_live(
            groups,
            self._inspect_group,
        )
        if recognized_count == 0:
            raise AllGroupsUnrecognizedError(
                "配置的群聊全部无法识别: " + ", ".join(groups)
            )
        return live_window
