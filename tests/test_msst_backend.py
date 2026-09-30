from __future__ import annotations

from pathlib import Path
import shutil

from stemscore.backends import msst
from stemscore.inventory import LocalRuntime


def _runtime(tmp_path: Path) -> LocalRuntime:
    runtime = LocalRuntime(tmp_path / "runtime" / "MSST", tmp_path / "runtime" / "UVR", tmp_path / "runtime" / "transcription")
    for path in (
        runtime.msst_root / "env" / "Scripts" / "python.exe",
        runtime.msst_root / msst.PUBLIC_MEGA53_MODEL.config,
        runtime.msst_root / msst.PUBLIC_MEGA53_MODEL.checkpoint,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
    return runtime


def test_separate_all_keeps_large_scratch_on_project_volume(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    source = tmp_path / "source.wav"
    source.write_bytes(b"audio")
    output = tmp_path / "project" / "audit" / "mega53-stems"
    observed: dict[str, Path] = {}

    def fake_command(_runtime, _model, input_dir, raw_dir):
        observed["input_dir"] = input_dir
        observed["raw_dir"] = raw_dir
        return ["fake-msst"]

    def fake_run(_command, _log):
        raw_dir = observed["raw_dir"]
        raw_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, raw_dir / "track_lead-vocal.wav")

    monkeypatch.setattr(msst, "_inference_command", fake_command)
    monkeypatch.setattr(msst, "run_logged", fake_run)
    monkeypatch.setattr(
        msst.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(total=100 * 1024**3, used=10 * 1024**3, free=90 * 1024**3),
    )

    outputs, command = msst.separate_all(runtime, source, output, msst.PUBLIC_MEGA53_MODEL, lambda _message: None)

    assert command == ["fake-msst"]
    assert observed["raw_dir"] == output
    assert observed["input_dir"].parent.parent == output.parent
    assert outputs["lead-vocal"] == output / "track_lead-vocal.wav"


def test_separate_all_reports_project_disk_shortage_before_launch(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    source = tmp_path / "source.wav"
    source.write_bytes(b"x" * 1024)
    output = tmp_path / "project" / "audit" / "mega53-stems"
    monkeypatch.setattr(
        msst.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(total=1024, used=1024, free=0),
    )

    import pytest

    with pytest.raises(RuntimeError, match="项目所在磁盘空间不足"):
        msst.separate_all(runtime, source, output, msst.PUBLIC_MEGA53_MODEL, lambda _message: None)
