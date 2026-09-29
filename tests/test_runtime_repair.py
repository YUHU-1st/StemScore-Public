from __future__ import annotations

from pathlib import Path
import os

import pytest

from stemscore.inventory import LocalRuntime
from stemscore.audio import convert_to_wav
from stemscore.runtime_repair import (
    MEGA53_CHECKPOINT_SHA256,
    MEGA53_CHECKPOINT_URL,
    MEGA53_CONFIG_SHA256,
    RTX40_PROFILE,
    RTX50_PROFILE,
    UV_SHA256,
    RuntimeRepairer,
    is_repairable_runtime_error,
    profile_for_compute_capability,
)


def test_runtime_repair_profiles_match_supported_gpu_generations() -> None:
    assert profile_for_compute_capability("8.9") == RTX40_PROFILE
    assert profile_for_compute_capability("12.0") == RTX50_PROFILE
    assert RTX40_PROFILE.torch == "2.11.0"
    assert RTX50_PROFILE.torch == "2.8.0"
    with pytest.raises(RuntimeError, match="暂不支持"):
        profile_for_compute_capability("7.5")


def test_runtime_repair_pins_verified_https_assets() -> None:
    assert MEGA53_CHECKPOINT_URL.startswith("https://github.com/ZFTurbo/")
    assert len(MEGA53_CHECKPOINT_SHA256) == 64
    assert len(MEGA53_CONFIG_SHA256) == 64
    assert len(UV_SHA256) == 64


def test_runtime_prefers_modern_windows_venv_layout(tmp_path: Path) -> None:
    runtime = LocalRuntime(tmp_path / "msst", tmp_path / "uvr", tmp_path / "transcription")
    python = runtime.msst_root / "env" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    assert runtime.msst_python == python


def test_core_validation_no_longer_requires_unused_uvr_demucs(tmp_path: Path, monkeypatch) -> None:
    runtime = LocalRuntime(tmp_path / "msst", tmp_path / "uvr", tmp_path / "transcription")
    required = [
        runtime.msst_root / "env" / "Scripts" / "python.exe",
        runtime.msst_root / "env" / "Scripts" / "msst.exe",
        runtime.public_mega53_config,
        runtime.public_mega53_checkpoint,
    ]
    for path in required:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
    monkeypatch.setattr("stemscore.inventory.shutil.which", lambda _name: "C:/ffmpeg.exe")

    runtime.validate_core()

    assert not runtime.demucs_repo.exists()


@pytest.mark.parametrize(
    "message",
    [
        "本地运行环境不完整：C:/StemScoreRuntime/MSST/env/Scripts/python.exe",
        "MSST 文件不存在：model.ckpt",
        "Basic Pitch 本地运行时未安装。",
        "TransKun V2 本地运行时未安装。",
        "未找到必需程序：ffmpeg",
        "未找到必需程序：ffprobe",
        "本地运行环境不完整：\nffmpeg / ffprobe",
    ],
)
def test_known_runtime_failures_offer_one_click_repair(message: str) -> None:
    assert is_repairable_runtime_error(message)


def test_audio_decode_failure_does_not_offer_runtime_repair() -> None:
    assert not is_repairable_runtime_error("Decoding error: Invalid data found when processing input")


def test_ffmpeg_repair_refreshes_existing_winget_links(tmp_path: Path, monkeypatch) -> None:
    runtime = LocalRuntime(tmp_path / "msst", tmp_path / "uvr", tmp_path / "transcription")
    repairer = RuntimeRepairer(runtime)
    links = tmp_path / "Local" / "Microsoft" / "WinGet" / "Links"
    links.mkdir(parents=True)
    (links / "ffmpeg.exe").write_bytes(b"")
    (links / "ffprobe.exe").write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("PATH", "")

    messages: list[str] = []
    repairer._ensure_ffmpeg(messages.append)

    assert str(links) in os.environ["PATH"]
    assert any("FFmpeg 已就绪" in message for message in messages)


def test_source_conversion_treats_decoder_errors_as_source_file_failures(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "broken.flac"
    source.write_bytes(b"not-a-flac")
    target = tmp_path / "source.wav"
    captured: list[str] = []

    monkeypatch.setattr("stemscore.audio.executable", lambda _name: "ffmpeg.exe")

    def fail(command, _log):
        captured.extend(command)
        target.write_bytes(b"partial")
        raise RuntimeError("decoder failed")

    monkeypatch.setattr("stemscore.audio.run_logged", fail)
    with pytest.raises(RuntimeError, match="源音频无法完整解码"):
        convert_to_wav(source, target, lambda _message: None)

    assert "-xerror" in captured
    assert not target.exists()
