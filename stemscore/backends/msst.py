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
    source_url: str | None = None
    license: str | None = None


VOCAL_MODEL = MsstModel(
    name="BS-Roformer Resurrection",
    model_type="bs_roformer",
    config="configs/BS-Roformer-Resurrection-Config.yaml",
    checkpoint="pretrain/BS-Roformer-Resurrection.ckpt",
    primary_stem="vocals",
)

DUAL_VOCAL_MODEL = MsstModel(
    name="Mel-Band RoFormer Deux",
    model_type="mel_band_roformer",
    config="configs/config_deux_becruily.yaml",
    checkpoint="pretrain/becruily_deux.ckpt",
    primary_stem="Vocals",
    extract_complement=False,
    source_url="https://huggingface.co/becruily/mel-band-roformer-deux",
    license="CC-BY-NC-4.0",
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

PUBLIC_MEGA53_MODEL = MsstModel(
    name="MVSep Mega 53 Stems v1",
    model_type="bs_roformer",
    config="configs/mvsep_mega_model_bs_roformer_53_stems.yaml",
    checkpoint="pretrain/mvsep_mega_model_bs_roformer_53_stems_v1.ckpt",
    primary_stem="lead-vocal",
    extract_complement=False,
    source_url="https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/tag/v1.0.21",
    license="MIT",
)


def model_evidence(runtime: LocalRuntime, model: MsstModel) -> dict[str, str | int]:
    config = runtime.msst_root / model.config
    checkpoint = runtime.msst_root / model.checkpoint
    evidence: dict[str, str | int] = {
        "backend": "MSST",
        "name": model.name,
        "model_type": model.model_type,
        "config": str(config.resolve()),
        "config_sha256": sha256_file(config),
        "checkpoint": str(checkpoint.resolve()),
        "checkpoint_sha256": sha256_file(checkpoint),
        "checkpoint_size": checkpoint.stat().st_size,
    }
    if model.source_url:
        evidence["source_url"] = model.source_url
    if model.license:
        evidence["license"] = model.license
    return evidence


def model_available(runtime: LocalRuntime, model: MsstModel) -> bool:
    return (runtime.msst_root / model.config).is_file() and (runtime.msst_root / model.checkpoint).is_file()


def _inference_command(
    runtime: LocalRuntime,
    model: MsstModel,
    input_dir: Path,
    raw_dir: Path,
) -> list[str]:
    config = runtime.msst_root / model.config
    checkpoint = runtime.msst_root / model.checkpoint
    common = [
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
    if runtime.msst_cli.is_file():
        command = [
            str(runtime.msst_cli),
            "inference",
            *common,
            "--filename_template",
            "{file_name}_{instr}",
        ]
    elif runtime.msst_inference.is_file():
        command = [str(runtime.msst_python), str(runtime.msst_inference), *common]
    else:
        raise RuntimeError(f"MSST 运行环境缺少推理入口：{runtime.msst_cli} / {runtime.msst_inference}")
    if model.extract_complement:
        command.append("--extract_instrumental")
    return command


def _project_temp_parent(output_dir: Path) -> Path:
    """Keep large MSST scratch data on the same volume as the project output."""
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def _check_project_free_space(source: Path, output_dir: Path, multiplier: int, log: Callable[[str], None]) -> None:
    free = shutil.disk_usage(_project_temp_parent(output_dir)).free
    estimated = max(source.stat().st_size * multiplier, 512 * 1024 * 1024)
    log(
        f"MSST 工作盘：{output_dir.anchor or output_dir.parent}，可用 {free / 1024 / 1024 / 1024:.1f} GiB，"
        f"本步骤预计至少需要 {estimated / 1024 / 1024 / 1024:.1f} GiB 临时/输出空间。"
    )
    if free < estimated:
        raise RuntimeError(
            f"项目所在磁盘空间不足：当前可用 {free / 1024 / 1024 / 1024:.1f} GiB，"
            f"本步骤预计至少需要 {estimated / 1024 / 1024 / 1024:.1f} GiB。"
        )


def separate_all(
    runtime: LocalRuntime,
    source: Path,
    output_dir: Path,
    model: MsstModel,
    log: Callable[[str], None],
) -> tuple[dict[str, Path], list[str]]:
    config = runtime.msst_root / model.config
    checkpoint = runtime.msst_root / model.checkpoint
    for path in (runtime.msst_python, config, checkpoint):
        if not path.is_file():
            raise RuntimeError(f"MSST 文件不存在：{path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    _check_project_free_space(source, output_dir, 80, log)
    for stale in output_dir.glob("track_*.wav"):
        stale.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".stemscore-msst-input-",
        dir=str(_project_temp_parent(output_dir)),
    ) as temporary:
        temporary_path = Path(temporary)
        input_dir = temporary_path / "input"
        input_dir.mkdir()
        staged = input_dir / "track.wav"
        try:
            os.link(source, staged)
        except OSError:
            shutil.copy2(source, staged)
        # The 53-stem model can emit ~5 GiB for a four-minute song. Writing it
        # directly into the project cache avoids filling %TEMP% on the system drive
        # and avoids a second full copy of all stems.
        command = _inference_command(runtime, model, input_dir, output_dir)
        run_logged(command, log)
        outputs: dict[str, Path] = {}
        for raw in output_dir.rglob("track_*.wav"):
            stem = raw.stem.removeprefix("track_")
            outputs[stem] = raw
        if not outputs:
            raise RuntimeError("MSST 未生成任何分轨文件。")
        return outputs, command


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
    for path in (runtime.msst_python, config, checkpoint):
        if not path.is_file():
            raise RuntimeError(f"MSST 文件不存在：{path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    _check_project_free_space(source, output_dir, 12, log)
    with tempfile.TemporaryDirectory(
        prefix=".stemscore-msst-",
        dir=str(_project_temp_parent(output_dir)),
    ) as temporary:
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

        command = _inference_command(runtime, model, input_dir, raw_dir)
        run_logged(command, log)

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
                candidate_names = {path.name for path in candidates}
                raw = next((path for path in raw_dir.rglob("*.wav") if path.name in candidate_names), None)
            if raw is None:
                existing = ", ".join(path.name for path in raw_dir.glob("*.wav")) or "无"
                raise RuntimeError(f"MSST 未生成预期 {role} 轨，实际输出：{existing}")
            target = output_dir / filename
            shutil.copy2(raw, target)
            copied.append(target)
        return copied, command

