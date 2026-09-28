# StemScore 模型许可清单

本清单区分应用代码许可、模型权重许可和当前发布决定。未声明许可的权重按“不可公开再分发”处理；公开仓库和公开 Release 不包含下列受限权重。

| 资产 | 来源与许可 | 当前发布决定 |
| --- | --- | --- |
| BS-RoFormer Resurrection | [pcunwa/BS-Roformer-Resurrection](https://huggingface.co/pcunwa/BS-Roformer-Resurrection)，未声明权重许可 | 仅进入 `localOnly=true` 私有运行时；公开版必须移除或取得书面许可 |
| Karaoke Frazer/Becruily | [becruily/bs-roformer-karaoke](https://huggingface.co/becruily/bs-roformer-karaoke)，未声明权重许可 | 仅进入私有运行时；公开版必须移除或取得书面许可 |
| BS-Rofo-SW-Fixed | [enerjazzer/BS-ROFO-SW-Fixed](https://huggingface.co/enerjazzer/BS-ROFO-SW-Fixed)，上游标为 `unknown` | 仅进入私有运行时；公开版必须移除或取得权利人许可 |
| Dereverb/Echo Fused | [Sucial/Dereverb-Echo_Mel_Band_Roformer](https://huggingface.co/Sucial/Dereverb-Echo_Mel_Band_Roformer)，CC BY-NC-SA 4.0 | 只允许符合非商业、署名、附许可、标记修改和相同方式共享条件的发行；商业公开包移除 |
| Demucs `htdemucs_6s` | [facebookresearch/demucs](https://github.com/facebookresearch/demucs)；代码 MIT，权重不在 MIT 范围且[维护者说明仅供研究](https://github.com/facebookresearch/demucs/issues/327#issuecomment-1134828611) | 仅可用于用户有权进行的本地研究；私有仓库状态不会扩大许可，公开发行必须移除该权重 |
| Spotify Basic Pitch 0.4.0 | [spotify/basic-pitch](https://github.com/spotify/basic-pitch)，Apache-2.0 | 可随包分发，保留 LICENSE、NOTICE 和归属 |
| TransKun V2 2.0.1 | [Yujia-Yan/Transkun](https://github.com/Yujia-Yan/Transkun)，MIT | 可随包分发，保留 MIT 版权和许可文本 |
| LAION CLAP HTSAT fused | [laion/clap-htsat-fused](https://huggingface.co/laion/clap-htsat-fused/tree/365dea6ef167def6676140ed93bbc43f84dabb28)，Apache-2.0 | 作为可选模型从上游固定修订下载；若以后镜像分发，随包附 Apache-2.0 文本和归属 |
| Qwen2.5 Instruct GGUF | [Qwen 官方 GGUF 仓库](https://huggingface.co/Qwen)，Apache-2.0 | 作为可选模型从固定修订下载；若以后镜像分发，随包附对应 LICENSE |
| llama.cpp b10887 | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp/releases/tag/b10887)，MIT；发布压缩包另含 LLVM OpenMP 许可 | 可选 runner 直接从上游下载；若以后镜像分发，另附 llama.cpp MIT 文本并保留 LLVM OpenMP 许可 |

## 当前 Release 边界

完整离线 runtime 位于独立私有仓库分支和私有 Release。该 runtime 内含上表的受限分离权重，因此必须保持私有，不能仅靠把 Release 从 prerelease 改为正式版来公开；私有状态也不改变“非商业”或“仅研究”等上游使用限制。公开应用核心 ZIP 不携带这些权重；CLAP、Qwen 和 llama.cpp 仍由模型管理器按用户选择直接从固定上游地址下载。

`1.0.0-public-rc1` 采用第二条路径：发布不含这些权重的核心客户端，让用户绑定自己有权使用的本地运行环境。任何公开资产都要重新生成清单和 SHA-256，不能复用私有 runtime 的发布声明。
