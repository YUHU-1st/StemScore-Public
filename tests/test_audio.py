from __future__ import annotations

import sys
import struct
import wave
from pathlib import Path

import pytest

from stemscore.audio import mix_audio, run_logged, subtract_audio, volume_stats_db


def test_run_logged_includes_useful_child_error_detail() -> None:
    messages: list[str] = []
    with pytest.raises(RuntimeError, match="error: synthetic failure"):
        run_logged(
            [
                sys.executable,
                "-c",
                "print('error: synthetic failure'); print('cleanup line'); raise SystemExit(1)",
            ],
            messages.append,
        )

    assert "error: synthetic failure" in messages


def _constant_wav(path: Path, amplitude: float) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<h", round(amplitude * 32767)) * 4410)


def test_overlapping_mix_is_attenuated_before_pcm_encoding(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    output = tmp_path / "mix.wav"
    _constant_wav(source, 0.8)
    logs: list[str] = []

    mix_audio([source, source], output, logs.append)

    _, peak = volume_stats_db(output)
    assert peak <= -0.8
    assert any("统一增益" in line for line in logs)


def test_residual_does_not_exceed_source_level_or_clip(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    opposing = tmp_path / "opposing.wav"
    output = tmp_path / "residual.wav"
    _constant_wav(source, 0.8)
    _constant_wav(opposing, -0.8)

    subtract_audio(source, opposing, output, lambda _line: None)

    source_rms, _ = volume_stats_db(source)
    output_rms, output_peak = volume_stats_db(output)
    assert output_peak <= -0.8
    assert output_rms <= source_rms + 0.1
