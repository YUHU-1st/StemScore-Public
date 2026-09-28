from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
from typing import Iterator

import pytest

from stemscore.model_manager import (
    ChecksumMismatchError,
    DownloadProgress,
    ModelManager,
    load_catalog,
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextmanager
def local_server(
    data: bytes,
    *,
    failures: int = 0,
    chunk_delay: float = 0.0,
) -> Iterator[tuple[str, dict[str, object]]]:
    state: dict[str, object] = {"ranges": [], "failures_left": failures}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            range_header = self.headers.get("Range")
            ranges = state["ranges"]
            assert isinstance(ranges, list)
            ranges.append(range_header)
            start = 0
            if range_header:
                assert range_header.startswith("bytes=") and range_header.endswith("-")
                start = int(range_header[6:-1])
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return

            body = data[start:]
            self.send_response(206 if range_header else 200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(body)))
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.end_headers()

            failures_left = state["failures_left"]
            assert isinstance(failures_left, int)
            if failures_left:
                state["failures_left"] = failures_left - 1
                partial = body[: max(1, len(body) // 3)]
                self.wfile.write(partial)
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return

            try:
                for offset in range(0, len(body), 32 * 1024):
                    self.wfile.write(body[offset : offset + 32 * 1024])
                    self.wfile.flush()
                    if chunk_delay:
                        time.sleep(chunk_delay)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/model.bin", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def write_catalog(path: Path, url: str, data: bytes, checksum: str | None = None) -> Path:
    payload = {
        "schema_version": 1,
        "storage_relative_path": "models",
        "tiers": [
            {
                "vram_gb": 4,
                "recommended_model_ids": ["test-model"],
            }
        ],
        "models": [
            {
                "id": "test-model",
                "family": "test",
                "display_name": "Test model",
                "optional": False,
                "source_url": "https://example.com/models/test-model",
                "license": "Apache-2.0",
                "license_url": "https://example.com/licenses/apache-2.0",
                "files": [
                    {
                        "path": "weights/model.bin",
                        "url": url,
                        "size": len(data),
                        "sha256": checksum or sha256(data),
                    }
                ],
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_bundled_catalog_has_required_tiers_and_official_metadata(tmp_path: Path) -> None:
    catalog = load_catalog()
    assert [tier["vram_gb"] for tier in catalog["tiers"]] == [4, 8, 16, 24]
    models = {model["id"]: model for model in catalog["models"]}
    assert models["clap-htsat-fused"]["optional"] is False
    runner = models["llama.cpp-b10887-win-cpu-x64"]
    assert runner["source_url"] == "https://github.com/ggml-org/llama.cpp/releases/tag/b10887"
    assert runner["license"] == "MIT"
    assert runner["runner"]["executable_path"] == "runtime/llama-server.exe"
    assert runner["files"][0]["sha256"] == "e650024739c5d627ddc7a5038439534a9ac4d49fa6a6cc74f2025d33db3f6a1a"
    hub_models = [model for model in models.values() if model["family"] != "llama.cpp"]
    assert all(model["source_url"].startswith("https://huggingface.co/") for model in hub_models)
    assert all(model["license"] == "Apache-2.0" for model in hub_models)

    manager = ModelManager(tmp_path)
    assert manager.models_root == tmp_path / "models"
    assert [model["family"] for model in manager.recommended_for_vram(4)] == [
        "CLAP",
        "Qwen",
        "llama.cpp",
    ]
    assert manager.recommended_for_vram(12)[1]["id"] == "qwen2.5-1.5b-instruct-q5-k-m"
    assert manager.recommended_for_vram(24)[1]["id"] == "qwen2.5-14b-instruct-q4-k-m"


def test_download_resumes_part_file_and_reports_progress_and_speed(tmp_path: Path) -> None:
    data = bytes(range(256)) * 4096
    with local_server(data) as (url, state):
        catalog_path = write_catalog(tmp_path / "catalog.json", url, data)
        manager = ModelManager(tmp_path / "app", catalog_path, retry_delay_seconds=0)
        part = manager.model_path("test-model") / "weights" / "model.bin.part"
        part.parent.mkdir(parents=True)
        prefix_size = 123_457
        part.write_bytes(data[:prefix_size])
        progress: list[DownloadProgress] = []
        speeds: list[float] = []

        result = manager.download("test-model", progress.append, speeds.append)

    target = result / "weights" / "model.bin"
    assert target.read_bytes() == data
    assert not part.exists()
    assert state["ranges"][0] == f"bytes={prefix_size}-"
    assert progress[-1].phase == "installed"
    assert progress[-1].percent == pytest.approx(100.0)
    assert any(speed > 0 for speed in speeds)
    assert manager.installation_status("test-model") == "installed"

    target.write_bytes(b"x" * len(data))
    assert manager.installation_status("test-model") == "corrupt"


def test_temporary_disconnect_retries_from_new_part_size(tmp_path: Path) -> None:
    data = b"retry-range-" * 100_000
    with local_server(data, failures=1) as (url, state):
        manager = ModelManager(
            tmp_path / "app",
            write_catalog(tmp_path / "catalog.json", url, data),
            max_retries=2,
            retry_delay_seconds=0,
        )
        result = manager.download("test-model")

    assert (result / "weights" / "model.bin").read_bytes() == data
    ranges = state["ranges"]
    assert ranges[0] is None
    assert any(value and value != "bytes=0-" for value in ranges[1:])


def test_complete_part_is_verified_without_another_request(tmp_path: Path) -> None:
    data = b"already-complete" * 10_000
    with local_server(data) as (url, state):
        manager = ModelManager(
            tmp_path / "app",
            write_catalog(tmp_path / "catalog.json", url, data),
            retry_delay_seconds=0,
        )
        part = manager.model_path("test-model") / "weights" / "model.bin.part"
        part.parent.mkdir(parents=True)
        part.write_bytes(data)
        result = manager.download("test-model")

    assert state["ranges"] == []
    assert (result / "weights" / "model.bin").read_bytes() == data
    assert not part.exists()


def test_pause_and_resume_blocks_an_active_download_without_losing_part(tmp_path: Path) -> None:
    data = b"pause-resume-" * 300_000
    with local_server(data, chunk_delay=0.001) as (url, _state):
        manager = ModelManager(
            tmp_path / "app",
            write_catalog(tmp_path / "catalog.json", url, data),
            retry_delay_seconds=0,
        )
        paused = threading.Event()
        errors: list[Exception] = []

        def progress(update: DownloadProgress) -> None:
            if update.phase == "downloading" and update.downloaded_bytes >= 1024 * 1024 and not paused.is_set():
                manager.pause()
                paused.set()

        def work() -> None:
            try:
                manager.download("test-model", progress)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=work)
        thread.start()
        assert paused.wait(timeout=5)
        part = manager.model_path("test-model") / "weights" / "model.bin.part"
        size_while_paused = part.stat().st_size
        time.sleep(0.05)
        assert thread.is_alive()
        assert part.stat().st_size == size_while_paused
        assert manager.paused is True
        manager.resume()
        thread.join(timeout=10)

    assert not errors
    assert not thread.is_alive()
    assert manager.paused is False
    assert manager.is_installed("test-model")


def test_checksum_mismatch_removes_invalid_part(tmp_path: Path) -> None:
    data = b"checksum" * 20_000
    with local_server(data) as (url, _state):
        manager = ModelManager(
            tmp_path / "app",
            write_catalog(tmp_path / "catalog.json", url, data, "0" * 64),
            retry_delay_seconds=0,
        )
        with pytest.raises(ChecksumMismatchError, match="SHA-256 mismatch"):
            manager.download("test-model")

    model_root = manager.model_path("test-model")
    assert not (model_root / "weights" / "model.bin").exists()
    assert not (model_root / "weights" / "model.bin.part").exists()
    assert manager.installation_status("test-model") == "not_installed"


@pytest.mark.parametrize(
    ("file_path", "url"),
    [
        ("../escape.bin", "https://example.com/model.bin"),
        ("model.bin", "http://example.com/model.bin"),
    ],
)
def test_catalog_rejects_unsafe_paths_and_urls(tmp_path: Path, file_path: str, url: str) -> None:
    data = b"catalog-boundary"
    path = write_catalog(tmp_path / "catalog.json", "http://127.0.0.1/model.bin", data)
    payload = json.loads(path.read_text(encoding="utf-8"))
    modified = copy.deepcopy(payload)
    modified["models"][0]["files"][0]["path"] = file_path
    modified["models"][0]["files"][0]["url"] = url
    path.write_text(json.dumps(modified), encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalog(path)
