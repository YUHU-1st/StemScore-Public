from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import threading
from typing import Any
from urllib.parse import urlparse
import zipfile

from .local_llm import normalize_local_llm_endpoint
from .model_manager import DownloadProgress, ModelManager


MAX_REQUEST_BYTES = 64 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConflictError(RuntimeError):
    pass


class ModelServerState:
    def __init__(self, manager: ModelManager) -> None:
        self.manager = manager
        self.lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.manager.models_root.mkdir(parents=True, exist_ok=True)
        self.selection_path = self.manager.models_root / "selection.json"
        self.model_ids = [str(model["id"]) for model in self.manager.catalog["models"]]
        self.models = {
            str(model["id"]): model for model in self.manager.catalog["models"]
        }
        self.statuses = {
            model_id: self.manager.installation_status(model_id) for model_id in self.model_ids
        }
        for model_id in self.model_ids:
            if self.statuses[model_id] == "installed" and self._runner_config(model_id):
                try:
                    self._install_runner(model_id)
                except (OSError, ValueError, zipfile.BadZipFile):
                    self.statuses[model_id] = "setup_failed"
        self.selection = self._load_selection()
        self.download_state: dict[str, Any] = {
            "state": "idle",
            "model_id": None,
            "filename": None,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "percent": 0.0,
            "speed_bytes_per_second": 0.0,
            "phase": "idle",
            "error": None,
            "updated_at": utc_now(),
        }

    def catalog_snapshot(self) -> dict[str, Any]:
        with self.lock:
            payload = json.loads(json.dumps(self.manager.catalog, ensure_ascii=False))
            payload["installation_status"] = dict(self.statuses)
            payload["selection"] = dict(self.selection)
            return payload

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "models": dict(self.statuses),
                "selection": dict(self.selection),
                "download": dict(self.download_state),
                "busy": bool(self.worker and self.worker.is_alive()),
            }

    def start_download(self, model_id: str) -> bool:
        if not isinstance(model_id, str) or model_id not in self.statuses:
            raise ValueError("未知模型。")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ConflictError("已有模型正在下载；一次只能下载一个模型。")
            if self.statuses[model_id] == "installed":
                self.download_state = {
                    "state": "complete",
                    "model_id": model_id,
                    "filename": None,
                    "downloaded_bytes": 0,
                    "total_bytes": 0,
                    "percent": 100.0,
                    "speed_bytes_per_second": 0.0,
                    "phase": "installed",
                    "error": None,
                    "updated_at": utc_now(),
                }
                return False
            self.manager.resume()
            self.statuses[model_id] = "installing"
            self.download_state = {
                "state": "downloading",
                "model_id": model_id,
                "filename": None,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "percent": 0.0,
                "speed_bytes_per_second": 0.0,
                "phase": "starting",
                "error": None,
                "updated_at": utc_now(),
            }
            self.worker = threading.Thread(
                target=self._download_worker,
                args=(model_id,),
                daemon=True,
                name=f"stemscore-model-{model_id}",
            )
            self.worker.start()
            return True

    def pause(self) -> None:
        with self.lock:
            if not self.worker or not self.worker.is_alive():
                raise ConflictError("当前没有正在下载的模型。")
            if self.download_state["state"] == "paused":
                return
            if self.download_state["phase"] != "downloading":
                raise ConflictError("只有传输阶段可以暂停。")
            self.manager.pause()
            self.download_state.update(
                state="paused",
                phase="paused",
                speed_bytes_per_second=0.0,
                updated_at=utc_now(),
            )

    def resume(self) -> None:
        with self.lock:
            if not self.worker or not self.worker.is_alive():
                raise ConflictError("当前没有可继续的下载。")
            if self.download_state["state"] != "paused":
                raise ConflictError("下载没有暂停。")
            self.manager.resume()
            self.download_state.update(
                state="downloading",
                phase="downloading",
                updated_at=utc_now(),
            )

    def select(self, model_ids: object, endpoint: object = None) -> dict[str, Any]:
        if not isinstance(model_ids, list) or not model_ids:
            raise ValueError("model_ids 必须是非空数组。")
        if any(not isinstance(model_id, str) for model_id in model_ids):
            raise ValueError("model_ids 只能包含字符串。")
        selected = list(dict.fromkeys(model_ids))
        unknown = [model_id for model_id in selected if model_id not in self.statuses]
        if unknown:
            raise ValueError("未知模型：" + ", ".join(unknown))
        with self.lock:
            unavailable = [model_id for model_id in selected if self.statuses[model_id] != "installed"]
            if unavailable:
                raise ConflictError("必须先完成模型下载：" + ", ".join(unavailable))
            selection = self._selection_payload(selected, utc_now(), endpoint, strict=True)
            self._save_selection(selection)
            self.selection = selection
            return dict(selection)

    def _download_worker(self, model_id: str) -> None:
        try:
            self.manager.download(model_id, progress_callback=self._progress)
            if self._runner_config(model_id):
                self._install_runner(model_id)
        except Exception as error:
            with self.lock:
                status = self.manager.installation_status(model_id)
                if self._runner_config(model_id) and status == "installed":
                    status = "setup_failed"
                self.statuses[model_id] = status
                self.download_state.update(
                    state="failed",
                    speed_bytes_per_second=0.0,
                    phase="failed",
                    error=str(error),
                    updated_at=utc_now(),
                )
        else:
            with self.lock:
                self.statuses[model_id] = "installed"
                self.download_state.update(
                    state="complete",
                    percent=100.0,
                    speed_bytes_per_second=0.0,
                    phase="installed",
                    error=None,
                    updated_at=utc_now(),
                )

    def _progress(self, update: DownloadProgress) -> None:
        with self.lock:
            paused = self.download_state["state"] == "paused"
            self.download_state.update(
                filename=update.filename,
                downloaded_bytes=update.downloaded_bytes,
                total_bytes=update.total_bytes,
                percent=round(update.percent, 3),
                speed_bytes_per_second=(
                    0.0 if paused else round(update.speed_bytes_per_second, 3)
                ),
                phase="paused" if paused else update.phase,
                updated_at=utc_now(),
            )

    def _load_selection(self) -> dict[str, Any]:
        if not self.selection_path.exists():
            return {
                "schema_version": 1,
                "selected_model_ids": [],
                "local_llm": {"enabled": False},
                "updated_at": None,
            }
        payload = json.loads(self.selection_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("selection.json 格式无效。")
        selected = payload.get("selected_model_ids")
        if not isinstance(selected, list) or any(
            not isinstance(model_id, str) or model_id not in self.model_ids for model_id in selected
        ):
            raise ValueError("selection.json 包含未知模型。")
        local_llm = payload.get("local_llm")
        endpoint = local_llm.get("endpoint") if isinstance(local_llm, dict) else None
        return self._selection_payload(
            list(dict.fromkeys(selected)),
            payload.get("updated_at"),
            endpoint,
            strict=False,
        )

    def _selection_payload(
        self,
        selected: list[str],
        updated_at: object,
        endpoint: object,
        *,
        strict: bool,
    ) -> dict[str, Any]:
        qwen_ids = [
            model_id
            for model_id in selected
            if str(self.models[model_id].get("family", "")).casefold() == "qwen"
        ]
        runner_ids = [model_id for model_id in selected if self._runner_config(model_id)]
        local_llm: dict[str, Any] = {"enabled": False}
        if qwen_ids:
            if len(qwen_ids) != 1:
                raise ValueError("每个配置只能选择一个 Qwen 模型。")
            if len(runner_ids) != 1:
                if strict:
                    raise ConflictError("选择 Qwen 时必须同时选择已安装的 llama.cpp runner。")
                local_llm.update(model=qwen_ids[0], reason="runner_not_selected")
            else:
                requested_endpoint = endpoint or "http://127.0.0.1:8091/v1"
                requested_path = urlparse(str(requested_endpoint)).path.rstrip("/")
                if requested_path not in {"", "/v1", "/v1/chat/completions"}:
                    raise ValueError("内置 llama.cpp runner 端点路径必须是 /v1。")
                completion_endpoint = normalize_local_llm_endpoint(
                    requested_endpoint
                )
                parsed_endpoint = urlparse(completion_endpoint)
                if parsed_endpoint.scheme != "http" or parsed_endpoint.port is None:
                    raise ValueError("内置 llama.cpp runner 只支持带端口的本地 HTTP 端点。")
                runner_id = runner_ids[0]
                runner_config = self._runner_config(runner_id)
                assert runner_config is not None
                qwen_model = self.models[qwen_ids[0]]
                gguf_files = [
                    artifact
                    for artifact in qwen_model["files"]
                    if str(artifact["path"]).casefold().endswith(".gguf")
                ]
                if not gguf_files:
                    raise ValueError("Qwen 目录未定义 GGUF 文件。")
                local_llm = {
                    "enabled": True,
                    "endpoint": completion_endpoint,
                    "model": qwen_ids[0],
                    "runner_model_id": runner_id,
                    "runner_executable": self._relative_model_path(
                        runner_id,
                        str(runner_config["executable_path"]),
                    ),
                    "model_path": self._relative_model_path(
                        qwen_ids[0],
                        str(gguf_files[0]["path"]),
                    ),
                    "timeout_seconds": 120,
                }
        return {
            "schema_version": 1,
            "selected_model_ids": selected,
            "local_llm": local_llm,
            "updated_at": updated_at,
        }

    def _runner_config(self, model_id: str) -> dict[str, Any] | None:
        value = self.models[model_id].get("runner")
        return value if isinstance(value, dict) else None

    @staticmethod
    def _relative_model_path(model_id: str, relative_path: str) -> str:
        return str(PurePosixPath(model_id) / PurePosixPath(relative_path))

    def _install_runner(self, model_id: str) -> None:
        config = self._runner_config(model_id)
        if config is None:
            return
        model_root = self.manager.model_path(model_id)
        archive = (model_root / Path(*PurePosixPath(str(config["archive_path"])).parts)).resolve()
        destination = (model_root / "runtime").resolve()
        if not archive.is_relative_to(model_root) or not destination.is_relative_to(model_root):
            raise ValueError("runner 路径越出模型目录。")
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                target = (destination / Path(*PurePosixPath(member.filename).parts)).resolve()
                if not target.is_relative_to(destination):
                    raise ValueError("runner 压缩包包含不安全路径。")
            package.extractall(destination)
        executable = (
            model_root / Path(*PurePosixPath(str(config["executable_path"])).parts)
        ).resolve()
        if not executable.is_relative_to(model_root) or not executable.is_file():
            raise ValueError("runner 压缩包中未找到 llama-server.exe。")

    def _save_selection(self, selection: dict[str, Any]) -> None:
        temporary = self.selection_path.with_name(self.selection_path.name + ".tmp")
        body = (json.dumps(selection, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.selection_path)


def make_handler(state: ModelServerState, web_path: Path | None = None):
    page_path = web_path or Path(__file__).resolve().parent / "web" / "models.html"

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _payload(self) -> dict[str, Any]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type 必须是 application/json。")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("Content-Length 无效。") from error
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("请求体大小无效。")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象。")
            return payload

        def do_GET(self) -> None:
            route = urlparse(self.path).path
            if route == "/api/catalog":
                self._json(state.catalog_snapshot())
                return
            if route == "/api/status":
                self._json(state.snapshot())
                return
            if route not in {"/", "/index.html"}:
                self._json({"error": "未找到。"}, HTTPStatus.NOT_FOUND)
                return
            body = page_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            try:
                payload = self._payload()
                if route == "/api/download":
                    started = state.start_download(payload.get("model_id"))
                    self._json(
                        {"accepted": started, "status": state.snapshot()},
                        HTTPStatus.ACCEPTED if started else HTTPStatus.OK,
                    )
                    return
                if route == "/api/pause":
                    state.pause()
                    self._json({"accepted": True, "status": state.snapshot()})
                    return
                if route == "/api/resume":
                    state.resume()
                    self._json({"accepted": True, "status": state.snapshot()})
                    return
                if route == "/api/select":
                    selection = state.select(
                        payload.get("model_ids"),
                        payload.get("endpoint"),
                    )
                    self._json({"accepted": True, "selection": selection})
                    return
                self._json({"error": "未找到。"}, HTTPStatus.NOT_FOUND)
            except ConflictError as error:
                self._json({"error": str(error)}, HTTPStatus.CONFLICT)
            except (KeyError, ValueError, json.JSONDecodeError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def start_background(manager: ModelManager | None = None) -> tuple[ThreadingHTTPServer, str]:
    state = ModelServerState(manager or ModelManager())
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="stemscore-model-ui")
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/"
