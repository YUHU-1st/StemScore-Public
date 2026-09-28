from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from stemscore.local_llm import load_local_llm_settings
from stemscore.model_manager import DownloadProgress
from stemscore.model_server import ModelServerState, start_background


class FakeManager:
    def __init__(self, root: Path) -> None:
        self.models_root = root / "models"
        self.catalog = {
            "schema_version": 1,
            "storage_relative_path": "models",
            "tiers": [
                {
                    "vram_gb": 4,
                    "recommended_model_ids": ["clap", "qwen-small"],
                }
            ],
            "models": [
                {
                    "id": "clap",
                    "family": "CLAP",
                    "display_name": "CLAP",
                    "description": "Audio embeddings",
                    "optional": False,
                    "source_url": "https://example.com/clap",
                    "license": "Apache-2.0",
                    "license_url": "https://example.com/license",
                    "files": [{"path": "model.bin", "size": 100}],
                },
                {
                    "id": "qwen-small",
                    "family": "Qwen",
                    "display_name": "Qwen Small",
                    "description": "Local assistant",
                    "optional": True,
                    "source_url": "https://example.com/qwen",
                    "license": "Apache-2.0",
                    "license_url": "https://example.com/license",
                    "files": [{"path": "model.gguf", "size": 100}],
                },
            ],
        }
        self.statuses = {"clap": "not_installed", "qwen-small": "not_installed"}
        self.resume_event = threading.Event()
        self.resume_event.set()
        self.started = threading.Event()

    def installation_status(self, model_id: str) -> str:
        return self.statuses[model_id]

    def model_path(self, model_id: str) -> Path:
        return self.models_root / model_id

    def pause(self) -> None:
        self.resume_event.clear()

    def resume(self) -> None:
        self.resume_event.set()

    def download(self, model_id: str, progress_callback=None, speed_callback=None) -> Path:
        self.started.set()
        target = self.models_root / model_id
        target.mkdir(parents=True, exist_ok=True)
        for step in range(1, 21):
            self.resume_event.wait()
            time.sleep(0.01)
            if progress_callback:
                progress_callback(
                    DownloadProgress(
                        model_id=model_id,
                        filename="model.bin",
                        downloaded_bytes=step * 5,
                        total_bytes=100,
                        percent=step * 5.0,
                        speed_bytes_per_second=2048.0,
                        phase="downloading" if step < 20 else "installed",
                    )
                )
        self.statuses[model_id] = "installed"
        return target


@contextmanager
def running_server(manager: FakeManager) -> Iterator[str]:
    server, url = start_background(manager)  # type: ignore[arg-type]
    try:
        yield url.rstrip("/")
    finally:
        manager.resume()
        server.shutdown()
        server.server_close()


def get(url: str, path: str) -> tuple[int, bytes, str]:
    with urlopen(url + path, timeout=5) as response:
        return response.status, response.read(), response.headers.get_content_type()


def get_json(url: str, path: str) -> tuple[int, dict]:
    status, body, _content_type = get(url, path)
    return status, json.loads(body)


