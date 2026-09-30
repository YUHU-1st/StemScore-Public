from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from .inventory import LocalRuntime


LogCallback = Callable[[str], None]


class _RestartDownload(RuntimeError):
    pass


@contextmanager
def _exclusive_download_lock(part: Path, log: LogCallback):
    """Serialize writers for one .part file, including across StemScore processes."""
    lock = part.with_name(part.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    waiting_logged = False
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if not waiting_logged:
                        log(f"检测到另一个修复任务正在下载 {part.stem}，等待其完成后复用结果…")
                        waiting_logged = True
                    time.sleep(1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waiting_logged:
                        log(f"检测到另一个修复任务正在下载 {part.stem}，等待其完成后复用结果…")
                        waiting_logged = True
                    time.sleep(1)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


UV_VERSION = "0.12.19"
UV_URL = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-pc-windows-msvc.zip"
UV_SIZE = 17_955_780
UV_SHA256 = "6dbb02d79e419522f1c500f0adb1cddcff0cda7d59b0d66ea7f5e3b4a1b2f5f0"

MSST_VERSION = "0.1.0"
FFMPEG_WINGET_ID = "Gyan.FFmpeg"
FFMPEG_WINGET_VERSION = "8.1.2"
MEGA53_CONFIG_URL = "https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/download/v1.0.21/mvsep_mega_model_bs_roformer_53_stems.yaml"
MEGA53_CONFIG_SIZE = 4_184
MEGA53_CONFIG_SHA256 = "7e198062a251587088adb91215a4f44ab59e67bd62fcc805cf54d6e7dfc51103"
MEGA53_CHECKPOINT_URL = "https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/download/v1.0.21/mvsep_mega_model_bs_roformer_53_stems_v1.ckpt"
MEGA53_CHECKPOINT_SIZE = 1_368_919_887
MEGA53_CHECKPOINT_SHA256 = "c62820893bbf86d4e734f966bd142d9157cfc8bb8e79e9d8f9ea553f3ff3519f"


@dataclass(frozen=True)
class GpuRuntimeProfile:
    profile_id: str
    torch: str
    torchvision: str
    torchaudio: str
    index_url: str


RTX40_PROFILE = GpuRuntimeProfile(
    "rtx40-cu126", "2.11.0", "0.26.0", "2.11.0", "https://download.pytorch.org/whl/cu126"
)
RTX50_PROFILE = GpuRuntimeProfile(
    "rtx50-cu128", "2.8.0", "0.23.0", "2.8.0", "https://download.pytorch.org/whl/cu128"
)


def profile_for_compute_capability(value: str) -> GpuRuntimeProfile:
    normalized = value.strip()
    if normalized == "8.9":
        return RTX40_PROFILE
    if normalized == "12.0":
        return RTX50_PROFILE
    raise RuntimeError(f"一键修复暂不支持 Compute Capability {normalized or '未知'}。")


def is_repairable_runtime_error(error: Exception | str) -> bool:
    message = str(error)
    return any(
        marker in message
        for marker in (
            "本地运行环境不完整",
            "MSST 文件不存在",
            "MSST 运行环境",
            "MSST Python 不存在",
            "Basic Pitch 本地运行时未安装",
            "TransKun V2 本地运行时未安装",
            "未找到必需程序：ffmpeg",
            "未找到必需程序：ffprobe",
            "ffmpeg / ffprobe",
        )
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class RuntimeRepairer:
    def __init__(self, runtime: LocalRuntime) -> None:
        self.runtime = runtime
        self.tools_root = runtime.msst_root.parent / ".repair-tools"

    def repair(self, log: LogCallback) -> None:
        self._ensure_ffmpeg(log)
        profile = self._detect_profile(log)
        uv = self._ensure_uv(log)
        python = self._ensure_msst(uv, profile, log)
        self._ensure_public_model(log)
        self._ensure_transcription(uv, python, log)
        self.runtime.activate()
        self.runtime.validate_core()
        log("一键修复完成：MSST、MVSep Mega 53-stem、Basic Pitch 与 TransKun 已通过检查。")

    def _ensure_ffmpeg(self, log: LogCallback) -> None:
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        links = local_app_data / "Microsoft" / "WinGet" / "Links"
        if links.is_dir():
            entries = os.environ.get("PATH", "").split(os.pathsep)
            if str(links).casefold() not in {entry.casefold() for entry in entries}:
                os.environ["PATH"] = str(links) + os.pathsep + os.environ.get("PATH", "")
        if shutil.which("ffmpeg") and shutil.which("ffprobe"):
            log(f"FFmpeg 已就绪：{shutil.which('ffmpeg')}")
            return
        winget = shutil.which("winget")
        if not winget:
            windows_apps = local_app_data / "Microsoft" / "WindowsApps" / "winget.exe"
            if windows_apps.is_file():
                winget = str(windows_apps)
        if not winget:
            raise RuntimeError(
                "缺少 ffmpeg/ffprobe，且系统未找到 Windows Package Manager (winget)。"
                "请先从 Microsoft Store 安装/更新“应用安装程序”，然后再次点击一键修复。"
            )
        log(
            f"未找到 FFmpeg，正在通过 Windows Package Manager 官方索引安装 "
            f"{FFMPEG_WINGET_ID} {FFMPEG_WINGET_VERSION}。"
        )
        self._run(
            Path(winget),
            [
                "install", "--id", FFMPEG_WINGET_ID,
                "--version", FFMPEG_WINGET_VERSION,
                "--exact", "--source", "winget",
                "--accept-package-agreements", "--accept-source-agreements",
                "--silent", "--disable-interactivity",
            ],
            log,
        )
        if links.is_dir():
            os.environ["PATH"] = str(links) + os.pathsep + os.environ.get("PATH", "")
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("WinGet 已完成 FFmpeg 安装，但当前进程仍找不到 ffmpeg/ffprobe。请重新启动 StemScore 后重试。")

    def _detect_profile(self, log: LogCallback) -> GpuRuntimeProfile:
        nvidia_smi = shutil.which("nvidia-smi")
        if not nvidia_smi:
            raise RuntimeError("未找到 nvidia-smi；一键修复需要受支持的 NVIDIA RTX 40/50 显卡。")
        result = subprocess.run(
            [nvidia_smi, "--query-gpu=name,compute_cap", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        for row in rows:
            name, _, capability = row.rpartition(",")
            try:
                profile = profile_for_compute_capability(capability)
            except RuntimeError:
                continue
            log(f"检测到 {name.strip()} / Compute Capability {capability.strip()}，选择 {profile.profile_id}。")
            return profile
        raise RuntimeError("未检测到一键修复支持的 RTX 40 (8.9) 或 RTX 50 (12.0) 显卡。")

    def _ensure_uv(self, log: LogCallback) -> Path:
        system_uv = shutil.which("uv")
        if system_uv:
            log(f"使用现有 uv：{system_uv}")
            return Path(system_uv)
        archive = self.tools_root / f"uv-{UV_VERSION}.zip"
        self._download_verified(UV_URL, archive, UV_SIZE, UV_SHA256, log)
        destination = self.tools_root / f"uv-{UV_VERSION}"
        executable = destination / "uv.exe"
        if not executable.is_file():
            destination.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as package:
                member = next((item for item in package.infolist() if Path(item.filename).name == "uv.exe"), None)
                if member is None:
                    raise RuntimeError("官方 uv 压缩包中未找到 uv.exe。")
                with package.open(member) as source, executable.open("wb") as target:
                    shutil.copyfileobj(source, target)
        log(f"uv 已就绪：{executable}")
        return executable

    def _ensure_msst(self, uv: Path, profile: GpuRuntimeProfile, log: LogCallback) -> Path:
        environment = self.runtime.msst_root / "env"
        python = self.runtime.msst_python
        self.runtime.msst_root.mkdir(parents=True, exist_ok=True)
        if not python.is_file():
            self._run(uv, ["venv", "--python", "3.10", str(environment)], log)
            python = environment / "Scripts" / "python.exe"
        self._run(
            uv,
            [
                "pip", "install", "--python", str(python),
                f"torch=={profile.torch}", f"torchvision=={profile.torchvision}",
                f"torchaudio=={profile.torchaudio}", "--index-url", profile.index_url,
            ],
            log,
        )
        self._run(
            uv,
            ["pip", "install", "--python", str(python), f"msst[bs-roformer]=={MSST_VERSION}"],
            log,
        )
        self._run(
            python,
            [
                "-c",
                (
                    "import torch,msst; "
                    f"assert torch.__version__.split('+')[0]=='{profile.torch}', torch.__version__; "
                    "assert torch.cuda.is_available(), 'CUDA unavailable'; "
                    "print('MSST/CUDA OK', torch.__version__, torch.cuda.get_device_name(0))"
                ),
            ],
            log,
        )
        if not self.runtime.msst_cli.is_file():
            raise RuntimeError(f"MSST CLI 安装后仍不存在：{self.runtime.msst_cli}")
        return python

    def _ensure_public_model(self, log: LogCallback) -> None:
        config = self.runtime.public_mega53_config
        checkpoint = self.runtime.public_mega53_checkpoint
        self._download_verified(MEGA53_CONFIG_URL, config, MEGA53_CONFIG_SIZE, MEGA53_CONFIG_SHA256, log)
        self._download_verified(
            MEGA53_CHECKPOINT_URL,
            checkpoint,
            MEGA53_CHECKPOINT_SIZE,
            MEGA53_CHECKPOINT_SHA256,
            log,
        )

    def _ensure_transcription(self, uv: Path, msst_python: Path, log: LogCallback) -> None:
        environment = self.runtime.transcription_root / ".venv"
        basic_python = environment / "Scripts" / "python.exe"
        self.runtime.transcription_root.mkdir(parents=True, exist_ok=True)
        if not basic_python.is_file():
            self._run(uv, ["venv", "--python", "3.11", str(environment)], log)
        self._run(
            uv,
            [
                "pip", "install", "--python", str(basic_python),
                "basic-pitch==0.4.0", "onnxruntime==1.23.2", "setuptools==80.9.0",
            ],
            log,
        )
        if not self.runtime.basic_pitch_model.is_file():
            raise RuntimeError("Basic Pitch 安装后 ONNX 模型仍缺失。")
        self.runtime.transkun_packages.mkdir(parents=True, exist_ok=True)
        self._run(
            uv,
            [
                "pip", "install", "--python", str(msst_python), "--target",
                str(self.runtime.transkun_packages), "--link-mode", "copy", "--no-deps",
                "transkun==2.0.1", "moduleconf==0.1.4", "pretty-midi==0.2.11.post0",
                "pydub==0.25.1", "soxr==1.0.0", "sox==1.5.0", "mido==1.3.3",
                "importlib-resources==6.5.2",
            ],
            log,
        )
        if not self.runtime.transkun_weight.is_file() or not self.runtime.transkun_config.is_file():
            raise RuntimeError("TransKun V2 安装后模型文件仍缺失。")

    @staticmethod
    def _run(executable: Path, arguments: list[str], log: LogCallback) -> None:
        command = [str(executable), *arguments]
        log("$ " + subprocess.list2cmdline(command))
        environment = os.environ.copy()
        environment.pop("PYTHONHOME", None)
        environment.pop("PYTHONPATH", None)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log(line.rstrip())
        code = process.wait()
        if code:
            raise RuntimeError(f"运行环境安装命令失败（退出码 {code}）：{executable.name}")

    @staticmethod
    def _download_verified(
        url: str,
        destination: Path,
        expected_size: int,
        expected_sha256: str,
        log: LogCallback,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        with _exclusive_download_lock(part, log):
            if destination.is_file():
                if destination.stat().st_size == expected_size and _sha256(destination) == expected_sha256:
                    log(f"已存在并通过 SHA-256：{destination.name}")
                    return
                log(f"现有文件大小或 SHA-256 不匹配，重新获取：{destination.name}")
                destination.unlink(missing_ok=True)

            def normalize_partial() -> bool:
                """Return True when the completed file is ready and installed."""
                if not part.is_file():
                    return False
                size = part.stat().st_size
                if size > expected_size:
                    log(
                        f"发现异常断点文件：{size / 1024 / 1024:.1f} MiB > "
                        f"{expected_size / 1024 / 1024:.1f} MiB；自动删除并从头重下。"
                    )
                    part.unlink(missing_ok=True)
                    return False
                if size == expected_size:
                    actual_hash = _sha256(part)
                    if actual_hash == expected_sha256:
                        os.replace(part, destination)
                        log(f"断点文件已完整并通过 SHA-256：{destination.name}")
                        return True
                    log(f"完整断点文件 SHA-256 不匹配，自动删除并从头重下：{destination.name}")
                    part.unlink(missing_ok=True)
                return False

            if normalize_partial():
                return

            for attempt in range(5):
                if normalize_partial():
                    return
                offset = part.stat().st_size if part.exists() else 0
                request = Request(url, headers={"User-Agent": "StemScore-runtime-repair/1.1"})
                if offset:
                    request.add_header("Range", f"bytes={offset}-")
                log(
                    f"下载 {destination.name}：{offset / 1024 / 1024:.1f} / "
                    f"{expected_size / 1024 / 1024:.1f} MiB"
                )
                started = time.monotonic()
                session_start = offset
                last_report = offset
                try:
                    with urlopen(request, timeout=60) as response:
                        status = response.getcode()
                        mode = "wb"
                        if offset and status == 206:
                            content_range = response.headers.get("Content-Range", "")
                            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                            if (
                                match is None
                                or int(match.group(1)) != offset
                                or match.group(3) == "*"
                                or int(match.group(3)) != expected_size
                            ):
                                part.unlink(missing_ok=True)
                                raise _RestartDownload(
                                    f"服务器返回了不一致的 Content-Range：{content_range or 'missing'}"
                                )
                            mode = "ab"
                        elif offset and status == 200:
                            log("服务器未接受断点 Range，将安全地从 0 重新下载，不追加旧数据。")
                            offset = 0
                            session_start = 0
                            mode = "wb"
                        elif not offset and status in {200, 206}:
                            if status == 206:
                                content_range = response.headers.get("Content-Range", "")
                                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                                if (
                                    match is None
                                    or int(match.group(1)) != 0
                                    or match.group(3) == "*"
                                    or int(match.group(3)) != expected_size
                                ):
                                    raise _RestartDownload(
                                        f"服务器返回了不一致的 Content-Range：{content_range or 'missing'}"
                                    )
                        else:
                            raise RuntimeError(f"服务器返回异常 HTTP 状态：{status}")

                        with part.open(mode) as handle:
                            downloaded = offset
                            while downloaded < expected_size:
                                block = response.read(min(1024 * 1024, expected_size - downloaded))
                                if not block:
                                    raise RuntimeError(
                                        f"连接提前结束：{downloaded} / {expected_size} bytes"
                                    )
                                handle.write(block)
                                downloaded += len(block)
                                if downloaded - last_report >= 64 * 1024 * 1024 or downloaded == expected_size:
                                    elapsed = max(time.monotonic() - started, 0.001)
                                    speed = (downloaded - session_start) / elapsed / 1024 / 1024
                                    log(
                                        f"下载 {destination.name}：{downloaded / 1024 / 1024:.1f} MiB / "
                                        f"{expected_size / 1024 / 1024:.1f} MiB，{speed:.1f} MiB/s"
                                    )
                                    last_report = downloaded
                except HTTPError as error:
                    if error.code == 416:
                        current = part.stat().st_size if part.exists() else 0
                        log(
                            f"服务器拒绝当前断点（HTTP 416，本地 {current / 1024 / 1024:.1f} MiB）；"
                            "自动丢弃无效断点并从 0 重新开始。"
                        )
                        part.unlink(missing_ok=True)
                    elif attempt >= 4:
                        raise RuntimeError(f"下载失败：{destination.name}：{error}") from error
                    else:
                        log(f"下载连接中断，2 秒后继续（{attempt + 1}/4）：{error}")
                except _RestartDownload as error:
                    log(f"断点响应无效，自动重置下载：{error}")
                    part.unlink(missing_ok=True)
                except Exception as error:
                    if attempt >= 4:
                        raise RuntimeError(f"下载失败：{destination.name}：{error}") from error
                    log(f"下载连接中断，2 秒后从安全断点继续（{attempt + 1}/4）：{error}")

                if normalize_partial():
                    return
                if attempt < 4:
                    time.sleep(2)

            actual_size = part.stat().st_size if part.exists() else 0
            raise RuntimeError(
                f"下载在多次重试后仍未完成：{destination.name} "
                f"({actual_size} / {expected_size} bytes)"
            )
