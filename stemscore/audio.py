from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
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
    for line in process.stdout:
        cleaned = line.rstrip()
        try:
            log(cleaned)
        except UnicodeEncodeError:
            encoding = sys.stdout.encoding or "utf-8"
            log(cleaned.encode(encoding, errors="replace").decode(encoding))
    code = process.wait()
    if code:
        raise RuntimeError(f"处理程序退出码为 {code}：{command[0]}")


def convert_to_wav(source: Path, target: Path, log: LogCallback) -> list[str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        executable("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-xerror",
        "-y",
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
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"源音频无法完整解码：{source}。文件可能损坏、下载不完整或包含无效音频帧；"
            "请先用播放器确认整曲可正常播放，或重新获取/转码源文件后再试。"
        ) from error
    return command


def mix_audio(sources: list[Path], target: Path, log: LogCallback) -> list[str]:
    if not sources:
        raise ValueError("至少需要一个音频输入。")
    target.parent.mkdir(parents=True, exist_ok=True)
    if len(sources) == 1:
        shutil.copy2(sources[0], target)
        return ["copy", str(sources[0]), str(target)]
    command = [executable("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-y"]
    for source in sources:
        command.extend(["-i", str(source)])
    command.extend(
        [
            "-filter_complex",
            f"amix=inputs={len(sources)}:normalize=0:dropout_transition=0",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "pcm_s24le",
            str(target),
        ]
    )
    run_logged(command, log)
    return command


def subtract_audio(source: Path, subtract: Path, target: Path, log: LogCallback) -> list[str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        executable("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(source),
        "-i",
        str(subtract),
        "-filter_complex",
        "[1:a]volume=-1[negative];[0:a][negative]amix=inputs=2:normalize=0:dropout_transition=0",
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

