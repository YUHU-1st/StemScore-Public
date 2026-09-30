from __future__ import annotations

import json
from pathlib import Path
import shutil
import wave

import mido
import pytest

from stemscore.manifest import Artifact, ProjectManifest
from stemscore.midi_tools import merge_midi_files, normalize_single_track, write_drum_midi, write_empty_midi
from stemscore.training.catalog import build_catalog
from stemscore.workflow import (
    PUBLIC_MEGA53_GROUPS,
    PUBLIC_MEGA53_VOCAL_STEMS,
    Workflow,
    dereverb_quality_status,
    safe_name,
)


def write_wav(path: Path, frames: int = 4410) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\0\0" * frames)


def write_audible_wav(path: Path, frames: int = 4410) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes((4096).to_bytes(2, "little", signed=True) * frames)


def write_midi(path: Path, note: int = 60) -> None:
    midi = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    track.append(mido.Message("note_on", note=note, velocity=90, time=0))
    track.append(mido.Message("note_off", note=note, velocity=0, time=480))
    midi.tracks.append(track)
    midi.save(path)


def test_safe_name_removes_windows_reserved_characters() -> None:
    assert safe_name(' A:B/C*D? "x" ') == "A_B_C_D_ _x_"


def test_drum_dereverb_rejects_muffled_candidate() -> None:
    accepted, status = dereverb_quality_status("drums", -19.3, -21.0, -18.2, -42.7)
    assert accepted is False
    assert status == "rejected_drum_high_frequency_loss"


def test_drum_dereverb_accepts_preserved_brightness() -> None:
    accepted, status = dereverb_quality_status("drums", -19.3, -21.0, -18.2, -20.5)
    assert accepted is True
    assert status == "accepted"


def test_piano_dereverb_rejects_lost_harmonics() -> None:
    accepted, status = dereverb_quality_status("piano", -21.3, -22.0, -20.0, -26.0)
    assert accepted is False
    assert status == "rejected_piano_high_frequency_loss"


def test_dereverb_rejects_lost_transients() -> None:
    accepted, status = dereverb_quality_status(
        "lead_vocal", -20.0, -21.0, -18.0, -18.5, 12.0, 8.0
    )
    assert accepted is False
    assert status == "rejected_lead_vocal_transient_loss"


def test_stage4_bypasses_unselected_role_without_model(tmp_path: Path) -> None:
    source = tmp_path / "piano.wav"
    write_audible_wav(source)
    workflow = Workflow.create(source, tmp_path / "projects", "bypass", dereverb_roles=[])
    for number in (1, 2, 3):
        workflow.manifest.stage(number).status = "reviewed"
    workflow.manifest.stage(3).artifacts = [Artifact.from_file(source, "piano", "audio/wav")]
    workflow.manifest.stage(4).status = "ready"
    workflow.manifest.save()
    workflow.run_stage(4)
    stage = workflow.manifest.stage(4)
    output = next(artifact for artifact in stage.artifacts if artifact.role == "piano")
    assert output.path.endswith("_04_piano.wav")
    assert output.metadata["quality_status"] == "bypassed_by_role_policy"
    assert output.sha256 == Artifact.from_file(source, "piano", "audio/wav").sha256
    assert stage.commands == []
    assert stage.models == []


def test_prepare_stage_marks_running_before_execution(tmp_path: Path) -> None:
    source = tmp_path / "song.wav"
    write_audible_wav(source)
    workflow = Workflow.create(source, tmp_path / "projects", "running-status")
    workflow.prepare_stage(1)
    assert workflow.manifest.stage(1).status == "running"
    reloaded = ProjectManifest.load(workflow.manifest.path)
    assert reloaded.stage(1).status == "running"


def test_skip_stage_unlocks_next_stage_and_normal_run_accepts_skipped_gate(tmp_path: Path) -> None:
    source = tmp_path / "song.wav"
    write_audible_wav(source)
    workflow = Workflow.create(source, tmp_path / "projects", "skip")
    workflow.manifest.stage(1).status = "reviewed"
    workflow.manifest.stage(1).artifacts = [Artifact.from_file(source, "source", "audio/wav")]
    workflow.manifest.stage(2).status = "ready"
    workflow.manifest.save()
    workflow.skip(2)
    assert workflow.manifest.stage(2).status == "skipped"
    assert workflow.manifest.stage(3).status == "ready"


