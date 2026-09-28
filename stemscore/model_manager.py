from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


CHUNK_SIZE = 1024 * 1024
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}
MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class DownloadError(RuntimeError):
    """A model file could not be downloaded."""


class ChecksumMismatchError(DownloadError):
    """A complete download did not match its catalog SHA-256."""


class _TransientDownloadError(OSError):
    pass


@dataclass(frozen=True)
class DownloadProgress:
    model_id: str
    filename: str
    downloaded_bytes: int
    total_bytes: int
    percent: float
    speed_bytes_per_second: float
    phase: str


ProgressCallback = Callable[[DownloadProgress], None]
SpeedCallback = Callable[[float], None]


def software_install_directory() -> Path:
    if getattr(sys, "frozen", False):
        application_directory = Path(sys.executable).resolve().parent
        deployment_root = application_directory.parent
        if (
            application_directory.name.casefold() == "app"
            and (deployment_root / "deployment-state.json").is_file()
        ):
            return deployment_root
        return application_directory
    return Path(__file__).resolve().parents[1]


def default_models_root() -> Path:
    return software_install_directory() / "models"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_url(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a URL string")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field} must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{field} must not contain credentials or a fragment")
    if parsed.scheme == "http":
        hostname = parsed.hostname.lower()
        loopback = hostname == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
        if not loopback:
            raise ValueError(f"{field} must use HTTPS unless it targets loopback")
    return value


def _validate_relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty relative path")
    if "\\" in value:
        raise ValueError(f"{field} must use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field} must stay inside its model directory")
    return value


def load_catalog(path: Path | None = None) -> dict[str, object]:
    catalog_path = path or Path(__file__).with_name("model_catalog.json")
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("model catalog schema_version must be 1")
    if payload.get("storage_relative_path") != "models":
        raise ValueError("model catalog storage_relative_path must be models")
    models = payload.get("models")
    tiers = payload.get("tiers")
    if not isinstance(models, list) or not isinstance(tiers, list):
        raise ValueError("model catalog must contain models and tiers arrays")

    model_ids: set[str] = set()
    for model_index, model in enumerate(models):
        field = f"models[{model_index}]"
        if not isinstance(model, dict):
            raise ValueError(f"{field} must be an object")
        model_id = model.get("id")
        if not isinstance(model_id, str) or MODEL_ID_PATTERN.fullmatch(model_id) is None:
            raise ValueError(f"{field}.id is invalid")
        if model_id in model_ids:
            raise ValueError(f"duplicate model id: {model_id}")
        model_ids.add(model_id)
        if not isinstance(model.get("optional"), bool):
            raise ValueError(f"{field}.optional must be boolean")
        _validate_url(model.get("source_url"), f"{field}.source_url")
        _validate_url(model.get("license_url"), f"{field}.license_url")
        if not isinstance(model.get("license"), str) or not model["license"]:
            raise ValueError(f"{field}.license is required")
        files = model.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError(f"{field}.files must be a non-empty array")
        file_paths: set[str] = set()
        for file_index, artifact in enumerate(files):
            artifact_field = f"{field}.files[{file_index}]"
            if not isinstance(artifact, dict):
                raise ValueError(f"{artifact_field} must be an object")
            relative_path = _validate_relative_path(artifact.get("path"), f"{artifact_field}.path")
            if relative_path in file_paths:
                raise ValueError(f"duplicate file path in {model_id}: {relative_path}")
            file_paths.add(relative_path)
            _validate_url(artifact.get("url"), f"{artifact_field}.url")
            size = artifact.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                raise ValueError(f"{artifact_field}.size must be a positive integer")
            checksum = artifact.get("sha256")
            if not isinstance(checksum, str) or SHA256_PATTERN.fullmatch(checksum.lower()) is None:
                raise ValueError(f"{artifact_field}.sha256 must be 64 hexadecimal characters")

    tier_sizes: set[int] = set()
    for tier_index, tier in enumerate(tiers):
        field = f"tiers[{tier_index}]"
        if not isinstance(tier, dict):
            raise ValueError(f"{field} must be an object")
        vram_gb = tier.get("vram_gb")
        if not isinstance(vram_gb, int) or isinstance(vram_gb, bool) or vram_gb <= 0:
            raise ValueError(f"{field}.vram_gb must be a positive integer")
        if vram_gb in tier_sizes:
            raise ValueError(f"duplicate VRAM tier: {vram_gb}")
        tier_sizes.add(vram_gb)
        recommended = tier.get("recommended_model_ids")
        if not isinstance(recommended, list) or not recommended:
            raise ValueError(f"{field}.recommended_model_ids must be a non-empty array")
        unknown = [model_id for model_id in recommended if model_id not in model_ids]
        if unknown:
            raise ValueError(f"{field} references unknown models: {', '.join(unknown)}")
    return payload


