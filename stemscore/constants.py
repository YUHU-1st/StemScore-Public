from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "StemScore AI 扒谱"
PROJECT_FILE = "stemscore-project.json"
DEFAULT_PROJECTS_ROOT = Path(
    os.environ.get("STEMSCORE_PROJECTS_ROOT", Path.home() / "Documents" / "StemScore Projects")
)
DEFAULT_MSST_ROOT = Path(
    os.environ.get("STEMSCORE_MSST_ROOT", Path.home() / "StemScoreRuntime" / "MSST")
)
DEFAULT_UVR_ROOT = Path(
    os.environ.get("STEMSCORE_UVR_ROOT", Path.home() / "StemScoreRuntime" / "UVR")
)
DEFAULT_TRANSCRIPTION_ROOT = Path(
    os.environ.get(
        "STEMSCORE_TRANSCRIPTION_ROOT",
        Path.home() / "StemScoreRuntime" / "transcription" / "basic-pitch",
    )
)

STAGES = (
    (1, "音乐导入", "01_source"),
    (2, "人声 / 伴奏", "02_vocal_accompaniment"),
    (3, "主唱 / 和声 / 乐器分轨", "03_stems"),
    (4, "去混响干声与乐器", "04_dereverb"),
    (5, "各轨 MIDI 乐谱", "05_midi"),
)

STAGE_NAMES = {number: name for number, name, _ in STAGES}
STAGE_DIRS = {number: folder for number, _, folder in STAGES}