def test_stage5_can_use_stage3_when_stage4_is_incomplete(tmp_path: Path) -> None:
    source = tmp_path / "song.wav"
    write_audible_wav(source)
    workflow = Workflow.create(source, tmp_path / "projects", "stage5-fallback")
    workflow.manifest.stage(1).status = "reviewed"
    workflow.manifest.stage(1).artifacts = [Artifact.from_file(source, "source", "audio/wav")]
    workflow.manifest.stage(3).status = "reviewed"
    roles = ["lead_vocal", "harmony_vocal", "bass", "drums", "guitar", "piano", "other"]
    workflow.manifest.stage(3).artifacts = [Artifact.from_file(source, role, "audio/wav") for role in roles]
    workflow.manifest.stage(4).status = "failed"
    partial = tmp_path / "piano_dry.wav"
    shutil.copy2(source, partial)
    workflow.manifest.stage(4).artifacts = [Artifact.from_file(partial, "piano_dry", "audio/wav")]
    workflow.manifest.save()

    artifacts = workflow._stage_5_audio_artifacts()
    by_role = {artifact.role.removesuffix("_dry"): artifact for artifact in artifacts}
    assert set(by_role) == set(roles)
    assert Path(by_role["piano"].path) == partial
    assert Path(by_role["guitar"].path) == source


def test_direct_stage5_does_not_require_stage4_completion(tmp_path: Path) -> None:
    source = tmp_path / "song.wav"
    write_audible_wav(source)
    workflow = Workflow.create(source, tmp_path / "projects", "direct-five")
    workflow.manifest.stage(1).status = "reviewed"
    workflow.manifest.stage(1).artifacts = [Artifact.from_file(source, "source", "audio/wav")]
    workflow.manifest.stage(3).status = "review_required"
    workflow.manifest.stage(3).artifacts = [Artifact.from_file(source, "piano", "audio/wav")]
    workflow.manifest.stage(4).status = "running"
    workflow.manifest.stage(5).status = "locked"
    workflow.manifest.save()
    workflow.prepare_stage(5, allow_unreviewed=True)
    assert workflow.manifest.stage(5).status == "running"


def test_stage4_preserves_selected_role_when_permissive_dereverb_is_unavailable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lead.wav"
    write_audible_wav(source)
    workflow = Workflow.create(source, tmp_path / "projects", "no-dereverb")
    for number in (1, 2, 3):
        workflow.manifest.stage(number).status = "reviewed"
    workflow.manifest.stage(3).artifacts = [Artifact.from_file(source, "lead_vocal", "audio/wav")]
    workflow.manifest.stage(4).status = "ready"
    workflow.manifest.save()

    workflow.run_stage(4)

    stage = workflow.manifest.stage(4)
    output = next(artifact for artifact in stage.artifacts if artifact.role == "lead_vocal_dry")
    assert output.metadata["quality_status"] == "bypassed_no_permissive_dereverb_model"
    assert output.sha256 == Artifact.from_file(source, "lead_vocal", "audio/wav").sha256
    assert stage.commands == []
    assert stage.models == []


def test_public_mega53_groups_keep_vocals_and_instrument_roles_disjoint() -> None:
    flattened = [stem for stems in PUBLIC_MEGA53_GROUPS.values() for stem in stems]
    assert len(flattened) == len(set(flattened))
    assert PUBLIC_MEGA53_GROUPS["lead_vocal"] == ("lead-vocal", "vocal")
    assert PUBLIC_MEGA53_GROUPS["harmony_vocal"] == ("back-vocal",)
    assert {"bass", "drums", "guitar", "piano"} <= PUBLIC_MEGA53_GROUPS.keys()


