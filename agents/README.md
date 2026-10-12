# 子代理挂载层（koubo-agent-dsh）

## ⛔ 先纠正一个误解：DSH **没有**「名册目录」

早期版本（我写的）说"把 `*.yaml` 放进 `~/.dsh/subagents/`，文件名即 agent id" ——
**DSH 里没有这回事**。本机 296 个官方包中**没有** `dsh-subagent-library` 这类读取器。

真实机制：**一个委派目标 = 挂载一个 `@deepseek-ai/dsh-tool-subagent` 实例**，
用 `toolName` 把它暴露成一个模型可见工具。配置就是本目录的 **`subagents.patch.yml`**（一个 Cordis patch）。

## 挂载的 4 个子代理

| 子代理（中文名） | 技术标识 | 专业 | 完整职责（从输入到交付） |
|---|---|---|---|
| **剪裁策划** | `delegate_transcript_editor` | 剪辑 | 转写 → 判断删留 → 剪切 → **建立剪后时间轴** → 缝接 + 铺 BGM |
| **字幕制作** | `delegate_subtitle_maker` | 字幕 | 断句 + 时间对齐 → 文本纠错 → **烧录字幕**（10s 测试片 → 全片） |
| **特效设计** | `delegate_overlay_designer` | 视觉包装 | 按剪后时间轴设计动效 → 渲染透明 MOV → **叠加进成片** |
| **成片质检** | `delegate_qc_inspector` | 质量把关 | 验收（含字幕层 + 特效层）→ 给回退目标（**不修改任何产物**） |

**主管**只做**编排**：排程、派发、按回退矩阵裁决、交付 —— **不碰任何专业产物**。

## 划分依据：谁最懂这个产物，就归谁

每个产物**只有一个主人**，不交叉、不共管：

| 产物 | 主人 |
|---|---|
| `transcript.json`（逐字稿 + 时间戳） | 剪裁策划 |
| `edit_decisions.json`（剪辑决策） | 剪裁策划 |
| `cut_blocks.json` + `edited_final.mp4` | 剪裁策划 |
| **`timeline_map.json`（剪后时间轴）** | **剪裁策划**（剪切完成即建立） |
| `assembled.mp4`（缝接 + BGM 的成片骨架） | 剪裁策划 |
| **`bgm/bgm.mp3`（背景音乐素材）** | **剪裁策划** —— 调 `suno-v4.5` 自己生成（约 0.49 元/次）；用户指定曲目时才用用户的 |
| `final.srt` + `subtitle_fixes.json` | 字幕制作 |
| `subtitle_test_10s.mp4` + `subtitled_full.mp4` | 字幕制作 |
| `overlays/index.html` + `overlays/renders/*.mov` | 特效设计 |
| `final.mp4`（带特效的最终片） | 特效设计 |
| `qc_report.json` | 成片质检 |

⇒ 每个环节都有明确的 agent 兜着，**不存在"没人负责、系统自己干"的环节**。

> ⭐ **工具白名单 = 权限边界**（2026-10-05 拆工具后）。
> 本目录 `subagents.patch.yml` 里每个子代理的 `toolFilter.allow` 就是它**能做的事的完整清单**。
> 因为插件已改成**一阶段一工具**（不再有 `kb_run(stage=…)`），白名单说什么就是什么：
> 字幕制作没有 `kb_cut`/`kb_transcribe`，特效设计没有 `kb_burn`，**没有任何子代理有 `kb_override`**。
> 这份矩阵由 `tools/plugin_smoke.mjs` §10 直接解析本文件断言 —— 它是一条被测试守住的边界。

## 挂载方式

```bash
dsh --profile web \
  --patch profile/yuanqi-provider.patch.yml \
  --patch profile/cordis.patch.yml \
  --patch agents/subagents.patch.yml
```

顺序有讲究：**插件 patch 在前**（先注册 `kb_*` 工具），子代理 patch 在后 ——
因为 `toolFilter.allow` 里的工具名在实例化时校验，未知名字会 **fail-loud**。

离线自查（不启动 harness、不调模型）：

