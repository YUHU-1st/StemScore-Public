# StemScore RTX 40 / RTX 50 兼容与公开发布

StemScore 使用一份共享应用内核，为 RTX 40 与 RTX 50 维护相互隔离的运行时 profile。PyTorch、CUDA 用户态 DLL 与自定义扩展不得在同一进程中混装。公开 Release 只发布应用核心、显卡选择器、部署器和 profile 元数据，不把 Python/CUDA runtime 或模型权重预装进应用 ZIP。RC3 可在用户点击“一键修复运行环境”后，从固定官方源下载许可明确的运行时与 MVSep Mega 53-stem v1，并在本机校验 SHA-256。

## 固定配置

| Profile | GPU / Compute Capability | PyTorch 组合 | CUDA wheel | Windows 驱动门槛 |
| --- | --- | --- | --- | --- |
| `rtx40-cu126` | RTX 40 / `8.9`；wheel 含 `sm_86` | torch 2.11.0 / torchvision 0.26.0 / torchaudio 2.11.0 | `cu126` | 建议 560.76 或更高；低于此值警告 |
| `rtx50-cu128` | RTX 50 / `12.0` (`sm_120`) | torch 2.8.0 / torchvision 0.23.0 / torchaudio 2.8.0 | `cu128` | 必须 572.30 或更高 |

机器可读配置位于 `packaging/runtime-profiles.json`。`cu126`/`cu128` 表示 PyTorch wheel 自带的 CUDA 用户态运行库；使用官方 wheel 不要求安装完整 CUDA Toolkit，但需要兼容的 NVIDIA 驱动。RTX 40 的官方 cu126 wheel 含 `sm_86` 而不含 `sm_89`；NVIDIA 同主版本二进制兼容规则允许面向 8.6 生成的 cubin 在 8.9 设备上运行，因此选择器验证 `sm_86`，不虚构 `sm_89` wheel。

依据：

- [NVIDIA CUDA GPU Compute Capability](https://developer.nvidia.com/cuda/gpus)
- [PyTorch 历史版本 Windows wheel 矩阵](https://docs.pytorch.org/get-started/previous-versions/)
- [PyTorch cu126 wheel 索引](https://download.pytorch.org/whl/cu126/torch/)
- [TorchAudio 稳定 ABI 说明](https://github.com/pytorch/audio/blob/main/docs/source/installation.rst)
- [NVIDIA CUDA 二进制兼容规则](https://docs.nvidia.com/cuda/cuda-programming-guide/01-introduction/cuda-platform.html)
- [CUDA 12.6 驱动与 minor-version compatibility 表](https://docs.nvidia.com/cuda/archive/12.6.0/cuda-toolkit-release-notes/index.html)

## 验证边界

RTX 50 已在 GeForce RTX 5080 Laptop GPU、Compute Capability 12.0、驱动 610.62 上完成 PyTorch CUDA 探测、BS-RoFormer 分离、最小 Demucs 六轨、完整五步流程和冻结应用启动验证。

RTX 40 profile 已固定 Python/PyTorch 版本并完成环境导入、`sm_86` 架构检查、发布分卷重组、模拟 CC 8.9 自动选择、空目录部署与 CPU 探测。测试机器是 RTX 5080，不能运行 cu126 的 RTX 40 CUDA 路径，因此仍缺 RTX 40 实机 CUDA 分离和完整五步回归。公开文档与清单始终保留 `targetHardwareInferencePerformed=false` 的边界。

## 选择 profile

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\Select-StemScoreRuntime.ps1
```

脚本使用 `nvidia-smi` 读取 GPU 名称、Compute Capability 和驱动版本。多显卡机器选择受支持且优先级最高的 profile；不支持的代际或低于 RTX 50 硬门槛的驱动会停止，而 RTX 40 低于建议线时只给出警告。

## 构建公开资产

先构建冻结客户端，再启用公开安全模式：

```powershell
.\build_stemscore.ps1 -Version 1.0.0-public-rc3
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\Build-StemScoreGpuRelease.ps1 `
  -Version 1.0.0-public-rc3 -PublicSafe
```

`-PublicSafe` 拒绝 `-RuntimePayloadRoot`，并扫描应用目录中的 `.ckpt`、`.pt`、`.pth`、`.th`、`.onnx`、`.safetensors` 和 `.gguf`。发布清单必须同时满足：

- `publicSafe=true`
- `runtimePayloadsIncluded=false`
- `coreModelWeightsIncluded=false`
- `optionalAnalysisModelsIncluded=false`

公开资产包含共享 core ZIP、两个 profile ZIP、部署器、选择器、机器可读清单、校验文件、安装说明、转写环境安装脚本和第三方声明。所有资产都低于 GitHub 单文件上限并记录 SHA-256。

## 部署与运行时修复

RC3 可以在没有现成 MSST/UVR 目录的机器上先部署客户端：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Deploy-StemScoreGpuPackage.ps1 `
  -Destination "$env:LOCALAPPDATA\StemScore"
```

部署器先验证 Release 资产并选择 GPU profile。若没有预装/外部 runtime，会写入 `runtimeRepairRequired=true` 并允许客户端启动；第一次遇到运行时缺失时，客户端提供一键修复。已有合法旧 runtime 的用户可用 `-MsstRoot` 与 `-TranscriptionRoot` 绑定；`-UvrRoot` 仅保留兼容性，不再是硬依赖。

权重许可和公开/私有边界见 `docs/StemScore-模型许可清单.md`。
