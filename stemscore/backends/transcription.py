from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
from typing import Callable

from ..audio import run_logged
from ..inventory import LocalRuntime
from ..manifest import sha256_file
from ..midi_tools import normalize_single_track, write_drum_midi


BASIC_PITCH_PROFILES = {
    "lead_vocal": (0.65, 0.45, 120, 40, 88),
    "harmony_vocal": (0.68, 0.48, 140, 40, 88),
    "bass": (0.65, 0.45, 120, 28, 60),
    "guitar": (0.68, 0.48, 110, 40, 96),
    "other": (0.72, 0.52, 140, 36, 96),
}


def _midi_frequency(note: int) -> float:
    return 440.0 * (2.0 ** ((note - 69) / 12.0))


def analyze(runtime: LocalRuntime, source: Path, drums: bool, log: Callable[[str], None]) -> tuple[dict, list[str]]:
    worker = Path(__file__).resolve().parents[1] / "runtime" / "analyze_audio.py"
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        output = Path(handle.name)
    command = [str(runtime.msst_python), str(worker), str(source), str(output)]
    if drums:
        command.append("--drums")
    try:
        run_logged(command, log)
        return json.loads(output.read_text(encoding="utf-8")), command
    finally:
        output.unlink(missing_ok=True)


def basic_pitch(
    runtime: LocalRuntime,
    source: Path,
    target: Path,
    role: str,
    bpm: float,
    log: Callable[[str], None],
) -> tuple[dict, list[str]]:
    results, commands = basic_pitch_many(runtime, [(source, target, role)], bpm, log)
    return results[role], commands[0]


def basic_pitch_many(
    runtime: LocalRuntime,
    jobs: list[tuple[Path, Path, str]],
    bpm: float,
    log: Callable[[str], None],
) -> tuple[dict[str, dict], list[list[str]]]:
    if not runtime.basic_pitch_python.is_file():
        raise RuntimeError(
            "Basic Pitch 本地运行时未安装。请先运行 tools\\setup_stemscore_runtime.ps1。"
        )
    results: dict[str, dict] = {}
    commands: list[list[str]] = []
    for source, target, role in jobs:
        with tempfile.TemporaryDirectory(prefix=f"stemscore-midi-{role}-") as temporary:
            output_dir = Path(temporary)
            onset, frame, minimum_ms, minimum_note, maximum_note = BASIC_PITCH_PROFILES[role]
            command = [
                str(runtime.basic_pitch_python),
                "-m",
                "basic_pitch.predict",
                "--model-serialization",
                "onnx",
                "--save-midi",
                "--onset-threshold",
                str(onset),
                "--frame-threshold",
                str(frame),
                "--minimum-note-length",
                str(minimum_ms),
                "--minimum-frequency",
                str(_midi_frequency(minimum_note)),
                "--maximum-frequency",
                str(_midi_frequency(maximum_note)),
                "--no-melodia",
                str(output_dir),
                str(source),
            ]
            run_logged(command, log, env={"PYTHONPATH": str(runtime.basic_pitch_packages)})
            commands.append(command)
            candidate = output_dir / f"{source.stem}_basic_pitch.mid"
            if not candidate.is_file():
                raise RuntimeError(f"Basic Pitch 未生成 {source.name} 的 MIDI 文件。")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, target)
            metadata = normalize_single_track(target, role, bpm)
            metadata.update(
                {
                    "algorithm": "Spotify Basic Pitch strict instrument profile",
                    "onset_threshold": onset,
                    "frame_threshold": frame,
                    "minimum_note_length_ms": minimum_ms,
                    "pitch_min_allowed": minimum_note,
                    "pitch_max_allowed": maximum_note,
                    "melodia_trick": False,
                }
            )
            results[role] = metadata
    return results, commands


def piano(
    runtime: LocalRuntime,
    source: Path,
    target: Path,
    bpm: float,
    log: Callable[[str], None],
) -> tuple[dict, list[str]]:
    if not runtime.transkun_weight.is_file() or not runtime.transkun_config.is_file():
        raise RuntimeError("TransKun V2 本地运行时未安装。请先运行 setup_runtime.ps1。")
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(runtime.msst_python),
        "-m",
        "transkun.transcribe",
        str(source),
        str(target),
        "--device",
        "cuda",
    ]
    run_logged(command, log, env={"PYTHONPATH": str(runtime.transkun_packages)})
    metadata = normalize_single_track(target, "piano", bpm)
    metadata.update(
        {
            "algorithm": "TransKun V2 neural semi-CRF",
            "model": str(runtime.transkun_weight),
            "model_sha256": sha256_file(runtime.transkun_weight),
        }
    )
    return metadata, command


def drums(
    runtime: LocalRuntime,
    source: Path,
    target: Path,
    bpm: float,
    log: Callable[[str], None],
) -> tuple[dict, list[str]]:
    result, command = analyze(runtime, source, True, log)
    target.parent.mkdir(parents=True, exist_ok=True)
    metadata = write_drum_midi(target, list(result["notes"]), bpm)
    metadata["algorithm"] = "librosa multi-band onset classifier v1"
    return metadata, command


def model_evidence(runtime: LocalRuntime) -> dict[str, object]:
    evidence: dict[str, object] = {
        "backend": "Spotify Basic Pitch + interpretable drum onset classifier",
        "basic_pitch_python": str(runtime.basic_pitch_python),
    }
    if runtime.basic_pitch_python.is_file():
        evidence["basic_pitch_python_sha256"] = sha256_file(runtime.basic_pitch_python)
    if runtime.basic_pitch_model.is_file():
        evidence["basic_pitch_model"] = str(runtime.basic_pitch_model)
        evidence["basic_pitch_model_sha256"] = sha256_file(runtime.basic_pitch_model)
    if runtime.transkun_weight.is_file():
        evidence["piano_backend"] = "TransKun V2"
        evidence["transkun_weight"] = str(runtime.transkun_weight)
        evidence["transkun_weight_sha256"] = sha256_file(runtime.transkun_weight)
    return evidence

