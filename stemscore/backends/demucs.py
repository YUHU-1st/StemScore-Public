from __future__ import annotations

from pathlib import Path
import shutil
from typing import Callable

from ..audio import run_logged
from ..inventory import LocalRuntime
from ..manifest import sha256_file


STEMS = ("bass", "drums", "guitar", "piano", "other")


def model_evidence(runtime: LocalRuntime) -> dict[str, object]:
    files = sorted(runtime.demucs_repo.glob("*.th"))
    return {
        "backend": "UVR/Demucs",
        "name": "htdemucs_6s",
        "repo": str(runtime.demucs_repo.resolve()),
        "weights": [
            {"name": path.name, "sha256": sha256_file(path), "size": path.stat().st_size}
            for path in files
        ],
    }


def separate_instruments(
    runtime: LocalRuntime,
    source: Path,
    output_dir: Path,
    prefix: str,
    log: Callable[[str], None],
) -> tuple[list[Path], list[str]]:
    if not runtime.demucs_repo.is_dir():
        raise RuntimeError(f"UVR Demucs 模型目录不存在：{runtime.demucs_repo}")
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / ".demucs"
    wrapper = Path(__file__).resolve().parents[1] / "runtime" / "demucs_cli.py"
    command = [
        str(runtime.msst_python),
        str(wrapper),
        "-n",
        "htdemucs_6s",
        "--repo",
        str(runtime.demucs_repo),
        "-d",
        "cuda",
        "--shifts",
        "1",
        "--overlap",
        "0.25",
        "--segment",
        "9",
        "--int24",
        "-o",
        str(raw_dir),
        str(source),
    ]
    run_logged(command, log)
    track_dir = raw_dir / "htdemucs_6s" / source.stem
    outputs: list[Path] = []
    for stem in STEMS:
        raw = track_dir / f"{stem}.wav"
        if not raw.is_file():
            raise RuntimeError(f"UVR htdemucs_6s 未生成：{raw}")
        target = output_dir / f"{prefix}_{stem}.wav"
        shutil.move(str(raw), target)
        outputs.append(target)
    shutil.rmtree(raw_dir, ignore_errors=True)
    return outputs, command

