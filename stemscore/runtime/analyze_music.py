from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import librosa
import numpy as np

PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
MAJOR_PROFILE = np.asarray([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.asarray([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def select_tempo(raw_bpm: float, slow_bpm_candidate: float) -> dict[str, object]:
    raw = max(30.0, min(300.0, float(raw_bpm)))
    slow = max(30.0, min(300.0, float(slow_bpm_candidate)))
    is_double_time = raw >= 130.0 and abs(raw - 2.0 * slow) / raw <= 0.08
    return {
        "bpm": slow if is_double_time else raw,
        "raw_bpm": raw,
        "slow_bpm_candidate": slow,
        "tempo_selection": "half_time_2_to_1" if is_double_time else "raw",
    }


def _safe_db(value: float) -> float:
    return float(20.0 * math.log10(max(abs(value), 1e-9)))


def _tempo_analysis(audio: np.ndarray, sample_rate: int, hop_length: int) -> tuple[dict, np.ndarray]:
    onset = librosa.onset.onset_strength(y=audio, sr=sample_rate, hop_length=hop_length)
    raw = librosa.beat.tempo(onset_envelope=onset, sr=sample_rate, hop_length=hop_length, aggregate=np.median)
    slow = librosa.beat.tempo(
        onset_envelope=onset,
        sr=sample_rate,
        hop_length=hop_length,
        aggregate=np.median,
        start_bpm=70.0,
        std_bpm=1.0,
    )
    raw_bpm = float(np.asarray(raw).reshape(-1)[0]) if np.size(raw) else 120.0
    slow_bpm = float(np.asarray(slow).reshape(-1)[0]) if np.size(slow) else raw_bpm
    result = select_tempo(raw_bpm, slow_bpm)
    beat_candidates = librosa.onset.onset_detect(
        onset_envelope=onset,
        sr=sample_rate,
        hop_length=hop_length,
        units="frames",
        backtrack=False,
    )
    if beat_candidates.size >= 3:
        intervals = np.diff(
            librosa.frames_to_time(beat_candidates, sr=sample_rate, hop_length=hop_length)
        )
        expected_interval = 60.0 / result["bpm"]
        nearest_multiples = np.maximum(1.0, np.round(intervals / expected_interval))
        residuals = np.abs(intervals - nearest_multiples * expected_interval) / expected_interval
        consistency = 1.0 - min(1.0, float(np.mean(residuals)))
        strength = float(np.median(onset[beat_candidates])) / max(
            float(np.percentile(onset, 95)), 1e-9
        )
        confidence = float(np.clip(0.6 * consistency + 0.4 * strength, 0.0, 1.0))
    else:
        confidence = 0.0
    result["confidence"] = round(confidence, 4)
    result["beat_count"] = int(beat_candidates.size)
    return result, onset


def _estimate_tonality(chroma: np.ndarray) -> dict:
    profile = np.mean(chroma, axis=1)
    if not np.any(profile > 0):
        return {"name": None, "tonic": None, "mode": None, "confidence": 0.0, "score": 0.0}
    profile = profile / max(float(np.linalg.norm(profile)), 1e-9)
    candidates: list[tuple[float, str, str]] = []
    for tonic, pitch in enumerate(PITCH_NAMES):
        for mode, template in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            rotated = np.roll(template, tonic)
            rotated = rotated / np.linalg.norm(rotated)
            candidates.append((float(np.dot(profile, rotated)), pitch, mode))
    candidates.sort(reverse=True)
    best, second = candidates[0], candidates[1]
    confidence = float(np.clip((best[0] - second[0]) / 0.12, 0.0, 1.0))
    return {
        "name": f"{best[1]} {best[2]}",
        "tonic": best[1],
        "mode": best[2],
        "confidence": round(confidence, 4),
        "score": round(best[0], 4),
        "second_candidate": f"{second[1]} {second[2]}",
    }


def _downsample_frames(values: np.ndarray, frames_per_bin: int) -> np.ndarray:
    bins = max(1, int(math.ceil(values.shape[1] / frames_per_bin)))
    return np.stack(
        [
            np.mean(values[:, index * frames_per_bin : min(values.shape[1], (index + 1) * frames_per_bin)], axis=1)
            for index in range(bins)
        ],
        axis=1,
    )


def _section_analysis(
    audio: np.ndarray,
    sample_rate: int,
    hop_length: int,
    chroma: np.ndarray,
    rms_db: np.ndarray,
) -> dict:
    duration = len(audio) / sample_rate
    mfcc = librosa.feature.mfcc(y=audio, sr=sample_rate, n_mfcc=8, hop_length=hop_length)
    frame_count = min(chroma.shape[1], mfcc.shape[1], rms_db.size)
    features = np.vstack((chroma[:, :frame_count], mfcc[:, :frame_count], rms_db[:frame_count][None, :]))
    frames_per_bin = max(1, int(round(sample_rate / hop_length / 2.0)))
    features = _downsample_frames(features, frames_per_bin)
    energy = _downsample_frames(rms_db[:frame_count][None, :], frames_per_bin)[0]
    features = (features - np.mean(features, axis=1, keepdims=True)) / (
        np.std(features, axis=1, keepdims=True) + 1e-8
    )
    vectors = features.T
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-8
    similarity = vectors @ vectors.T
    count = vectors.shape[0]
    window = max(3, min(12, count // 8))
    novelty = np.zeros(count, dtype=float)
    for index in range(window, count - window):
        left = similarity[index - window : index, index - window : index]
        right = similarity[index : index + window, index : index + window]
        cross = similarity[index - window : index, index : index + window]
        self_similarity_change = 0.5 * (float(np.mean(left)) + float(np.mean(right))) - float(np.mean(cross))
        energy_jump = abs(float(np.mean(energy[index - window : index]) - np.mean(energy[index : index + window]))) / 20.0
        novelty[index] = max(0.0, 0.75 * self_similarity_change + 0.25 * energy_jump)

    candidates: list[int] = []
    if count >= 2 * window + 1 and np.any(novelty > 0):
        threshold = float(np.percentile(novelty[window : count - window], 75))
        local_maxima = [
            index
            for index in range(window, count - window)
            if novelty[index] >= threshold
            and novelty[index] >= novelty[index - 1]
            and novelty[index] >= novelty[index + 1]
        ]
        min_gap_bins = 16
        for index in sorted(local_maxima, key=lambda item: novelty[item], reverse=True):
            if all(abs(index - selected) >= min_gap_bins for selected in candidates):
                candidates.append(index)
            if len(candidates) >= 10:
                break
        candidates.sort()

    seconds_per_bin = duration / max(count, 1)
    boundary_scores = {round(index * seconds_per_bin, 6): float(novelty[index]) for index in candidates}
    boundaries = [0.0, *boundary_scores.keys(), duration]
    sections: list[dict] = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]), start=1):
        start_bin = min(count - 1, int(start / max(seconds_per_bin, 1e-9)))
        end_bin = min(count, max(start_bin + 1, int(math.ceil(end / max(seconds_per_bin, 1e-9)))))
        sections.append(
            {
                "index": index,
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "duration_seconds": round(end - start, 3),
                "mean_energy_db": round(float(np.mean(energy[start_bin:end_bin])), 3),
                "boundary_score": round(boundary_scores.get(round(start, 6), 0.0), 4),
            }
        )
    return {
        "method": "self_similarity_novelty_plus_energy_jump",
        "boundaries_seconds": [round(value, 3) for value in boundaries],
        "sections": sections,
    }


def analyze(path: str) -> dict:
    audio, sample_rate = librosa.load(path, sr=44100, mono=True)
    if audio.size == 0:
        raise ValueError("音频为空。")
    hop_length = 512
    tempo, onset = _tempo_analysis(audio, sample_rate, hop_length)
    stft = np.abs(librosa.stft(audio, n_fft=2048, hop_length=hop_length))
    rms = librosa.feature.rms(S=stft, frame_length=2048, hop_length=hop_length)[0]
    rms_db = 20.0 * np.log10(np.maximum(rms, 1e-9))
    chroma = librosa.feature.chroma_stft(S=stft, sr=sample_rate)
    centroid = librosa.feature.spectral_centroid(S=stft, sr=sample_rate)[0]
    bandwidth = librosa.feature.spectral_bandwidth(S=stft, sr=sample_rate)[0]
    rolloff = librosa.feature.spectral_rolloff(S=stft, sr=sample_rate, roll_percent=0.85)[0]
    duration = len(audio) / sample_rate
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset,
        sr=sample_rate,
        hop_length=hop_length,
        units="frames",
        backtrack=False,
    )
    return {
        "method": "librosa_local_music_analysis_v1",
        "duration_seconds": round(duration, 6),
        "sample_rate": sample_rate,
        "tempo": tempo,
        "tonality": _estimate_tonality(chroma),
        "loudness": {
            "rms_dbfs": round(_safe_db(float(np.sqrt(np.mean(np.square(audio))))), 4),
            "peak_dbfs": round(_safe_db(float(np.max(np.abs(audio)))), 4),
            "crest_factor_db": round(
                _safe_db(float(np.max(np.abs(audio))))
                - _safe_db(float(np.sqrt(np.mean(np.square(audio))))),
                4,
            ),
            "dynamic_range_db": round(float(np.percentile(rms_db, 95) - np.percentile(rms_db, 10)), 4),
        },
        "spectral": {
            "centroid_hz": round(float(np.mean(centroid)), 3),
            "bandwidth_hz": round(float(np.mean(bandwidth)), 3),
            "rolloff_85_hz": round(float(np.mean(rolloff)), 3),
            "zero_crossing_rate": round(float(np.mean(librosa.feature.zero_crossing_rate(audio))), 6),
        },
        "onset": {
            "count": int(onset_frames.size),
            "rate_per_second": round(float(onset_frames.size / max(duration, 1e-9)), 4),
            "mean_strength": round(float(np.mean(onset)), 4),
            "peak_strength": round(float(np.max(onset)) if onset.size else 0.0, 4),
        },
        "structure": _section_analysis(audio, sample_rate, hop_length, chroma, rms_db),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    args = parser.parse_args()
    payload = analyze(args.input)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
