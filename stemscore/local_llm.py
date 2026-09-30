from __future__ import annotations

from dataclasses import asdict, dataclass
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .model_manager import default_models_root


MUSIC3_HEADINGS = (
    "### Global Metadata",
    "### Vocal Details",
    "### Arrangement",
)
MIN_CAPTION_WORDS = 250
MAX_CAPTION_WORDS = 450
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_SELECTION_PATH = default_models_root() / "selection.json"
_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
_FACT_TERMS = {
    "female", "male", "child", "soprano", "alto", "tenor", "baritone",
    "piano", "guitar", "drums", "bass", "strings", "violin", "cello",
    "synth", "synthesizer", "organ", "brass", "saxophone", "flute",
    "acoustic", "electric", "rock", "pop", "jazz", "classical", "folk",
    "electronic", "metal", "hip-hop", "reggae", "country", "ballad",
}
_KEY_PATTERN = re.compile(r"\b[A-G](?:#|b)?\s+(?:major|minor)\b", re.IGNORECASE)
_BPM_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\s*BPM\b", re.IGNORECASE)


@dataclass(frozen=True)
class LocalLlmSettings:
    enabled: bool
    endpoint: str
    model: str
    timeout_seconds: float
    runner_executable: Path | None = None
    model_path: Path | None = None


