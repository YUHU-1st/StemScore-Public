# StemScore 音乐分析与 MiniMax Music 3

StemScore 的音乐分析与 Music 3 提示词独立于五步分轨/MIDI 流程。打开客户端后可直接点击“独立音乐分析 / Music 3 提示词”并选择歌曲；已有项目也可随时运行。程序不会改写分轨或 MIDI；若当前项目已有可用分轨，会使用其元数据补充配器证据，否则只依据原曲音频生成报告和提示词。结果写入项目的 `06_music_analysis` 目录。

## 输出文件

| 文件后缀 | 内容 | 审查用途 |
|---|---|---|
| `_music-analysis.json` | 完整机器可读特征、证据、候选和限制 | 查看原始数值及后续自动化 |
| `_music-analysis.md` | 面向人的分析报告 | 快速试听复核 BPM、调性、配器和段落 |
| `_minimax-music3-instructions.txt` | 英文三段式 Structured Caption | 粘贴到 MiniMax Music 3 的 Caption / music description 输入 |
| `_comfyui-minimax-music3-input.json` | ComfyUI 原生节点的 `caption` / `lyrics` / 采样参数 | 直接填入 `MiniMaxMusic3TextEncode` |
| `_analysis-audit.json` | 输入、可用分轨、命令和输出哈希 | 追踪分析来自哪首本地歌曲 |

`_comfyui-minimax-music3-input.json` 是可直接映射到 ComfyUI 原生 `MiniMaxMusic3TextEncode` 节点的传递对象，不冒充包含节点坐标和连线的完整 workflow。JSON 直接提供 `caption`、`lyrics`、`seed`、`max_duration`、`cfg_scale` 和 `top_k`，并附官方 ComfyUI 模板地址；导入官方模板后将同名值填入节点即可。

## 分析证据如何产生

### BPM

本地 librosa worker 从起音强度计算原始速度，并以约 70 BPM 的先验再求一个慢速候选。只有原始候选至少为 130 BPM，且它与慢速候选的二倍关系误差不超过 8%，才选择慢速候选并记录 `tempo_selection: half_time_2_to_1`；否则保留 `raw`。报告同时保留原始值、慢速值、选择结果、起音数量和一致性置信度，不能只看最终 BPM。

### 调性

系统将整曲色度分布与 12 个大调、12 个小调模板比较，输出第一、第二候选及二者差距换算的置信度。精确调性只有在置信度至少为 `0.35` 时才进入 Music 3 提示词；低于阈值的候选仍留在分析报告供人审查，但不得伪装成确定事实。

### 曲风

基础曲风是带证据的保守候选，不是真值。v1 组合 BPM、起音密度和当前可用分轨的活跃角色。例如，有效主唱与钢琴同时存在且 BPM 小于 95 时，候选为 `piano-led pop ballad`；鼓、贝斯和吉他同时有效时，候选为 `pop-rock band`。每个候选均保留置信度和中文证据；没有分轨时不推断人声是否存在。

若用户已经在模型管理器中选中并完整安装 `clap-htsat-fused`，系统会额外启用本地 LAION CLAP 零样本增强：以 48 kHz 单声道读取全曲，在约 10%、50%、90% 位置各取一个 10 秒窗口，与固定曲风标签计算相似度，输出前三个语义候选。CLAP 候选带有 `method: local_clap_zero_shot`，完整采样位置、设备和分数写入 `semantic_audio`；原有 DSP/分轨候选仍保留，配器证据仍优先。模型未选中或未完整安装时直接使用确定性规则，不生成 `semantic_audio`；已启用但推理失败时也继续确定性流程，并把失败信息写入 `semantic_audio`。两种情况都不会阻断报告和提示词。CLAP 也是候选排序器，不证明唯一曲风。

### 配器

配器不从混音盲猜，而是读取第四步最终分轨的角色、质量门状态与 `output_mean_volume_db`。近静音质量门会直接标记不活跃；未被质量门标记的分轨若平均能量不高于 −55 dBFS，或比最强分轨低超过 30 dB，也按保守策略判为不活跃；其余分轨标为 dominant、supporting 或 subtle。分轨泄漏仍可能让某一乐器被误判，所以配器结果必须结合独听和相消检查。

### 编曲结构

