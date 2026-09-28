from __future__ import annotations

from stemscore.analysis import (
    apply_clap_style_candidates,
    build_music3_caption,
    build_music_report,
    infer_instrumentation,
    render_report_markdown,
    select_tempo,
)
from stemscore.local_llm import validate_music3_caption


def synthetic_worker_analysis() -> dict:
    return {
        "method": "synthetic",
        "duration_seconds": 96.0,
        "tempo": {
            **select_tempo(180.0, 90.0),
            "confidence": 0.84,
            "beat_count": 144,
        },
        "tonality": {
            "name": "A minor",
            "tonic": "A",
            "mode": "minor",
            "confidence": 0.72,
            "score": 0.91,
        },
        "loudness": {
            "rms_dbfs": -16.0,
            "peak_dbfs": -1.0,
            "crest_factor_db": 15.0,
            "dynamic_range_db": 10.0,
        },
        "spectral": {
            "centroid_hz": 2200.0,
            "bandwidth_hz": 2600.0,
            "rolloff_85_hz": 6500.0,
            "zero_crossing_rate": 0.08,
        },
        "onset": {"count": 192, "rate_per_second": 2.0, "mean_strength": 1.2, "peak_strength": 5.0},
        "structure": {
            "method": "self_similarity_novelty_plus_energy_jump",
            "boundaries_seconds": [0.0, 16.0, 40.0, 64.0, 84.0, 96.0],
            "sections": [
                {"index": 1, "start_seconds": 0.0, "end_seconds": 16.0, "mean_energy_db": -24.0, "boundary_score": 0.0},
                {"index": 2, "start_seconds": 16.0, "end_seconds": 40.0, "mean_energy_db": -18.0, "boundary_score": 0.48},
                {"index": 3, "start_seconds": 40.0, "end_seconds": 64.0, "mean_energy_db": -12.0, "boundary_score": 0.71},
                {"index": 4, "start_seconds": 64.0, "end_seconds": 84.0, "mean_energy_db": -15.0, "boundary_score": 0.62},
                {"index": 5, "start_seconds": 84.0, "end_seconds": 96.0, "mean_energy_db": -27.0, "boundary_score": 0.44},
            ],
        },
    }


def synthetic_stems() -> list[dict]:
    return [
        {"role": "lead_vocal_dry", "metadata": {"output_mean_volume_db": -15.0}},
        {"role": "harmony_vocal_dry", "metadata": {"output_mean_volume_db": -29.0}},
        {"role": "drums_dry", "metadata": {"output_mean_volume_db": -12.0}},
        {"role": "bass_dry", "metadata": {"output_mean_volume_db": -17.0}},
        {"role": "guitar_dry", "metadata": {"output_mean_volume_db": -19.0}},
        {
            "role": "piano_dry",
            "metadata": {"output_mean_volume_db": -80.0, "quality_status": "skipped_near_silent_stem"},
        },
    ]


def test_select_tempo_reuses_double_time_disambiguation() -> None:
    assert select_tempo(180.0, 90.0) == {
        "bpm": 90.0,
        "raw_bpm": 180.0,
        "slow_bpm_candidate": 90.0,
        "tempo_selection": "half_time_2_to_1",
    }
    assert select_tempo(124.0, 62.0)["tempo_selection"] == "raw"


def test_instrumentation_uses_relative_stem_energy_and_silence_gate() -> None:
    instrumentation = infer_instrumentation(synthetic_stems())
    by_role = {item["role"]: item for item in instrumentation}
    assert by_role["drums"]["prominence"] == "dominant"
    assert by_role["guitar"]["prominence"] == "supporting"
    assert by_role["harmony_vocal"]["prominence"] == "supporting"
    assert by_role["piano"]["active"] is False
    assert "-80.0" not in by_role["piano"]["evidence_zh"]


def test_report_is_explainable_json_and_markdown() -> None:
    report = build_music_report(synthetic_worker_analysis(), synthetic_stems())
    assert report["tempo"]["bpm"] == 90.0
    assert report["tonality"]["name"] == "A minor"
    assert report["style_candidates"][0]["name_en"] == "pop-rock band"
    assert report["structure"]["method"] == "self_similarity_novelty_plus_energy_jump"
    assert any("双倍拍" in item for item in report["limitations_zh"])
    markdown = render_report_markdown(report)
    assert "# 本地音乐分析报告" in markdown
    assert "90.0" in markdown
    assert "配器证据" in markdown
    assert "S3: 40.0–64.0 s" in markdown


def test_music3_caption_is_deterministic_structured_and_never_copies_lyrics() -> None:
    report = build_music_report(synthetic_worker_analysis(), synthetic_stems())
    lyrics = "[Verse]\nA secret sentence must never appear\n[Chorus]\nAnother private lyric"
    first = build_music3_caption(report, lyrics)
    second = build_music3_caption(report, lyrics)
    assert first == second
    assert [line for line in first.splitlines() if line.startswith("### ")] == [
        "### Global Metadata",
        "### Vocal Details",
        "### Arrangement",
    ]
    assert "A secret sentence must never appear" not in first
    assert "Another private lyric" not in first
    assert "Verse:" in first
    assert "Chorus:" in first
    assert "90.0 BPM" in first
    assert "A minor" in first
    assert 250 <= validate_music3_caption(first, lyrics) <= 450


def test_low_confidence_key_is_not_asserted_in_music3_caption() -> None:
    analysis = synthetic_worker_analysis()
    analysis["tonality"]["confidence"] = 0.12
    report = build_music_report(analysis, synthetic_stems())
    caption = build_music3_caption(report)
    assert "Tonal center" not in caption


def test_local_clap_candidates_are_explainable_and_keep_rule_evidence() -> None:
    report = build_music_report(synthetic_worker_analysis(), synthetic_stems())
    apply_clap_style_candidates(
        report,
        {
            "status": "ok",
            "method": "laion_clap_zero_shot_three_windows",
            "genre_candidates": [
                {"label": "piano-led pop ballad", "score": 0.61},
                {"label": "jazz", "score": 0.14},
            ],
        },
    )
    assert report["style_candidates"][0]["name_en"] == "pop-rock band"
    assert report["style_candidates"][0]["method"] == "dsp_and_stem_rules"
    assert any(item["method"] == "local_clap_zero_shot" for item in report["style_candidates"])
    assert any(item["method"] == "dsp_and_stem_rules" for item in report["style_candidates"])
    assert report["semantic_audio"]["status"] == "ok"