def post_json(url: str, path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url + path,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def wait_for(url: str, predicate, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _status, payload = get_json(url, "/api/status")
        if predicate(payload):
            return payload
        time.sleep(0.02)
    raise AssertionError("server state did not reach the expected condition")


def test_catalog_status_and_html_endpoints(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)
    with running_server(manager) as url:
        status, body, content_type = get(url, "/")
        assert status == 200
        assert content_type == "text/html"
        assert "本地模型管理" in body.decode("utf-8")

        status, catalog = get_json(url, "/api/catalog")
        assert status == 200
        assert catalog["tiers"][0]["vram_gb"] == 4
        assert catalog["installation_status"] == {
            "clap": "not_installed",
            "qwen-small": "not_installed",
        }

        status, snapshot = get_json(url, "/api/status")
        assert status == 200
        assert snapshot["busy"] is False
        assert snapshot["download"]["phase"] == "idle"


def test_single_download_pause_resume_and_progress(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)
    with running_server(manager) as url:
        status, result = post_json(url, "/api/download", {"model_id": "clap"})
        assert status == 202
        assert result["accepted"] is True
        assert manager.started.wait(timeout=2)

        status, conflict = post_json(url, "/api/download", {"model_id": "qwen-small"})
        assert status == 409
        assert "一次只能下载一个" in conflict["error"]

        before = wait_for(
            url,
            lambda payload: payload["download"]["phase"] == "downloading"
            and payload["download"]["percent"] > 0,
        )
        status, paused = post_json(url, "/api/pause", {})
        assert status == 200
        assert paused["status"]["download"]["state"] == "paused"
        paused_percent = before["download"]["percent"]
        time.sleep(0.05)
        _status, still_paused = get_json(url, "/api/status")
        assert still_paused["download"]["state"] == "paused"
        assert still_paused["download"]["speed_bytes_per_second"] == 0.0
        assert still_paused["download"]["percent"] >= paused_percent

        status, resumed = post_json(url, "/api/resume", {})
        assert status == 200
        assert resumed["status"]["download"]["state"] == "downloading"
        complete = wait_for(url, lambda payload: payload["download"]["state"] == "complete")
        assert complete["models"]["clap"] == "installed"
        assert complete["download"]["percent"] == 100.0
        assert complete["download"]["phase"] == "installed"


def test_select_requires_installed_models_and_saves_atomically(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)
    manager.statuses["clap"] = "installed"
    with running_server(manager) as url:
        status, unavailable = post_json(
            url,
            "/api/select",
            {"model_ids": ["clap", "qwen-small"]},
        )
        assert status == 409
        assert "qwen-small" in unavailable["error"]

        status, selected = post_json(url, "/api/select", {"model_ids": ["clap", "clap"]})
        assert status == 200
        assert selected["selection"]["selected_model_ids"] == ["clap"]

    selection_path = manager.models_root / "selection.json"
    assert json.loads(selection_path.read_text(encoding="utf-8"))["selected_model_ids"] == ["clap"]
    assert not selection_path.with_name("selection.json.tmp").exists()

    restored = ModelServerState(manager)  # type: ignore[arg-type]
    assert restored.snapshot()["selection"]["selected_model_ids"] == ["clap"]


def test_select_qwen_writes_usable_loopback_runner_configuration(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)
    runner_id = "llama-runner"
    manager.catalog["models"].append(
        {
            "id": runner_id,
            "family": "llama.cpp",
            "display_name": "llama.cpp runner",
            "description": "Local runner",
            "optional": False,
            "source_url": "https://example.com/runner",
            "license": "MIT",
            "license_url": "https://example.com/license",
            "runner": {
                "archive_path": "runner.zip",
                "executable_path": "runtime/llama-server.exe",
            },
            "files": [{"path": "runner.zip", "size": 100}],
        }
    )
    manager.statuses.update({"qwen-small": "installed", runner_id: "installed"})
    qwen_file = manager.model_path("qwen-small") / "model.gguf"
    qwen_file.parent.mkdir(parents=True)
    qwen_file.write_bytes(b"gguf")
    runner_archive = manager.model_path(runner_id) / "runner.zip"
    runner_archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(runner_archive, "w") as package:
        package.writestr("llama-server.exe", b"runner")

    with running_server(manager) as url:
        status, selected = post_json(
            url,
            "/api/select",
            {
                "model_ids": ["qwen-small", runner_id],
                "endpoint": "http://127.0.0.1:9017/v1",
            },
        )
        assert status == 200
        local_llm = selected["selection"]["local_llm"]
        assert local_llm["enabled"] is True
        assert local_llm["endpoint"] == "http://127.0.0.1:9017/v1/chat/completions"
        assert local_llm["model"] == "qwen-small"
        assert local_llm["runner_executable"] == "llama-runner/runtime/llama-server.exe"
        assert local_llm["model_path"] == "qwen-small/model.gguf"

    settings = load_local_llm_settings(manager.models_root / "selection.json")
    assert settings.runner_executable == manager.model_path(runner_id) / "runtime" / "llama-server.exe"
    assert settings.model_path == qwen_file

    state = ModelServerState(manager)  # type: ignore[arg-type]
    try:
        state.select(
            ["qwen-small", runner_id],
            "https://api.openai.com/v1",
        )
    except ValueError as error:
        assert "回环" in str(error)
    else:
        raise AssertionError("cloud endpoint should be rejected")


def test_post_boundary_validation_and_unknown_model(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)
    with running_server(manager) as url:
        request = Request(url + "/api/download", data=b"{}", method="POST")
        try:
            urlopen(request, timeout=5)
        except HTTPError as error:
            assert error.code == 400
            assert "Content-Type" in json.loads(error.read())["error"]
        else:
            raise AssertionError("request without application/json should fail")

        status, payload = post_json(url, "/api/download", {"model_id": "unknown"})
        assert status == 400
        assert "未知模型" in payload["error"]
