from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from .constants import DEFAULT_MSST_ROOT, DEFAULT_TRANSCRIPTION_ROOT, DEFAULT_UVR_ROOT
from .manifest import sha256_file


@dataclass(frozen=True)
class LocalRuntime:
    msst_root: Path
    uvr_root: Path
    transcription_root: Path

    @property
    def msst_python(self) -> Path:
        legacy = self.msst_root / "env" / "python.exe"
        modern = self.msst_root / "env" / "Scripts" / "python.exe"
        return legacy if legacy.is_file() else modern

    @property
    def msst_cli(self) -> Path:
        return self.msst_root / "env" / "Scripts" / "msst.exe"

    @property
    def msst_inference(self) -> Path:
        return self.msst_root / "inference.py"

    @property
    def demucs_repo(self) -> Path:
        return self.uvr_root / "models" / "Demucs_Models" / "v3_v4_repo"

    @property
    def public_mega53_config(self) -> Path:
        return self.msst_root / "configs" / "mvsep_mega_model_bs_roformer_53_stems.yaml"

    @property
    def public_mega53_checkpoint(self) -> Path:
        return self.msst_root / "pretrain" / "mvsep_mega_model_bs_roformer_53_stems_v1.ckpt"

    @property
    def basic_pitch_exe(self) -> Path:
        return self.transcription_root / ".venv" / "Scripts" / "basic-pitch.exe"

    @property
    def basic_pitch_python(self) -> Path:
        portable = self.transcription_root.parent / "python-base" / "python.exe"
        if portable.is_file():
            return portable
        return self.transcription_root / ".venv" / "Scripts" / "python.exe"

    @property
    def basic_pitch_packages(self) -> Path:
        return self.transcription_root / ".venv" / "Lib" / "site-packages"

    @property
    def basic_pitch_model(self) -> Path:
        return (
            self.transcription_root
            / ".venv"
            / "Lib"
            / "site-packages"
            / "basic_pitch"
            / "saved_models"
            / "icassp_2022"
            / "nmp.onnx"
        )

    @property
    def transkun_packages(self) -> Path:
        return self.transcription_root.parent / "transkun-packages"

    @property
    def transkun_weight(self) -> Path:
        return self.transkun_packages / "transkun" / "pretrained" / "2.0.pt"

    @property
    def transkun_config(self) -> Path:
        return self.transkun_packages / "transkun" / "pretrained" / "2.0.conf"

    @classmethod
    def defaults(cls) -> "LocalRuntime":
        if getattr(sys, "frozen", False):
            application_directory = Path(sys.executable).resolve().parent
            deployment_root = application_directory.parent
            state_path = deployment_root / "deployment-state.json"
            if application_directory.name.casefold() == "app" and state_path.is_file():
                state = json.loads(state_path.read_text(encoding="utf-8-sig"))
                external = state.get("externalRuntimeRoots")
                if (
                    isinstance(external, dict)
                    and {"msst", "transcription"} <= external.keys()
                    and state.get("launchReady") is True
                ):
                    return cls(
                        Path(str(external["msst"])),
                        Path(str(external.get("uvr", DEFAULT_UVR_ROOT))),
                        Path(str(external["transcription"])),
                    )
                relative = Path(str(state.get("expectedRuntimeDirectory", "")))
                runtime_root = (deployment_root / relative).resolve()
                if (
                    state.get("launchReady") is True
                    and not relative.is_absolute()
                    and runtime_root.is_relative_to(deployment_root.resolve())
                ):
                    return cls(
                        runtime_root / "msst",
                        runtime_root / "uvr",
                        runtime_root / "transcription" / "basic-pitch",
                    )
        return cls(DEFAULT_MSST_ROOT, DEFAULT_UVR_ROOT, DEFAULT_TRANSCRIPTION_ROOT)

    def validate_core(self) -> None:
        missing: list[str] = []
        if not self.msst_python.is_file():
            missing.append(str(self.msst_python))
        if not self.msst_cli.is_file() and not self.msst_inference.is_file():
            missing.append(f"MSST 推理入口：{self.msst_cli} 或 {self.msst_inference}")
        public_model_ready = self.public_mega53_config.is_file() and self.public_mega53_checkpoint.is_file()
        legacy_model_ready = all(
            path.is_file()
            for path in (
                self.msst_root / "configs" / "BS-Roformer-Resurrection-Config.yaml",
                self.msst_root / "pretrain" / "BS-Roformer-Resurrection.ckpt",
                self.msst_root / "configs" / "config_karaoke_frazer_becruily.yaml",
                self.msst_root / "pretrain" / "bs_roformer_karaoke_frazer_becruily.ckpt",
                self.msst_root / "configs" / "BS-Rofo-SW-Fixed.yaml",
                self.msst_root / "pretrain" / "BS-Rofo-SW-Fixed.ckpt",
            )
        )
        vocal_model_ready = all(
            path.is_file()
            for path in (
                self.msst_root / "configs" / "BS-Roformer-Resurrection-Config.yaml",
                self.msst_root / "pretrain" / "BS-Roformer-Resurrection.ckpt",
            )
        )
        dual_model_ready = all(
            path.is_file()
            for path in (
                self.msst_root / "configs" / "config_deux_becruily.yaml",
                self.msst_root / "pretrain" / "becruily_deux.ckpt",
            )
        )
        if not public_model_ready and not legacy_model_ready and not vocal_model_ready and not dual_model_ready:
            missing.extend([str(self.public_mega53_config), str(self.public_mega53_checkpoint)])
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            missing.append("ffmpeg / ffprobe")
        if missing:
            raise RuntimeError("本地运行环境不完整：\n" + "\n".join(missing))

    def activate(self) -> None:
        runtime_bin = self.msst_root / "env"
        if not (runtime_bin / "ffmpeg.exe").is_file() or not (runtime_bin / "ffprobe.exe").is_file():
            return
        current = os.environ.get("PATH", "")
        entries = current.split(os.pathsep) if current else []
        if str(runtime_bin).casefold() not in {entry.casefold() for entry in entries}:
            os.environ["PATH"] = str(runtime_bin) + os.pathsep + current

    def report(self) -> dict[str, Any]:
        models: list[dict[str, Any]] = []
        for folder in (
            self.msst_root / "pretrain",
            self.uvr_root / "models" / "MDX_Net_Models",
            self.uvr_root / "models" / "VR_Models",
            self.demucs_repo,
        ):
            if not folder.is_dir():
                continue
            for path in sorted(folder.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".ckpt", ".pth", ".onnx", ".th"}:
                    models.append(
                        {
                            "path": str(path.resolve()),
                            "size": path.stat().st_size,
                            "sha256": sha256_file(path),
                        }
                    )
        return {
            "msst_root": str(self.msst_root),
            "uvr_root": str(self.uvr_root),
            "transcription_root": str(self.transcription_root),
            "public_mega53_ready": self.public_mega53_config.is_file() and self.public_mega53_checkpoint.is_file(),
            "public_mega53_checkpoint": str(self.public_mega53_checkpoint),
            "public_mega53_checkpoint_sha256": (
                sha256_file(self.public_mega53_checkpoint) if self.public_mega53_checkpoint.is_file() else None
            ),
            "basic_pitch_ready": self.basic_pitch_python.is_file(),
            "basic_pitch_model": str(self.basic_pitch_model),
            "basic_pitch_model_sha256": (
                sha256_file(self.basic_pitch_model) if self.basic_pitch_model.is_file() else None
            ),
            "transkun_ready": self.transkun_weight.is_file() and self.transkun_config.is_file(),
            "transkun_weight": str(self.transkun_weight),
            "transkun_weight_sha256": (
                sha256_file(self.transkun_weight) if self.transkun_weight.is_file() else None
            ),
            "models": models,
        }

    def save_report(self, path: Path) -> None:
        path.write_text(json.dumps(self.report(), ensure_ascii=False, indent=2), encoding="utf-8")

