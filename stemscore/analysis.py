from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json
from pathlib import Path
import re
import tempfile
from typing import Any

from .audio import run_logged
from .inventory import LocalRuntime


LogCallback = Callable[[str], None]
CaptionRefiner = Callable[[str, dict[str, Any]], str]

ROLE_NAMES = {
    "lead_vocal": ("主唱", "lead vocal"),
    "harmony_vocal": ("和声/叠唱", "backing vocals"),
    "vocals": ("人声", "vocals"),
    "bass": ("贝斯", "bass"),
    "drums": ("鼓组", "drums"),
    "guitar": ("吉他", "guitar"),
    "piano": ("钢琴", "piano"),
    "other": ("其他乐器/纹理", "additional instruments and textures"),
}

CLAP_STYLE_NAMES = {
    "piano-led pop ballad": "钢琴主导抒情流行",
    "pop-rock band": "流行摇滚乐队",
    "electronic contemporary pop": "电子/现代流行",
    "acoustic folk": "原声民谣",
    "R&B and soul": "R&B / 灵魂乐",
    "jazz": "爵士",
    "classical": "古典",
    "ambient texture-led": "环境/纹理导向",
    "hip-hop": "嘻哈",
    "dance pop": "舞曲流行",
}

SECTION_TAGS = {
    "intro": "Intro",
    "verse": "Verse",
    "pre-chorus": "Pre-Chorus",
    "pre chorus": "Pre-Chorus",
    "chorus": "Chorus",
    "post-chorus": "Post-Chorus",
    "post chorus": "Post-Chorus",
    "bridge": "Bridge",
    "instrumental": "Instrumental",
    "solo": "Solo",
    "outro": "Outro",
}


def select_tempo(raw_bpm: float, slow_bpm_candidate: float) -> dict[str, Any]:
    """Apply StemScore's existing 2:1 double-time disambiguation rule."""
    raw = max(30.0, min(300.0, float(raw_bpm)))
    slow = max(30.0, min(300.0, float(slow_bpm_candidate)))
    is_double_time = raw >= 130.0 and abs(raw - 2.0 * slow) / raw <= 0.08
    return {
        "bpm": slow if is_double_time else raw,
        "raw_bpm": raw,
        "slow_bpm_candidate": slow,
        "tempo_selection": "half_time_2_to_1" if is_double_time else "raw",
    }


def _stem_record(stem: Any) -> tuple[str, Mapping[str, Any]]:
    if isinstance(stem, Mapping):
        role = str(stem.get("role", "other"))
        metadata = stem.get("metadata", stem)
    else:
        role = str(getattr(stem, "role", "other"))
        metadata = getattr(stem, "metadata", {})
    role = role.removesuffix("_dry").removesuffix("_midi")
    return role, metadata if isinstance(metadata, Mapping) else {}


def _stem_level(metadata: Mapping[str, Any]) -> float | None:
    for key in (
        "output_mean_volume_db",
        "mean_volume_db",
        "rms_dbfs",
        "source_mean_volume_db",
        "candidate_mean_volume_db",
    ):
        value = metadata.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def infer_instrumentation(stems: Iterable[Any]) -> list[dict[str, Any]]:
    """Convert existing stem roles and energy metadata into explainable presence labels."""
    records: list[tuple[str, Mapping[str, Any], float | None]] = []
    for stem in stems:
        role, metadata = _stem_record(stem)
        if role not in ROLE_NAMES:
            continue
        records.append((role, metadata, _stem_level(metadata)))

    measured = [level for _, _, level in records if level is not None and level > -65.0]
    loudest = max(measured) if measured else None
    result: list[dict[str, Any]] = []
    for role, metadata, level in records:
        status = str(metadata.get("quality_status", ""))
        if status == "skipped_near_silent_stem" or (level is not None and level <= -65.0):
            prominence = "inactive"
            active = False
            evidence = "分轨被质量门标记为近静音。"
        elif level is None or loudest is None:
            prominence = "present_unknown_energy"
            active = True
            evidence = "存在该分轨，但元数据未提供可比的平均能量。"
        elif level <= -55.0:
            prominence = "inactive"
            active = False
            evidence = f"平均能量 {level:.1f} dBFS，低于保守呈现阈值。"
        else:
            relative_db = level - loudest
            active = relative_db >= -30.0
            if relative_db >= -6.0:
                prominence = "dominant"
            elif relative_db >= -18.0:
                prominence = "supporting"
            elif relative_db >= -30.0:
                prominence = "subtle"
            else:
                prominence = "inactive"
            evidence = f"平均能量 {level:.1f} dBFS，相对最强分轨 {relative_db:.1f} dB。"
        chinese, english = ROLE_NAMES[role]
        result.append(
            {
                "role": role,
                "name_zh": chinese,
                "name_en": english,
                "active": active,
                "prominence": prominence,
                "mean_volume_db": level,
                "evidence_zh": evidence,
            }
        )
    order = {role: index for index, role in enumerate(ROLE_NAMES)}
    return sorted(result, key=lambda item: order[item["role"]])


