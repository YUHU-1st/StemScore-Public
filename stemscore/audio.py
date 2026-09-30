from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from collections import deque
from typing import Any, Callable


LogCallback = Callable[[str], None]


def executable(name: str) -> str:
    value = shutil.which(name)
    if value is None:
        raise RuntimeError(f"未找到必需程序：{name}")
    return value


def run_logged(
    command: list[str],
    log: LogCallback,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    output_encoding: str = "utf-8",
) -> None:
    log("$ " + subprocess.list2cmdline(command))
    child_env = os.environ.copy()
    child_env.pop("PYTHONHOME", None)
    child_env.pop("PYTHONPATH", None)
    if env:
        child_env.update(env)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding=output_encoding,
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    assert process.stdout is not None
    recent_lines: deque[str] = deque(maxlen=12)
    error_lines: deque[str] = deque(maxlen=5)
    for line in process.stdout:
        cleaned = line.rstrip()
        if cleaned:
            recent_lines.append(cleaned)
            if re.search(r"(?:error|exception|traceback|failed|cannot|no space|out of space)", cleaned, re.IGNORECASE):
                error_lines.append(cleaned)
        try:
            log(cleaned)
        except UnicodeEncodeError:
            encoding = sys.stdout.encoding or "utf-8"
            log(cleaned.encode(encoding, errors="replace").decode(encoding))
    code = process.wait()
    if code:
        detail = error_lines[-1] if error_lines else (recent_lines[-1] if recent_lines else "未返回错误详情")
        raise RuntimeError(f"处理程序退出码为 {code}：{command[0]}\n错误详情：{detail}")


def convert_to_wav(source: Path, target: Path, log: LogCallback) -> list[str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        executable("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-err_detect",
        "ignore_err",
        "-i",
        str(source),
        "-ar",
        "44100",
        "-ac",
        "2",
        "-c:a",
        "pcm_s24le",
        str(target),
    ]
    try:
        run_logged(command, log)
    except RuntimeError as error:
        if not target.is_file() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise RuntimeError(
                f"源音频无法解码出可用内容：{source}。可以换一个源文件，或先用播放器/转码工具另存后再试。"
            ) from error
        log("FFmpeg 返回了错误，但已经生成 WAV；StemScore 将优先继续使用可解码部分。")
    try:
        details = probe_audio(target)
    except Exception as error:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"源音频没有生成可用 WAV：{source}") from error
    if float(details.get("duration_seconds", 0) or 0) <= 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"源音频没有生成有效时长的 WAV：{source}")
    return command


def _filter_levels_db(inputs: list[str], audio_filter: str, log: LogCallback) -> tuple[float, float]:
    command = [
        executable("ffmpeg"), "-hide_banner", "-nostats", "-y", *inputs,
        "-filter_complex", f"{audio_filter},astats=metadata=0:reset=0",
        "-f", "null", "NUL" if os.name == "nt" else "/dev/null",
    ]
    log("$ " + subprocess.list2cmdline(command))
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    peak = re.findall(r"Peak level dB:\s*(-?(?:inf|\d+(?:\.\d+)?))", result.stderr)
    rms = re.findall(r"RMS level dB:\s*(-?(?:inf|\d+(?:\.\d+)?))", result.stderr)
    if not peak or not rms:
        raise RuntimeError("FFmpeg 未返回混音电平，不能安全写入 PCM。")
    return float(rms[-1]), float(peak[-1])


