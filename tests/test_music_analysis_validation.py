from __future__ import annotations

import pytest

from stemscore.analysis import build_music3_caption, build_music_report


def _quiet_worker_snapshot() -> dict:
    """Non-copyrighted numeric snapshot from the reviewed local validation run."""
    boundaries = [
        0.0,
        81.312,
        135.187,
        147.658,
        197.044,
        221.488,
        247.926,
        259.4,
        278.855,
        309.783,
        326.245,
        334.227,
    ]
    energies = [
        -24.196,
        -19.114,
        -29.956,
        -16.3,
        -15.403,
        -15.799,
        -19.994,
        -15.487,
        -15.533,
        -26.63,
        -55.968,
    ]
    scores = [0.0, 0.2574, 0.3922, 0.6827, 0.2557, 0.318, 0.268, 0.7004, 0.3413, 0.3554, 0.6878]
    sections = [
        {
            "index": index + 1,
            "start_seconds": boundaries[index],
            "end_seconds": boundaries[index + 1],
            "mean_energy_db": energies[index],
            "boundary_score": scores[index],
        }
        for index in range(11)
    ]
    return {
        "method": "librosa_local_music_analysis_v1",
        "duration_seconds": 334.226667,
        "tempo": {
            "bpm": 71.77734375,
            "raw_bpm": 139.6748310810811,
            "slow_bpm_candidate": 71.77734375,
            "tempo_selection": "half_time_2_to_1",
            "confidence": 1.0,
            "beat_count": 594,
        },
        "tonality": {
            "name": "D minor",
            "tonic": "D",
            "mode": "minor",
            "confidence": 0.0733,
            "score": 0.9694,
            "second_candidate": "A minor",
        },
        "loudness": {"dynamic_range_db": 15.4743},
        "spectral": {"centroid_hz": 1975.407},
        "onset": {"count": 594, "rate_per_second": 1.7772},
        "structure": {
            "method": "self_similarity_novelty_plus_energy_jump",
            "boundaries_seconds": boundaries,
            "sections": sections,
        },
    }


def _quiet_stem_snapshot() -> list[dict]:
    levels = {
        "lead_vocal_dry": -18.1,
        "harmony_vocal_dry": -51.4,
        "bass_dry": -18.0,
        "drums_dry": -25.1,
        "piano_dry": -21.3,
        "other_dry": -22.1,
    }
    stems = [
        {"role": role, "metadata": {"output_mean_volume_db": level}}
        for role, level in levels.items()
    ]
    stems.append(
        {
            "role": "guitar_dry",
            "metadata": {
                "output_mean_volume_db": -90.3,
                "quality_status": "skipped_near_silent_stem",
            },
        }
    )
    return stems


def test_reviewed_quiet_snapshot_keeps_evidence_and_safe_music3_constraints() -> None:
    report = build_music_report(_quiet_worker_snapshot(), _quiet_stem_snapshot())

    assert report["duration_seconds"] == pytest.approx(334.226667)
    assert report["tempo"]["bpm"] == pytest.approx(71.77734375)
    assert report["tempo"]["raw_bpm"] == pytest.approx(139.6748310810811)
    assert report["tempo"]["tempo_selection"] == "half_time_2_to_1"
    assert report["tonality"]["name"] == "D minor"
    assert report["tonality"]["confidence"] == pytest.approx(0.0733)
    assert len(report["structure"]["sections"]) == 11

    by_role = {item["role"]: item for item in report["instrumentation"]}
    assert by_role["piano"]["active"] is True
    assert by_role["piano"]["mean_volume_db"] == pytest.approx(-21.3)
    assert by_role["guitar"]["active"] is False
    assert report["style_candidates"][0]["name_en"] == "piano-led pop ballad"

    caption = build_music3_caption(report)
    assert [line for line in caption.splitlines() if line.startswith("### ")] == [
        "### Global Metadata",
        "### Vocal Details",
        "### Arrangement",
    ]
    assert "piano-led pop ballad" in caption
    assert "71.8 BPM" in caption
    assert "Tonal center" not in caption
    assert "D minor" not in caption
    assert "guitar" not in caption
