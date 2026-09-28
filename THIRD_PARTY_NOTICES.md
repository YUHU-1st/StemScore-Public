# StemScore third-party notices

This file documents third-party components only. It does not grant a license for StemScore's own source code. The public application package contains no model-weight files.

- Mido, MIT License, <https://github.com/mido/mido>
- Basic Pitch, Apache-2.0, <https://github.com/spotify/basic-pitch>
- TransKun, MIT License, <https://github.com/Yujia-Yan/Transkun>
- Music Source Separation Training code, MIT License, <https://github.com/ZFTurbo/Music-Source-Separation-Training>. This code license does not grant redistribution rights for third-party checkpoints loaded by StemScore.
- Demucs code, MIT License, <https://github.com/facebookresearch/demucs>. The pretrained weights are outside that MIT grant; the upstream maintainer describes them as research-only.
- librosa, ISC License, <https://github.com/librosa/librosa>
- NumPy, BSD-3-Clause License, <https://github.com/numpy/numpy>
- PyTorch, BSD-style License, <https://github.com/pytorch/pytorch>
- Transformers, Apache-2.0 License, <https://github.com/huggingface/transformers>

# Optional locally downloaded analysis models and runner

- LAION CLAP HTSAT fused, Apache-2.0, pinned revision `365dea6ef167def6676140ed93bbc43f84dabb28`, <https://huggingface.co/laion/clap-htsat-fused>
- Qwen2.5 Instruct GGUF 0.5B, 1.5B, 7B and 14B, Apache-2.0; each model is pinned by revision and SHA-256 in `stemscore/model_catalog.json`, <https://huggingface.co/Qwen>
- llama.cpp b10887 Windows x64 CPU runner, MIT License, pinned archive and SHA-256 in `stemscore/model_catalog.json`, <https://github.com/ggml-org/llama.cpp/releases/tag/b10887>

These optional files are not part of the application source archive. StemScore downloads them into the software installation's writable `models` directory after the user chooses a model. The catalog records each upstream URL, revision or release, size, SHA-256 and license link.

# BS-RoFormer SW Fixed six-stem checkpoint

- Local source: `enerjazzer/BS-ROFO-SW-Fixed` on Hugging Face.
- Upstream attribution: jarredou, BS-Rofo-SW-Fixed.
- Checkpoint SHA-256: `24e7d35ee9c64415673d3fd33e06a67cac2c103c5df6267ba1576459c775916e`.
- License status: the original checkpoint does not declare a license. StemScore records the source and hash and does not redistribute the checkpoint in its application archive. Users must review the upstream terms before use or redistribution.

# Other core separation checkpoints

- BS-RoFormer Resurrection, source `pcunwa/BS-Roformer-Resurrection`: no upstream license declaration found.
- Karaoke Frazer/Becruily, source `becruily/bs-roformer-karaoke`: no upstream license declaration found.
- Dereverb/Echo Fused, source `Sucial/Dereverb-Echo_Mel_Band_Roformer`: CC BY-NC-SA 4.0; redistribution is non-commercial only and requires attribution, the license text, modification notices, and ShareAlike treatment.
- Demucs `htdemucs_6s`, source `facebookresearch/demucs`: its checkpoint is not covered by the repository MIT license and is documented upstream as research-only.

The separate private offline runtime is marked `localOnly=true` because it contains these weights. Those runtime assets are not copied to this public repository or public Release. A future public runtime may include them only after replacing the restricted assets or obtaining the missing permissions.

# TransKun V2

- Project: Yujia-Yan/Transkun.
- License: MIT for the official package and its included V2 checkpoint; retain the copyright and license text when redistributing.
- Purpose: dedicated local piano audio-to-MIDI transcription.

See `docs/StemScore-模型许可清单.md` for the per-model release decision and source links.