def mix_audio(sources: list[Path], target: Path, log: LogCallback, *, float_output: bool = False) -> list[str]:
    if not sources:
        raise ValueError("至少需要一个音频输入。")
    target.parent.mkdir(parents=True, exist_ok=True)
    if len(sources) == 1:
        shutil.copy2(sources[0], target)
        return ["copy", str(sources[0]), str(target)]
    inputs = [item for source in sources for item in ("-i", str(source))]
    command = [executable("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-y", *inputs]
    audio_filter = f"amix=inputs={len(sources)}:normalize=0:dropout_transition=0"
    gain_db = 0.0
    if not float_output:
        _, peak_db = _filter_levels_db(inputs, audio_filter, log)
        gain_db = min(0.0, -1.0 - peak_db)
        log(f"混音浮点峰值 {peak_db:.2f} dBFS，写入前统一增益 {gain_db:.2f} dB。")
    command.extend(
        [
            "-filter_complex",
            f"{audio_filter},volume={gain_db:.6f}dB" if gain_db else audio_filter,
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "pcm_f32le" if float_output else "pcm_s24le",
            str(target),
        ]
    )
    run_logged(command, log)
    return command


def subtract_audio(source: Path, subtract: Path, target: Path, log: LogCallback) -> list[str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    inputs = ["-i", str(source), "-i", str(subtract)]
    audio_filter = "[1:a]volume=-1[negative];[0:a][negative]amix=inputs=2:normalize=0:dropout_transition=0"
    mixed_rms_db, mixed_peak_db = _filter_levels_db(inputs, audio_filter, log)
    source_rms_db, source_peak_db = volume_stats_db(source)
    gain_db = min(0.0, min(-1.0, source_peak_db) - mixed_peak_db, source_rms_db - mixed_rms_db)
    log(
        f"残差浮点峰值 {mixed_peak_db:.2f} dBFS / RMS {mixed_rms_db:.2f} dBFS；"
        f"原轨峰值 {source_peak_db:.2f} dBFS / RMS {source_rms_db:.2f} dBFS；"
        f"统一增益 {gain_db:.2f} dB。"
    )
    command = [
        executable("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        *inputs,
        "-filter_complex",
        f"{audio_filter},volume={gain_db:.6f}dB" if gain_db else audio_filter,
        "-ar",
        "44100",
        "-ac",
        "2",
        "-c:a",
        "pcm_s24le",
        str(target),
    ]
    run_logged(command, log)
    return command


def probe_audio(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            executable("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,duration:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    data = json.loads(result.stdout)
    stream = data.get("streams", [{}])[0]
    duration = stream.get("duration") or data.get("format", {}).get("duration") or 0
    return {
        "codec": stream.get("codec_name"),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
        "duration_seconds": round(float(duration), 6),
    }


def volume_stats_db(path: Path, audio_filter: str | None = None) -> tuple[float, float]:
    filter_chain = f"{audio_filter},volumedetect" if audio_filter else "volumedetect"
    result = subprocess.run(
        [
            executable("ffmpeg"),
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            filter_chain,
            "-f",
            "null",
            "NUL" if os.name == "nt" else "/dev/null",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    mean_match = re.search(r"mean_volume:\s*(-?(?:inf|\d+(?:\.\d+)?))\s*dB", result.stderr)
    peak_match = re.search(r"max_volume:\s*(-?(?:inf|\d+(?:\.\d+)?))\s*dB", result.stderr)
    if mean_match is None or peak_match is None:
        raise RuntimeError(f"无法读取音频电平：{path}")
    mean_db = -120.0 if mean_match.group(1) == "-inf" else float(mean_match.group(1))
    peak_db = -120.0 if peak_match.group(1) == "-inf" else float(peak_match.group(1))
    return mean_db, peak_db


def mean_volume_db(path: Path, audio_filter: str | None = None) -> float:
    return volume_stats_db(path, audio_filter)[0]


def high_frequency_balance_db(path: Path, full_band_db: float | None = None) -> float:
    full_band_db = mean_volume_db(path) if full_band_db is None else full_band_db
    return mean_volume_db(path, "highpass=f=6000") - full_band_db


def validate_durations(paths: list[Path], tolerance_seconds: float = 0.1) -> dict[str, Any]:
    probes = {path.name: probe_audio(path) for path in paths}
    durations = [entry["duration_seconds"] for entry in probes.values()]
    spread = max(durations) - min(durations) if durations else 0.0
    if spread > tolerance_seconds:
        raise RuntimeError(f"分轨时长不一致，最大差值 {spread:.3f} 秒")
    return {"duration_spread_seconds": round(spread, 6), "files": probes}

