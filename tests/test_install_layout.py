from __future__ import annotations

from pathlib import Path
import json
import os
import sys

from stemscore.model_manager import software_install_directory
from stemscore.inventory import LocalRuntime


def test_deployed_app_uses_shared_install_root_for_models(
    tmp_path: Path, monkeypatch
) -> None:
    application = tmp_path / "StemScore" / "app"
    application.mkdir(parents=True)
    (application.parent / "deployment-state.json").write_text("{}", encoding="utf-8")
    executable = application / "StemScore.exe"
    executable.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert software_install_directory() == application.parent.resolve()


def test_portable_app_keeps_models_beside_executable(
    tmp_path: Path, monkeypatch
) -> None:
    application = tmp_path / "portable"
    application.mkdir()
    executable = application / "StemScore.exe"
    executable.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert software_install_directory() == application.resolve()


def test_launch_ready_deployment_uses_selected_profile_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    deployment = tmp_path / "StemScore"
    application = deployment / "app"
    application.mkdir(parents=True)
    (deployment / "deployment-state.json").write_text(
        '{"launchReady":true,"expectedRuntimeDirectory":"runtimes/rtx50-cu128"}',
        encoding="utf-8",
    )
    executable = application / "StemScore.exe"
    executable.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    runtime = LocalRuntime.defaults()

    root = deployment / "runtimes" / "rtx50-cu128"
    assert runtime.msst_root == root / "msst"
    assert runtime.uvr_root == root / "uvr"
    assert runtime.transcription_root == root / "transcription" / "basic-pitch"


def test_public_deployment_uses_bound_external_runtime(tmp_path: Path, monkeypatch) -> None:
    deployment = tmp_path / "StemScore"
    application = deployment / "app"
    application.mkdir(parents=True)
    roots = {
        "msst": str(tmp_path / "external" / "msst"),
        "uvr": str(tmp_path / "external" / "uvr"),
        "transcription": str(tmp_path / "external" / "transcription"),
    }
    (deployment / "deployment-state.json").write_text(
        json.dumps({"launchReady": True, "externalRuntimeRoots": roots}),
        encoding="utf-8",
    )
    executable = application / "StemScore.exe"
    executable.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    runtime = LocalRuntime.defaults()

    assert runtime.msst_root == Path(roots["msst"])
    assert runtime.uvr_root == Path(roots["uvr"])
    assert runtime.transcription_root == Path(roots["transcription"])


def test_runtime_activation_prepends_bundled_ffmpeg_directory(tmp_path: Path, monkeypatch) -> None:
    runtime = LocalRuntime(tmp_path / "msst", tmp_path / "uvr", tmp_path / "transcription")
    runtime_bin = runtime.msst_root / "env"
    runtime_bin.mkdir(parents=True)
    (runtime_bin / "ffmpeg.exe").write_bytes(b"")
    (runtime_bin / "ffprobe.exe").write_bytes(b"")
    monkeypatch.setenv("PATH", str(tmp_path / "other"))

    runtime.activate()

    assert os.environ["PATH"].split(os.pathsep)[0] == str(runtime_bin)