def test_public_mega53_fallback_runs_model_once_and_reuses_raw_stems(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "song.wav"
    write_audible_wav(source, frames=2205)
    workflow = Workflow.create(source, tmp_path / "projects", "public-fallback")
    workflow.manifest.stage(1).status = "reviewed"
    workflow.manifest.stage(1).artifacts = [Artifact.from_file(source, "source", "audio/wav")]
    workflow.manifest.stage(2).status = "ready"
    workflow.manifest.save()
    monkeypatch.setattr("stemscore.inventory.LocalRuntime.validate_core", lambda _runtime: None)
    monkeypatch.setattr("stemscore.workflow.msst.model_available", lambda _runtime, _model: False)
    monkeypatch.setattr(
        "stemscore.workflow.msst.model_evidence",
        lambda _runtime, model: {"name": model.name, "backend": "MSST"},
    )
    calls: list[Path] = []

    def fake_separate_all(_runtime, input_path, output_dir, _model, _log):
        calls.append(input_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        stems = {"lead-vocal", "back-vocal", "vocal", "saxophone"}
        stems.update(stem for group in PUBLIC_MEGA53_GROUPS.values() for stem in group)
        result = {}
        for stem in stems:
            path = output_dir / f"track_{stem}.wav"
            shutil.copy2(source, path)
            result[stem] = path
        return result, ["msst", "inference"]

    monkeypatch.setattr("stemscore.workflow.msst.separate_all", fake_separate_all)

    workflow.run_stage(2)
    workflow.approve(2)
    workflow.run_stage(3)

    assert len(calls) == 1
    assert workflow.manifest.settings["separation_backend"] == "public-mega53-v1"
    roles = {
        artifact.role
        for artifact in workflow.manifest.stage(3).artifacts
        if artifact.media_type == "audio/wav"
    }
    assert roles == {"lead_vocal", "harmony_vocal", "bass", "drums", "guitar", "piano", "other"}


def test_public_stage2_builds_accompaniment_only_from_non_vocal_stems(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "song.wav"
    write_audible_wav(source, frames=2205)
    workflow = Workflow.create(source, tmp_path / "projects", "public-accompaniment")
    workflow.manifest.stage(1).status = "reviewed"
    workflow.manifest.stage(1).artifacts = [Artifact.from_file(source, "source", "audio/wav")]
    workflow.manifest.stage(2).status = "ready"
    workflow.manifest.save()
    monkeypatch.setattr("stemscore.inventory.LocalRuntime.validate_core", lambda _runtime: None)
    monkeypatch.setattr("stemscore.workflow.msst.model_available", lambda _runtime, _model: False)
    monkeypatch.setattr(
        "stemscore.workflow.msst.model_evidence",
        lambda _runtime, model: {"name": model.name, "backend": "MSST"},
    )

    def fake_separate_all(_runtime, _input_path, output_dir, _model, _log):
        output_dir.mkdir(parents=True, exist_ok=True)
        result = {}
        for stem in (*PUBLIC_MEGA53_VOCAL_STEMS, "drums", "bass", "piano"):
            path = output_dir / f"track_{stem}.wav"
            shutil.copy2(source, path)
            result[stem] = path
        return result, ["msst", "inference"]

    mix_calls: list[list[str]] = []

    def fake_mix_audio(sources, target, _log):
        mix_calls.append([path.stem.removeprefix("track_") for path in sources])
        shutil.copy2(source, target)
        return ["mix"]

    monkeypatch.setattr("stemscore.workflow.msst.separate_all", fake_separate_all)
    monkeypatch.setattr("stemscore.workflow.mix_audio", fake_mix_audio)
    monkeypatch.setattr(
        "stemscore.workflow.subtract_audio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stage 2 must not subtract vocals from source")
        ),
    )

    workflow.run_stage(2)

    assert set(mix_calls[0]) == set(PUBLIC_MEGA53_VOCAL_STEMS)
    assert set(mix_calls[1]) == {"drums", "bass", "piano"}
    assert not (set(mix_calls[1]) & set(PUBLIC_MEGA53_VOCAL_STEMS))


def test_public_stage2_reuses_complete_53_stem_cache_on_retry(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "song.wav"
    write_audible_wav(source, frames=2205)
    workflow = Workflow.create(source, tmp_path / "projects", "public-cache-retry")
    workflow.manifest.stage(1).status = "reviewed"
    workflow.manifest.stage(1).artifacts = [Artifact.from_file(source, "source", "audio/wav")]
    workflow.manifest.stage(2).status = "ready"
    cache = workflow.root / "audit" / "mega53-stems"
    cache.mkdir(parents=True, exist_ok=True)
    names = list(PUBLIC_MEGA53_VOCAL_STEMS) + [f"instrument-{index:02d}" for index in range(50)]
    for name in names:
        shutil.copy2(source, cache / f"track_{name}.wav")
    workflow.manifest.save()

    monkeypatch.setattr("stemscore.inventory.LocalRuntime.validate_core", lambda _runtime: None)
    monkeypatch.setattr("stemscore.workflow.msst.model_available", lambda _runtime, _model: False)
    monkeypatch.setattr(
        "stemscore.workflow.msst.model_evidence",
        lambda _runtime, model: {"name": model.name, "backend": "MSST"},
    )
    monkeypatch.setattr(
        "stemscore.workflow.msst.separate_all",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete cache should be reused")
        ),
    )

    workflow.run_stage(2)

    assert workflow.manifest.stage(2).status == "review_required"
    assert workflow.manifest.stage(2).commands[0][0] == "reuse-mega53-cache"


def test_manifest_roundtrip_and_hash_verification(tmp_path: Path) -> None:
    source = tmp_path / "song.wav"
    source.write_bytes(b"audio")
    project = tmp_path / "project"
    manifest = ProjectManifest.create("id", "title", project, source, {})
    artifact = Artifact.from_file(source, "source", "audio/wav")
    manifest.stage(1).artifacts.append(artifact)
    manifest.save()
    loaded = ProjectManifest.load(project / "stemscore-project.json")
    assert loaded.stage(1).artifacts[0].verify() == (True, "")
    source.write_bytes(b"changed")
    assert loaded.stage(1).artifacts[0].verify()[0] is False


def test_review_gate_unlocks_exactly_next_stage(tmp_path: Path) -> None:
    source = tmp_path / "song.wav"
    source.write_bytes(b"audio")
    project = tmp_path / "project"
    manifest = ProjectManifest.create("id", "title", project, source, {})
    (project / "stage.txt").parent.mkdir(parents=True, exist_ok=True)
    evidence = project / "stage.txt"
    evidence.write_text("ok", encoding="utf-8")
    manifest.stage(1).status = "review_required"
    manifest.stage(1).artifacts = [Artifact.from_file(evidence, "evidence", "text/plain")]
    manifest.save()
    workflow = Workflow(manifest)
    workflow.approve(1)
    assert workflow.manifest.stage(1).status == "reviewed"
    assert workflow.manifest.stage(2).status == "ready"
    assert workflow.manifest.stage(3).status == "locked"


def test_modified_artifact_cannot_be_approved(tmp_path: Path) -> None:
    source = tmp_path / "song.wav"
    source.write_bytes(b"audio")
    project = tmp_path / "project"
    manifest = ProjectManifest.create("id", "title", project, source, {})
    evidence = project / "result.wav"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"first")
    manifest.stage(1).status = "review_required"
    manifest.stage(1).artifacts = [Artifact.from_file(evidence, "source", "audio/wav")]
    workflow = Workflow(manifest)
    evidence.write_bytes(b"second")
    with pytest.raises(RuntimeError, match="校验失败"):
        workflow.approve(1)


