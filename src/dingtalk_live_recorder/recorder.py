from __future__ import annotations

import ctypes
import json
import logging
import shutil
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyaudiowpatch as pyaudio
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit

from .dingtalk import ASSET_ROOT, create_controller, normalize_text, recognize_ocr, screencap

LOGGER = logging.getLogger(__name__)
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 720
FRAME_RATE = 30
VIDEO_CODEC = "libx264"
VIDEO_CRF = 24
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "128k"
AUDIO_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=async=1:first_pts=0"
END_TEXT = "直播已结束"
POLL_INTERVAL_SECONDS = 2.0
FFMPEG_STOP_TIMEOUT_SECONDS = 30
AUDIO_CHUNK_FRAMES = 2048

USER32 = ctypes.windll.user32
SW_RESTORE = 9
SW_MAXIMIZE = 3
SW_SHOWNOACTIVATE = 4
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

USER32.IsWindow.argtypes = [wintypes.HWND]
USER32.IsWindow.restype = wintypes.BOOL
USER32.IsIconic.argtypes = [wintypes.HWND]
USER32.IsIconic.restype = wintypes.BOOL
USER32.IsZoomed.argtypes = [wintypes.HWND]
USER32.IsZoomed.restype = wintypes.BOOL
USER32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
USER32.GetWindowRect.restype = wintypes.BOOL
USER32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
USER32.SetWindowPos.restype = wintypes.BOOL


@dataclass(frozen=True)
class WindowState:
    rect: tuple[int, int, int, int]
    maximized: bool


@dataclass(frozen=True)
class AudioSource:
    index: int
    name: str
    sample_rate: int
    channels: int