结构分析每约 2 秒聚合色度、MFCC 和 RMS，在自相似矩阵中寻找局部新颖度，再结合边界两侧能量跳变。边界间隔至少约 32 秒，最多选 10 个内部边界，因此整曲最多产生 11 个段落候选。输出的 `S1`、`S2` 等是无语义候选，不等同于已经确认的 Intro、Verse 或 Chorus。

## MiniMax Music 3 三段式提示词

MiniMax 官方仓库建议 Structured Caption 依次包含：

1. `### Global Metadata`：曲风、BPM、可信调性、情绪发展和制作特征。
2. `### Vocal Details`：只写本地证据支持的人声存在性、和声及处理；未测得的性别、音域和音色保持未指定。
3. `### Arrangement`：有效乐器、段落级密度和能量发展、律动、转场及空间层次。

官方规范把歌词和音乐描述作为两个互补输入，并明确要求歌词留在 Lyrics / `input`，不要混入 Structured Caption。StemScore 可以读取歌词中的合法段落标签来组织 Arrangement，但会阻止复制歌词正文；未提供歌词时使用不带歌词的结构模板。官方资料还说明这些条件是生成控制，不是符号级保证，生成结果的 BPM、调性、配器和结构仍可能偏离要求。

参考来源：

- [MiniMax-AI/MiniMax-Music3 官方 README](https://github.com/MiniMax-AI/MiniMax-Music3#fine-grained-music-control)
- [Comfy-Org 官方 MiniMax Music 3 工作流模板](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/audio_minimax_music_3.json)

## 本地 LLM 精炼与确定性回退

确定性三段提示词始终先生成。本地 LLM 只负责在不新增未经证实事实的前提下扩写制作语言，端点必须是 `localhost` 或回环 IP 的 OpenAI 兼容 `chat/completions` 接口。精炼结果必须：标题恰好三项且顺序正确、英文正文 250–450 词、三个章节都非空、不得包含歌词原句或连续四词歌词片段。

出现以下任一情况时，系统保留确定性提示词并把原因写入 `music3_caption_refinement`：没有 `models/selection.json`、配置停用或无效、端点失败、响应格式错误、字数或标题不合规、命中歌词防复制检查。回退不影响 DSP 分析和 ComfyUI 传递 JSON 的生成；它表示“未应用本地文本精炼”，而不是分析失败。

## 人工审查步骤

1. 在审计 JSON 中核对源文件哈希；若使用了已有分轨，再核对相应分轨哈希。无需先完成五步流程。
2. 用节拍器分别试听最终 BPM、原始 BPM 以及必要的半拍候选，重点检查弱起、复合拍号和自由速度段。
3. 对照键盘或 DAW 调性工具试听主歌、高潮和尾奏；置信度低于 `0.35` 时保持提示词无调性约束。
4. 独听每个标为活跃的分轨，确认目标乐器不是泄漏；特别检查 dominant 与 inactive 的临界项。
5. 在每个结构边界前后各试听约 5 秒，删除伪边界，并由人把 `S1…Sn` 标成真实段落名。
6. 检查三段提示词只写了报告支持的事实，没有歌手身份、源歌词、未测量声线或虚构乐器。
7. 打开 JSON 里 `workflow_template_url` 指向的 ComfyUI 官方模板，将 `caption`、`lyrics` 和采样参数填入同名节点字段；需要人声时只加入自有或已授权歌词。先用固定 seed 小范围试听，再决定是否采用生成结果。

## 准确性边界

- BPM 可能出现半拍、倍拍或复合拍误判；`confidence: 1.0` 只反映起音网格在当前候选上的内部一致性，不等于音乐学真值。
- 调性是整曲色度模板相关结果；转调、借用和弦、调式混用、强打击乐和分离伪影都会降低可信度。
- 曲风候选只覆盖 v1 规则明确写出的少量组合，不能证明年代、地域、子流派或歌手风格。
- 可选 CLAP 只比较固定标签集和三个 10 秒抽样窗口，可能遗漏短暂段落或标签集外风格；相似度不是校准后的音乐学置信度。
- 分轨电平用于“是否存在、相对突出”判断，不等于听感响度，也不能识别 `other` 内部的具体乐器。
- 结构边界来自音色与能量变化，不识别歌词语义、和声功能或正式段落名；必须人工试听命名。
- StemScore 生成的是“基于特征的相似制作提示”，不复刻旋律、和弦、歌词、歌手身份或受版权保护的具体录音。
- MiniMax Music 3 的文本条件是软控制；最终生成仍需试听并核对授权范围。
