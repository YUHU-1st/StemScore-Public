# StemScore AI 扒谱用户手册

StemScore 是完全本地运行的 Windows 客户端。音频、模型输入、分轨和 MIDI 不上传到网络。客户端复用本机 MSST-GUI 与 UVR 模型，并为每一步保存命令、模型 SHA-256、输入输出 SHA-256、音频参数和日志。

## 首次安装

1. 准备自己有权使用的 MSST-GUI、UVR 与核心分离模型；公开包不包含也不自动下载这些权重。
2. 运行 `powershell -ExecutionPolicy Bypass -File .\tools\setup_stemscore_runtime.ps1 -MsstRoot "C:\你的MSST目录"`，在 `%LOCALAPPDATA%\StemScore\transcription` 安装许可明确的 Basic Pitch 与 TransKun。脚本不会修改或下载 MSST/UVR 权重。
3. 使用 `Deploy-StemScoreGpuPackage.ps1` 的 `-MsstRoot`、`-UvrRoot` 和 `-TranscriptionRoot` 参数一次绑定三个本地目录，详见 `StemScore-Public-Install.md`。
4. 源码运行：`.\.venv312\Scripts\python.exe -m stemscore.app`。部署包运行：`%LOCALAPPDATA%\StemScore\app\StemScore.exe`。

## 五步操作

### 1. 音乐导入

点击“新建项目”，选择 WAV、FLAC、MP3、OGG、M4A、AIFF 等音乐文件，再选择项目保存目录。程序生成 44.1 kHz、双声道、24-bit PCM WAV，同时建立本机模型清单。

完成通知出现后，双击源 WAV 试听，并检查 `audit/stage-1.json`。确认后点击“审查通过，开始下一步”。

### 2. 人声 / 伴奏

MSST 使用本机 `BS-Roformer-Resurrection.ckpt` 预测人声，并从混音中计算伴奏。输出位于 `02_vocal_accompaniment`。

重点检查主唱是否缺字、鼓和镲是否串入人声、伴奏是否残留明显人声。任何一步不满意，都保留当前文件后点“重试当前步骤”；不要先通过审查。

### 3. 主唱 / 和声 / 乐器分轨

人声轨由本机 Karaoke RoFormer 分成主唱和和声/叠唱；伴奏轨由 BS-RoFormer SW Fixed 六分轨模型分成 bass、drums、guitar、piano、other。输出位于 `03_stems`。旧 `htdemucs_6s` 仍保留作兼容后端，但不再作为默认 guitar/piano 模型。

逐轨试听。模型分类是概率判断，合成器、失真吉他和复合打击乐可能落入 `other`，这不是文件丢失；第 2 步原始伴奏仍保留。

### 4. 去混响

客户端左侧“第 4 步去混响角色”可逐项选择主唱、和声和各类乐器。默认只勾选主唱：和声及所有乐器直接复制第 3 步原分轨到 `04_dereverb`，供后续 MIDI 使用，不调用去混响模型；元数据记录 `bypassed_by_role_policy`。选择必须在第 4 步开始前完成，运行后会锁定以保证复现。

已勾选轨道会送入本机 MelBand-RoFormer。候选平均电平损失超过 6 dB、受保护角色在 6 kHz 以上相对能量损失超过 4 dB，或峰均比下降超过 3 dB 时，程序恢复原分轨；低于 −65 dB 的近静音轨直接跳过。响度用于发现主体被删，高频平衡用于发现发闷，峰均比用于发现鼓点、拨弦和辅音瞬态被磨平。自动门只能判定候选是否明显受损，不能证明艺术听感更好；用户仍应 A/B 检查齿音、尾音、镲片、和声层次和空间类合成器。

对 MIDI 而言，优先条件是分轨中目标乐器清晰、串音少、音高与起点完整；残留少量自然混响通常比被去混响模型削掉泛音和瞬态更安全。因此除非试听确认有必要，不建议为和声、鼓、钢琴、吉他、贝斯和 other 开启去混响。

### 5. 各轨 MIDI

钢琴使用本地 TransKun V2；主唱、和声、贝斯、吉他和 other 使用带专属置信度、音域及最短时值的本地 Spotify Basic Pitch；鼓轨使用确定性的多频带起音分类器。有音高 MIDI 量化到 1/16 拍；若原始拍速与 120 BPM 以下候选形成明确 2:1 关系，程序采用较慢候选，避免抒情曲被写成双倍速度谱面。平均电平低于 −65 dB 的轨生成带跳过原因的 0 音符 MIDI，不再把模型底噪变成假谱。每轨生成一个 MIDI，另生成 `all_tracks.mid`。

将 MIDI 导入 FL Studio、Ableton Live 或 Studio One，检查速度、音域、连音和鼓映射。自动扒谱不能替代最终人工校谱；程序记录转写参数与量化网格，便于回放和比较。

## 目录和命名

每个项目独立保存：

```text
项目名-随机ID/
  01_source/
  02_vocal_accompaniment/
  03_stems/
  04_dereverb/
  05_midi/
  audit/
  logs/
  stemscore-project.json
```

文件名包含项目名、步骤号和角色，不覆盖上一步。项目清单是恢复进度和复现实验的唯一入口，不要只移动单个文件。

## 训练中心

点击“训练中心”会启动只监听 `127.0.0.1` 的本机 WebUI。用户在页面中填写本机 Ableton 工程、Image-Line 用户目录或其他数据目录；扫描结果仅保留在本机，不放入源码仓库或发布包。目录工具把数据分成：

- `paired`：同目录、同名音频与 MIDI，可作为监督式候选；
- `candidate`：存在多轨音频与 MIDI/DAW 工程，必须人工确认对齐和标签；
- `unpaired`：只适合无监督、增广或等待补标签。

扫描本身不会开始训练。只有在人工确认音频与 MIDI 对齐、拥有训练权利、独立测试集显示稳定且可度量的域偏差时才应微调。训练前必须划分训练/验证/测试集并审查训练配方 JSON；WebUI 会解析训练日志中的百分比并显示进度、最近 200 行日志和终态。

## 故障恢复

- `failed`：查看 `logs/stage-N.log`，修复后点“重试当前步骤”。
- 文件被手工改动：审查按钮会检测 SHA-256 变化并拒绝继续。若改动是有意的，请新建项目或在代码层重新登记产物，不要绕过清单。
- CUDA 显存不足：关闭其他 GPU 程序后重试。本机默认六分轨模型已在 16GB 显存完成整曲验证。
- Basic Pitch 或 TransKun 未安装：重新运行 `tools/setup_stemscore_runtime.ps1`。核心分离权重缺失时，请从你有权使用的本地 MSST/UVR 环境导入；公开版不会替用户下载许可不明的权重。
- 自动续跑任务不会替你点击审查通过；等待人工审查时会保持安静。

