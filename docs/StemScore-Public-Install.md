# StemScore 公开版安装

StemScore 公开版包含同一套五步客户端、RTX 40/50 运行时选择逻辑、训练 WebUI、音乐分析、模型管理和本地 Music 3 提示词功能，但不包含 MSST、UVR、Basic Pitch、TransKun、CLAP、Qwen 或其他模型权重，也不包含预装 Python/CUDA 运行时。这样可以公开验证源码、应用核心和部署逻辑，而不会再次分发许可不明确、仅限研究或仅限非商业使用的权重。

## 下载与校验

从 GitHub Release 下载同一版本的全部资产到同一目录。至少需要：

- `StemScore-*-win64-core.zip`
- 与显卡匹配的 `*-profile.zip`
- `Deploy-StemScoreGpuPackage.ps1`
- `Select-StemScoreRuntime.ps1`
- `runtime-profiles.json`
- `release-manifest.json`
- `SHA256SUMS.txt`

先用 `Get-FileHash -Algorithm SHA256` 对照 `SHA256SUMS.txt`。部署脚本还会再次核对 `release-manifest.json` 中记录的字节数与 SHA-256。

## 准备本地运行环境

用户必须自行准备并确认有权使用的 MSST-GUI、UVR 和分离模型。公开版不会自动下载下列受限权重：BS-RoFormer Resurrection、Karaoke Frazer/Becruily、BS-RoFormer SW Fixed、Dereverb/Echo Fused 和 Demucs `htdemucs_6s`。所需文件名与许可边界见 `docs/StemScore-模型许可清单.md`。

Basic Pitch 与 TransKun 可通过公开包内的 `setup_runtime.ps1` 安装到本机；该脚本不下载 MSST/UVR 权重：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_runtime.ps1 `
  -MsstRoot "C:\你的MSST目录" `
  -RuntimeRoot "$env:LOCALAPPDATA\StemScore\transcription\basic-pitch"
```

## 一次部署并绑定现有模型

把三个根目录一次传给部署器。路径仅写入本机 `deployment-state.json`，不会上传：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Deploy-StemScoreGpuPackage.ps1 `
  -Destination "$env:LOCALAPPDATA\StemScore" `
  -MsstRoot "C:\你的MSST目录" `
  -UvrRoot "C:\你的UVR目录" `
  -TranscriptionRoot "$env:LOCALAPPDATA\StemScore\transcription\basic-pitch"
```

部署器会自动读取 NVIDIA 型号、Compute Capability 与驱动版本，选择 RTX 40 `cu126` 或 RTX 50 `cu128` profile，并检查应用包与本地运行目录。三个路径必须同时提供；必需文件缺失时部署会停止并列出缺失路径，不会把不完整安装标记为可运行。

部署成功后运行：

```powershell
& "$env:LOCALAPPDATA\StemScore\app\StemScore.exe"
```

RTX 50 已在 Compute Capability 12.0 设备上完成真实 CUDA 和五步回归。RTX 40 profile 依据 NVIDIA/PyTorch 兼容资料、固定环境、分卷重组和模拟 CC 8.9 选择验证，但尚无 RTX 40 实机 CUDA 回归证据；公开版保持这个边界，不声称已经实机通过。

## 可选本地分析模型

客户端的模型管理页可按显存推荐并下载许可明确的 CLAP、Qwen2.5 GGUF 与 llama.cpp。下载保存在安装目录的 `models` 子目录，支持进度、速度、暂停、继续、断点续传、网络重试和 SHA-256 校验。它们只用于可选曲风/配器分析和提示词增强，不替代核心分轨模型。

## 许可

公开可见不等于获得模型使用或再分发许可。用户必须分别遵守应用依赖、模型、音乐和训练数据的条款；不得把本机导入的权重再次上传到本项目的公开 Release。
