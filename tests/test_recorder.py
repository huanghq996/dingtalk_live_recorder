from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from dingtalk_live_recorder import recorder


def test_auto_encoder_falls_back_to_cpu_when_nvenc_probe_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        recorder,
        "probe_nvidia_video_encoder",
        lambda _: "NVENC driver is unavailable",
    )

    assert recorder.select_video_codec("ffmpeg", "auto") == "libx264"


def test_explicit_nvenc_fails_when_gpu_probe_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        recorder,
        "probe_nvidia_video_encoder",
        lambda _: "NVENC driver is unavailable",
    )

    with pytest.raises(RuntimeError, match="NVENC driver is unavailable"):
        recorder.select_video_codec("ffmpeg", "h264_nvenc")


def test_auto_encoder_uses_nvenc_after_successful_probe(monkeypatch) -> None:
    monkeypatch.setattr(recorder, "probe_nvidia_video_encoder", lambda _: None)

    assert recorder.select_video_codec("ffmpeg", "auto") == "h264_nvenc"


def test_video_codec_arguments_use_encoder_specific_quality_controls() -> None:
    x264_args = recorder.video_codec_args("libx264")
    nvenc_args = recorder.video_codec_args("h264_nvenc")

    assert x264_args == ["-preset", "fast", "-crf", "24"]
    assert "-crf" not in nvenc_args
    assert nvenc_args == [
        "-preset",
        "p5",
        "-tune",
        "hq",
        "-rc",
        "vbr",
        "-cq",
        "24",
        "-b:v",
        "0",
    ]


def test_nvenc_probe_reports_ffmpeg_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        recorder.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args, 1, stderr="driver too old\n"),
    )

    assert recorder.probe_nvidia_video_encoder("ffmpeg") == "driver too old"
