from __future__ import annotations

import json
from pathlib import Path

from stemscore.manifest import Artifact, ProjectManifest
from stemscore.workflow import Workflow


def _workflow(tmp_path: Path) -> Workflow:
    source = tmp_path / "song.wav"
    source.write_bytes(b"source-audio")
    project = tmp_path / "project"
    manifest = ProjectManifest.create("analysis-test", "Analysis Test", project, source, {})
    manifest.stage(1).artifacts = [Artifact.from_file(source, "source", "audio/wav")]
    stem = project / "piano.wav"
    stem.parent.mkdir(parents=True, exist_ok=True)
    stem.write_bytes(b"piano-stem")
    manifest.stage(4).artifacts = [
        Artifact.from_file(
            stem,
            "piano_dry",
            "audio/wav",
            {"output_mean_volume_db": -18.0},
        )
    ]
    manifest.save()
    return Workflow(manifest)


def test_music_analysis_writes_reviewable_outputs_and_audit(
    tmp_path: Path, monkeypatch
) -> None:
    workflow = _workflow(tmp_path)

    def fake_analyze(_runtime, source, stems, log):
        assert source.name == "song.wav"
        assert [item.role for item in stems] == ["piano_dry"]
        log("analysis complete")
        return (
            {
                "schema_version": 1,
                "instrumentation": [{"role": "piano", "active": True}],
                "music3_caption": "### Global Metadata\nLocal.\n\n### Vocal Details\nInstrumental.\n\n### Arrangement\nPiano.\n",
                "markdown": "# 本地音乐分析报告\n",
            },
            ["local-python", "analyze_music.py"],
        )

    monkeypatch.setattr("stemscore.analysis.analyze_music", fake_analyze)
    artifacts = workflow.run_music_analysis()

    assert {artifact.role for artifact in artifacts} == {
        "music_analysis_json",
        "music_analysis_report",
        "minimax_music3_instructions",
        "comfyui_minimax_music3_input",
        "music_analysis_audit",
    }
    assert all(artifact.verify() == (True, "") for artifact in artifacts)
    output = tmp_path / "project" / "06_music_analysis"
    comfyui = json.loads(
        (output / "Analysis Test_comfyui-minimax-music3-input.json").read_text(encoding="utf-8")
    )
    assert comfyui["model"] == "MiniMax Music 3"
    assert comfyui["node"] == "MiniMaxMusic3TextEncode"
    assert comfyui["lyrics"].startswith("[Intro]")
    assert "### Global Metadata" in comfyui["caption"]
    assert comfyui["max_duration"] == 120.0
    audit = json.loads(
        (output / "Analysis Test_analysis-audit.json").read_text(encoding="utf-8")
    )
    assert audit["command"] == ["local-python", "analyze_music.py"]
    assert audit["source_preparation_command"] is None
    assert workflow.manifest.settings["music_analysis"]["artifacts"]


def test_music_analysis_runs_without_any_five_step_artifact(tmp_path: Path, monkeypatch) -> None:
    workflow = _workflow(tmp_path)
    workflow.manifest.stage(1).artifacts = []
    workflow.manifest.stage(4).artifacts = []
    workflow.manifest.save()

    def fake_convert(source, target, _log):
        target.write_bytes(source.read_bytes())
        return ["convert", str(source), str(target)]

    def fake_analyze(_runtime, source, stems, log):
        assert source.name == "source.wav"
        assert not stems
        return ({"instrumentation": [], "music3_caption": "caption", "markdown": "report"}, ["analyze"])

    monkeypatch.setattr("stemscore.workflow.convert_to_wav", fake_convert)
    monkeypatch.setattr("stemscore.analysis.analyze_music", fake_analyze)
    artifacts = workflow.run_music_analysis()

    assert len(artifacts) == 5
    assert workflow.manifest.stage(5).status != "reviewed"
    audit = json.loads((workflow.root / "06_music_analysis" / "Analysis Test_analysis-audit.json").read_text(encoding="utf-8"))
    assert audit["source"]["role"] == "source_original"
    assert audit["source_preparation_command"][0] == "convert"