def _active_roles(instrumentation: list[dict[str, Any]]) -> set[str]:
    return {item["role"] for item in instrumentation if item["active"]}


def infer_style_candidates(
    features: Mapping[str, Any], instrumentation: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return conservative, evidence-bearing style candidates rather than a false ground truth."""
    roles = _active_roles(instrumentation)
    bpm = float(features.get("tempo", {}).get("bpm", 0.0) or 0.0)
    onset_rate = float(features.get("onset", {}).get("rate_per_second", 0.0) or 0.0)
    candidates: list[dict[str, Any]] = []

    def add(name_zh: str, name_en: str, score: float, evidence: str) -> None:
        candidates.append(
            {
                "name_zh": name_zh,
                "name_en": name_en,
                "confidence": round(max(0.0, min(1.0, score)), 3),
                "evidence_zh": evidence,
            }
        )

    if {"drums", "bass", "guitar"}.issubset(roles):
        add("流行/摇滚乐队编制候选", "pop-rock band", 0.68, "鼓、贝斯和吉他分轨均有有效能量。")
    piano_led_ballad = (
        "piano" in roles
        and bool({"lead_vocal", "vocals"}.intersection(roles))
        and bpm < 95.0
    )
    if piano_led_ballad:
        add("钢琴主导抒情流行候选", "piano-led pop ballad", 0.78, "主唱与钢琴分轨有效，且速度低于 95 BPM。")
    if (
        {"drums", "bass", "other"}.issubset(roles)
        and "guitar" not in roles
        and not piano_led_ballad
    ):
        score = 0.66 if bpm >= 105.0 else 0.56
        add("电子/现代流行候选", "electronic contemporary pop", score, "节奏组完整，主要额外音色集中在 other 分轨。")
    if "piano" in roles and "lead_vocal" in roles and "drums" not in roles and not piano_led_ballad:
        add("钢琴抒情歌候选", "piano-led ballad", 0.72, "主唱与钢琴有效，鼓组未达活跃阈值。")
    if onset_rate < 1.0 and "other" in roles and "drums" not in roles:
        add("环境/纹理导向候选", "ambient texture-led", 0.58, "起音密度低，且主要能量包含在纹理分轨。")
    if not candidates:
        add("当代流行编曲框架", "contemporary song form", 0.35, "仅能根据速度、起音和分轨结构给出低置信候选。")
    return sorted(candidates, key=lambda item: (-item["confidence"], item["name_en"]))


def apply_clap_style_candidates(
    report: dict[str, Any], semantic: Mapping[str, Any]
) -> None:
    candidates = semantic.get("genre_candidates")
    if not isinstance(candidates, list) or not candidates:
        return
    semantic_candidates: list[dict[str, Any]] = []
    for candidate in candidates[:3]:
        if not isinstance(candidate, Mapping):
            continue
        name = str(candidate.get("label", ""))
        score = float(candidate.get("score", 0.0) or 0.0)
        if name not in CLAP_STYLE_NAMES or score <= 0.0:
            continue
        semantic_candidates.append(
            {
                "name_zh": CLAP_STYLE_NAMES[name] + "候选",
                "name_en": name,
                "confidence": round(max(0.0, min(1.0, score)), 3),
                "evidence_zh": "本地 LAION CLAP 对全曲三个抽样片段与固定曲风标签计算相似度。",
                "method": "local_clap_zero_shot",
            }
        )
    if not semantic_candidates:
        return
    deterministic = [
        {**candidate, "method": candidate.get("method", "dsp_and_stem_rules")}
        for candidate in report.get("style_candidates", [])
    ]
    report["style_candidates"] = deterministic + semantic_candidates
    report["semantic_audio"] = dict(semantic)
    report.setdefault("explanations_zh", []).append(
        "曲风增强候选由本地 CLAP 零样本相似度产生；分轨配器证据仍优先于语义标签。"
    )


def _selected_clap_model() -> Path | None:
    from .model_manager import default_models_root

    models_root = default_models_root()
    selection_path = models_root / "selection.json"
    if not selection_path.is_file():
        return None
    try:
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    selected = selection.get("selected_model_ids", []) if isinstance(selection, dict) else []
    if "clap-htsat-fused" not in selected:
        return None
    model_root = models_root / "clap-htsat-fused"
    required = ("config.json", "model.safetensors", "preprocessor_config.json", "tokenizer.json")
    return model_root if all((model_root / name).is_file() for name in required) else None


def _run_optional_clap(
    runtime: LocalRuntime, source: Path, log: LogCallback
) -> tuple[dict[str, Any] | None, list[str] | None]:
    model_root = _selected_clap_model()
    if model_root is None:
        return None, None
    worker = Path(__file__).resolve().parent / "runtime" / "analyze_clap.py"
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        output = Path(handle.name)
    command = [str(runtime.msst_python), str(worker), str(source), str(model_root), str(output)]
    try:
        log("正在使用本地 CLAP 生成曲风语义候选…")
        run_logged(command, log)
        return json.loads(output.read_text(encoding="utf-8")), command
    except Exception as error:
        log(f"CLAP 可选分析失败，继续使用确定性规则：{error}")
        return {"status": "failed", "error": str(error)}, command
    finally:
        output.unlink(missing_ok=True)


def _feature_descriptions(features: Mapping[str, Any]) -> dict[str, str]:
    bpm = float(features.get("tempo", {}).get("bpm", 0.0) or 0.0)
    centroid = float(features.get("spectral", {}).get("centroid_hz", 0.0) or 0.0)
    dynamic = float(features.get("loudness", {}).get("dynamic_range_db", 0.0) or 0.0)
    onset_rate = float(features.get("onset", {}).get("rate_per_second", 0.0) or 0.0)
    tempo_zh = "慢速" if bpm < 80 else "中速" if bpm < 120 else "快速"
    tempo_en = "slow" if bpm < 80 else "mid-tempo" if bpm < 120 else "up-tempo"
    brightness_zh = "偏暗暖" if centroid < 1400 else "明暗均衡" if centroid < 2600 else "偏明亮"
    brightness_en = "warm and dark" if centroid < 1400 else "tonally balanced" if centroid < 2600 else "bright"
    dynamics_zh = "压缩紧凑" if dynamic < 7 else "动态适中" if dynamic < 14 else "动态宽广"
    dynamics_en = "tightly controlled" if dynamic < 7 else "moderately dynamic" if dynamic < 14 else "wide and dynamic"
    transient_zh = "起音稀疏" if onset_rate < 1.0 else "起音适中" if onset_rate < 3.0 else "起音密集"
    transient_en = "sparse transients" if onset_rate < 1.0 else "moderate transient activity" if onset_rate < 3.0 else "dense transient activity"
    return {
        "tempo_zh": tempo_zh,
        "tempo_en": tempo_en,
        "brightness_zh": brightness_zh,
        "brightness_en": brightness_en,
        "dynamics_zh": dynamics_zh,
        "dynamics_en": dynamics_en,
        "transient_zh": transient_zh,
        "transient_en": transient_en,
    }


def build_music_report(
    worker_analysis: Mapping[str, Any], stems: Iterable[Any] = ()
) -> dict[str, Any]:
    """Build the stable JSON report from local DSP output and existing stem metadata."""
    instrumentation = infer_instrumentation(stems)
    descriptions = _feature_descriptions(worker_analysis)
    styles = infer_style_candidates(worker_analysis, instrumentation)
    tempo = dict(worker_analysis.get("tempo", {}))
    tonality = dict(worker_analysis.get("tonality", {}))
    structure = dict(worker_analysis.get("structure", {}))
    bpm = float(tempo.get("bpm", 0.0) or 0.0)
    key_name = tonality.get("name") or "未确定"
    key_confidence = float(tonality.get("confidence", 0.0) or 0.0)
    active_names = [item["name_zh"] for item in instrumentation if item["active"]]
    explanations = [
        f"速度为 {bpm:.1f} BPM，选择规则为 {tempo.get('tempo_selection', 'raw')}。",
        f"调性候选为 {key_name}，置信度 {key_confidence:.2f}；低置信时不应写入精确生成约束。",
        f"分轨能量支持的配器：{'、'.join(active_names) if active_names else '未提供可用分轨能量'}。",
        f"本地特征显示{descriptions['brightness_zh']}、{descriptions['dynamics_zh']}、{descriptions['transient_zh']}。",
        f"基于自相似度新颖度与能量跳变产生 {len(structure.get('sections', []))} 个段落候选，它们需要人工试听确认。",
    ]
    return {
        "schema_version": 1,
        "method": {
            "scope": "local_deterministic_analysis",
            "audio_worker": worker_analysis.get("method", "librosa"),
            "style_policy": "conservative_candidates_from_dsp_and_stem_energy",
        },
        "duration_seconds": float(worker_analysis.get("duration_seconds", 0.0) or 0.0),
        "tempo": tempo,
        "tonality": tonality,
        "loudness": dict(worker_analysis.get("loudness", {})),
        "spectral": dict(worker_analysis.get("spectral", {})),
        "onset": dict(worker_analysis.get("onset", {})),
        "instrumentation": instrumentation,
        "style_candidates": styles,
        "structure": structure,
        "descriptions": descriptions,
        "explanations_zh": explanations,
        "limitations_zh": [
            "BPM 是节拍网格估计，已应用 2:1 双倍拍消歧，仍可能在复合拍号中产生半拍或倍拍误差。",
            "调性使用色度调性模板相关性估计，转调、调式混用和无调性音乐会降低可信度。",
            "曲风只是由速度、起音、频谱和分轨组合推导的候选，不是分类真值。",
        ],
    }


def render_report_markdown(report: Mapping[str, Any]) -> str:
    tempo = report["tempo"]
    tonality = report["tonality"]
    lines = [
        "# 本地音乐分析报告",
        "",
        "## 速度与调性",
        "",
        f"- BPM：{float(tempo.get('bpm', 0.0)):.1f}（原始候选 {float(tempo.get('raw_bpm', 0.0)):.1f}，规则 `{tempo.get('tempo_selection', 'raw')}`）",
        f"- 调性候选：{tonality.get('name', '未确定')}（置信度 {float(tonality.get('confidence', 0.0)):.2f}）",
        "",
        "## 曲风与制作特征",
        "",
    ]
    for candidate in report.get("style_candidates", []):
        lines.append(
            f"- {candidate['name_zh']}（{candidate['confidence']:.2f}）：{candidate['evidence_zh']}"
        )
    descriptions = report.get("descriptions", {})
    lines.extend(
        [
            f"- 频谱：{descriptions.get('brightness_zh', '未知')} ",
            f"- 动态：{descriptions.get('dynamics_zh', '未知')} ",
            f"- 起音：{descriptions.get('transient_zh', '未知')} ",
            "",
            "## 配器证据",
            "",
        ]
    )
    for item in report.get("instrumentation", []):
        state = "活跃" if item["active"] else "未达阈值"
        lines.append(f"- {item['name_zh']}：{state} / {item['prominence']}。{item['evidence_zh']}")
    lines.extend(["", "## 段落候选", ""])
    for section in report.get("structure", {}).get("sections", []):
        lines.append(
            f"- S{section['index']}: {section['start_seconds']:.1f}–{section['end_seconds']:.1f} s，"
            f"平均能量 {section.get('mean_energy_db', -120.0):.1f} dBFS，"
            f"边界分数 {section.get('boundary_score', 0.0):.2f}"
        )
    lines.extend(["", "## 解释与限制", ""])
    lines.extend(f"- {item}" for item in report.get("explanations_zh", []))
    lines.extend(f"- {item}" for item in report.get("limitations_zh", []))
    refinement = report.get("music3_caption_refinement")
    if isinstance(refinement, Mapping):
        state = "已应用本地 LLM" if refinement.get("applied") else "已回退到确定性结果"
        lines.extend(
            [
                "",
                "## Music 3 提示词精炼",
                "",
                f"- 状态：{state}",
                f"- 原因：{refinement.get('reason_zh', '未提供')}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _lyric_section_tags(lyrics: str | None) -> list[str]:
    if not lyrics:
        return []
    result: list[str] = []
    for value in re.findall(r"\[([^\]]+)\]", lyrics):
        normalized = re.split(r"[:|,;]", value, maxsplit=1)[0].strip().casefold()
        section = SECTION_TAGS.get(normalized)
        if section:
            result.append(section)
    return result[:16]


def _default_sections(report: Mapping[str, Any]) -> list[str]:
    count = len(report.get("structure", {}).get("sections", []))
    if count <= 3:
        return ["Intro", "Main Section", "Outro"]
    if count <= 5:
        return ["Intro", "Section A", "Section B", "Final Section", "Outro"]
    return ["Intro", "Section A", "Section B", "Development", "Climax", "Outro"]


def _assert_caption_contract(caption: str, lyrics: str | None) -> None:
    headings = ["### Global Metadata", "### Vocal Details", "### Arrangement"]
    if [line for line in caption.splitlines() if line.startswith("### ")] != headings:
        raise ValueError("Music 3 caption 必须只包含规定的三个顶级标题。")
    if lyrics:
        lyric_lines = [line.strip() for line in lyrics.splitlines() if line.strip() and not line.lstrip().startswith("[")]
        if any(len(line) >= 4 and line.casefold() in caption.casefold() for line in lyric_lines):
            raise ValueError("Music 3 caption 不得复制歌词正文。")


def build_music3_caption(
    report: Mapping[str, Any],
    lyrics: str | None = None,
    refiner: CaptionRefiner | None = None,
) -> str:
    """Render the official three-section English caption without reproducing lyrics."""
    tempo = report.get("tempo", {})
    tonality = report.get("tonality", {})
    descriptions = report.get("descriptions", {})
    styles = report.get("style_candidates", [])
    instrumentation = report.get("instrumentation", [])
    bpm = float(tempo.get("bpm", 0.0) or 0.0)
    style = styles[0]["name_en"] if styles else "contemporary song form"
    key_clause = ""
    if tonality.get("name") and float(tonality.get("confidence", 0.0) or 0.0) >= 0.35:
        key_clause = f" Tonal center: {tonality['name']}."
    active = [item for item in instrumentation if item.get("active")]
    lead_vocal = any(item["role"] in {"lead_vocal", "vocals"} for item in active)
    backing_vocal = any(item["role"] == "harmony_vocal" for item in active)
    instruments = [
        item["name_en"]
        for item in active
        if item["role"] not in {"lead_vocal", "harmony_vocal", "vocals"}
    ]
    instrument_text = ", ".join(instruments) if instruments else "the detected instrumental texture"
    if lead_vocal:
        vocal_text = (
            "Lead vocals are present; gender, register, and timbre are intentionally unspecified because "
            "the local analysis does not measure them. Keep the delivery natural and style-appropriate, "
            "with clear diction, controlled dynamics, and phrasing that follows the measured slow-to-moderate "
            "energy contour. Build an original vocal melody rather than reproducing the source performance"
        )
        if backing_vocal:
            vocal_text += ", with supporting backing vocals used for contrast and lift"
        vocal_text += (
            ". Use restrained vocal effects that preserve intelligibility. Let room tone, delay, and reverb "
            "change only when the arrangement opens up, and keep the lead centered while any supporting "
            "voices remain wider and lower in level. Do not quote, translate, or paraphrase source lyrics."
        )
    else:
        vocal_text = (
            f"Instrumental piece. Let {instruments[0] if instruments else 'the main instrumental texture'} "
            "carry the lead melodic role; do not add vocals. Shape an original motif with breath-like phrasing, "
            "clear pauses, and a gradual intensity arc. Keep the foreground instrument centered and intelligible, "
            "use ambience only to support depth, and avoid reproducing any source melody or identifiable solo."
        )

    tags = _lyric_section_tags(lyrics)
    sections = tags or _default_sections(report)
    structure_sections = report.get("structure", {}).get("sections", [])
    energy_values = [float(item.get("mean_energy_db", -120.0)) for item in structure_sections]
    peak_index = energy_values.index(max(energy_values)) if energy_values else max(0, len(sections) - 2)
    arrangement_parts: list[str] = []
    for index, name in enumerate(sections):
        if index == 0:
            action = f"establish {instrument_text} with a restrained opening texture"
        elif index == len(sections) - 1:
            action = "release the accumulated energy and let the core elements exit naturally"
        elif index == min(peak_index, len(sections) - 2):
            action = f"reach the strongest energy with the full active arrangement: {instrument_text}"
        else:
            action = "develop the groove by adding or intensifying one active element while preserving continuity"
        arrangement_parts.append(f"{name}: {action}.")

    caption = "\n".join(
        [
            "### Global Metadata",
            (
                f"A {descriptions.get('tempo_en', 'mid-tempo')} {style} at {bpm:.1f} BPM."
                f"{key_clause} The sound is {descriptions.get('brightness_en', 'tonally balanced')}, "
                f"{descriptions.get('dynamics_en', 'moderately dynamic')}, with "
                f"{descriptions.get('transient_en', 'moderate transient activity')}. "
                "Treat the genre label as a conservative production reference derived from local signal and stem evidence. "
                "The tempo, spectral balance, onset density, dynamic range, and reviewed stem presence are measured "
                "locally; artist identity and unmeasured performance traits are not part of the instruction. Preserve "
                "the broad pacing, instrumentation hierarchy, and sectional energy contour while composing new melody, "
                "harmony, lyrics, and performance details. Keep the mix clean and contemporary, leave headroom for the "
                "largest section, and use automation to make transitions audible without abrupt changes in overall tone."
            ),
            "",
            "### Vocal Details",
            vocal_text,
            "",
            "### Arrangement",
            " ".join(arrangement_parts)
            + (
                " Keep instrument entrances, exits, groove changes, transitions, and spatial depth coherent across "
                "sections. Begin with fewer layers and a narrower image, then add only elements supported by the reviewed "
                "stems. Let the rhythm section define pulse without masking the lead, keep bass movement locked to the "
                "kick where both are present, and give harmonic instruments separate registers. Use short fills, filtered "
                "tails, or brief dropouts to mark boundaries. The climax should increase density and width rather than "
                "simply becoming louder. In the ending, remove layers in a deliberate order and retain a natural decay. "
                "All melodic figures, chord voicings, hooks, and transitions must be newly composed while following this "
                "measured production outline."
            ),
        ]
    )
    if refiner is not None:
        caption = refiner(caption, dict(report)).strip()
    _assert_caption_contract(caption, lyrics)
    return caption.rstrip() + "\n"


def analyze_music(
    runtime: LocalRuntime,
    source: Path,
    stems: Iterable[Any] = (),
    lyrics: str | None = None,
    log: LogCallback | None = None,
    caption_refiner: CaptionRefiner | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Run the local librosa worker, then create JSON, Markdown and Music 3 caption output."""
    worker = Path(__file__).resolve().parent / "runtime" / "analyze_music.py"
    if not runtime.msst_python.is_file():
        raise RuntimeError(f"MSST Python 不存在：{runtime.msst_python}")
    if not source.is_file():
        raise FileNotFoundError(source)
    logger = log or (lambda _message: None)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        output = Path(handle.name)
    command = [str(runtime.msst_python), str(worker), str(source), str(output)]
    try:
        run_logged(command, logger)
        worker_analysis = json.loads(output.read_text(encoding="utf-8"))
    finally:
        output.unlink(missing_ok=True)
    report = build_music_report(worker_analysis, stems)
    semantic, semantic_command = _run_optional_clap(runtime, source, logger)
    if semantic and semantic.get("status") != "failed":
        apply_clap_style_candidates(report, semantic)
    elif semantic:
        report["semantic_audio"] = semantic
    if semantic_command:
        report["method"]["semantic_command"] = semantic_command
    deterministic_caption = build_music3_caption(report, lyrics=lyrics)
    if caption_refiner is None:
        from .local_llm import refine_music3_caption

        refinement = refine_music3_caption(deterministic_caption, lyrics=lyrics)
        report["music3_caption"] = refinement.caption
        report["music3_caption_refinement"] = refinement.metadata()
    else:
        try:
            report["music3_caption"] = build_music3_caption(
                report, lyrics=lyrics, refiner=caption_refiner
            )
            report["music3_caption_refinement"] = {
                "applied": True,
                "reason_code": "callback_refined",
                "reason_zh": "外部本地精炼回调输出已通过标题和歌词防复制校验。",
                "endpoint": None,
                "model": None,
            }
        except Exception as error:
            report["music3_caption"] = deterministic_caption
            report["music3_caption_refinement"] = {
                "applied": False,
                "reason_code": "callback_failed",
                "reason_zh": f"外部本地精炼回调失败（{type(error).__name__}），已保留确定性提示词。",
                "endpoint": None,
                "model": None,
            }
    report["markdown"] = render_report_markdown(report)
    return report, command