def test_drum_midi_and_merge(tmp_path: Path) -> None:
    drums = tmp_path / "drums.mid"
    piano = tmp_path / "piano.mid"
    merged = tmp_path / "all.mid"
    metadata = write_drum_midi(
        drums,
        [{"time": 0.0, "duration": 0.1, "note": 36, "velocity": 100}],
        120.0,
    )
    write_midi(piano)
    result = merge_midi_files([("drums", drums), ("piano", piano)], merged, 120.0)
    assert metadata["notes"] == 1
    assert result["tracks"] == 2
    assert result["notes"] == 2
    assert len(mido.MidiFile(merged).tracks) == 3


def test_empty_midi_is_explicit_and_mergeable(tmp_path: Path) -> None:
    empty = tmp_path / "silent_piano.mid"
    merged = tmp_path / "merged.mid"
    metadata = write_empty_midi(empty, "piano", 120.0)
    result = merge_midi_files([("piano", empty)], merged, 120.0)
    assert metadata["notes"] == 0
    assert result["notes"] == 0
    assert len(mido.MidiFile(merged).tracks) == 2


def test_normalize_quantizes_pitched_notes_to_sixteenth_grid(tmp_path: Path) -> None:
    path = tmp_path / "guitar.mid"
    midi = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    track.append(mido.Message("note_on", note=60, velocity=90, time=37))
    track.append(mido.Message("note_off", note=60, velocity=0, time=91))
    midi.tracks.append(track)
    midi.save(path)
    metadata = normalize_single_track(path, "guitar", 120.0)
    normalized = mido.merge_tracks(mido.MidiFile(path).tracks)
    note_events = [message for message in normalized if message.type in {"note_on", "note_off"}]
    assert metadata["quantized_grid"] == "1/16"
    assert note_events[0].time % 120 == 0
    assert note_events[1].time >= 120


def test_dataset_catalog_finds_paired_audio_and_midi(tmp_path: Path) -> None:
    write_wav(tmp_path / "song.wav")
    write_midi(tmp_path / "song.mid")
    output = tmp_path / "catalog.json"
    payload = build_catalog([tmp_path], output)
    assert payload["counts"]["paired"] == 1
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["groups"][0]["quality"] == "paired"
    assert all(len(item["sha256"]) == 64 for item in loaded["groups"][0]["files"])


def test_dataset_catalog_groups_ableton_project_subdirectories(tmp_path: Path) -> None:
    project = tmp_path / "Demo Project"
    project.mkdir()
    (project / "Demo.als").write_bytes(b"ableton")
    samples = project / "Samples"
    midi = project / "MIDI"
    samples.mkdir()
    midi.mkdir()
    write_wav(samples / "bass.wav")
    write_wav(samples / "drums.wav")
    write_midi(midi / "notes.mid")
    output = tmp_path / "catalog.json"
    payload = build_catalog([tmp_path], output)
    assert payload["counts"]["candidate"] == 1
    assert payload["groups"][0]["directory"] == str(project)
    assert len(payload["groups"][0]["files"]) == 4

