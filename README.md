# StemScore 本地 AI 扒谱

StemScore 是完全本地运行的 Windows 客户端，复用用户已有且有权使用的 MSST-GUI / UVR 环境，按五步人工审查流程完成：音乐导入 → 人声/伴奏 → 主唱/和声/乐器分轨 → 选择性去混响 → 各轨 MIDI。每一步保存命令、模型与产物 SHA-256、参数和日志，并在用户点击审查通过前停止。

`1.0.0-public-rc8` 是可公开分发的核心版。应用包本身不携带模型权重或预装 Python/CUDA runtime；缺少运行环境时，客户端会提供“一键修复运行环境”，从固定的官方公开源安装 FFmpeg、MSST、GPU 运行时、MIT 许可的 MVSep Mega 53-stem v1、Basic Pitch 与 TransKun，并在固定下载资产上执行校验。完整离线私有模型包仍因部分旧权重未声明再分发许可、仅限研究或仅限非商业使用而保持私有。

RC3 增加运行时报错后的自动修复：全新 Windows 机器可以先安装并启动公开客户端，在首次遇到缺失运行环境时一键补齐合法公开组件，完成后自动重试失败步骤。当前五步主流程已经不再把未使用的 UVR/Demucs 目录当作硬依赖。

RC4 进一步简化实际使用：损坏但仍可解码的 FLAC/音频会尽量跳过坏帧继续处理；步骤状态会在任务线程启动前立即显示“处理中”；第 4 步未选去混响的分轨保持原名称；第 5 步可在第 4 步未完全结束时直接使用第 3 步分轨；界面新增“跳过当前步骤”和“直接运行所选步骤”。

RC6 修复大体积 53-stem 推理占满系统盘的问题：原始 53 个 WAV 直接写入项目目录，临时输入也跟随项目磁盘；运行前会检查项目盘剩余空间。外部处理程序失败时，界面同时显示关键错误行，不再只有“退出码为 1”。

RC8 修复 RC7 的响度失真：Mega53 的父级/子级预测轨会重叠，不能把 50 轨当作原始伴奏相加。第 2 步优先使用用户自行安装的双输出人声/伴奏模型，其次使用已有专用人声模型；公开版只有 Mega53 时只取 `vocal` 轨，并从原混音计算伴奏残差，写入 PCM 前检查浮点峰值和平均电平。第 3 步同样不再叠加重叠标签。独立音乐分析与 Music 3 提示词无需先完成五步流程，打开客户端即可选择歌曲生成。

## 主要功能

- MSST 模型兼容与 RTX 40 `cu126`、RTX 50 `cu128` profile 自动选择；旧的用户自备 MSST 模型仍可继续使用。
- 主唱、和声、bass、drums、guitar、piano、other 分轨，按角色选择性去混响并进行响度、高频和瞬态质量门判断。
- 钢琴使用 TransKun V2，其他有音高轨使用 Basic Pitch，鼓使用本地确定性起音算法；逐轨输出 WAV 与 MIDI。
- BPM、调性置信度、曲风、配器、编曲结构和响度报告。
- 按 MiniMax Music 3 三段规范生成可交给本地 ComfyUI 的提示词和 JSON；可选 Qwen2.5 与 llama.cpp 全程本地运行。
- 模型管理支持按显存推荐、安装目录存储、进度/速度、暂停/继续、断点续传、网络重试和 SHA-256 校验。
- 训练中心扫描本地音频、MIDI 与 DAW 工程，整理候选配对数据并显示可视化训练进度；扫描不会自动开始训练。

## 安装

下载公开 Release 的全部资产到同一目录，然后按照 [公开版安装文档](docs/StemScore-Public-Install.md) 校验并部署。RC3 不再要求部署前先准备 MSST/UVR；首次运行到需要 AI 分离的步骤时，可直接点击“一键修复运行环境”。公开部署命令示例：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Deploy-StemScoreGpuPackage.ps1 `
  -Destination "$env:LOCALAPPDATA\StemScore"
```

如果用户已经有合法的旧版私有运行环境，仍可显式传入 `-MsstRoot` 与 `-TranscriptionRoot` 继续复用；`-UvrRoot` 在 RC3 中仅为兼容旧部署而保留，不再是当前五步流程硬依赖。公开 Release 的 `release-manifest.json` 继续明确记录 `publicSafe=true`、`runtimePayloadsIncluded=false` 和 `coreModelWeightsIncluded=false`；一键修复下载发生在用户本机运行阶段，不把权重重新打进公开应用 ZIP。

## 从源码运行与验证

```powershell
py -3.12 -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt pytest pyinstaller
.\.venv312\Scripts\python.exe -m pytest -q
.\.venv312\Scripts\python.exe -m stemscore.app
```

公开构建：

```powershell
.\build_stemscore.ps1 -Version 1.0.0-public-rc8
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\Build-StemScoreGpuRelease.ps1 `
  -Version 1.0.0-public-rc8 -PublicSafe
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
