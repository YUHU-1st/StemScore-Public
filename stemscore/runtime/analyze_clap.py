from __future__ import annotations

import argparse
import json
from pathlib import Path

import librosa
import numpy as np
import torch
from transformers import ClapModel, ClapProcessor


GENRE_LABELS = (
    "piano-led pop ballad",
    "pop-rock band",
    "electronic contemporary pop",
    "acoustic folk",
    "R&B and soul",
    "jazz",
    "classical",
    "ambient texture-led",
    "hip-hop",
    "dance pop",
)
SAMPLE_RATE = 48_000
WINDOW_SECONDS = 10.0


def sample_windows(audio: np.ndarray) -> tuple[list[np.ndarray], list[float]]:
    window = int(SAMPLE_RATE * WINDOW_SECONDS)
    if audio.size <= window:
        padded = np.pad(audio, (0, max(0, window - audio.size)))
        return [padded.astype(np.float32)], [0.0]
    last_start = audio.size - window
    starts = sorted({int(last_start * fraction) for fraction in (0.1, 0.5, 0.9)})
    return (
        [audio[start : start + window].astype(np.float32) for start in starts],
        [round(start / SAMPLE_RATE, 4) for start in starts],
    )


def analyze(source: Path, model_root: Path) -> dict[str, object]:
    np.random.seed(0)
    torch.manual_seed(0)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(0)
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
    audio, _ = librosa.load(source, sr=SAMPLE_RATE, mono=True)
    windows, starts = sample_windows(audio)
    processor = ClapProcessor.from_pretrained(model_root, local_files_only=True)
    model = ClapModel.from_pretrained(model_root, local_files_only=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device).eval()
    text_inputs = processor.tokenizer(
        list(GENRE_LABELS),
        return_tensors="pt",
        padding=True,
    )
    audio_inputs = processor.feature_extractor(
        windows,
        sampling_rate=SAMPLE_RATE,
        return_tensors="pt",
        padding=True,
    )
    inputs = {**text_inputs, **audio_inputs}
    inputs = {name: value.to(device) for name, value in inputs.items()}
    with torch.inference_mode():
        logits = model(**inputs).logits_per_audio
        probabilities = logits.softmax(dim=-1).mean(dim=0).detach().cpu().numpy()
    order = np.argsort(probabilities)[::-1]
    return {
        "status": "ok",
        "method": "laion_clap_zero_shot_three_windows",
        "model": "clap-htsat-fused",
        "device": str(device),
        "sample_rate": SAMPLE_RATE,
        "window_seconds": WINDOW_SECONDS,
        "window_starts_seconds": starts,
        "genre_candidates": [
            {"label": GENRE_LABELS[int(index)], "score": round(float(probabilities[index]), 6)}
            for index in order[:5]
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("model_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = analyze(args.source, args.model_root)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