```bash
dsh --profile web --patch agents/subagents.patch.yml --dump-config | grep -A4 koubo-subagent
```

## 字段依据（全部取自本机已安装的官方包，非推测）

| 字段 | 真实含义 |
|---|---|
| `provider` | **`ctx.subagents` 后端名**（`spawn` / `fork` / `acp`）—— **不是 LLM 路由** |
| `toolName` | 模型可见的工具名；每个实例必须唯一 |
| `persona` | 给每个子代理的专属人格/指令（需后端声明 `persona` 能力） |
| `toolFilter.allow` | 白名单：**名单外的工具从子代理提示词中消失并拒绝执行**（硬隔离） |
| `maxDepth` | 递归深度上限（`0` 禁止再派），需后端 `depthLimit` 能力 |
| `backgroundMode` | `one-shot` 前台等结果 / `continuable` 默认后台并返回 childId |
| `agentOptions` | 子代理的 `provider` / `model` / `reasoningEffort` / `maxTokens` |

出处：`dsh-tool-subagent/lib/types/index.d.ts` 的 `Config`；
`dsh-subagent-spawn-in-process` 声明 `capabilities = { agentOptions, depthLimit, toolFilter, persona, prepareContinuable }`。

**两条由此推出的事实**：

1. **不需要配 `agentOptions`** —— `spawn` 子代理继承父代理的 provider / model / reasoning effort。
   主管用元气中转站，子代理就用同一个。
2. **子代理对话为空**（spawn 不继承历史）⇒ 主管派发时的任务提示词必须**自包含**，
   子代理看不到主管的对话。工具描述本身也会据此措辞。

## 两个主线子代理的产物互不越界（重要）

**各拥有一个文件，不允许交叉写**：

| 文件 | 拥有者 | 内容 |
|---|---|---|
| `edit/edit_decisions.json` | **剪裁策划** | 删除集 `drop_segment_ids[]` + 文本修正 |
| `edit/subtitle_fixes.json` | **字幕制作** | 字幕文本修正（`text_fixes`，同 idx 覆盖前者） |

内核 `stage3_srt.py` 会**合并**这两个文件的 `text_fixes`。设计成两个文件是刻意的：
若字幕子代理去改写 `edit_decisions.json`，会**连带抹掉整个删除集** —— 不可恢复。

**`text_fixes` 的职责划分（防"谁后写谁赢"的静默冲突）**：

- **剪裁策划** 只改**影响它判断删留**的文本（例如错字让它误判某段是废话）；
- **全部纯文本纠错（专名 / 数字 / 同音字）归 **字幕制作****。

两边若都改同一处，合并规则是"字幕文件覆盖剪裁文件" —— 但那是**兜底**，不是分工。

⚠️ **能力边界**：`toolFilter` 只能按**工具名**过滤，**不能按路径**过滤。
所以"不许写别的文件"目前是**提示词约束 + 文件分离**，不是硬隔离。
要按路径硬隔离，须在插件的 `tools/pre-execute` 里按 `exec` 的目标参数判定。

## 与主管的协作契约

- 主管**只通过产物文件**与子代理交接：`edit/edit_decisions.json`（剪辑决策）、
  `edit/subtitle_fixes.json` + `edit/srt_report.json`（字幕）、
  `edit/qc_report.json` + 帧图（审片）、`overlays/index.html`（动效）。
  子代理的对话内容不进主管上下文，主管只读文件与工具报告。
- 子代理**不自己决定回退**，只产出 `cause` / 问题清单；**回退目标由主管按
  `docs/contracts.md` 的「回退矩阵」裁决**。这是为了避免"每个子代理都以为自己该重做一遍"
  而导致的重复烧钱。

## 验证状态（2026-10-04 实测）

| 验证 | 结果 |
|---|---|
| patch 离线合成 | ✅ 4 个实例落位（`dsh --patch … --dump-config`） |
| 真启动挂载 | ✅ 无 fail-loud，模型列出全部 4 个 `delegate_*` 工具 |
| **真派发一次** | ✅ 主管调 `delegate_transcript_editor` → 子代理起独立会话 → 自己调 `kb_status` → 回传结果 |