def require_program(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise FileNotFoundError(f"未找到 {name}，请先安装 FFmpeg 并加入 PATH")
    return path


def prepare_window(hwnd: int) -> WindowState:
    if not USER32.IsWindow(hwnd):
        raise RuntimeError(f"直播窗口已不存在: hwnd={hwnd}")
    rect = wintypes.RECT()
    if not USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("读取直播窗口尺寸失败")
    state = WindowState(
        rect=(rect.left, rect.top, rect.right, rect.bottom),
        maximized=bool(USER32.IsZoomed(hwnd)),
    )
    USER32.ShowWindow(hwnd, SW_RESTORE)
    if not USER32.SetWindowPos(
        hwnd,
        0,
        0,
        0,
        WINDOW_WIDTH,
        WINDOW_HEIGHT,
        SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
    ):
        raise RuntimeError("调整直播窗口到 1280×720 失败")
    time.sleep(0.5)
    resized = wintypes.RECT()
    if not USER32.GetWindowRect(hwnd, ctypes.byref(resized)):
        raise RuntimeError("确认直播窗口尺寸失败")
    size = (resized.right - resized.left, resized.bottom - resized.top)
    LOGGER.info("直播窗口已调整: hwnd=%s, size=%s", hwnd, size)
    return state


def restore_window(hwnd: int, state: WindowState) -> None:
    if not USER32.IsWindow(hwnd):
        return
    if state.maximized:
        USER32.ShowWindow(hwnd, SW_MAXIMIZE)
        return
    left, top, right, bottom = state.rect
    USER32.SetWindowPos(
        hwnd,
        0,
        left,
        top,
        right - left,
        bottom - top,
        SWP_NOZORDER | SWP_NOACTIVATE,
    )


def keep_window_capturable(hwnd: int) -> None:
    if USER32.IsIconic(hwnd):
        USER32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def choose_audio_source() -> AudioSource:
    with pyaudio.PyAudio() as audio:
        info = audio.get_default_wasapi_loopback()
    channels = min(2, int(info["maxInputChannels"]))
    if channels < 1:
        raise RuntimeError(f"WASAPI 回环设备没有输入声道: {info['name']!r}")
    return AudioSource(
        index=int(info["index"]),
        name=str(info["name"]),
        sample_rate=int(info["defaultSampleRate"]),
        channels=channels,
    )


class WasapiAudioRecorder:
    def __init__(self, ffmpeg: str, source: AudioSource, output_path: Path) -> None:
        self.source = source
        self._audio = pyaudio.PyAudio()
        self._stream = self._audio.open(
            format=pyaudio.paInt16,
            channels=source.channels,
            rate=source.sample_rate,
            input=True,
            input_device_index=source.index,
            frames_per_buffer=AUDIO_CHUNK_FRAMES,
            start=False,
        )
        self._process = subprocess.Popen(
            [
                ffmpeg,
                "-hide_banner",
                "-y",
                "-f",
                "s16le",
                "-ar",
                str(source.sample_rate),
                "-ac",
                str(source.channels),
                "-i",
                "pipe:0",
                "-af",
                AUDIO_FILTER,
                "-c:a",
                AUDIO_CODEC,
                "-b:a",
                AUDIO_BITRATE,
                "-ar",
                "48000",
                "-f",
                "matroska",
                str(output_path),
            ],
            stdin=subprocess.PIPE,
            bufsize=0,
        )
        self._stop_event = threading.Event()
        self._error: BaseException | None = None
        self._started = False
        self._thread = threading.Thread(
            target=self._capture,
            name="wasapi-loopback",
            daemon=True,
        )

    def start(self) -> None:
        LOGGER.info(
            "开始 WASAPI 回环录音: device=[%s] %r, input=%sHz/%sch, output=AAC %s",
            self.source.index,
            self.source.name,
            self.source.sample_rate,
            self.source.channels,
            AUDIO_BITRATE,
        )
        self._stream.start_stream()
        self._thread.start()
        self._started = True

    def _capture(self) -> None:
        try:
            while not self._stop_event.is_set():
                data = self._stream.read(
                    AUDIO_CHUNK_FRAMES,
                    exception_on_overflow=False,
                )
                if self._process.stdin is None:
                    raise RuntimeError("FFmpeg 音频输入管道不可用")
                remaining = memoryview(data)
                while remaining:
                    written = self._process.stdin.write(remaining)
                    if not written:
                        raise BrokenPipeError
                    remaining = remaining[written:]
        except BaseException as error:
            if not self._stop_event.is_set():
                self._error = error

    def check(self) -> None:
        if self._error is not None:
            raise RuntimeError("WASAPI 回环录音失败") from self._error
        if self._process.poll() is not None:
            raise RuntimeError(f"音频编码提前退出，退出码: {self._process.returncode}")

    def stop(self, allow_nonzero: bool = False) -> None:
        self._stop_event.set()
        if self._started:
            self._stream.stop_stream()
            self._thread.join(timeout=5)
        self._stream.close()
        self._audio.terminate()
        if self._process.poll() is None and self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                self._process.wait(timeout=FFMPEG_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                self._process.wait(timeout=5)
        if self._process.returncode != 0 and not allow_nonzero:
            raise RuntimeError(
                f"FFmpeg 音频编码异常退出，退出码: {self._process.returncode}"
            )


def start_video_recording(
    ffmpeg: str,
    output_path: Path,
    frame_size: tuple[int, int],
) -> subprocess.Popen:
    frame_width, frame_height = frame_size
    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-thread_queue_size",
        "1024",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-video_size",
        f"{frame_width}x{frame_height}",
        "-framerate",
        str(FRAME_RATE),
        "-i",
        "pipe:0",
        "-vf",
        (
            f"scale={WINDOW_WIDTH}:{WINDOW_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WINDOW_WIDTH}:{WINDOW_HEIGHT}:(ow-iw)/2:(oh-ih)/2"
        ),
        "-c:v",
        VIDEO_CODEC,
        "-preset",
        "fast",
        "-crf",
        str(VIDEO_CRF),
        "-profile:v",
        "high",
        "-level:v",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(FRAME_RATE * 2),
        "-an",
        "-f",
        "matroska",
        str(output_path),
    ]
    LOGGER.info(
        "开始视频录制: output=%s, capture=%sx%s, video=H.264 %sx%s %sfps",
        output_path,
        frame_width,
        frame_height,
        WINDOW_WIDTH,
        WINDOW_HEIGHT,
        FRAME_RATE,
    )
    process = subprocess.Popen(command, stdin=subprocess.PIPE, bufsize=0)
    if process.poll() is not None:
        raise RuntimeError(f"FFmpeg 视频编码启动失败，退出码: {process.returncode}")
    return process


def stop_video_recording(
    process: subprocess.Popen,
    allow_nonzero: bool = False,
) -> None:
    if process.poll() is None and process.stdin is not None:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            process.wait(timeout=FFMPEG_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    if process.returncode != 0 and not allow_nonzero:
        raise RuntimeError(f"FFmpeg 视频编码异常退出，退出码: {process.returncode}")


def mux_recording(
    ffmpeg: str,
    video_path: Path,
    audio_path: Path,
    output_path: Path,
) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-shortest",
            str(output_path),
        ],
        timeout=FFMPEG_STOP_TIMEOUT_SECONDS,
        check=True,
    )


