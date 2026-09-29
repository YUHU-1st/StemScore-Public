from __future__ import annotations

import hashlib
import io
from pathlib import Path
import os
import threading
import time
from urllib.error import HTTPError
import wave

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


def test_source_conversion_keeps_valid_partial_output_when_decoder_reports_errors(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "broken.flac"
    source.write_bytes(b"not-a-flac")
    target = tmp_path / "source.wav"
    captured: list[str] = []

    monkeypatch.setattr("stemscore.audio.executable", lambda _name: "ffmpeg.exe")

    def fail(command, _log):
        captured.extend(command)
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            handle.writeframes(b"\0\0\0\0" * 4410)
        raise RuntimeError("decoder failed")

    monkeypatch.setattr("stemscore.audio.run_logged", fail)
    monkeypatch.setattr(
        "stemscore.audio.probe_audio",
        lambda _path: {"duration_seconds": 0.1, "sample_rate": 44100, "channels": 2},
    )
    messages: list[str] = []
    convert_to_wav(source, target, messages.append)

    assert "-xerror" not in captured
    assert "ignore_err" in captured
    assert target.exists()
    assert any("优先继续使用可解码部分" in message for message in messages)


def test_source_conversion_still_rejects_unusable_output(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "broken.flac"
    source.write_bytes(b"not-a-flac")
    target = tmp_path / "source.wav"
    monkeypatch.setattr("stemscore.audio.executable", lambda _name: "ffmpeg.exe")

    def fail(_command, _log):
        target.write_bytes(b"")
        raise RuntimeError("decoder failed")

    monkeypatch.setattr("stemscore.audio.run_logged", fail)
    with pytest.raises(RuntimeError, match="无法解码出可用内容"):
        convert_to_wav(source, target, lambda _message: None)
    assert not target.exists()


class _FakeResponse:
    def __init__(self, data: bytes, status: int = 200, headers: dict[str, str] | None = None, delay: float = 0.0):
        self._stream = io.BytesIO(data)
        self._status = status
        self.headers = headers or {}
        self._delay = delay

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self) -> int:
        return self._status

    def read(self, size: int = -1) -> bytes:
        if self._delay:
            time.sleep(self._delay)
        return self._stream.read(size)


def test_runtime_download_discards_oversized_partial_before_request(tmp_path: Path, monkeypatch) -> None:
    data = b"correct-model-data"
    target = tmp_path / "model.ckpt"
    part = target.with_name(target.name + ".part")
    part.write_bytes(data + b"duplicated-bytes")
    requests = []

    def fake_urlopen(request, timeout=60):
        requests.append((request, timeout))
        assert request.headers.get("Range") is None
        return _FakeResponse(data)

    monkeypatch.setattr("stemscore.runtime_repair.urlopen", fake_urlopen)
    messages: list[str] = []
    RuntimeRepairer._download_verified(
        "https://example.invalid/model.ckpt",
        target,
        len(data),
        hashlib.sha256(data).hexdigest(),
        messages.append,
    )

    assert target.read_bytes() == data
    assert len(requests) == 1
    assert any("异常断点文件" in message for message in messages)


def test_runtime_download_recovers_from_http_416_by_restarting(tmp_path: Path, monkeypatch) -> None:
    data = b"0123456789abcdef"
    target = tmp_path / "model.ckpt"
    part = target.with_name(target.name + ".part")
    part.write_bytes(data[:7])
    calls = 0

    def fake_urlopen(request, timeout=60):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert request.headers.get("Range") == "bytes=7-"
            raise HTTPError(request.full_url, 416, "range invalid", {}, None)
        assert request.headers.get("Range") is None
        return _FakeResponse(data)

    monkeypatch.setattr("stemscore.runtime_repair.urlopen", fake_urlopen)
    messages: list[str] = []
    RuntimeRepairer._download_verified(
        "https://example.invalid/model.ckpt",
        target,
        len(data),
        hashlib.sha256(data).hexdigest(),
        messages.append,
    )

    assert calls == 2
    assert target.read_bytes() == data
    assert any("HTTP 416" in message for message in messages)


def test_runtime_download_rejects_mismatched_content_range(tmp_path: Path, monkeypatch) -> None:
    data = b"abcdefghij"
    target = tmp_path / "model.ckpt"
    part = target.with_name(target.name + ".part")
    part.write_bytes(data[:4])
    calls = 0

    def fake_urlopen(request, timeout=60):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _FakeResponse(
                data[4:],
                status=206,
                headers={"Content-Range": f"bytes 3-{len(data) - 1}/{len(data)}"},
            )
        assert request.headers.get("Range") is None
        return _FakeResponse(data)

    monkeypatch.setattr("stemscore.runtime_repair.urlopen", fake_urlopen)
    RuntimeRepairer._download_verified(
        "https://example.invalid/model.ckpt",
        target,
        len(data),
        hashlib.sha256(data).hexdigest(),
        lambda _message: None,
    )

    assert calls == 2
    assert target.read_bytes() == data


def test_runtime_download_serializes_concurrent_repair_workers(tmp_path: Path, monkeypatch) -> None:
    data = b"x" * (2 * 1024 * 1024)
    target = tmp_path / "model.ckpt"
    calls = 0
    calls_lock = threading.Lock()

    def fake_urlopen(_request, timeout=60):
        nonlocal calls
        with calls_lock:
            calls += 1
        return _FakeResponse(data, delay=0.03)

    monkeypatch.setattr("stemscore.runtime_repair.urlopen", fake_urlopen)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            RuntimeRepairer._download_verified(
                "https://example.invalid/model.ckpt",
                target,
                len(data),
                hashlib.sha256(data).hexdigest(),
                lambda _message: None,
            )
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    time.sleep(0.01)
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert errors == []
    assert calls == 1
    assert target.read_bytes() == data
