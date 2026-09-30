from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Iterator

import pytest

from stemscore.local_llm import (
    _assert_preserves_supported_facts,
    load_local_llm_settings,
    refine_music3_caption,
    validate_music3_caption,
)


def _caption(global_words: str | None = None) -> str:
    global_body = global_words or " ".join(["measured"] * 90)
    return "\n".join(
        [
            "### Global Metadata",
            global_body,
            "",
            "### Vocal Details",
            " ".join(["natural"] * 90),
            "",
            "### Arrangement",
            " ".join(["developing"] * 90),
        ]
    )


@contextmanager
def _local_chat_server(content: str) -> Iterator[tuple[str, dict[str, object]]]:
    captured: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            captured["path"] = self.path
            captured["request"] = json.loads(self.rfile.read(length).decode("utf-8"))
            body = json.dumps(
                {"choices": [{"message": {"content": content}}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _write_selection(path: Path, endpoint: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "local_llm": {
                    "enabled": True,
                    "endpoint": endpoint,
                    "model": "qwen-local.gguf",
                    "timeout_seconds": 2,
                },
            }
        ),
        encoding="utf-8",
    )


def test_selection_accepts_only_loopback_openai_compatible_endpoint(tmp_path: Path) -> None:
    selection = tmp_path / "selection.json"
    _write_selection(selection, "http://localhost:8080/v1")
    settings = load_local_llm_settings(selection)
    assert settings.endpoint == "http://localhost:8080/v1/chat/completions"

    _write_selection(selection, "https://api.openai.com/v1")
    with pytest.raises(ValueError, match="回环"):
        load_local_llm_settings(selection)


def test_selection_resolves_managed_runner_files_inside_models_root(tmp_path: Path) -> None:
    runner = tmp_path / "llama-runner" / "runtime" / "llama-server.exe"
    model = tmp_path / "qwen" / "model.gguf"
    runner.parent.mkdir(parents=True)
    model.parent.mkdir(parents=True)
    runner.write_bytes(b"runner")
    model.write_bytes(b"gguf")
    selection = tmp_path / "selection.json"
    selection.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "local_llm": {
                    "enabled": True,
                    "endpoint": "http://127.0.0.1:8091/v1",
                    "model": "qwen",
                    "runner_executable": "llama-runner/runtime/llama-server.exe",
                    "model_path": "qwen/model.gguf",
                    "timeout_seconds": 120,
                },
            }
        ),
        encoding="utf-8",
    )

    settings = load_local_llm_settings(selection)
    assert settings.runner_executable == runner
    assert settings.model_path == model

    payload = json.loads(selection.read_text(encoding="utf-8"))
    payload["local_llm"]["model_path"] = "../outside.gguf"
    selection.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="models 目录"):
        load_local_llm_settings(selection)


def test_refinement_uses_local_chat_endpoint_without_sending_lyrics(tmp_path: Path) -> None:
    refined = _caption()
    lyrics = "[Verse]\nsecret words never leave this machine"
    with _local_chat_server(refined) as (endpoint, captured):
        selection = tmp_path / "selection.json"
        _write_selection(selection, endpoint)
        result = refine_music3_caption("deterministic draft\n", lyrics, selection)

    assert result.applied is True
    assert result.reason_code == "refined"
    assert result.caption == refined + "\n"
    assert captured["path"] == "/v1/chat/completions"
    request_text = json.dumps(captured["request"], ensure_ascii=False)
    assert "secret words never leave this machine" not in request_text
    assert "only permitted genre and instrument fact words" in request_text
    assert captured["request"]["stream"] is False  # type: ignore[index]


def test_invalid_model_caption_falls_back_with_length_reason(tmp_path: Path) -> None:
    deterministic = "deterministic fallback\n"
    too_short = "\n".join(
        [
            "### Global Metadata",
            "short draft",
            "### Vocal Details",
            "natural vocal",
            "### Arrangement",
            "simple arrangement",
        ]
    )
    with _local_chat_server(too_short) as (endpoint, _captured):
        selection = tmp_path / "selection.json"
        _write_selection(selection, endpoint)
        result = refine_music3_caption(deterministic, None, selection)

    assert result.applied is False
    assert result.reason_code == "caption_invalid"
    assert result.caption == deterministic
    assert "250–450" in result.reason_zh


def test_copied_lyric_phrase_is_rejected_after_other_checks_pass(tmp_path: Path) -> None:
    lyric_phrase = "secret constellation falls behind"
    copied = _caption(lyric_phrase + " " + " ".join(["measured"] * 86))
    assert validate_music3_caption(copied) == 270
    with _local_chat_server(copied) as (endpoint, _captured):
        selection = tmp_path / "selection.json"
        _write_selection(selection, endpoint)
        result = refine_music3_caption("safe deterministic caption", lyric_phrase, selection)

    assert result.applied is False
    assert result.reason_code == "caption_invalid"
    assert "歌词" in result.reason_zh
    assert result.caption == "safe deterministic caption\n"


def test_missing_selection_is_an_explainable_offline_fallback(tmp_path: Path) -> None:
    result = refine_music3_caption("deterministic caption", selection_path=tmp_path / "missing.json")
    assert result.applied is False
    assert result.reason_code == "selection_missing"
    assert result.caption == "deterministic caption\n"
    assert "确定性提示词" in result.reason_zh


def test_fact_guard_rejects_new_instrument_and_key() -> None:
    source = "A piano ballad at 72 BPM with an unverified tonal center."
    with pytest.raises(ValueError, match="事实词"):
        _assert_preserves_supported_facts(
            "A piano and guitar ballad at 72 BPM with an unverified tonal center.",
            source,
        )
    with pytest.raises(ValueError, match="调性"):
        _assert_preserves_supported_facts(
            "A piano ballad at 72 BPM in D minor.",
            source,
        )


def test_fact_guard_rejects_changed_bpm() -> None:
    with pytest.raises(ValueError, match="BPM"):
        _assert_preserves_supported_facts(
            "A piano ballad at 144 BPM.",
            "A piano ballad at 72 BPM.",
        )
