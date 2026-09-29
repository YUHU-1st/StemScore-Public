from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="PowerShell release scripts are Windows-only")


def _powershell(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _write_fake_runtime(root: Path) -> None:
    files = [
        root / "msst" / "env" / "python.exe",
        root / "msst" / "inference.py",
        root / "msst" / "pretrain" / "BS-Roformer-Resurrection.ckpt",
        root / "msst" / "pretrain" / "bs_roformer_karaoke_frazer_becruily.ckpt",
        root / "msst" / "pretrain" / "BS-Rofo-SW-Fixed.ckpt",
        root / "msst" / "pretrain" / "dereverb_echo_mbr_fused_0.5_v2_0.25_big_0.25_super.ckpt",
        root / "uvr" / "models" / "Demucs_Models" / "v3_v4_repo" / "htdemucs_6s.yaml",
        root / "uvr" / "models" / "Demucs_Models" / "v3_v4_repo" / "5c90dfd2-34c22ccb.th",
        root / "transcription" / "basic-pitch" / ".venv" / "Lib" / "site-packages" / "basic_pitch" / "__init__.py",
        root / "transcription" / "basic-pitch" / ".venv" / "Lib" / "site-packages" / "basic_pitch" / "saved_models" / "icassp_2022" / "nmp.onnx",
        root / "transcription" / "python-base" / "python.exe",
        root / "transcription" / "transkun-packages" / "transkun" / "pretrained" / "2.0.pt",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((path.name + "\n").encode())


def _write_fake_external_runtime(root: Path) -> tuple[Path, Path, Path]:
    _write_fake_runtime(root)
    extra = [
        root / "msst" / "configs" / "BS-Roformer-Resurrection-Config.yaml",
        root / "msst" / "configs" / "config_karaoke_frazer_becruily.yaml",
        root / "msst" / "configs" / "BS-Rofo-SW-Fixed.yaml",
        root / "msst" / "configs" / "config_dereverb_echo_mbr_v2.yaml",
        root / "transcription" / "basic-pitch" / ".venv" / "Scripts" / "python.exe",
        root / "transcription" / "transkun-packages" / "transkun" / "pretrained" / "2.0.conf",
    ]
    for path in extra:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((path.name + "\n").encode())
    return (
        root / "msst",
        root / "uvr",
        root / "transcription" / "basic-pitch",
    )


def _write_fake_nvidia_smi(directory: Path) -> None:
    script = directory / "nvidia-smi.cmd"
    script.write_text(
        "@echo off\r\n"
        "echo 0, NVIDIA GeForce RTX 5080 Laptop GPU, 12.0, 610.62\r\n",
        encoding="ascii",
    )


def test_release_builder_and_deployer_install_runtime_payload(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    build_script = repository / "packaging" / "Build-StemScoreGpuRelease.ps1"
    deploy_script_name = "Deploy-StemScoreGpuPackage.ps1"

    application = tmp_path / "StemScore"
    application.mkdir()
    (application / "StemScore.exe").write_bytes(b"fake-app")

    runtime_root = tmp_path / "runtimes"
    _write_fake_runtime(runtime_root / "rtx50-cu128")
    release = tmp_path / "release"

    _powershell(
        build_script,
        "-Version",
        "1.0.0-test",
        "-SourceApplication",
        str(application),
        "-OutputDirectory",
        str(release),
        "-RuntimePayloadRoot",
        str(runtime_root),
    )

    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8-sig"))
    profile = next(item for item in manifest["profiles"] if item["profileId"] == "rtx50-cu128")
    assert profile["runtimeIncluded"] is True
    assert profile["coreModelWeightsIncluded"] is True
    assert profile["optionalAnalysisModelsIncluded"] is False
    assert profile["launchReady"] is True
    assert profile["partCount"] >= 1
    parts = [
        item
        for item in manifest["assets"]
        if item["kind"] == "runtime-payload-part" and item["profileId"] == "rtx50-cu128"
    ]
    assert len(parts) == profile["partCount"]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_nvidia_smi(fake_bin)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    destination = tmp_path / "installed"
    _powershell(
        release / deploy_script_name,
        "-ReleaseDirectory",
        str(release),
        "-Destination",
        str(destination),
        env=env,
    )

    state = json.loads((destination / "deployment-state.json").read_text(encoding="utf-8-sig"))
    assert state["selectedProfile"] == "rtx50-cu128"
    assert state["runtimePayloadIncluded"] is True
    assert state["coreModelWeightsIncluded"] is True
    assert state["optionalAnalysisModelsIncluded"] is False
    assert state["launchReady"] is True
    runtime = destination / "runtimes" / "rtx50-cu128"
    assert (runtime / "msst" / "env" / "python.exe").is_file()
    assert (runtime / "transcription" / "python-base" / "python.exe").is_file()


def test_release_builder_keeps_metadata_only_profiles_not_launch_ready(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    build_script = repository / "packaging" / "Build-StemScoreGpuRelease.ps1"
    application = tmp_path / "StemScore"
    application.mkdir()
    (application / "StemScore.exe").write_bytes(b"fake-app")
    release = tmp_path / "release"

    _powershell(
        build_script,
        "-Version",
        "1.0.0-test",
        "-SourceApplication",
        str(application),
        "-OutputDirectory",
        str(release),
    )

    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["runtimePayloadsIncluded"] is False
    assert manifest["coreModelWeightsIncluded"] is False
    assert manifest["optionalAnalysisModelsIncluded"] is False
    assert manifest["payloads"]["launchReady"] is False
    assert all(profile["runtimeIncluded"] is False for profile in manifest["profiles"])
    assert all(profile["coreModelWeightsIncluded"] is False for profile in manifest["profiles"])
    assert all(profile["optionalAnalysisModelsIncluded"] is False for profile in manifest["profiles"])
    assert not any(asset["kind"] == "runtime-payload-part" for asset in manifest["assets"])


def test_public_safe_release_binds_user_supplied_runtime(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    build_script = repository / "packaging" / "Build-StemScoreGpuRelease.ps1"
    application = tmp_path / "StemScore"
    application.mkdir()
    (application / "StemScore.exe").write_bytes(b"fake-app")
    release = tmp_path / "release"

    _powershell(
        build_script,
        "-Version",
        "1.0.0-public-test",
        "-SourceApplication",
        str(application),
        "-OutputDirectory",
        str(release),
        "-PublicSafe",
    )

    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["publicSafe"] is True
    assert manifest["runtimePayloadsIncluded"] is False
    assert manifest["coreModelWeightsIncluded"] is False
    assert {item["kind"] for item in manifest["assets"]} >= {
        "public-install-guide",
        "third-party-notices",
        "transcription-runtime-setup",
    }
    assert (release / "PUBLIC_RELEASE_README.md").is_file()
    assert (release / "THIRD_PARTY_NOTICES.md").is_file()
    assert (release / "setup_runtime.ps1").is_file()

    msst, uvr, transcription = _write_fake_external_runtime(tmp_path / "external")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_nvidia_smi(fake_bin)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    destination = tmp_path / "installed"
    _powershell(
        release / "Deploy-StemScoreGpuPackage.ps1",
        "-ReleaseDirectory",
        str(release),
        "-Destination",
        str(destination),
        "-MsstRoot",
        str(msst),
        "-UvrRoot",
        str(uvr),
        "-TranscriptionRoot",
        str(transcription),
        env=env,
    )

    state = json.loads((destination / "deployment-state.json").read_text(encoding="utf-8-sig"))
    assert state["runtimePayloadIncluded"] is False
    assert state["coreModelWeightsIncluded"] is False
    assert state["launchReady"] is True
    assert Path(state["externalRuntimeRoots"]["msst"]) == msst
    assert Path(state["externalRuntimeRoots"]["uvr"]) == uvr
    assert Path(state["externalRuntimeRoots"]["transcription"]) == transcription


def test_public_safe_deployer_rejects_missing_runtime_binding(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    build_script = repository / "packaging" / "Build-StemScoreGpuRelease.ps1"
    application = tmp_path / "StemScore"
    application.mkdir()
    (application / "StemScore.exe").write_bytes(b"fake-app")
    release = tmp_path / "release"

    _powershell(
        build_script,
        "-Version",
        "1.0.0-public-test",
        "-SourceApplication",
        str(application),
        "-OutputDirectory",
        str(release),
        "-PublicSafe",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_nvidia_smi(fake_bin)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    destination = tmp_path / "installed"

    with pytest.raises(subprocess.CalledProcessError) as error:
        _powershell(
            release / "Deploy-StemScoreGpuPackage.ps1",
            "-ReleaseDirectory",
            str(release),
            "-Destination",
            str(destination),
            env=env,
        )

    assert "No runnable runtime is available" in error.value.stderr
    assert not destination.exists()


def test_transcription_setup_does_not_require_msst_before_basic_pitch() -> None:
    repository = Path(__file__).resolve().parents[1]
    script = (repository / "tools" / "setup_stemscore_runtime.ps1").read_text(encoding="utf-8")
    assert "[Parameter(Mandatory)]" not in script
    assert "Basic Pitch is ready; TransKun was skipped" in script


def test_public_safe_release_rejects_model_weights_and_runtime_payload(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    build_script = repository / "packaging" / "Build-StemScoreGpuRelease.ps1"
    application = tmp_path / "StemScore"
    application.mkdir()
    (application / "StemScore.exe").write_bytes(b"fake-app")
    (application / "forbidden.onnx").write_bytes(b"model")

    with pytest.raises(subprocess.CalledProcessError):
        _powershell(
            build_script,
            "-Version",
            "1.0.0-public-test",
            "-SourceApplication",
            str(application),
            "-OutputDirectory",
            str(tmp_path / "release-with-model"),
            "-PublicSafe",
        )

    (application / "forbidden.onnx").unlink()
    runtime_root = tmp_path / "runtimes"
    runtime_root.mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        _powershell(
            build_script,
            "-Version",
            "1.0.0-public-test",
            "-SourceApplication",
            str(application),
            "-OutputDirectory",
            str(tmp_path / "release-with-runtime"),
            "-RuntimePayloadRoot",
            str(runtime_root),
            "-PublicSafe",
        )


def test_rtx40_prepare_plan_pins_compatible_cu126_components(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    profiles = json.loads((repository / "packaging" / "runtime-profiles.json").read_text(encoding="utf-8"))
    profile = next(item for item in profiles["runtimeProfiles"] if item["id"] == "rtx40-cu126")
    assert profile["torch"] == {
        "version": "2.12.1",
        "torchvisionVersion": "0.27.1",
        "torchaudioVersion": "2.11.0",
        "pythonVersion": "3.10",
        "cuda": "12.6",
        "requiredWheelArchitecture": "8.6",
        "indexUrl": "https://download.pytorch.org/whl/cu126",
    }

    output_root = tmp_path / "payloads"
    msst_environment = Path("C:/msst-env")
    result = _powershell(
        repository / "packaging" / "Prepare-StemScoreRuntimePayload.ps1",
        "-ProfileId",
        "rtx40-cu126",
        "-OutputRoot",
        str(output_root),
        "-MsstEnvironmentRoot",
        str(msst_environment),
        "-PlanOnly",
    )
    assert "2.12.1" in result.stdout
    assert "0.27.1" in result.stdout
    assert "2.11.0" in result.stdout
    assert "8.6" in result.stdout
    assert str(msst_environment) in result.stdout
    assert "12.6" in result.stdout
    assert not (output_root / "rtx40-cu126").exists()