class ModelManager:
    def __init__(
        self,
        install_directory: Path | None = None,
        catalog_path: Path | None = None,
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        install_root = (install_directory or software_install_directory()).resolve()
        if install_root.exists() and not install_root.is_dir():
            raise ValueError("install_directory must be a directory")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0 or retry_delay_seconds < 0:
            raise ValueError("retry settings must not be negative")
        self.install_directory = install_root
        self.models_root = install_root / "models"
        self.catalog = load_catalog(catalog_path)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._models = {str(model["id"]): model for model in self.catalog["models"]}  # type: ignore[index]

    def pause(self) -> None:
        self._resume_event.clear()

    def resume(self) -> None:
        self._resume_event.set()

    @property
    def paused(self) -> bool:
        return not self._resume_event.is_set()

    def recommended_for_vram(self, vram_gb: int) -> list[dict[str, object]]:
        if vram_gb <= 0:
            raise ValueError("vram_gb must be positive")
        tiers = sorted(self.catalog["tiers"], key=lambda tier: int(tier["vram_gb"]))  # type: ignore[index]
        eligible = [tier for tier in tiers if int(tier["vram_gb"]) <= vram_gb]
        tier = eligible[-1] if eligible else tiers[0]
        return [self._models[model_id] for model_id in tier["recommended_model_ids"]]

    def model_path(self, model_id: str) -> Path:
        self._model(model_id)
        path = (self.models_root / model_id).resolve()
        if not path.is_relative_to(self.models_root.resolve()):
            raise ValueError("model path escaped the models directory")
        return path

    def installation_status(self, model_id: str) -> str:
        model = self._model(model_id)
        model_root = self.model_path(model_id)
        any_partial = False
        any_corrupt = False
        installed = 0
        for artifact in model["files"]:
            target = self._target_path(model_root, str(artifact["path"]))
            part = target.with_name(target.name + ".part")
            if target.is_file():
                if self._file_matches(target, artifact):
                    installed += 1
                else:
                    any_corrupt = True
            if part.exists():
                any_partial = True
        if installed == len(model["files"]):
            return "installed"
        if any_corrupt:
            return "corrupt"
        if any_partial or installed:
            return "partial"
        return "not_installed"

    def is_installed(self, model_id: str) -> bool:
        return self.installation_status(model_id) == "installed"

    def installed_model_ids(self) -> list[str]:
        return [model_id for model_id in self._models if self.is_installed(model_id)]

    def download(
        self,
        model_id: str,
        progress_callback: ProgressCallback | None = None,
        speed_callback: SpeedCallback | None = None,
    ) -> Path:
        model = self._model(model_id)
        model_root = self.model_path(model_id)
        model_root.mkdir(parents=True, exist_ok=True)
        total_bytes = sum(int(artifact["size"]) for artifact in model["files"])
        completed_bytes = 0

        for artifact in model["files"]:
            target = self._target_path(model_root, str(artifact["path"]))
            if target.is_file() and self._file_matches(target, artifact):
                completed_bytes += int(artifact["size"])
                self._notify(
                    model_id,
                    str(artifact["path"]),
                    completed_bytes,
                    total_bytes,
                    0.0,
                    "installed",
                    progress_callback,
                    speed_callback,
                )
                continue
            if target.exists():
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            self._download_file(
                model_id,
                artifact,
                target,
                completed_bytes,
                total_bytes,
                progress_callback,
                speed_callback,
            )
            completed_bytes += int(artifact["size"])
        return model_root

    def _model(self, model_id: str) -> dict[str, object]:
        if not isinstance(model_id, str) or MODEL_ID_PATTERN.fullmatch(model_id) is None:
            raise ValueError("invalid model id")
        try:
            return self._models[model_id]
        except KeyError as error:
            raise KeyError(f"unknown model: {model_id}") from error

    def _target_path(self, model_root: Path, relative_path: str) -> Path:
        _validate_relative_path(relative_path, "model file path")
        target = (model_root / Path(*PurePosixPath(relative_path).parts)).resolve()
        if not target.is_relative_to(model_root.resolve()):
            raise ValueError("model file escaped its model directory")
        return target

    @staticmethod
    def _file_matches(path: Path, artifact: dict[str, object]) -> bool:
        return path.stat().st_size == int(artifact["size"]) and sha256_file(path) == str(
            artifact["sha256"]
        ).lower()

    def _download_file(
        self,
        model_id: str,
        artifact: dict[str, object],
        target: Path,
        completed_bytes: int,
        total_bytes: int,
        progress_callback: ProgressCallback | None,
        speed_callback: SpeedCallback | None,
    ) -> None:
        part = target.with_name(target.name + ".part")
        expected_size = int(artifact["size"])
        if part.exists() and part.stat().st_size > expected_size:
            part.unlink()

        if not part.exists() or part.stat().st_size < expected_size:
            for attempt in range(self.max_retries + 1):
                try:
                    self._download_file_once(
                        model_id,
                        artifact,
                        part,
                        completed_bytes,
                        total_bytes,
                        progress_callback,
                        speed_callback,
                    )
                    break
                except Exception as error:
                    if not self._retryable(error) or attempt >= self.max_retries:
                        if isinstance(error, DownloadError):
                            raise
                        raise DownloadError(f"download failed for {artifact['path']}: {error}") from error
                    if self.retry_delay_seconds:
                        time.sleep(self.retry_delay_seconds * (2**attempt))

        actual_size = part.stat().st_size if part.exists() else 0
        if actual_size != expected_size:
            raise DownloadError(
                f"downloaded size mismatch for {artifact['path']}: {actual_size} != {expected_size}"
            )
        self._notify(
            model_id,
            str(artifact["path"]),
            completed_bytes + expected_size,
            total_bytes,
            0.0,
            "verifying",
            progress_callback,
            speed_callback,
        )
        actual_sha256 = sha256_file(part)
        expected_sha256 = str(artifact["sha256"]).lower()
        if actual_sha256 != expected_sha256:
            part.unlink()
            raise ChecksumMismatchError(
                f"SHA-256 mismatch for {artifact['path']}: {actual_sha256} != {expected_sha256}"
            )
        os.replace(part, target)
        self._notify(
            model_id,
            str(artifact["path"]),
            completed_bytes + expected_size,
            total_bytes,
            0.0,
            "installed",
            progress_callback,
            speed_callback,
        )

    def _download_file_once(
        self,
        model_id: str,
        artifact: dict[str, object],
        part: Path,
        completed_bytes: int,
        total_bytes: int,
        progress_callback: ProgressCallback | None,
        speed_callback: SpeedCallback | None,
    ) -> None:
        expected_size = int(artifact["size"])
        offset = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "StemScore/0.2 model-manager"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = Request(str(artifact["url"]), headers=headers)
        started_at = time.monotonic()
        session_bytes = 0

        with urlopen(request, timeout=self.timeout_seconds) as response:
            status = response.getcode()
            mode = "ab"
            if offset and status == 206:
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                if (
                    match is None
                    or int(match.group(1)) != offset
                    or match.group(3) == "*"
                    or int(match.group(3)) != expected_size
                ):
                    raise DownloadError(f"invalid Content-Range for {artifact['path']}: {content_range}")
            elif offset and status == 200:
                offset = 0
                mode = "wb"
            elif status not in {200, 206}:
                raise DownloadError(f"unexpected HTTP status {status} for {artifact['path']}")

            self._notify(
                model_id,
                str(artifact["path"]),
                completed_bytes + offset,
                total_bytes,
                0.0,
                "downloading",
                progress_callback,
                speed_callback,
            )
            with part.open(mode) as handle:
                while offset + session_bytes < expected_size:
                    self._resume_event.wait()
                    block = response.read(min(CHUNK_SIZE, expected_size - offset - session_bytes))
                    if not block:
                        raise _TransientDownloadError(
                            f"connection ended after {offset + session_bytes} of {expected_size} bytes"
                        )
                    handle.write(block)
                    session_bytes += len(block)
                    elapsed = max(time.monotonic() - started_at, 1e-9)
                    speed = session_bytes / elapsed
                    self._notify(
                        model_id,
                        str(artifact["path"]),
                        completed_bytes + offset + session_bytes,
                        total_bytes,
                        speed,
                        "downloading",
                        progress_callback,
                        speed_callback,
                    )

    @staticmethod
    def _retryable(error: Exception) -> bool:
        if isinstance(error, HTTPError):
            return error.code in RETRYABLE_HTTP_STATUS
        return isinstance(
            error,
            (
                URLError,
                TimeoutError,
                ConnectionError,
                http.client.IncompleteRead,
                _TransientDownloadError,
            ),
        )

    @staticmethod
    def _notify(
        model_id: str,
        filename: str,
        downloaded_bytes: int,
        total_bytes: int,
        speed: float,
        phase: str,
        progress_callback: ProgressCallback | None,
        speed_callback: SpeedCallback | None,
    ) -> None:
        if progress_callback:
            progress_callback(
                DownloadProgress(
                    model_id=model_id,
                    filename=filename,
                    downloaded_bytes=downloaded_bytes,
                    total_bytes=total_bytes,
                    percent=(downloaded_bytes / total_bytes) * 100.0,
                    speed_bytes_per_second=speed,
                    phase=phase,
                )
            )
        if speed_callback:
            speed_callback(speed)
