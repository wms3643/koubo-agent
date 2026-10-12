# 内核与产物契约

本文记录 `kb.py` 视频内核、项目文件和编排层之间的主要约定。DSH 的 `kb_*` 插件工具在 `plugin/src/index.js` 中映射到内核命令；LangGraph 使用 `langgraph_app/kernel.py` 调用同一内核。两个入口的工具名称和授权机制不同，不能混用。

## 一、工具与命令结果

内核阶段在标准输出返回 JSON 对象。退出码 `0` 表示成功，`2` 表示可识别的业务失败，`1` 表示环境或代码异常。失败报告中的 `cause` 用于路由；调用方还需检查退出码、报告内容以及预期文件是否生成。时间计算以秒为单位，字幕时间码使用 `HH:MM:SS,mmm`；逐字稿和剪辑决策中的片段索引从 `0` 开始。

### 阶段与主要产物

| 阶段 | 主要输入 | 主要输出 |
| --- | --- | --- |
| `transcribe` | `input/raw.mp4` | `edit/transcript.json` 及文本、SRT |
| `cut-plan` | 逐字稿 | `edit/plan_context.json` |
| `cut-apply` | `edit/edit_decisions.json` | `edit/cut_blocks.json`、`edit/timeline_map.json`、剪辑视频 |
| `srt` | 逐字稿、保留块、文本修正 | `edit/subtitles/final.srt`、字幕报告 |
| `burn-test` | 剪辑视频、成品 SRT | 短测试片与抽帧 |
| `burn-full` | 剪辑视频、SRT | 完整字幕版视频 |
| `overlay-render` | `overlays/index.html` | `overlays/renders/` 下的透明叠加层 |
| `assemble` | `edit/assemble.json`、各媒体层 | 装配视频 |
| `bgm-generate`、`mix` | BGM 任务或用户音频、`edit/mix.json` | 音频素材、混音视频与报告 |
| `qc` | 指定目标的视频及关联产物 | `edit/qc_report.json`、抽帧证据 |

内核命令列表见 `kb.py` 顶部说明；各阶段的参数由对应 `pipeline/stage*.py` 解析。`assemble` 的 `dry_run` 只校验输入并预测时长，不生成视频。强制覆盖逐字稿或允许删除幻觉嫌疑段属于例外路径，必须显式记录理由；相关操作会使部分下游产物失效。

## DSH 工具映射

DSH 插件注册 `kb_status`、`kb_plan_cut`、`kb_transcribe`、`kb_cut`、`kb_srt`、`kb_burn`、`kb_assemble`、`kb_overlay`、`kb_qc`、`kb_bgm` 和 `kb_override`。前十个覆盖状态、处理与检查阶段；`kb_override` 只分配给主管。`agents/subagents.patch.yml` 按工具名配置子代理白名单。该白名单不能限制代理对共享项目目录中特定路径的写入。

DSH 工具参数 Schema 使用框架支持的受限子集。未知或跨字段参数不能只依赖 Schema 阻止，插件和内核必须继续校验。插件的后台任务、超时和付费确认属于 DSH 入口的运行行为，不等同于 LangGraph 入口的实现。

### 超时与 BGM

LangGraph 阶段超时定义于 `langgraph_app/config.py`：状态查询 2 分钟、普通短阶段 10 分钟、BGM 20 分钟、编码阶段 30 分钟、转写 60 分钟。实际子进程超时处理见 `langgraph_app/kernel.py`。

BGM 可由用户提供，也可通过付费接口生成。生成前应完成价格检查与授权；超时或断线后应先读取任务记录并查询既有任务状态，不能直接重新提交。混音后由质检检查音乐是否存在、相对响度、真峰值及片尾淡出。具体阈值和检查项以 `pipeline/stage6_qc.py` 为准。

## 二、文件交接

| 文件 | 写入方 | 消费方 |
| --- | --- | --- |
| `edit/transcript.json` | 转写阶段 | 剪辑、字幕 |
| `edit/edit_decisions.json` | 剪辑任务 | 裁切阶段 |
| `edit/subtitle_fixes.json` | 字幕任务 | SRT 阶段 |
| `edit/cut_blocks.json`、`edit/timeline_map.json` | 裁切阶段 | 字幕、动效、质检 |
| `edit/subtitles/final.srt` | SRT 阶段 | 烧录、质检 |
| `overlays/index.html`、`overlays/renders/` | 动效任务与渲染阶段 | 装配、质检 |
| `edit/qc_report.json` | 质检阶段 | 主管与交付流程 |

字幕修正写入独立文件，避免覆盖剪辑的删留决策。剪后时间轴发生变化时，依赖该时间轴的字幕、动效和质检结果均需重新生成或复核。子代理的最终文字回复是执行摘要，项目产物与工具报告才是后续阶段的输入。

## 三、回退矩阵

| 失败类别 | 处理方向 |
| --- | --- |
| `bad_spec`、`index_out_of_range`、`empty_keep_set` | 修改对应决策文件，重跑当前阶段 |
| `hallucination_cut_forbidden` | 复核删留决策；确需放行时由主管记录例外理由 |
| 逐字稿或上游产物缺失 | 补齐上游阶段，再运行当前阶段 |
| `srt_*`、`subtitle_occlusion`、`srt_timeline_mismatch` | 根据检查项修正字幕或剪辑时间轴，重新生成测试片 |
| `missing_overlay`、`hyperframes_render_failed` | 检查动效工程与渲染依赖，重新渲染后装配 |
| `bgm_*`、`audio_clipping` | 复核授权、素材或混音参数，并重新执行对应检查 |
| `qc_probe_failed`、`missing_stream`、`kernel_exception` | 暂停自动重试，检查日志和运行环境 |

LangGraph 的实际 `cause` 映射以 `langgraph_app/routing.py` 为准。默认预算在 `langgraph_app/config.py` 中定义：单阶段回退上限为 5 次，跨代理回路上限为 1 次，同一 `cause` 连续 2 次未解决即停止。DSH 提示词中的回退要求需要与其独立实现核对，不能假定两套入口完全等价。

质检报告包含量化检查及抽帧路径。抽帧可以支持画面复核，但报告并非对所有中间模型输出、工具调用全过程的自动审计。

## 四、实现差异

`pipeline/stage5_assemble.py` 使用 FFmpeg 转场时间轴计算；`pipeline/stage3_srt.py` 生成不带 BOM 的 SRT。两项约定以当前内核与质检代码为准，旧版技能材料中的不同表述不构成运行契约。

### D-1 转场时长计算

对多个片段做时长为 `T` 的相邻转场时，预计输出时长按 `累计时长 = 累计时长 - T + 当前片段时长` 递推。`assemble --dry-run` 与实际渲染共用这一计算。更改转场实现时需同时验证预测时长与 FFmpeg 输出时长。

## 五、子代理契约

剪辑角色负责删留决策和剪后时间轴；字幕角色负责文本修正与字幕产物；动效角色负责透明层设计和渲染；质检角色报告检查结论与证据。主管负责跨角色依赖、失败路由和付费或例外操作的授权。DSH 白名单见 `agents/subagents.patch.yml`，LangGraph 白名单见 `langgraph_app/agent/subagents.py`。
