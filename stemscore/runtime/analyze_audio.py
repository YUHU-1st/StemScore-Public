from __future__ import annotations

import argparse
import json

import librosa
import numpy as np


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--drums", action="store_true")
    args = parser.parse_args()

    audio, sample_rate = librosa.load(args.input, sr=44100, mono=True)
    onset_envelope = librosa.onset.onset_strength(y=audio, sr=sample_rate)
    tempo = librosa.beat.tempo(onset_envelope=onset_envelope, sr=sample_rate, aggregate=np.median)
    raw_bpm = float(np.asarray(tempo).reshape(-1)[0]) if np.size(tempo) else 120.0
    slow_tempo = librosa.beat.tempo(
        onset_envelope=onset_envelope,
        sr=sample_rate,
        aggregate=np.median,
        start_bpm=70.0,
        std_bpm=1.0,
    )
    slow_bpm = float(np.asarray(slow_tempo).reshape(-1)[0]) if np.size(slow_tempo) else raw_bpm
    is_double_time = raw_bpm >= 130.0 and abs(raw_bpm - 2.0 * slow_bpm) / raw_bpm <= 0.08
    bpm = slow_bpm if is_double_time else raw_bpm
    payload: dict[str, object] = {
        "bpm": max(30.0, min(300.0, bpm)),
        "raw_bpm": raw_bpm,
        "slow_bpm_candidate": slow_bpm,
        "tempo_selection": "half_time_2_to_1" if is_double_time else "raw",
        "notes": [],
    }

    if args.drums:
        stft = np.abs(librosa.stft(audio, n_fft=2048, hop_length=512))
        frequencies = librosa.fft_frequencies(sr=sample_rate, n_fft=2048)
        frames = librosa.onset.onset_detect(
            onset_envelope=onset_envelope,
            sr=sample_rate,
            hop_length=512,
            backtrack=False,
            units="frames",
        )
        peak = float(np.max(onset_envelope)) if onset_envelope.size else 1.0
        notes: list[dict[str, object]] = []
        bands = (
            (36, frequencies < 180),
            (38, (frequencies >= 180) & (frequencies < 3500)),
            (42, frequencies >= 3500),
        )
        for frame in frames:
            if frame >= stft.shape[1]:
                continue
            spectrum = stft[:, frame]
            energies = [(note, float(np.mean(spectrum[mask]))) for note, mask in bands]
            maximum = max((energy for _, energy in energies), default=0.0)
            if maximum <= 0:
                continue
            velocity = int(np.clip(30 + 97 * onset_envelope[min(frame, len(onset_envelope) - 1)] / max(peak, 1e-9), 1, 127))
            for note, energy in energies:
                if energy >= maximum * 0.38:
                    notes.append(
                        {
                            "time": float(librosa.frames_to_time(frame, sr=sample_rate, hop_length=512)),
                            "duration": 0.08,
                            "note": note,
                            "velocity": velocity,
                        }
                    )
        payload["notes"] = notes

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

