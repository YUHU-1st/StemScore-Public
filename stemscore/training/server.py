from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from typing import Any
from urllib.parse import urlparse

from .catalog import build_catalog
from ..manifest import utc_now


class TrainingState:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.catalog_path = workspace / "training" / "dataset-catalog.json"
        self.lock = threading.Lock()
        self.status: dict[str, Any] = {
            "state": "idle",
            "message": "已找到可用预训练模型；当前没有必须执行的训练任务。",
            "progress": 0.0,
            "updated_at": utc_now(),
            "catalog": None,
            "log": [],
        }
        if self.catalog_path.is_file():
            catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            self.status["catalog"] = catalog.get("counts")
            self.status["message"] = "已载入本机训练数据目录；训练前仍须人工确认候选组与授权。"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.status, ensure_ascii=False))

    def update(self, **changes: Any) -> None:
        with self.lock:
            self.status.update(changes)
            self.status["updated_at"] = utc_now()

    def catalog(self, roots: list[str]) -> None:
        def work() -> None:
            try:
                self.update(state="cataloging", message="正在建立训练数据目录与 SHA-256…", progress=0.05)
                payload = build_catalog([Path(root) for root in roots], self.catalog_path)
                self.update(
                    state="idle",
                    message="训练数据目录已完成，必须在训练前人工确认候选组与授权。",
                    progress=1.0,
                    catalog=payload["counts"],
                )
            except Exception as error:
                self.update(state="failed", message=str(error), progress=0.0)

        threading.Thread(target=work, daemon=True).start()

    def train(self, recipe_path: str) -> None:
        def work() -> None:
            try:
                recipe = json.loads(Path(recipe_path).read_text(encoding="utf-8"))
                command = [str(item) for item in recipe["command"]]
                cwd = Path(recipe["cwd"]).resolve()
                allowed = (cwd / "train.py").resolve()
                if len(command) < 2 or Path(command[1]).resolve() != allowed:
                    raise RuntimeError("训练配方只能调用其工作目录中的 train.py。")
                self.update(state="training", message="训练正在运行。", progress=0.0, log=[])
                child_env = os.environ.copy()
                child_env.pop("PYTHONHOME", None)
                child_env.pop("PYTHONPATH", None)
                child_env["PYTHONIOENCODING"] = "utf-8"
                child_env["PYTHONUTF8"] = "1"
                process = subprocess.Popen(
                    command,
                    cwd=str(cwd),
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                )
                assert process.stdout is not None
                lines: list[str] = []
                for line in process.stdout:
                    lines.append(line.rstrip())
                    match = re.search(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%", line)
                    changes: dict[str, Any] = {
                        "log": lines[-200:],
                        "message": lines[-1] if lines else "训练正在运行。",
                    }
                    if match:
                        changes["progress"] = min(0.99, float(match.group(1)) / 100.0)
                    self.update(**changes)
                code = process.wait()
                if code:
                    raise RuntimeError(f"训练进程退出码：{code}")
                self.update(state="complete", message="训练完成。", progress=1.0, log=lines[-200:])
            except Exception as error:
                self.update(state="failed", message=str(error), progress=0.0)

        threading.Thread(target=work, daemon=True).start()


def make_handler(state: TrainingState):
    web_path = Path(__file__).resolve().parents[1] / "web" / "training.html"

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            route = urlparse(self.path).path
            if route == "/api/status":
                self._json(state.snapshot())
                return
            if route == "/api/catalog" and state.catalog_path.is_file():
                self._json(json.loads(state.catalog_path.read_text(encoding="utf-8")))
                return
            if route not in {"/", "/index.html"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = web_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            route = urlparse(self.path).path
            if route == "/api/catalog":
                roots = payload.get("roots") or []
                if not roots:
                    self._json({"error": "至少需要一个扫描目录。"}, 400)
                    return
                state.catalog([str(root) for root in roots])
                self._json({"accepted": True}, 202)
                return
            if route == "/api/train":
                state.train(str(payload.get("recipe_path", "")))
                self._json({"accepted": True}, 202)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def start_background(workspace: Path) -> tuple[ThreadingHTTPServer, str]:
    state = TrainingState(workspace)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/"

