from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable

from ..audio import run_logged
from ..inventory import LocalRuntime
from ..manifest import sha256_file


@dataclass(frozen=True)
class MsstModel:
    name: str
    model_type: str
    config: str
    checkpoint: str
    primary_stem: str
    extract_complement: bool = True


VOCAL_MODEL = MsstModel(
    name="BS-Roformer Resurrection",
    model_type="bs_roformer",
    config="configs/BS-Roformer-Resurrection-Config.yaml",
    checkpoint="pretrain/BS-Roformer-Resurrection.ckpt",
    primary_stem="vocals",
)

KARAOKE_MODEL = MsstModel(
    name="BS-Roformer Karaoke Frazer/Becruily",
    model_type="bs_roformer",
    config="configs/config_karaoke_frazer_becruily.yaml",
    checkpoint="pretrain/bs_roformer_karaoke_frazer_becruily.ckpt",
    primary_stem="Vocals",
)

SIX_STEM_MODEL = MsstModel(
    name="BS-RoFormer SW Fixed 6-Stem",
    model_type="bs_roformer",
    config="configs/BS-Rofo-SW-Fixed.yaml",
    checkpoint="pretrain/BS-Rofo-SW-Fixed.ckpt",
    primary_stem="vocals",
    extract_complement=False,
)

DEREVERB_MODEL = MsstModel(
    name="MelBand RoFormer Dereverb/Echo Fused",
    model_type="mel_band_roformer",
    config="configs/config_dereverb_echo_mbr_v2.yaml",
    checkpoint="pretrain/dereverb_echo_mbr_fused_0.5_v2_0.25_big_0.25_super.ckpt",
    primary_stem="dry",
    extract_complement=False,
)


def model_evidence(runtime: LocalRuntime, model: MsstModel) -> dict[str, str | int]:
    config = runtime.msst_root / model.config
    checkpoint = runtime.msst_root / model.checkpoint
    return {
        "backend": "MSST",
        "name": model.name,
        "model_type": model.model_type,
        "config": str(config.resolve()),
        "config_sha256": sha256_file(config),
        "checkpoint": str(checkpoint.resolve()),
        "checkpoint_sha256": sha256_file(checkpoint),
        "checkpoint_size": checkpoint.stat().st_size,
    }


def separate(
    runtime: LocalRuntime,
    source: Path,
    output_dir: Path,
    model: MsstModel,
    output_names: dict[str, str],
    log: Callable[[str], None],
) -> tuple[list[Path], list[str]]:
    config = runtime.msst_root / model.config
    checkpoint = runtime.msst_root / model.checkpoint
    for path in (runtime.msst_python, runtime.msst_inference, config, checkpoint):
        if not path.is_file():
            raise RuntimeError(f"MSST 文件不存在：{path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stemscore-msst-") as temporary:
        temporary_path = Path(temporary)
        input_dir = temporary_path / "input"
        raw_dir = temporary_path / "output"
        input_dir.mkdir()
        raw_dir.mkdir()
        staged = input_dir / "track.wav"
        try:
            os.link(source, staged)
        except OSError:
            shutil.copy2(source, staged)

        command = [
            str(runtime.msst_python),
            str(runtime.msst_inference),
            "--model_type",
            model.model_type,
            "--config_path",
            str(config),
            "--start_check_point",
            str(checkpoint),
            "--input_folder",
            str(input_dir),
            "--store_dir",
            str(raw_dir),
            "--disable_detailed_pbar",
        ]
        if model.extract_complement:
            command.append("--extract_instrumental")
        run_logged(command, log, output_encoding="gbk")

        copied: list[Path] = []
        for role, filename in output_names.items():
            if role == "primary":
                candidates = [raw_dir / f"track_{model.primary_stem}.wav"]
            elif role == "complement":
                candidates = [
                    raw_dir / "track_instrumental.wav",
                    raw_dir / "track_other.wav",
                    raw_dir / "track_Instrumental.wav",
                ]
            else:
                candidates = [
                    raw_dir / f"track_{role}.wav",
                    raw_dir / f"track_{role.capitalize()}.wav",
                ]
            raw = next((path for path in candidates if path.is_file()), None)
            if raw is None:
                existing = ", ".join(path.name for path in raw_dir.glob("*.wav")) or "无"
                raise RuntimeError(f"MSST 未生成预期 {role} 轨，实际输出：{existing}")
            target = output_dir / filename
            shutil.copy2(raw, target)
            copied.append(target)
        return copied, command