@dataclass(frozen=True)
class RefinementResult:
    caption: str
    applied: bool
    reason_code: str
    reason_zh: str
    endpoint: str | None = None
    model: str | None = None

    def metadata(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("caption")
        return data


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def normalize_local_llm_endpoint(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("local_llm.endpoint 必须是非空 URL。")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("local_llm.endpoint 必须是 HTTP(S) URL。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("local_llm.endpoint 不得包含凭据、查询参数或片段。")
    hostname = parsed.hostname.casefold()
    is_loopback = hostname == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise ValueError("local_llm.endpoint 只允许 localhost 或回环 IP。")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("local_llm.endpoint 端口无效。") from error

    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        completion_path = path
    elif path.endswith("/v1"):
        completion_path = path + "/chat/completions"
    elif path:
        completion_path = path + "/v1/chat/completions"
    else:
        completion_path = "/v1/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, completion_path, "", ""))


def _local_file(selection_path: Path, value: object, field: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"local_llm.{field} 必须是非空相对路径。")
    candidate = Path(value)
    if candidate.is_absolute():
        raise ValueError(f"local_llm.{field} 必须位于 models 目录内。")
    models_root = selection_path.parent.resolve()
    resolved = (models_root / candidate).resolve()
    if not resolved.is_relative_to(models_root):
        raise ValueError(f"local_llm.{field} 不得越出 models 目录。")
    if not resolved.is_file():
        raise ValueError(f"local_llm.{field} 指向的本地文件不存在。")
    return resolved


def load_local_llm_settings(path: Path | None = None) -> LocalLlmSettings:
    selection_path = path or DEFAULT_SELECTION_PATH
    payload = json.loads(selection_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("models/selection.json 必须是 JSON 对象。")
    config = payload.get("local_llm", payload)
    if not isinstance(config, dict):
        raise ValueError("selection.json 的 local_llm 必须是对象。")

    enabled = config.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("local_llm.enabled 必须是布尔值。")
    if not enabled:
        return LocalLlmSettings(False, "", "", 30.0)

    endpoint = normalize_local_llm_endpoint(config.get("endpoint", config.get("base_url")))
    model = config.get("model", config.get("model_id", config.get("selected_model_id")))
    if not isinstance(model, str) or not model.strip():
        raise ValueError("local_llm.model 必须是非空字符串。")
    timeout = config.get("timeout_seconds", 30.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("local_llm.timeout_seconds 必须是数字。")
    if not 0 < float(timeout) <= 120:
        raise ValueError("local_llm.timeout_seconds 必须大于 0 且不超过 120。")
    runner_executable = _local_file(
        selection_path,
        config.get("runner_executable"),
        "runner_executable",
    )
    model_path = _local_file(selection_path, config.get("model_path"), "model_path")
    if (runner_executable is None) != (model_path is None):
        raise ValueError("local_llm.runner_executable 和 model_path 必须同时配置。")
    return LocalLlmSettings(
        True,
        endpoint,
        model.strip(),
        float(timeout),
        runner_executable,
        model_path,
    )


def _health_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def _runner_is_ready(endpoint: str) -> bool:
    request = Request(_health_url(endpoint), headers={"Accept": "application/json"})
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=0.5) as response:
            return 200 <= response.status < 300
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def _start_managed_runner(settings: LocalLlmSettings) -> subprocess.Popen[bytes] | None:
    if settings.runner_executable is None or settings.model_path is None:
        return None
    if _runner_is_ready(settings.endpoint):
        return None
    parsed = urlsplit(settings.endpoint)
    if parsed.scheme != "http" or parsed.port is None:
        raise OSError("内置 llama.cpp runner 需要带端口的本地 HTTP 端点。")
    command = [
        str(settings.runner_executable),
        "--model",
        str(settings.model_path),
        "--alias",
        settings.model,
        "--host",
        parsed.hostname or "127.0.0.1",
        "--port",
        str(parsed.port),
        "--ctx-size",
        "4096",
        "--jinja",
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    deadline = time.monotonic() + min(settings.timeout_seconds, 60.0)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise OSError(f"llama.cpp runner 启动失败，退出码 {process.returncode}。")
        if _runner_is_ready(settings.endpoint):
            return process
        time.sleep(0.1)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    raise TimeoutError("llama.cpp runner 在时限内未就绪。")


def _lyric_lines(lyrics: str | None) -> list[str]:
    if not lyrics:
        return []
    return [
        line.strip()
        for line in lyrics.splitlines()
        if line.strip() and not line.lstrip().startswith("[")
    ]


def _normalized_words(value: str) -> list[str]:
    return [match.casefold() for match in _WORD_PATTERN.findall(value)]


def _assert_no_lyric_copy(caption: str, lyrics: str | None) -> None:
    caption_casefold = caption.casefold()
    caption_words = _normalized_words(caption)
    for line in _lyric_lines(lyrics):
        if len(line) >= 4 and line.casefold() in caption_casefold:
            raise ValueError("输出复制了歌词原句。")
        words = _normalized_words(line)
        if len(words) < 4:
            continue
        for index in range(len(words) - 3):
            phrase = words[index : index + 4]
            if any(caption_words[offset : offset + 4] == phrase for offset in range(len(caption_words) - 3)):
                raise ValueError("输出包含与歌词相同的连续四词片段。")


def _assert_preserves_supported_facts(caption: str, deterministic_caption: str) -> None:
    source_words = set(_normalized_words(deterministic_caption))
    output_words = set(_normalized_words(caption))
    unsupported = sorted((output_words & _FACT_TERMS) - (source_words & _FACT_TERMS))
    if unsupported:
        raise ValueError(f"输出新增了确定性草稿未支持的事实词：{', '.join(unsupported)}。")
    source_keys = {item.casefold() for item in _KEY_PATTERN.findall(deterministic_caption)}
    output_keys = {item.casefold() for item in _KEY_PATTERN.findall(caption)}
    if not output_keys.issubset(source_keys):
        raise ValueError("输出新增了确定性草稿未支持的调性。")
    source_bpms = {re.sub(r"\s+", "", item.casefold()) for item in _BPM_PATTERN.findall(deterministic_caption)}
    output_bpms = {re.sub(r"\s+", "", item.casefold()) for item in _BPM_PATTERN.findall(caption)}
    if output_bpms != source_bpms:
        raise ValueError("输出修改、删除或新增了 BPM 数值。")


def validate_music3_caption(
    caption: str,
    lyrics: str | None = None,
    *,
    min_words: int = MIN_CAPTION_WORDS,
    max_words: int = MAX_CAPTION_WORDS,
) -> int:
    if not isinstance(caption, str) or not caption.strip():
        raise ValueError("本地模型未返回提示词正文。")
    stripped = caption.strip()
    heading_lines = [
        line.strip()
        for line in stripped.splitlines()
        if re.match(r"^#{1,6}\s+", line.strip())
    ]
    if heading_lines != list(MUSIC3_HEADINGS):
        raise ValueError("输出必须依次且仅包含三个官方标题。")
    nonempty = [line.strip() for line in stripped.splitlines() if line.strip()]
    if not nonempty or nonempty[0] != MUSIC3_HEADINGS[0]:
        raise ValueError("输出必须从 Global Metadata 标题开始。")
    for index, heading in enumerate(MUSIC3_HEADINGS):
        start = nonempty.index(heading) + 1
        end = nonempty.index(MUSIC3_HEADINGS[index + 1]) if index + 1 < len(MUSIC3_HEADINGS) else len(nonempty)
        if start >= end:
            raise ValueError(f"{heading} 章节不得为空。")

    body = "\n".join(line for line in stripped.splitlines() if line.strip() not in MUSIC3_HEADINGS)
    word_count = len(_normalized_words(body))
    if not min_words <= word_count <= max_words:
        raise ValueError(
            f"英文正文长度必须为 {min_words}–{max_words} 词，当前为 {word_count} 词。"
        )
    _assert_no_lyric_copy(stripped, lyrics)
    return word_count


def _request_refinement(settings: LocalLlmSettings, deterministic_caption: str) -> str:
    permitted_facts = sorted(set(_normalized_words(deterministic_caption)) & _FACT_TERMS)
    system_prompt = (
        "You rewrite music-production captions for MiniMax Music 3. Return only the caption. "
        "Use exactly these Markdown headings, once each and in this order: ### Global Metadata, "
        "### Vocal Details, ### Arrangement. Write 250 to 450 English words. Preserve only facts "
        "supported by the draft. Do not introduce any new genre or instrument names, even as examples; "
        f"the only permitted genre and instrument fact words are: {', '.join(permitted_facts) or 'none'}. "
        "If the draft says vocal presence is unmeasured, do not call the source vocal or instrumental. "
        "Do not quote, paraphrase, translate, or invent song lyrics."
    )
    user_prompt = (
        "Polish the deterministic local-analysis draft below. Keep its BPM, evidence limits, uncertain "
        "vocal status, and supported instruments unchanged. Improve only phrasing, section development, "
        "transitions, and production texture; do not fill in facts the draft did not measure.\n\n"
        + deterministic_caption.strip()
    )
    body = json.dumps(
        {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 900,
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        settings.endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    process = _start_managed_runner(settings)
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
        with opener.open(request, timeout=settings.timeout_seconds) as response:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    if len(response_body) > MAX_RESPONSE_BYTES:
        raise ValueError("本地模型响应超过 2 MiB 限制。")
    payload = json.loads(response_body.decode("utf-8"))
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("本地端点响应不符合 OpenAI chat/completions 格式。") from error
    if not isinstance(content, str):
        raise ValueError("本地端点返回的 message.content 不是字符串。")
    return content.strip()


def _fallback(
    caption: str,
    reason_code: str,
    reason_zh: str,
    settings: LocalLlmSettings | None = None,
) -> RefinementResult:
    return RefinementResult(
        caption=caption.rstrip() + "\n",
        applied=False,
        reason_code=reason_code,
        reason_zh=reason_zh,
        endpoint=settings.endpoint if settings and settings.endpoint else None,
        model=settings.model if settings and settings.model else None,
    )


def refine_music3_caption(
    deterministic_caption: str,
    lyrics: str | None = None,
    selection_path: Path | None = None,
) -> RefinementResult:
    """Refine a caption through a loopback-only OpenAI-compatible endpoint."""
    try:
        settings = load_local_llm_settings(selection_path)
    except FileNotFoundError:
        return _fallback(
            deterministic_caption,
            "selection_missing",
            "未找到 models/selection.json，已保留确定性提示词。",
        )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _fallback(
            deterministic_caption,
            "selection_invalid",
            f"本地模型选择配置无效：{error} 已保留确定性提示词。",
        )
    if not settings.enabled:
        return _fallback(
            deterministic_caption,
            "disabled",
            "selection.json 已停用本地 LLM 精炼，已保留确定性提示词。",
            settings,
        )

    try:
        refined = _request_refinement(settings, deterministic_caption)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        return _fallback(
            deterministic_caption,
            "request_failed",
            f"本地 LLM 端点请求失败（{type(error).__name__}），已保留确定性提示词。",
            settings,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        return _fallback(
            deterministic_caption,
            "response_invalid",
            f"本地 LLM 响应无法解析：{error} 已保留确定性提示词。",
            settings,
        )

    try:
        word_count = validate_music3_caption(refined, lyrics)
        _assert_preserves_supported_facts(refined, deterministic_caption)
    except ValueError as error:
        return _fallback(
            deterministic_caption,
            "caption_invalid",
            f"本地 LLM 输出未通过 Music 3 校验：{error} 已保留确定性提示词。",
            settings,
        )
    return RefinementResult(
        caption=refined.rstrip() + "\n",
        applied=True,
        reason_code="refined",
        reason_zh=f"本地 LLM 输出通过三段标题、{word_count} 词长度、歌词防复制和事实约束校验。",
        endpoint=settings.endpoint,
        model=settings.model,
    )