def find_window(hwnd: int):
    for window in Toolkit.find_desktop_windows():
        value = window.hwnd.value if hasattr(window.hwnd, "value") else int(window.hwnd)
        if value == hwnd:
            return window
    raise RuntimeError(f"未找到直播窗口: hwnd={hwnd}")


def create_monitor_tasker(hwnd: int) -> tuple[Tasker, Resource, object]:
    controller = create_controller(find_window(hwnd))
    resource = Resource()
    if not resource.post_bundle(ASSET_ROOT / "resource").wait().status.succeeded:
        raise RuntimeError("MaaFramework 资源加载失败")
    tasker = Tasker()
    if not tasker.bind(resource, controller) or not tasker.inited:
        raise RuntimeError("直播结束检测 Tasker 初始化失败")
    return tasker, resource, controller


def find_end_marker(recognition):
    for result in recognition.filtered_results:
        if END_TEXT in normalize_text(result.text):
            return result
    return None


def recognize_end_box(tasker: Tasker, image) -> tuple[int, int, int, int] | None:
    height, width = image.shape[:2]
    recognition = recognize_ocr(
        tasker,
        image,
        (0, 0, width, min(height, max(240, height // 2))),
        [r"直播\s*已\s*结束"],
    )
    marker = find_end_marker(recognition)
    return None if marker is None else tuple(marker.box)


def monitor_until_end(
    hwnd: int,
    tasker: Tasker,
    controller,
    process: subprocess.Popen,
    audio_recorder: WasapiAudioRecorder,
    first_frame,
) -> str:
    source_height, source_width = first_frame.shape[:2]
    started_at = time.monotonic()
    next_ocr_at = started_at
    frame_index = 0
    image = first_frame
    ocr_future: Future | None = None
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-end-ocr")
    try:
        while True:
            now = time.monotonic()
            if ocr_future is not None and ocr_future.done():
                marker_box = ocr_future.result()
                ocr_future = None
                if marker_box is not None:
                    return f"检测到“{END_TEXT}”: box={marker_box}"
            if not USER32.IsWindow(hwnd):
                return "直播窗口已自动关闭"
            keep_window_capturable(hwnd)
            if process.poll() is not None:
                if process.returncode == 0:
                    return "直播录制输入已结束"
                raise RuntimeError(f"FFmpeg 在直播结束前退出，退出码: {process.returncode}")
            audio_recorder.check()
            if frame_index:
                capture_at = started_at + frame_index / FRAME_RATE
                delay = capture_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                image = screencap(controller)
            height, width = image.shape[:2]
            if (width, height) != (source_width, source_height):
                marker_box = recognize_end_box(tasker, image)
                if marker_box is not None:
                    return f"检测到“{END_TEXT}”: box={marker_box}"
                raise RuntimeError(
                    "直播窗口尺寸在录制期间改变: "
                    f"{source_width}x{source_height} -> {width}x{height}"
                )
            if process.stdin is None:
                raise RuntimeError("FFmpeg 视频输入管道不可用")
            frame = memoryview(image).cast("B")
            try:
                while frame:
                    written = process.stdin.write(frame)
                    if not written:
                        raise BrokenPipeError
                    frame = frame[written:]
            except BrokenPipeError as error:
                raise RuntimeError("FFmpeg 视频输入管道已关闭") from error
            frame_index += 1
            now = time.monotonic()
            if now >= next_ocr_at and ocr_future is None:
                ocr_future = executor.submit(recognize_end_box, tasker, image.copy())
                next_ocr_at = now + POLL_INTERVAL_SECONDS
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def probe_recording(ffprobe: str, output_path: Path) -> dict:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate",
            "-show_entries",
            "format=duration,size",
            "-of",
            "json",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    )
    detail = json.loads(result.stdout)
    streams = detail.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        raise RuntimeError("录制文件缺少视频流或音频流")
    if video.get("codec_name") != "h264" or audio.get("codec_name") != "aac":
        raise RuntimeError(
            f"录制编码异常: video={video.get('codec_name')}, audio={audio.get('codec_name')}"
        )
    if (video.get("width"), video.get("height")) != (WINDOW_WIDTH, WINDOW_HEIGHT):
        raise RuntimeError(f"录制文件分辨率异常: {video.get('width')}x{video.get('height')}")
    if video.get("r_frame_rate") != f"{FRAME_RATE}/1":
        raise RuntimeError(f"录制文件帧率异常: {video.get('r_frame_rate')}")
    return detail


def _safe_filename(value: str) -> str:
    cleaned = "".join("_" if character in '<>:"/\\|?*' else character for character in value)
    cleaned = cleaned.strip(" .")
    return cleaned or "直播"


class LiveRecorder:
    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._ffmpeg = require_program("ffmpeg")
        self._ffprobe = require_program("ffprobe")

    def record(self, hwnd: int, group_name: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = self._output_dir / f"{_safe_filename(group_name)}_{timestamp}.mkv"
        video_path = output_path.with_suffix(".video.mkv")
        audio_path = output_path.with_suffix(".audio.mka")
        tasker, resource, controller = create_monitor_tasker(hwnd)
        window_state = None
        video_process = None
        audio_recorder = None
        allow_nonzero_ffmpeg_exit = False
        try:
            window_state = prepare_window(hwnd)
            first_frame = screencap(controller)
            frame_height, frame_width = first_frame.shape[:2]
            audio_source = choose_audio_source()
            video_process = start_video_recording(
                self._ffmpeg,
                video_path,
                (frame_width, frame_height),
            )
            audio_recorder = WasapiAudioRecorder(
                self._ffmpeg,
                audio_source,
                audio_path,
            )
            audio_recorder.start()
            try:
                reason = monitor_until_end(
                    hwnd,
                    tasker,
                    controller,
                    video_process,
                    audio_recorder,
                    first_frame,
                )
            except KeyboardInterrupt:
                reason = "收到 Ctrl+C，手动停止录制"
            allow_nonzero_ffmpeg_exit = reason in {
                "直播窗口已自动关闭",
                "收到 Ctrl+C，手动停止录制",
            }
            LOGGER.info("停止录制: group=%s, reason=%s", group_name, reason)
        finally:
            try:
                if audio_recorder is not None:
                    audio_recorder.stop(allow_nonzero_ffmpeg_exit)
            finally:
                try:
                    if video_process is not None:
                        stop_video_recording(video_process, allow_nonzero_ffmpeg_exit)
                finally:
                    if window_state is not None:
                        restore_window(hwnd, window_state)
                    tasker.post_stop().wait()
                    del resource
        mux_recording(self._ffmpeg, video_path, audio_path, output_path)
        detail = probe_recording(self._ffprobe, output_path)
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
        LOGGER.info(
            "录制文件完成: group=%s, path=%s, duration=%ss, size=%s bytes",
            group_name,
            output_path,
            detail["format"]["duration"],
            detail["format"]["size"],
        )
        return output_path
