from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Iterable

from ..manifest import sha256_file, utc_now


AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aif", ".aiff"}
MIDI_SUFFIXES = {".mid", ".midi"}
DAW_SUFFIXES = {".als", ".flp", ".song", ".rpp", ".cpr"}
IGNORED_NAMES = {".git", ".venv", ".venv312", "build", "dist", "__pycache__"}


def normalized_name(path: Path) -> str:
    name = path.stem.casefold()
    name = re.sub(
        r"(?:[_ -](?:vocals?|instrumental|accompaniment|bass|drums?|guitar|piano|other|lead|harmony|dry|stem|part)[_ -]*\d*)+$",
        "",
        name,
    )
    return re.sub(r"[^\w]+", "", name)


@dataclass
class DatasetFile:
    path: str
    kind: str
    size: int
    modified_ns: int
    sha256: str


@dataclass
class DatasetGroup:
    group_id: str
    directory: str
    quality: str
    reason: str
    files: list[DatasetFile]


def scan_files(roots: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        root = root.resolve()
        if root.is_file():
            found.append(root)
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if any(part in IGNORED_NAMES for part in path.parts):
                continue
            if path.is_file() and path.suffix.casefold() in AUDIO_SUFFIXES | MIDI_SUFFIXES | DAW_SUFFIXES:
                found.append(path)
    return sorted(set(found))


def project_directory(path: Path, daw_directories: set[Path]) -> Path:
    for parent in path.parents:
        if parent.name.casefold().endswith(" project"):
            return parent
    for parent in path.parents:
        if parent in daw_directories:
            return parent
    return path.parent


def build_catalog(roots: list[Path], output: Path) -> dict[str, object]:
    paths = scan_files(roots)
    daw_directories = {
        path.parent for path in paths if path.suffix.casefold() in DAW_SUFFIXES
    }
    by_directory: dict[Path, list[Path]] = defaultdict(list)
    for path in paths:
        by_directory[project_directory(path, daw_directories)].append(path)

    groups: list[DatasetGroup] = []
    for directory, members in sorted(by_directory.items(), key=lambda item: str(item[0])):
        audio = [path for path in members if path.suffix.casefold() in AUDIO_SUFFIXES]
        midi = [path for path in members if path.suffix.casefold() in MIDI_SUFFIXES]
        daw = [path for path in members if path.suffix.casefold() in DAW_SUFFIXES]
        matching_names = {
            normalized_name(a) for a in audio if any(normalized_name(a) == normalized_name(m) for m in midi)
        }
        if matching_names:
            quality = "paired"
            reason = "同目录存在同名音频与 MIDI，可用于监督式音符转写微调。"
        elif len(audio) >= 2 and (midi or daw):
            quality = "candidate"
            reason = "同目录存在多轨音频和 MIDI/DAW 工程，需要人工确认时间对齐与轨道标签。"
        elif audio or midi or daw:
            quality = "unpaired"
            reason = "可用于无监督/增广或等待补齐标签，不能直接视为监督训练对。"
        else:
            continue
        entries: list[DatasetFile] = []
        for path in sorted(members):
            suffix = path.suffix.casefold()
            kind = "audio" if suffix in AUDIO_SUFFIXES else "midi" if suffix in MIDI_SUFFIXES else "daw"
            entries.append(
                DatasetFile(
                    path=str(path),
                    kind=kind,
                    size=path.stat().st_size,
                    modified_ns=path.stat().st_mtime_ns,
                    sha256=sha256_file(path),
                )
            )
        groups.append(
            DatasetGroup(
                group_id=f"group-{len(groups) + 1:05d}",
                directory=str(directory),
                quality=quality,
                reason=reason,
                files=entries,
            )
        )

    payload = {
        "schema_version": 1,
        "created_at": utc_now(),
        "roots": [str(path.resolve()) for path in roots],
        "rights_review_required": True,
        "rights_note": "目录中的文件默认不代表可再分发。训练前必须由用户确认拥有训练和模型分发所需权利。",
        "counts": {
            "groups": len(groups),
            "paired": sum(group.quality == "paired" for group in groups),
            "candidate": sum(group.quality == "candidate" for group in groups),
            "unpaired": sum(group.quality == "unpaired" for group in groups),
            "files": sum(len(group.files) for group in groups),
        },
        "groups": [asdict(group) for group in groups],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload

