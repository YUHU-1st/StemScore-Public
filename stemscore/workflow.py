from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re
import shutil
from typing import Callable
import uuid

from .audio import convert_to_wav, high_frequency_balance_db, mean_volume_db, mix_audio, probe_audio, subtract_audio, validate_durations, volume_stats_db
from .backends import demucs, msst, transcription
from .constants import PROJECT_FILE, STAGE_DIRS, STAGE_NAMES
from .inventory import LocalRuntime
from .manifest import Artifact, ProjectManifest, StageRecord, utc_now
from .midi_tools import merge_midi_files, write_empty_midi


LogCallback = Callable[[str], None]

DEREVERB_MAX_LEVEL_LOSS_DB = 6.0
TIMBRE_MAX_HIGH_FREQUENCY_LOSS_DB = 4.0
TRANSIENT_MAX_CREST_FACTOR_LOSS_DB = 3.0
ALL_DEREVERB_ROLES = ("lead_vocal", "harmony_vocal", "bass", "drums", "guitar", "piano", "other")
DEFAULT_DEREVERB_ROLES = ("lead_vocal",)
HIGH_FREQUENCY_PROTECTED_ROLES = {"lead_vocal", "harmony_vocal", "drums", "guitar", "piano", "other"}

PUBLIC_MEGA53_GROUPS = {
    "lead_vocal": ("lead-vocal", "vocal"),
    "harmony_vocal": ("back-vocal",),
    "bass": ("bass", "double-bass"),
    "drums": ("drums", "kick", "snare", "hh", "toms", "percussion", "congas", "tambourine", "timpani", "triangle"),
    "guitar": ("guitar", "acoustic-guitar", "electric-guitar", "dobro", "banjo", "mandolin", "sitar", "ukulele"),
    "piano": ("piano", "digital-piano", "keys", "harpsichord", "organ"),
}


def dereverb_quality_status(
    role: str,
    source_level_db: float,
    candidate_level_db: float,
    source_high_balance_db: float = 0.0,
    candidate_high_balance_db: float = 0.0,
    source_crest_factor_db: float = 0.0,
    candidate_crest_factor_db: float = 0.0,
) -> tuple[bool, str]:
    if candidate_level_db - source_level_db < -DEREVERB_MAX_LEVEL_LOSS_DB:
        return False, "rejected_level_loss"
    if (
        role in HIGH_FREQUENCY_PROTECTED_ROLES
        and candidate_high_balance_db - source_high_balance_db < -TIMBRE_MAX_HIGH_FREQUENCY_LOSS_DB
    ):
        status_role = "drum" if role == "drums" else role
        return False, f"rejected_{status_role}_high_frequency_loss"
    if candidate_crest_factor_db - source_crest_factor_db < -TRANSIENT_MAX_CREST_FACTOR_LOSS_DB:
        status_role = "drum" if role == "drums" else role
        return False, f"rejected_{status_role}_transient_loss"
    return True, "accepted"


def safe_name(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip(" .")
    value = re.sub(r"\s+", " ", value)
    return value[:80] or "untitled"


class Workflow:
    def __init__(self, manifest: ProjectManifest, runtime: LocalRuntime | None = None) -> None:
        self.manifest = manifest
        self.root = Path(manifest.root)
        self.runtime = runtime or LocalRuntime.defaults()
        self.runtime.activate()

    @classmethod
    def create(
        cls,
        source: Path,
        projects_root: Path,
        title: str | None = None,
        runtime: LocalRuntime | None = None,
        dereverb_roles: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> "Workflow":
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        title = safe_name(title or source.stem)
        project_id = f"{title}-{uuid.uuid4().hex[:8]}"
        root = projects_root.resolve() / project_id
        root.mkdir(parents=True, exist_ok=False)
        runtime = runtime or LocalRuntime.defaults()
        selected_roles = set(DEFAULT_DEREVERB_ROLES if dereverb_roles is None else dereverb_roles)
        unknown_roles = selected_roles.difference(ALL_DEREVERB_ROLES)
        if unknown_roles:
            raise ValueError(f"未知去混响角色：{', '.join(sorted(unknown_roles))}")
        settings = {
            "msst_root": str(runtime.msst_root),
            "uvr_root": str(runtime.uvr_root),
            "transcription_root": str(runtime.transcription_root),
            "output_sample_rate": 44100,
            "manual_review_required": True,
            "dereverb_roles": [role for role in ALL_DEREVERB_ROLES if role in selected_roles],
        }
        manifest = ProjectManifest.create(project_id, title, root, source, settings)
        for number in range(1, 6):
            (root / STAGE_DIRS[number]).mkdir(parents=True, exist_ok=True)
        (root / "logs").mkdir()
        (root / "audit").mkdir()
        manifest.save()
        return cls(manifest, runtime)

    @classmethod
    def load(cls, project_file: Path) -> "Workflow":
        manifest = ProjectManifest.load(project_file)
        interrupted = False
        for stage in manifest.stages:
            if stage.status == "running":
                stage.status = "failed"
                stage.error = "上次运行在程序关闭前未完成，可直接重试或跳过。"
                interrupted = True
        if interrupted:
            manifest.save()
        settings = manifest.settings
        runtime = LocalRuntime(
            Path(settings["msst_root"]),
            Path(settings["uvr_root"]),
            Path(settings["transcription_root"]),
        )
        return cls(manifest, runtime)

    def _has_artifact(self, stage_number: int, role: str | None = None) -> bool:
        for artifact in self.manifest.stage(stage_number).artifacts:
            if artifact.media_type != "audio/wav":
                continue
            if role is not None and artifact.role != role:
                continue
            if Path(artifact.path).is_file():
                return True
        return False

    def _validate_direct_start(self, number: int) -> None:
        if number == 1:
            return
        if number == 2 and not self._has_artifact(1, "source"):
            raise RuntimeError("第 2 步需要第 1 步已经生成 source WAV。")
        if number == 3 and not (
            self._has_artifact(2, "vocals") and self._has_artifact(2, "accompaniment")
        ):
            raise RuntimeError("第 3 步需要第 2 步已经生成 vocals 和 accompaniment。")
        if number == 4 and not self._has_artifact(3):
            raise RuntimeError("第 4 步需要第 3 步至少已有一条可用分轨。")
        if number == 5 and not (self._has_artifact(1, "source") and self._has_artifact(3)):
            raise RuntimeError("第 5 步需要第 1 步 source WAV 和第 3 步分轨；不要求第 4 步完成。")

    def prepare_stage(self, number: int, allow_unreviewed: bool = False) -> None:
        stage = self.manifest.stage(number)
        if stage.status == "running":
            raise RuntimeError(f"第 {number} 步已经在处理中。")
        if not allow_unreviewed and stage.status != "ready":
            raise RuntimeError(f"第 {number} 步当前状态为 {stage.status}，不能运行。")
        if allow_unreviewed:
            self._validate_direct_start(number)
        else:
            for previous in self.manifest.stages[: number - 1]:
                if previous.status not in {"reviewed", "skipped"}:
                    raise RuntimeError(f"必须先完成并审查第 {previous.number} 步，或明确跳过该步骤。")

        stage.status = "running"
        stage.started_at = utc_now()
        stage.completed_at = None
        stage.reviewed_at = None
        stage.error = None
        stage.artifacts = []
        stage.commands = []
        stage.models = []
        stage.explanation = []
        self.manifest.save()

    def run_stage(
        self,
        number: int,
        callback: LogCallback | None = None,
        allow_unreviewed: bool = False,
    ) -> None:
        stage = self.manifest.stage(number)
        if stage.status != "running":
            self.prepare_stage(number, allow_unreviewed=allow_unreviewed)

        log_path = self.root / "logs" / f"stage-{number}.log"
        log_handle = log_path.open("a", encoding="utf-8")

        def log(message: str) -> None:
            line = str(message)
            log_handle.write(line + "\n")
            log_handle.flush()
            if callback:
                callback(line)

        try:
            getattr(self, f"_stage_{number}")(stage, log)
            stage.status = "review_required"
            stage.completed_at = utc_now()
            stage.artifacts.append(Artifact.from_file(log_path, "execution_log", "text/plain"))
            self._write_stage_audit(stage)
            self.manifest.save()
        except Exception as error:
            stage.status = "failed"
            stage.error = str(error)
            self.manifest.save()
            raise
        finally:
            log_handle.close()

    def approve(self, number: int) -> None:
        stage = self.manifest.stage(number)
        if stage.status != "review_required":
            raise RuntimeError("只有已完成且等待审查的步骤可以通过。")
        errors = [message for artifact in stage.artifacts for ok, message in [artifact.verify()] if not ok]
        if errors:
            raise RuntimeError("审查前校验失败：\n" + "\n".join(errors))
        stage.status = "reviewed"
        stage.reviewed_at = utc_now()
        if number < 5:
            self.manifest.stage(number + 1).status = "ready"
        self.manifest.save()

    def retry(self, number: int) -> None:
        stage = self.manifest.stage(number)
        if stage.status not in {"failed", "review_required"}:
            raise RuntimeError("当前步骤不能重试。")
        stage.status = "ready"
        stage.error = None
        self.manifest.save()

    def skip(self, number: int) -> None:
        stage = self.manifest.stage(number)
        if stage.status == "running":
            raise RuntimeError("正在运行的步骤不能直接跳过；可先等待本次运行结束，或直接选择其他步骤运行。")
        if stage.status == "reviewed":
            raise RuntimeError("该步骤已经审查通过，无需跳过。")
        stage.status = "skipped"
        stage.completed_at = utc_now()
        stage.reviewed_at = utc_now()
        stage.error = None
        stage.explanation.append("用户选择跳过此步骤。后续步骤会尽量使用最近的可用上游产物。")
        if number < 5 and self.manifest.stage(number + 1).status == "locked":
            self.manifest.stage(number + 1).status = "ready"
        self.manifest.save()

    def _prefix(self, number: int) -> str:
        return f"{safe_name(self.manifest.title)}_{number:02d}"

    def _artifact_by_role(self, stage: int, role: str) -> Path:
        item = next((a for a in self.manifest.stage(stage).artifacts if a.role == role), None)
        if item is None:
            raise RuntimeError(f"第 {stage} 步缺少产物：{role}")
        return Path(item.path)

    def _add_audio(
        self,
        stage: StageRecord,
        path: Path,
        role: str,
        metadata: dict | None = None,
    ) -> None:
        details = probe_audio(path)
        details.update(metadata or {})
        stage.artifacts.append(Artifact.from_file(path, role, "audio/wav", details))

    def _stage_1(self, stage: StageRecord, log: LogCallback) -> None:
        output = self.root / STAGE_DIRS[1] / f"{self._prefix(1)}_source.wav"
        command = convert_to_wav(Path(self.manifest.source_original), output, log)
        stage.commands.append(command)
        self._add_audio(stage, output, "source")
        stage.explanation.extend(
            [
                "将原始音乐统一转换为 44.1 kHz、双声道、24-bit PCM WAV，后续模型读取同一确定输入。",
                "原文件保持只读；清单记录原始绝对路径、源 WAV 哈希和音频参数。",
            ]
        )
        self.runtime.save_report(self.root / "audit" / "local-model-inventory.json")
        stage.artifacts.append(
            Artifact.from_file(
                self.root / "audit" / "local-model-inventory.json",
                "local_model_inventory",
                "application/json",
            )
        )

    def _stage_2(self, stage: StageRecord, log: LogCallback) -> None:
        self.runtime.validate_core()
        source = self._artifact_by_role(1, "source")
        output = self.root / STAGE_DIRS[2]
        legacy_ready = all(
            msst.model_available(self.runtime, model)
            for model in (msst.VOCAL_MODEL, msst.KARAOKE_MODEL, msst.SIX_STEM_MODEL)
        )
        if legacy_ready:
            files, command = msst.separate(
                self.runtime,
                source,
                output,
                msst.VOCAL_MODEL,
                {
                    "primary": f"{self._prefix(2)}_vocals.wav",
                    "complement": f"{self._prefix(2)}_accompaniment.wav",
                },
                log,
            )
            stage.commands.append(command)
            stage.models.append(msst.model_evidence(self.runtime, msst.VOCAL_MODEL))
            self.manifest.settings["separation_backend"] = "legacy-private"
        else:
            cache = self.root / "audit" / "mega53-stems"
            if cache.exists():
                shutil.rmtree(cache)
            raw_stems, command = msst.separate_all(
                self.runtime, source, cache, msst.PUBLIC_MEGA53_MODEL, log
            )
            vocals = output / f"{self._prefix(2)}_vocals.wav"
            accompaniment = output / f"{self._prefix(2)}_accompaniment.wav"
            vocal_sources = [raw_stems[name] for name in ("lead-vocal", "back-vocal", "vocal") if name in raw_stems]
            if not vocal_sources:
                raise RuntimeError("MVSep Mega 53-stem 未生成任何人声轨。")
            mix_command = mix_audio(vocal_sources, vocals, log)
            subtract_command = subtract_audio(source, vocals, accompaniment, log)
            files = [vocals, accompaniment]
            stage.commands.extend([command, mix_command, subtract_command])
            stage.models.append(msst.model_evidence(self.runtime, msst.PUBLIC_MEGA53_MODEL))
            self.manifest.settings["separation_backend"] = "public-mega53-v1"
            self.manifest.save()
        self._add_audio(stage, files[0], "vocals")
        self._add_audio(stage, files[1], "accompaniment")
        validation = validate_durations(files)
        stage.explanation.extend(
            [
                (
                    "使用用户已有的私有 MSST 人声模型分离人声与伴奏。"
                    if legacy_ready
                    else "使用 MIT 许可的 MVSep Mega 53-stem v1；lead-vocal、back-vocal、vocal 合成为人声，伴奏由原混音减去人声得到。"
                ),
                f"两轨时长差 {validation['duration_spread_seconds']:.6f} 秒。",
            ]
        )

    def _stage_3(self, stage: StageRecord, log: LogCallback) -> None:
        vocals = self._artifact_by_role(2, "vocals")
        accompaniment = self._artifact_by_role(2, "accompaniment")
        output = self.root / STAGE_DIRS[3]
        if self.manifest.settings.get("separation_backend") == "public-mega53-v1":
            cache = self.root / "audit" / "mega53-stems"
            raw_stems = {
                path.stem.removeprefix("track_"): path
                for path in cache.glob("track_*.wav")
            }
            if not raw_stems:
                raise RuntimeError("MVSep Mega 53-stem 缓存不存在；请重试第 2 步。")
            grouped: dict[str, Path] = {}
            commands: list[list[str]] = []
            for role, names in PUBLIC_MEGA53_GROUPS.items():
                sources = [raw_stems[name] for name in names if name in raw_stems]
                if not sources:
                    raise RuntimeError(f"MVSep Mega 53-stem 缺少 {role} 所需分轨：{', '.join(names)}")
                target = output / f"{self._prefix(3)}_{role}.wav"
                commands.append(mix_audio(sources, target, log))
                grouped[role] = target
            known_instruments = output / ".known-instruments.wav"
            commands.append(
                mix_audio(
                    [grouped[role] for role in ("bass", "drums", "guitar", "piano")],
                    known_instruments,
                    log,
                )
            )
            other = output / f"{self._prefix(3)}_other.wav"
            commands.append(subtract_audio(accompaniment, known_instruments, other, log))
            known_instruments.unlink(missing_ok=True)
            stage.commands.extend(commands)
            stage.models.append(msst.model_evidence(self.runtime, msst.PUBLIC_MEGA53_MODEL))
            self._add_audio(stage, grouped["lead_vocal"], "lead_vocal")
            self._add_audio(stage, grouped["harmony_vocal"], "harmony_vocal")
            for role in ("bass", "drums", "guitar", "piano"):
                self._add_audio(stage, grouped[role], role)
            self._add_audio(stage, other, "other")
            files = [grouped["lead_vocal"], grouped["harmony_vocal"], grouped["bass"], grouped["drums"], grouped["guitar"], grouped["piano"], other]
            validation = validate_durations(files, tolerance_seconds=0.2)
            stage.explanation.extend(
                [
                    "沿用第 2 步一次生成的 MVSep Mega 53-stem v1 原始分轨，不重复加载 1.37 GB 模型。",
                    "lead-vocal/vocal 合为主唱，back-vocal 为和声；低音、鼓、吉他和键盘按 53-stem 标签聚合，其余伴奏残差作为 other。",
                    f"七轨最大时长差 {validation['duration_spread_seconds']:.6f} 秒。",
                ]
            )
            return
        vocal_files, vocal_command = msst.separate(
            self.runtime,
            vocals,
            output,
            msst.KARAOKE_MODEL,
            {
                "primary": f"{self._prefix(3)}_lead_vocal.wav",
                "complement": f"{self._prefix(3)}_harmony_vocal.wav",
            },
            log,
        )
        instrument_files, instrument_command = msst.separate(
            self.runtime,
            accompaniment,
            output,
            msst.SIX_STEM_MODEL,
            {
                role: f"{self._prefix(3)}_{role}.wav"
                for role in ("bass", "drums", "guitar", "piano", "other")
            },
            log,
        )
        stage.commands.extend([vocal_command, instrument_command])
        stage.models.extend(
            [
                msst.model_evidence(self.runtime, msst.KARAOKE_MODEL),
                msst.model_evidence(self.runtime, msst.SIX_STEM_MODEL),
            ]
        )
        self._add_audio(stage, vocal_files[0], "lead_vocal")
        self._add_audio(stage, vocal_files[1], "harmony_vocal")
        for path in instrument_files:
            self._add_audio(stage, path, path.stem.rsplit("_", 1)[-1])
        validation = validate_durations(vocal_files + instrument_files, tolerance_seconds=0.2)
        stage.explanation.extend(
            [
                "和声模型在已分离人声上预测主唱，残差作为和声/叠唱，减少乐器串音。",
                "六分轨 BS-RoFormer 在伴奏上直接预测 bass、drums、guitar、piano、other；其 guitar/piano 为独立训练输出，不再使用 htdemucs_6s 的弱六轨头。",
                "该六分轨权重来源为 enerjazzer/BS-ROFO-SW-Fixed 镜像，原作者未声明模型许可证；审计记录哈希，不随安装包再分发。",
                f"七轨最大时长差 {validation['duration_spread_seconds']:.6f} 秒。",
            ]
        )

    def _stage_4(self, stage: StageRecord, log: LogCallback) -> None:
        output = self.root / STAGE_DIRS[4]
        inputs = [artifact for artifact in self.manifest.stage(3).artifacts if artifact.media_type == "audio/wav"]
        commands: list[list[str]] = []
        generated: list[Path] = []
        rejected: list[str] = []
        skipped: list[str] = []
        bypassed: list[str] = []
        unavailable: list[str] = []
        selected_roles = set(self.manifest.settings.get("dereverb_roles", DEFAULT_DEREVERB_ROLES))
        for artifact in inputs:
            target_name = f"{self._prefix(4)}_{artifact.role}_dry.wav"
            target_path = output / target_name
            source_level = mean_volume_db(Path(artifact.path))
            if artifact.role not in selected_roles:
                target_name = f"{self._prefix(4)}_{artifact.role}.wav"
                target_path = output / target_name
                shutil.copy2(Path(artifact.path), target_path)
                generated.append(target_path)
                bypassed.append(artifact.role)
                log(f"按角色策略保留原分轨，不执行去混响：{artifact.role}")
                self._add_audio(
                    stage,
                    target_path,
                    artifact.role,
                    {
                        "source_mean_volume_db": source_level,
                        "candidate_mean_volume_db": None,
                        "output_mean_volume_db": source_level,
                        "dereverb_accepted": False,
                        "quality_status": "bypassed_by_role_policy",
                        "quality_gate": "role not selected for dereverb; original stem preserved for MIDI",
                    },
                )
                continue
            if source_level < -65.0:
                shutil.copy2(Path(artifact.path), target_path)
                generated.append(target_path)
                skipped.append(artifact.role)
                log(f"去混响质量门跳过近静音轨：{artifact.role} ({source_level:.1f} dB)")
                self._add_audio(
                    stage,
                    target_path,
                    f"{artifact.role}_dry",
                    {
                        "source_mean_volume_db": source_level,
                        "candidate_mean_volume_db": None,
                        "output_mean_volume_db": source_level,
                        "dereverb_accepted": False,
                        "quality_status": "skipped_near_silent_stem",
                        "quality_gate": "skip dereverb below -65 dB",
                    },
                )
                continue
            if not msst.model_available(self.runtime, msst.DEREVERB_MODEL):
                shutil.copy2(Path(artifact.path), target_path)
                generated.append(target_path)
                unavailable.append(artifact.role)
                log(f"未安装许可兼容的去混响模型，保留原分轨：{artifact.role}")
                self._add_audio(
                    stage,
                    target_path,
                    f"{artifact.role}_dry",
                    {
                        "source_mean_volume_db": source_level,
                        "candidate_mean_volume_db": None,
                        "output_mean_volume_db": source_level,
                        "dereverb_accepted": False,
                        "quality_status": "bypassed_no_permissive_dereverb_model",
                        "quality_gate": "no auto-downloadable permissively licensed dereverb checkpoint configured",
                    },
                )
                continue
            source_level, source_peak = volume_stats_db(Path(artifact.path))
            source_crest_factor = source_peak - source_level
            files, command = msst.separate(
                self.runtime,
                Path(artifact.path),
                output,
                msst.DEREVERB_MODEL,
                {"primary": target_name},
                log,
            )
            generated.extend(files)
            commands.append(command)
            candidate_level, candidate_peak = volume_stats_db(files[0])
            candidate_crest_factor = candidate_peak - candidate_level
            source_high_balance = None
            candidate_high_balance = None
            if artifact.role in HIGH_FREQUENCY_PROTECTED_ROLES:
                source_high_balance = high_frequency_balance_db(Path(artifact.path), source_level)
                candidate_high_balance = high_frequency_balance_db(files[0], candidate_level)
            accepted, quality_status = dereverb_quality_status(
                artifact.role,
                source_level,
                candidate_level,
                source_high_balance or 0.0,
                candidate_high_balance or 0.0,
                source_crest_factor,
                candidate_crest_factor,
            )
            if not accepted:
                shutil.copy2(Path(artifact.path), files[0])
                rejected.append(artifact.role)
                log(
                    f"去混响质量门回退 {artifact.role}: "
                    f"{source_level:.1f} dB -> {candidate_level:.1f} dB; "
                    f"状态 {quality_status}"
                )
            self._add_audio(
                stage,
                files[0],
                f"{artifact.role}_dry",
                {
                    "source_mean_volume_db": source_level,
                    "candidate_mean_volume_db": candidate_level,
                    "output_mean_volume_db": mean_volume_db(files[0]),
                    "source_high_frequency_balance_db": source_high_balance,
                    "candidate_high_frequency_balance_db": candidate_high_balance,
                    "high_frequency_balance_change_db": (
                        candidate_high_balance - source_high_balance
                        if source_high_balance is not None and candidate_high_balance is not None
                        else None
                    ),
                    "source_crest_factor_db": source_crest_factor,
                    "candidate_crest_factor_db": candidate_crest_factor,
                    "crest_factor_change_db": candidate_crest_factor - source_crest_factor,
                    "dereverb_accepted": accepted,
                    "quality_status": quality_status,
                    "quality_gate": "reject above 6 dB level loss, above 4 dB relative high-frequency loss at 6 kHz for protected roles, or above 3 dB crest-factor loss",
                },
            )
        stage.commands.extend(commands)
        if commands:
            stage.models.append(msst.model_evidence(self.runtime, msst.DEREVERB_MODEL))
        validation = validate_durations(generated, tolerance_seconds=0.2)
        stage.explanation.extend(
            [
                f"本项目选择去混响的角色：{', '.join(role for role in ALL_DEREVERB_ROLES if role in selected_roles) if selected_roles else '无'}；未选择角色直接保留第 3 步分轨供 MIDI 使用。",
                "本地 MelBand-RoFormer 只为已选择角色生成候选；平均电平损失超过 6 dB、受保护角色在 6 kHz 以上相对能量损失超过 4 dB，或峰均比下降超过 3 dB时自动拒绝。",
                f"本次质量门回退轨道：{', '.join(rejected) if rejected else '无'}；回退轨保留第 3 步原始分轨并明确写入元数据。",
                f"按角色策略跳过去混响轨道：{', '.join(bypassed) if bypassed else '无'}。",
                f"因未安装许可兼容去混响模型而保留原轨：{', '.join(unavailable) if unavailable else '无'}。",
                f"近静音跳过去混响轨道：{', '.join(skipped) if skipped else '无'}。",
                f"去混响轨最大时长差 {validation['duration_spread_seconds']:.6f} 秒。",
            ]
        )

    def _stage_5_audio_artifacts(self) -> list[Artifact]:
        stage4 = self.manifest.stage(4)
        stage3 = self.manifest.stage(3)
        selected: dict[str, Artifact] = {}
        if stage4.status != "running":
            for artifact in stage4.artifacts:
                if artifact.media_type != "audio/wav" or not Path(artifact.path).is_file():
                    continue
                role = artifact.role.removesuffix("_dry")
                selected[role] = artifact
        for artifact in stage3.artifacts:
            if artifact.media_type != "audio/wav" or not Path(artifact.path).is_file():
                continue
            selected.setdefault(artifact.role, artifact)
        if not selected:
            raise RuntimeError("第 5 步没有找到可用分轨。可以先运行第 3 步，或恢复已有项目产物。")
        return list(selected.values())

    def _stage_5(self, stage: StageRecord, log: LogCallback) -> None:
        source = self._artifact_by_role(1, "source")
        analysis, tempo_command = transcription.analyze(self.runtime, source, False, log)
        bpm = float(analysis["bpm"])
        output = self.root / STAGE_DIRS[5]
        commands: list[list[str]] = [tempo_command]
        midi_files: list[tuple[str, Path]] = []
        pitched_jobs: list[tuple[Path, Path, str]] = []
        drum_results: dict[str, tuple[dict, list[str]]] = {}
        piano_results: dict[str, tuple[dict, list[str]]] = {}
        skipped_results: dict[str, dict] = {}
        stage5_inputs = self._stage_5_audio_artifacts()
        if self.manifest.stage(4).status != "reviewed":
            log("第 4 步尚未审查完成；第 5 步将优先使用已完成的第 4 步轨道，并用第 3 步原分轨补齐缺失角色。")
        for artifact in stage5_inputs:
            role = artifact.role.removesuffix("_dry")
            target = output / f"{self._prefix(5)}_{role}.mid"
            if role == "drums":
                drum_results[role] = transcription.drums(
                    self.runtime, Path(artifact.path), target, bpm, log
                )
            elif mean_volume_db(Path(artifact.path)) < -65.0:
                metadata = write_empty_midi(target, role, bpm)
                metadata.update(
                    {
                        "algorithm": "quality gate - no transcription",
                        "quality_status": "skipped_near_silent_stem",
                        "source_mean_volume_db": mean_volume_db(Path(artifact.path)),
                        "silence_threshold_db": -65.0,
                    }
                )
                skipped_results[role] = metadata
                log(f"MIDI 质量门跳过近静音轨：{role}")
            elif role == "piano":
                piano_results[role] = transcription.piano(
                    self.runtime, Path(artifact.path), target, bpm, log
                )
            else:
                pitched_jobs.append((Path(artifact.path), target, role))
            midi_files.append((role, target))
        pitched_results, pitched_commands = transcription.basic_pitch_many(
            self.runtime, pitched_jobs, bpm, log
        )
        commands.extend(pitched_commands)
        commands.extend(command for _, command in drum_results.values())
        commands.extend(command for _, command in piano_results.values())
        for role, target in midi_files:
            if role == "drums":
                metadata = drum_results[role][0]
            elif role in skipped_results:
                metadata = skipped_results[role]
            elif role == "piano":
                metadata = piano_results[role][0]
            else:
                metadata = pitched_results[role]
            stage.artifacts.append(Artifact.from_file(target, f"{role}_midi", "audio/midi", metadata))
        merged = output / f"{self._prefix(5)}_all_tracks.mid"
        merged_metadata = merge_midi_files(midi_files, merged, bpm)
        stage.artifacts.append(Artifact.from_file(merged, "all_tracks_midi", "audio/midi", merged_metadata))
        stage.commands.extend(commands)
        stage.models.append(transcription.model_evidence(self.runtime))
        stage.explanation.extend(
            [
                f"全曲检测速度为 {bpm:.4f} BPM；原始候选 {float(analysis.get('raw_bpm', bpm)):.4f} BPM，选择规则 {analysis.get('tempo_selection', 'raw')}；各轨 MIDI 统一保留绝对时间后写入该速度。",
                "钢琴使用本地 TransKun V2；主唱、和声和其余有音高乐器使用 Basic Pitch 的乐器专属高置信度、音域和最短时值约束；鼓轨使用可解释的三频带起音分类器。",
                "平均电平低于 -65 dB 的近静音分轨输出带原因的空 MIDI，不再把模型噪声伪装成音符。",
                "单轨 MIDI 与合并多轨 MIDI 同时保留，算法、阈值、跳过原因、音符数量和音域均写入产物元数据。",
            ]
        )

    def run_music_analysis(self, callback: LogCallback | None = None) -> list[Artifact]:
        if self.manifest.stage(5).status != "reviewed":
            raise RuntimeError("请先完成并审查通过五步分轨与 MIDI 流程。")
        from .analysis import analyze_music

        log = callback or (lambda _message: None)
        source = self._artifact_by_role(1, "source")
        stems = [
            artifact
            for artifact in self.manifest.stage(4).artifacts
            if artifact.media_type == "audio/wav"
        ]
        output = self.root / "06_music_analysis"
        output.mkdir(parents=True, exist_ok=True)
        log("正在本地计算 BPM、调性、配器与结构候选…")
        report, command = analyze_music(self.runtime, source, stems=stems, log=log)
        prefix = safe_name(self.manifest.title)
        json_path = output / f"{prefix}_music-analysis.json"
        markdown_path = output / f"{prefix}_music-analysis.md"
        prompt_path = output / f"{prefix}_minimax-music3-instructions.txt"
        comfyui_path = output / f"{prefix}_comfyui-minimax-music3-input.json"
        audit_path = output / f"{prefix}_analysis-audit.json"

        markdown = str(report.pop("markdown"))
        caption = str(report["music3_caption"])
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_path.write_text(markdown, encoding="utf-8")
        prompt_path.write_text(caption, encoding="utf-8")
        has_vocal = any(
            item.get("active") and item.get("role") in {"lead_vocal", "vocals"}
            for item in report.get("instrumentation", [])
        )
        input_template = (
            "[Intro]\n[Verse]\n[Chorus]\n[Bridge]\n[Chorus]\n[Outro]"
            if has_vocal
            else "[Intro]\n[Instrumental]\n[Solo]\n[Outro]"
        )
        comfyui_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "model": "MiniMax Music 3",
                    "workflow_template_url": "https://github.com/Comfy-Org/workflow_templates/blob/main/templates/audio_minimax_music_3.json",
                    "node": "MiniMaxMusic3TextEncode",
                    "caption": caption.rstrip(),
                    "lyrics": input_template,
                    "seed": 0,
                    "max_duration": min(
                        300.0,
                        max(30.0, float(report.get("duration_seconds", 120.0) or 120.0)),
                    ),
                    "cfg_scale": 1.7,
                    "top_k": 50,
                    "lyrics_note_zh": "人声歌曲请在段落标签下填入自有或已授权歌词；本文件不复制源歌歌词。",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        artifacts = [
            Artifact.from_file(json_path, "music_analysis_json", "application/json"),
            Artifact.from_file(markdown_path, "music_analysis_report", "text/markdown"),
            Artifact.from_file(prompt_path, "minimax_music3_instructions", "text/plain"),
            Artifact.from_file(comfyui_path, "comfyui_minimax_music3_input", "application/json"),
        ]
        audit_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "completed_at": utc_now(),
                    "source": asdict(self.manifest.stage(1).artifacts[0]),
                    "stems": [asdict(artifact) for artifact in stems],
                    "command": command,
                    "outputs": [asdict(artifact) for artifact in artifacts],
                    "explanation": [
                        "BPM、调性、响度、频谱、起音与结构由本地 librosa 算法生成。",
                        "配器由已审查分轨的角色和相对能量推导；曲风只输出带证据的保守候选。",
                        "Music 3 instructions 遵循官方三段式 caption，不复制源歌歌词。",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        artifacts.append(Artifact.from_file(audit_path, "music_analysis_audit", "application/json"))
        self.manifest.settings["music_analysis"] = {
            "completed_at": utc_now(),
            "directory": str(output.resolve()),
            "artifacts": [asdict(artifact) for artifact in artifacts],
        }
        self.manifest.save()
        log("音乐分析报告与 MiniMax Music 3 提示词已生成。")
        return artifacts

    def _write_stage_audit(self, stage: StageRecord) -> None:
        path = self.root / "audit" / f"stage-{stage.number}.json"
        path.write_text(
            json.dumps(asdict(stage), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        stage.artifacts.append(Artifact.from_file(path, "stage_audit", "application/json"))


def find_project(path: Path) -> Path:
    if path.is_file() and path.name == PROJECT_FILE:
        return path
    candidate = path / PROJECT_FILE
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"未找到 {PROJECT_FILE}：{path}")

