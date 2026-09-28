# StemScore 本地 AI 扒谱

StemScore 是完全本地运行的 Windows 客户端，复用用户已有且有权使用的 MSST-GUI / UVR 环境，按五步人工审查流程完成：音乐导入 → 人声/伴奏 → 主唱/和声/乐器分轨 → 选择性去混响 → 各轨 MIDI。每一步保存命令、模型与产物 SHA-256、参数和日志，并在用户点击审查通过前停止。

`1.0.0-public-rc1` 是可公开分发的核心版。它不携带任何模型权重或预装 Python/CUDA runtime；部署时绑定本机 MSST、UVR 和转写目录。完整离线模型包因部分权重未声明再分发许可、仅限研究或仅限非商业使用而继续保留在独立私有仓库分支和私有 Release 中。

## 主要功能

- MSST/UVR 模型兼容与 RTX 40 `cu126`、RTX 50 `cu128` profile 自动选择。
- 主唱、和声、bass、drums、guitar、piano、other 分轨，按角色选择性去混响并进行响度、高频和瞬态质量门判断。
- 钢琴使用 TransKun V2，其他有音高轨使用 Basic Pitch，鼓使用本地确定性起音算法；逐轨输出 WAV 与 MIDI。
- BPM、调性置信度、曲风、配器、编曲结构和响度报告。
- 按 MiniMax Music 3 三段规范生成可交给本地 ComfyUI 的提示词和 JSON；可选 Qwen2.5 与 llama.cpp 全程本地运行。
- 模型管理支持按显存推荐、安装目录存储、进度/速度、暂停/继续、断点续传、网络重试和 SHA-256 校验。
- 训练中心扫描本地音频、MIDI 与 DAW 工程，整理候选配对数据并显示可视化训练进度；扫描不会自动开始训练。

## 安装

下载公开 Release 的全部资产到同一目录，然后按照 [公开版安装文档](docs/StemScore-Public-Install.md) 校验、安装 Basic Pitch/TransKun，并绑定现有 MSST/UVR。公开部署命令示例：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Deploy-StemScoreGpuPackage.ps1 `
  -Destination "$env:LOCALAPPDATA\StemScore" `
  -MsstRoot "C:\你的MSST目录" `
  -UvrRoot "C:\你的UVR目录" `
  -TranscriptionRoot "$env:LOCALAPPDATA\StemScore\transcription\basic-pitch"
```

三个目录必须同时提供，部署器会校验必需文件。公开 Release 的 `release-manifest.json` 明确记录 `publicSafe=true`、`runtimePayloadsIncluded=false` 和 `coreModelWeightsIncluded=false`。

## 从源码运行与验证

```powershell
py -3.12 -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt pytest pyinstaller
.\.venv312\Scripts\python.exe -m pytest -q
.\.venv312\Scripts\python.exe -m stemscore.app
```

公开构建：

```powershell
.\build_stemscore.ps1 -Version 1.0.0-public-rc1
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\Build-StemScoreGpuRelease.ps1 `
  -Version 1.0.0-public-rc1 -PublicSafe
```

`-PublicSafe` 禁止传入 runtime payload，并拒绝应用目录内的常见模型权重文件。不要把自己导入的模型、音乐、训练数据、项目输出或运行日志提交到公开仓库或 Release。

## 验证边界

RTX 50 已在 GeForce RTX 5080 Laptop GPU、Compute Capability 12.0 上完成真实 CUDA 分离、五步流程和冻结客户端启动验证。RTX 40 profile 已完成官方兼容性核对、固定环境、静态自检、分卷部署和模拟 CC 8.9 选择，但尚缺 RTX 40 实机 CUDA 与五步回归，不能把该项标成实机通过。

详细文档：

- [用户手册](docs/StemScore-用户手册.md)
- [公开版安装](docs/StemScore-Public-Install.md)
- [可解释与复现](docs/StemScore-可解释与复现.md)
- [训练方案](docs/StemScore-训练方案.md)
- [音乐分析与 Music 3](docs/StemScore-音乐分析与Music3.md)
- [RTX 40/50 兼容与发布](docs/StemScore-RTX40-RTX50兼容与发布.md)
- [模型许可清单](docs/StemScore-模型许可清单.md)
- [第三方声明](THIRD_PARTY_NOTICES.md)

项目当前没有授予 StemScore 自有代码的开源许可证；公开可见不等于允许复制、修改或再分发。第三方组件和模型分别适用其自身许可证。
