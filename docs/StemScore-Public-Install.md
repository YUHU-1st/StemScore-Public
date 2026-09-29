# StemScore 公开版安装

StemScore 公开版包含同一套五步客户端、RTX 40/50 运行时选择逻辑、训练 WebUI、音乐分析、模型管理和本地 Music 3 提示词功能，但应用 ZIP 本身不包含模型权重或预装 Python/CUDA 运行时。RC3 在客户端内加入“一键修复运行环境”：缺失组件时才从固定的官方公开源下载许可明确的依赖和模型，并做 SHA-256 校验。

RC4 不再要求用户严格按 1→5 的审查节奏操作：可以显式跳过当前步骤，也可以直接运行已经具备真实上游文件的指定步骤。第 5 步尤其不再强制等待第 4 步全部完成；未去混响或尚未生成的角色会从第 3 步原分轨补齐。对包含少量损坏帧但仍能生成有效 WAV 的 FLAC/其他音频，FFmpeg 会记录警告并继续，而不是因为单个坏帧直接终止整个项目。

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

## 一键修复运行环境

全新机器不需要预先安装 MSST 或 UVR。部署并启动 StemScore 后，如果运行到 AI 分离步骤时检测到本地运行环境不完整，左侧“一键修复运行环境”按钮会变为可用。点击后会自动：

- 检测 RTX 40/50 并选择对应的官方 PyTorch CUDA wheel；
- 若系统缺少 FFmpeg，则通过 Windows WinGet 的 `Gyan.FFmpeg` 包安装并重新检测；
- 安装官方 `msst==0.1.0` 与 BS-RoFormer 依赖；
- 从 ZFTurbo 官方 GitHub Release 下载 MVSep Mega 53-stem v1 配置和权重，并核对固定 SHA-256；
- 安装 Spotify Basic Pitch 0.4.0 与 TransKun 2.0.1；
- 修复完成后自动重试刚才失败的步骤。

RC3 不再要求 UVR/Demucs 才能执行当前五步主流程。公开版仍不会自动下载旧的 BS-RoFormer Resurrection、Karaoke Frazer/Becruily、BS-RoFormer SW Fixed、Dereverb/Echo Fused 或 Demucs `htdemucs_6s`；这些旧权重的许可边界保持不变。若没有用户自行准备的许可兼容去混响权重，第 4 步会明确记录“保留原轨”，而不是暗中下载受限模型。

`setup_runtime.ps1` 仍保留为手工维护入口；普通用户优先使用客户端内的一键修复。脚本自身仍只处理 Basic Pitch/TransKun，不会下载旧的受限分离权重：

```powershell
# 纯新机：先安装 Basic Pitch；TransKun 会明确提示暂时跳过
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_runtime.ps1 `
  -RuntimeRoot "$env:LOCALAPPDATA\StemScore\transcription\basic-pitch"

# 已准备合法 MSST 运行时后，再补齐 TransKun
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_runtime.ps1 `
  -MsstRoot "C:\你的MSST目录" `
  -RuntimeRoot "$env:LOCALAPPDATA\StemScore\transcription\basic-pitch"
```

## 部署

RC3 的公开包可以直接部署，不需要先传入外部运行时路径：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Deploy-StemScoreGpuPackage.ps1 `
  -Destination "$env:LOCALAPPDATA\StemScore"
```

部署器会自动读取 NVIDIA 型号、Compute Capability 与驱动版本，选择 RTX 40 `cu126` 或 RTX 50 `cu128` profile，并检查应用包。如果公开包没有预装运行时，`deployment-state.json` 会写入 `runtimeRepairRequired=true`，客户端仍可正常启动并在需要时执行一键修复。已有合法私有环境的用户可传 `-MsstRoot` 与 `-TranscriptionRoot`；`-UvrRoot` 仅为旧环境兼容保留，RC3 不再要求它存在。

部署成功后运行：

```powershell
& "$env:LOCALAPPDATA\StemScore\app\StemScore.exe"
```

RTX 50 已在 Compute Capability 12.0 设备上完成真实 CUDA 和五步回归。RTX 40 profile 依据 NVIDIA/PyTorch 兼容资料、固定环境、分卷重组和模拟 CC 8.9 选择验证，但尚无 RTX 40 实机 CUDA 回归证据；公开版保持这个边界，不声称已经实机通过。

## 可选本地分析模型

客户端的模型管理页可按显存推荐并下载许可明确的 CLAP、Qwen2.5 GGUF 与 llama.cpp。下载保存在安装目录的 `models` 子目录，支持进度、速度、暂停、继续、断点续传、网络重试和 SHA-256 校验。它们只用于可选曲风/配器分析和提示词增强，不替代核心分轨模型。

## 许可

公开可见不等于获得模型使用或再分发许可。用户必须分别遵守应用依赖、模型、音乐和训练数据的条款；不得把本机导入的权重再次上传到本项目的公开 Release。
