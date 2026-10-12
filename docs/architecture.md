# 架构：DSH 上的多代理口播剪辑系统

## 0. 一句话结论

**用 DeepSeek Harness 做"多智能体协作"是成立的，但把 6 道工序全做成 6 个 agent 是错的。**
正确切法是：**机械动作下沉为工具（0 token、可断言），只有 4 件真需要判断的事交给子代理**，
由主管按**显式回退矩阵**裁决。理由见 §1。

---

## 1. 为什么这样切（三个理由）

### 理由一：DSH 的强项是"执行层"，不是"流程编排"
DSH 官方定位是 *agent harness*（`Agent = Model + Harness`），它给你的是工具协议、插件微内核、
会话日志、子代理委派、后台作业 —— 这些是**让模型能动手**的能力。
它**不提供**带类型化回退目标的确定性图。而口播剪辑的失败模式非常具体
（幻觉段被误删、SRT 带 BOM、切点吞字），必须靠**断言 + cause 路由**兜住，不能靠"再聊一轮"。

### 理由二：确定性动作交给 LLM 是纯烧钱
转写参数、编码参数、字幕重映射公式、xfade 递推 —— 这些**没有判断空间**，
差 0.1 秒就是错。把它们放进 LLM 的决策范围，等于每跑一次都赌一次模型心情，
而且每次都付 token。DSH 的 session log 会把这些 token 一笔笔记下来。

### 理由三：上下文经济学
把 573 段转写（约 5 万字符）+ 34 张切点帧 + ffmpeg 日志全塞进一个 agent，
上下文会被撑爆且在长上下文里漂移。所以：
- **证据走文件不走对话**：`kb_plan_cut` 只回摘要统计，把 573 行证据写进
  `plan_context.json`，子代理自己去读、读完不往上报
- **子代理的对话不进主管上下文**，主管只读它们的产物文件

> ⚠️ **2026-10-05 修正**：本节原有一个子条 —— **"工具数压到 5 个"** —— 已删除。
> 它当时的理由是"每个工具的 schema 都常驻系统提示词"，但实测后不成立：
> 每条 schema 都很小，真正膨胀的是聚合工具 `kb_run` **自己那段 description**
> （为了讲清 9 个 stage，它成了全文最长的一条）；拆开之后每条更短更聚焦。
> 更关键的是 **DSH 的 `defineTool` 支持 `deferLoading: true`** —— 延迟加载的工具
> schema **不常驻**、按需检索 ⇒ "控提示词预算"和"多而窄的工具"**不是二选一**。
> 而那个数字本身就是先定后用坏的：写"4 个"时还没有 `kb_bgm`，后来为了权限不得不加第 5 个。
>
> 于是把它换成了**更强的一条约束**（见 §7 第六轮·补）：
> **一个阶段一个工具** —— 因为 `toolFilter` 只能按工具名授权，"一个工具承载 N 个阶段"
> 等于把 N 种能力绑在同一个权限开关上（实测出过权限泄露）。

---

## 2. 分层

```
┌─ 编排层（DSH）─────────────────────────────────────────────────┐
│  主管 Director（agent preset）                                  │
│   · 排程 · 派发 · 按回退矩阵裁决 · 交付                          │
│   · 原生能力：subagent / agent-team / jobs / ask_user_question   │
└───────────────┬────────────────────────────────────────────────┘
                │ 派发（每个子代理领一整段完整职责）
   ┌──────────────────────┴──────────────────────┐   ┌──────┴──────┐
   │        四个专业子代理（按「专业」切）           │   │  主管只编排  │
   ▼                ▼                    ▼                ▼
 剪裁策划          字幕制作             特效设计          成片质检
 转写→判删留→剪切    做字幕→修回          按剪后时间轴       看图判能否交付
 →建剪后时间轴       →烧录字幕           设计+渲染 MOV      (含特效层)
 →缝接+铺 BGM       (测试片→全片)        →叠加进成片        (不修改产物)
                │ 交接面 = 文件（不是对话）
                ▼
┌─ 工具层（dsh-plugin-koubo）────────────────────────────────────┐
│  kb_status kb_plan_cut kb_transcribe kb_cut kb_srt kb_burn      │
│  kb_assemble kb_overlay kb_qc kb_bgm kb_override                │
│  （一阶段一工具；`kb_override` 是主管专属的例外开关）             │
│  只做：装配参数 → 起子进程 → 解析 JSON → 原样回传 ok/cause        │
└───────────────┬────────────────────────────────────────────────┘
                ▼
┌─ 确定性内核（Python，可脱离 DSH 单独跑）───────────────────────┐
│  kb.py  →  pipeline/stage1..6                                    │
│  转写 · 剪裁(计划/落地) · 字幕 · 烧录 · 装配 · 质检               │
│  每个 stage：给定输入 → 固定输出 + 自检 + 带 cause 的退出码       │
└───────────────┬────────────────────────────────────────────────┘
                ▼
┌─ 介质层 ───────────────────────────────────────────────────────┐
│  FFmpeg 8.1.2(libass) · faster-whisper large-v3(GPU)            │
│  HyperFrames 0.7.94(透明叠加层)                                  │
└────────────────────────────────────────────────────────────────┘
```

---

## 3. 三个检查点，各有专属回退目标

> 这是本设计最核心的一条经验，来自同工作区 `mg-agent-system` 的教训：
> **把"审片不通过"当成一个笼统状态，会导致打回后模型"盲改"。**
> 每个失败必须带 `cause`，每个 `cause` 必须映射到**唯一**的回退目标。

| 检查点 | 时机 | 谁判 | 不过时按 cause 分流 |
|---|---|---|---|
| **CP1** | `edit_decisions.json` 产出后、真裁切前 | 主管对照铁律复核决策文件 | 删了幻觉段 → 打回 `剪裁策划`；下标越界 → 工具带 `index_out_of_range` 直接报错，改文件重跑 |
| **CP2** | 10s 测试片抓帧后、烧全片前 | `成片质检` 看图 | 挡脸 → 只改 `force_style.MarginV` 重烧；字幕没烧进 → 查 filter 路径转义 |
| **CP3** | 成片质检 | `kb_qc` 断言 + `成片质检` 看帧 | `srt_*` → 重跑 srt（确定性修复）；`cut_duration_mismatch`/`srt_timeline_mismatch` → 回 `剪裁策划`；`junction_glitch`/`audio_clipping`/`av_desync` → 回 `cut-apply` 调切点；`black_frame` → 回装配；`missing_stream`/`spec_mismatch` → **停手升级人工** |

**关键区别**：`srt_bom` 这类错误**回退到工具**，不回退到 LLM。
让模型"重写一遍字幕"是既贵又不确定的；重跑 `stage3` 是确定性的。

### 护栏
`retake_budget=5`（单阶段累计）· 同一 cause 连续 2 次未解决即停手报告 ·
任何更贵动作（重转写/重渲全片）**先问用户**（skill 红线第 8 条"偏离先报批"）。

---

## 4. 与 DSH 原生能力的映射

以下包在**本机已安装的 `@deepseek-ai/*`（296 个包）中实测存在**：

| DSH 原生能力 | 包 | 本方案怎么用 |
|---|---|---|
| 工具注册 | `dsh-tools`（`defineTool`） | **11 个 `kb_*` 工具**（一阶段一工具，见 §2 工具层）；**插件零业务逻辑** |
| 子代理委派 | `dsh-tool-subagent`、`dsh-subagent-spawn-in-process`、`dsh-subagent-fork-in-process`、`dsh-subagent-in-process-driver` | 派发 **4 个专业子代理**（剪辑 / 字幕 / 视觉包装 / 质量把关）；`fork` 变体用于需要继承当前目录上下文的场合 |
| 子代理控制 | `dsh-tool-subagent-control`（`send_message`/`interrupt_agent`/`list_agents`） | 长任务打断与进度查询 |
| **团队协作** | `dsh-experimental-tool-agent-team`（`spawn_teammate`、`team_task_create/get/list/update`、`wait_agent`）＋ `dsh-experimental-agent-team-profile` | 想要"共享任务板 + 多代理并行"时启用；**默认禁用**，需在 profile patch 打开 |
| 人工确认（HITL） | `dsh-tool-ask-user`（`ask_user_question`） | 落实"偏离先报批"：更贵动作前停下来问 |
| 长任务 | `dsh-tool-jobs`（`job_list`/`job_output`/`job_kill`） | 转写/全片烧录/装配这类分钟级任务的作业化（**当前为同步执行 + abort 传播，见 §7**） |
| 复用既有技能 | `dsh-tool-skill` | 可直接把已装的 `koubo-video-pipeline` skill 挂给子代理，让它读 8 条红线原文 |
| 会话可观测 | session log（append-only） | 每次回退的 cause 与决策都可回溯，便于复盘"为什么这次多花了钱" |

### 子代理的落地方式（❗ 早期说法已纠正）

**DSH 没有「名册目录」** —— 本机 296 个官方包里**没有** `dsh-subagent-library`。
真实机制：**一个委派目标 = 挂载一个 `@deepseek-ai/dsh-tool-subagent` 实例**，
用 `toolName` 暴露成一个模型可见工具；配置即 `agents/subagents.patch.yml`。

- `provider` 是 **`ctx.subagents` 后端名**（`spawn` / `fork` / `acp`）—— **不是 LLM 路由**。
  `spawn` 声明 capabilities：`{agentOptions, depthLimit, toolFilter, persona, prepareContinuable}`。
- 子代理**继承父代理的 provider / model / reasoning effort** ⇒ 不必配 `agentOptions`。
- 子代理**对话为空**（spawn 不继承历史）⇒ 派发任务必须**自包含**。
- `toolFilter.allow` 是**白名单**：名单外的工具从子代理提示词中消失并拒绝执行 ——
  这是硬隔离的一种，但**只能按工具名、不能按路径**。
- `maxDepth: 1` 禁止子代理再生孙子代理。

**验证链（2026-10-04 实测）**：patch 离线合成 ✅（4 实例落位）→ 真启动无 fail-loud、
模型列出全部 4 个 `delegate_*` ✅ → **真派发一次成功** ✅
（主管调 `delegate_transcript_editor` → 子代理起独立会话 → 自己调 `kb_status` → 结果回传）。

---

## 5. 一次完整跑动的数据流

```
input/raw.mp4
  ├─ kb_transcribe ───────────► edit/transcript.json        (573 段, word 级时间戳)
  ├─ kb_plan_cut ─────────────► edit/plan_context.json      (证据表：关键词命中/幻觉嫌疑/长空白/重复簇)
  │      └─ 剪裁策划 写 ► edit/edit_decisions.json    (drop_segment_ids 0-based + text_fixes)
  ├─ kb_cut(run=true) ────────► edit/cut_blocks.json        (18 块, 1328.5s → 1186.0s)
  │                             edit/timeline_map.json      (剪后时间轴)
  │                             renders/edited_final.mp4
  ├─ kb_srt ──────────────────► edit/subtitles/final.srt    (525 条；18 处幻觉链合并；无 BOM)
  │      └─ 字幕制作 写 ► edit/subtitle_fixes.json    (文本修回；报告含 off_block_segments 明细)
  │                             edit/srt_report.json
  ├─ kb_burn(mode=test) ──────► renders/subtitle_test_10s.mp4 + edit/tmpimg/*.png
  │      └─ 成片质检 看帧（kb_qc target=subtitle_test）
  ├─ kb_burn(mode=full) ──────► renders/subtitled_full.mp4
  ├─ kb_assemble(mode=mix) ───► renders/final.mp4           (xfade 缝接 + BGM)
  ├─ kb_overlay ──────────────► overlays/renders/*.mov      (透明层，按剪后时间轴)
  ├─ kb_assemble + overlays ──► renders/final.mp4           (叠上动效层的最终成片)
  └─ kb_qc(target=final) ─────► edit/qc_report.json         (checks[] 每项带 cause + 34 张切点帧)
```

---

## 6. 本仓库已实测验证的事实（避免下次重新踩）

| 事实 | 验证方式 |
|---|---|
| `defineTool` 来自 `@deepseek-ai/dsh-tools`；工具定义键为 `name/description/parameters/output/execute` | 读了官方编译产物的 `.d.ts` 与 `lib/index.js`，并用桩 ctx 实跑注册 |
| **插件绝不能 `export default`** | 官方 postmortem 0001：Loader 的 `unwrapExports` 会折叠模块并丢掉 `inject` |
| `parameters` DSL：`{type, required, description, enum, items{...}, additionalProperties}` | 从 `dsh-tool-todo` 的编译产物逐字段抄证 |
| 内核在真实 573 段转写上跑通阶段 2→3→6 | 见 README「冒烟验证」的实测数字 |
| 负例能产出可路由 cause | 5s 假成片 → `exit=2`、`causes=[cut_duration_mismatch, srt_timeline_mismatch]` |
| 正例能全绿 | 等长合成成片 → `exit=0`、10/10 checks PASS |
| **中文路径 + ESM**：必须用 `fileURLToPath` | 用 `new URL(import.meta.url).pathname` 会得到百分号编码路径并报 `ERR_MODULE_NOT_FOUND` |
| `@deepseek-ai/dsh` 最新版 `0.2.0-rc.2`（alpha `0.2.1-alpha.1`） | `npm view` + 本机安装 296 个 `@deepseek-ai/*` 包 |
| `ctx.tools.guard(ToolGuard): () => void`；`ToolGuard = (exec) => string \| undefined`，**没有 allow 返回值 ⇒ 真 monotonic** | 读 `dsh-tools/lib/types/index.d.ts:655`；并在**真实** ToolRuntime 上注册成功 |
| `ctx.jobs.start(spec): JobId` **同步**返回 id；`run(job)` 同步返回 `{cancel, done}` | 读 `dsh-jobs/lib/types/index.d.ts` |
| `JobHandle = {id, append(text,{channel?}), updateProgress(line)}`；`JobOutcome = {status, detail?, result?}` | 读 `dsh-jobs/lib/types/types.d.ts:79,13` |
| `Agent.id` **就是** `SessionId`，可直接作 `JobSpec.owner` | `dsh-agent/lib/types/types.d.ts:13` |
| `JobKindMap` 是 declaration-merging 可扩展的，registry 把 kind 当**不透明**命名空间（运行时接受自定义 kind） | `dsh-jobs/lib/types/view.d.ts:26` |
| ⭐ **`cordis.patch.yml` ＝ 顶层数组**（patchList）；`insert` 后是**数组**；**config 整体替换、不是深合并**；未知 target "warned and skipped" | `dsh --profile web --dump-config-schema` 的 `$defs.patch.description` |
| patch 里的**相对路径锚定在 patch 文件旁** | 用 `--patch` 实测：`../plugin` → `file:///…/koubo-agent-dsh/plugin`（合成树里可见） |
| ⛔ **patch 的 `name` 必须指向入口文件，不能指向包目录** | 目录 URL → `ERR_UNSUPPORTED_DIR_IMPORT` → 插件**静默不激活**（日志只留一行 `warning: 1 entry did not activate`）。真跑 headless 抓出，`tools/check-plugin-import.mjs` 可复现 |
| base bundle **已挂载** `llm-pi-ai` / `credentials-local` / `agent-default-model`，且 `llm-pi-ai` 是**休眠**的（零路由） | `dsh-base/cordis.patch.yml:117,127`；补一层 patch 即生效，无需新增插件 |
| 手写 provider 路由**必须**有 `api` + `baseURL` + 非空 `models`；`apiKeyEnv` 是**凭据引用**（密钥不进配置文件） | `dsh-llm-pi-ai/README.md`「Configure provider routes」+ 实跑 |
| 凭据存储＝`$DSH_HOME/.credentials.yaml`，格式 `version: 1` + `refs:`；顶层只允许 `version`/`refs`/`records`，其余**fail-loud 拒绝** | 读 `dsh-credentials-local/lib/index.js:136-157`（含旧扁平布局的迁移报错文案） |
| `agent-default-model` 的 config 是 `{provider, model}`；patch **整体替换** ⇒ 两个键都要重述 | `dsh-base/cordis.patch.yml:80-83` + 实跑 |
| 用户设置落点＝**profile 的 `cordis.patch.yml`** | `dsh-settings` 文档："Changes persist through the active profile's Cordis patch" |
| `ToolRuntime.inject = ["systemPrompt"]` ⇒ 想在最小上下文里挂真实注册表，必须先挂 `dsh-system-prompt` | `registry_smoke.mjs` 实测 |
| **ESM 不认 `NODE_PATH`** ⇒ 裸模块名必须在解析链上有真实 `node_modules`（用目录联接） | `tools/` 下解析失败 → 建 Junction 后通过 |
| ⚠️ **目录联接（Junction）在沙箱进程里不可遍历**：`os.listdir('node_modules')` → `OSError [WinError 448] 无法遍历该路径，因为它包含不受信任的装入点` | 2026-10-05 在受沙箱约束的 shell 里复现；`Get-ChildItem` 返回 0 项且不报错（**极易被误判成"依赖没装"**）。普通终端不受影响 —— 但这也说明"node tools/*.mjs 全绿"这条验证**依赖运行环境**。绕开办法：把 `node_modules` **真拷贝**到临时克隆后运行 |
| 插件工具实际是 **11 个**（一阶段一工具），且**内核 12 个子命令全被覆盖**、没有任何工具暴露自由 `args` | `node tools/registry_smoke.mjs` 读真实 `ctx.tools.schemas()`；与 `kb.py` 的 `DISPATCH` + 特判分支逐项比对 |
| ⭐ **工具参数根对象天生开放**：`ParameterSchemaSpec` 官方注释 *"The map itself is an implicit open object root"* ⇒ **无法**声明 `additionalProperties: false`，模型多传的字段被**静默忽略** | 读 `dsh-tools/lib/types/schema.d.ts`；并用桩 `ctx` 实调 `kb_status(project, force:true, nonsense:42)` → 正常返回 ok |
| `defineTool.execute` **会先校验参数**：违规抛 `ToolArgsError`（`INVALID_ARGS`，逐条 violations） | 读 `dsh-tools/lib/index.js` 的 `defineTool`（`const violations = validate(args); if (violations.length) throw …`） |
| JSON Schema **只支持受限子集**（type/oneOf/properties/required/additionalProperties(bool)/items/enum/const + description/title/default/examples）；`minLength`/`pattern`/`format`/`minimum` 等**会 authorError 报错**（是拒绝，不是静默） | 读 `dsh-tools` 的 `CONSTRAINT_KEYWORDS`/`ANNOTATION_KEYWORDS`；`default`/`examples` 会真的出现在线上 schema 里（`dump_schemas.mjs` 实测） |
| `defineTool` 还支持 **per-tool `timeoutMs` / `deferLoading`**；但工具级超时只放弃等待、**不杀子进程**（可能留孤儿） | 读 `defineTool` 的 options 透传；⇒ 本项目坚持在插件里自己 `child.kill()`，并按阶段分档 |

---

## 7. 硬化进度与未完成项（诚实清单）

### 已完成（2026-10-05 第六轮：**全量代码审查与加固**，不启动 harness）

用真实 `node_modules`（临时克隆，绕开沙箱对目录联接的限制）+ 真实 ffmpeg 跑通全部离线测试，
逐文件审查 `pipeline/` · `plugin/` · `tools/`，修掉 9 类**真实缺陷**（其中 4 类属于
"文档写了、代码里没有"或"断言看起来在跑、其实从未运行"）。逐条见 `docs/contracts.md` D-4。

| 缺陷 | 性质 | 后果（若不修） |
|---|---|---|
| `cut-apply` 不校验"幻觉段不得删" | **红线只写在提示词里** | 删幻觉段会切断说话声 —— 文档称"写进代码"，实际没有 |
| `bgm-generate` 查价失败即静默放过 | **付费闸门漏网** | pricing 报错/超时/结构变了 ⇒ 在**不知道单价**的情况下提交付费任务 |
| `bgm-generate` 提交后 HTTP≠200 不落盘 `task_id` | **重复扣费风险** | 任务已创建、钱已扣，本地没记 ⇒ 下次**重新提交** |
| `kb_qc(target=subtitle_test)` 拿全片 SRT 比 10s 测试片 | **恒定假 cause** | CP2 被污染成 `srt_timeline_mismatch`，引向最贵的一类回退 |
| `kb_qc` 无音频流时抛异常 | 失败不可路由 | 报成 `kernel_exception`（退出码 1）而非 `missing_stream` |
| `av_duration_equal` 拿不到 duration 时**从检查项里消失** | **断言静默失效** | 报告看着全绿，其实 A/V 同步从未被检查 |
| `assemble --dry-run` 照跑 ffmpeg | **参数名骗人** | "干跑"依然渲染长片 |
| 决策文件写错崩成 `kernel_exception` | 失败不可路由 | 明明是可自动修的文件问题，却"需人工看 traceback" |
| `plugin` 的 `timeoutMs` 定义了从未使用 | **死配置** | 长阶段卡死 ⇒ 工具调用永久挂住，模型永远等不到 cause |

⭐ 两条方法论上的收获（与本项目一贯主张一致）：
1. **"文档说了"不等于"代码做了"** —— 铁律第一条是最典型的例子：README 写着"写进代码，
   不是文档口号"，而代码里根本没有这道断言。审查必须**逐条去代码里找那道断言**。
2. **"检查项存在"不等于"检查项在跑"** —— `av_duration_equal` 会因拿不到 duration 而
   静默消失、`_volume_checks` 会整段崩掉。**凡断言，必须能在报告里看到它失败了**。

### 已完成（2026-10-05 第六轮·补：**工具层与 JSON Schema 专项**）

先把"框架能做什么"读清楚（`@deepseek-ai/dsh-tools` 的 `.d.ts` + 编译产物 + 真实投影），
再回头审我们写的参数 —— 避免把框架限制当成我们的 bug，也避免把我们的疏漏当成框架行为。

| 发现 | 性质 | 处置 |
|---|---|---|
| 参数**根对象天生开放**（`ParameterSchemaSpec` 注释即 *implicit open object root*）⇒ 写不了 `additionalProperties:false`，多传字段被**静默忽略** | 框架既定行为（**不是**我们的 bug） | 登记为事实，并把回归钉进 `plugin_smoke`（防止以后误以为有校验）；⇒ **参数集合约束只能写代码** |
| JSON Schema 只支持受限子集（`minLength`/`pattern`/`format`/`minimum` 等写了会 **authorError 报错**） | 框架限制 | 登记；"别用 JSON Schema 做格式校验" |
| `kb_run.args` 是无约束自由数组，**所有内核 CLI 开关自动对每个拿 `kb_run` 的代理开放**（含 `--allow-hallucination-cut`、以及 `transcribe --force` 覆盖已确认逐字稿） | **我们的疏漏**（护栏把手被直通暴露） | ✅ **已修（同日）**：删掉 `kb_run`，改成一阶段一工具（5→11）；例外开关收进主管专属的 `kb_override` 并强制 `reason`。见下「第六轮·补」 |
| `projectsRoot` **只当 cwd、没有包含校验** —— 而 `cordis.patch.yml` 的注释声称"只允许操作它下面的项目" | **承诺的边界不在代码里** | 已实现 `checkProjectsRoot()` → `cause=project_outside_root`（回归：`plugin_smoke` §9） |
| `kb_qc.target` 可选（静默默认 `final`），而选错目标会拿到**完全不同的检查项集合** | 设计隐患（CP2 要的正是 `subtitle_test`） | 改为 `required: true`（保留 `default` 注解） |
| `kb_qc.frames_at` 用 `type:'string'` 表达"一串时间点" | 类型建模错 | 改为 `array`/`items:number`（插件兼容旧式字符串） |
| 一个全局 60 分钟超时照顾不了两头：`qc` 卡死要白等一小时、`transcribe` 又必须给足 | 粒度问题 | 改为**按阶段分档** `STAGE_TIMEOUT_MS` + `config.timeouts.<stage>` 覆盖 |
| 工具描述里 10 处 `+ '' +` 空串拼接、`kb_qc` 描述与上一轮改动不一致 | 可读性/漂移 | 清理并补 `\n` 分段；描述与实现重新对齐 |

⭐ 方法论收获：**审查框架集成时，要先分清"框架不允许"和"我们没做"。**
`additionalProperties` 就是典型：我原本打算"给所有工具加上 `additionalProperties:false`"，
读完 `.d.ts` 才发现**根本加不了** —— 如果直接动手改，会得到一个"看起来加固了、其实无效"的改动。

### 已完成（2026-10-05 第六轮·补2：**拆掉聚合入口，改成一阶段一工具**）

**问题**：`kb_run` 一个工具承载 9 个 stage。而 DSH 的 `toolFilter` **只能按工具名过滤**，
所以这等于把 9 种能力绑在**同一个权限开关**上。实测后果：

- 字幕制作 / 特效设计都拿到 `kb_run` ⇒ 它们能跑 `qc`（绕过 `kb_qc` 的必填 `target`）、
  能 `transcribe --force` 覆盖已确认的 573 段逐字稿、能渲染别人的 overlays；
- `args` 是**自由字符串数组** ⇒ 内核以后新增任何 CLI 开关都**自动**对它们开放。

**修法**：一阶段一工具，5 → 11 个。

| 工具 | 子命令 | 谁有 |
|---|---|---|
| `kb_status` / `kb_plan_cut` | status / cut-plan | 全部 / 剪裁策划 |
| `kb_transcribe` | transcribe | 剪裁策划 |
| `kb_cut` | cut-apply | 剪裁策划 |
| `kb_srt` / `kb_burn` | srt / burn-test\|burn-full | 字幕制作 |
| `kb_assemble` | assemble \| mix | 剪裁策划 + 特效设计 |
| `kb_overlay` | overlay-render | 特效设计 |
| `kb_qc` | qc | 成片质检 |
| `kb_bgm` | bgm-generate | 剪裁策划（唯一花钱） |
| `kb_override` | transcribe --force ／ cut-apply --allow-hallucination-cut | **只有主管** |

连带的三件事：

1. **自由 `args` 彻底消失** ⇒ "新开关自动开放"这类问题从根上没有；
2. 例外开关收进 `kb_override`，且**强制 `reason`（≥10 字）** ⇒ 例外是一次有记录的决定；
3. 护栏改认 `kb_burn(mode="full")`（否则拆工具后**静默失效**）。

**回归怎么守住的**：`plugin_smoke` §10 直接**解析 `agents/subagents.patch.yml`**，
断言"`kb_override` 不在任何子代理白名单里"、"`kb_bgm` 只授给剪裁策划"、
"`kb_qc` 只授给成片质检" 等 —— 权限矩阵是唯一真正的边界，必须由测试守住。

⭐ 附带的判断修正：本文件 §1「工具数压到 5 个」这条论据**站不住**（见 §1 理由三的修正框）。
拆开之后每条描述更短更聚焦，总量并没有变差；`deferLoading` 还能进一步降低常驻开销。

### 已完成（2026-10-04 第三轮：端到端真跑，接入元气中转站）
1. ✅ **完整 harness 真启动** —— `dsh headless` 起真实会话、真实调模型
   （元气中转站 `doubao-seed-2-0-pro-260215`）。模型**直接调用 `kb_status` 工具**拿到
   `next_stage=2_apply`（不是绕道 shell）⇒ 「插件加载 → 工具注册 → 模型可见 → 调用 →
   Python 内核 → 报告回传」全链路打通。
2. ✅ **护栏在真实管线里拦得住** —— 让模型调烧全片（当时是 `kb_run(stage="burn-full")`；
   2026-10-05 拆工具后对应的入口是 `kb_burn(mode="full")`），被 monotonic deny 拒绝，模型无法绕过。
3. ✅ **模型路由接入** —— 元气中转站作为 `llm-pi-ai` 的 `openai-completions` 路由
   （`apiKeyEnv` 引用凭据，**密钥不进配置文件**）；实测 `GET /v1/models` = HTTP 200 / 180 个模型。
4. ✅ **抓到并修掉一个致命静默 bug** —— patch 的 `name` 写**包目录**会导致
   `ERR_UNSUPPORTED_DIR_IMPORT`、插件**静默不激活**（日志只剩一行 `warning: 1 entry did not
   activate`）。改为**入口文件**后复跑通过。
   ⭐ 这个 bug **只有真跑才能发现** —— 桩测试与 `--dump-config` 都看不出来（`--dump-config`
   只做配置合成，不 import 模块）。**这就是坚持端到端验证的价值。**

### 已完成（2026-10-04 第二轮：按官方 `.d.ts` 实现，不靠猜）
1. ✅ **长任务真后台化** —— `ctx.jobs.start` 已接上。
   `run(job)` 同步返回 `{cancel, done}`；子进程 stdout/stderr 经 `job.append(text,{channel})`
   进 output ring；完整 JSON 报告走 `JobOutcome.result`。
   ⭐ **退出码 0 与 2 都记 `status:'completed'`** —— `cause` 是领域结果，不是基础设施故障；
   只有其它退出码 / spawn 失败才记 `'failed'`。
   无 job controller 时 `start()` 抛错 ⇒ **自动退化为同步执行**，返回带 `background:false`
   与 `background_fallback_reason`。
   验证：桩自检真跑 `start → run(handle) → done`，断言解析出 `stage=srt` 的报告、
   输出已 append、`updateProgress` 被调用、`cancel()` 幂等。
2. ✅ **护栏已落地** —— `ctx.tools.guard()` 注册 monotonic deny：
   `kb_burn(mode="full")`（原为 `kb_run(stage="burn-full")`，2026-10-05 拆工具后改名）
   只有在 `renders/subtitle_test_10s.mp4` **与** `edit/tmpimg/frame_*.png` 都在时才放行
   （skill 红线第 7 条）。
   验证：桩自检 4 条断言（拦截缺测试片 / 拦截缺 project / 放行 mode=test / 放行有证据）。
   ⚠️ **边界**：只覆盖**插件工具路径**；模型绕过工具直接用 `bash` 调 ffmpeg 不拦 ——
   **护栏不是沙箱**，真要不可绕过须由部署层 sandbox/权限策略承担。
   ℹ️ 拆工具后，"该不该做"主要已由 `toolFilter` 白名单承担（字幕制作连 `kb_cut` 都没有），
   护栏退为第二道 —— 管的是"顺序错了"，而不是"没权限"。

### 未完成
3. ✅ **完整 harness 启动已验证**（第三轮完成）—— `dsh headless` 真调模型、真调工具。
   另有 `dsh web` / `dsh tui` 两个交互式入口未跑（它们只是同一套 profile + Web/TUI 界面，
   风险低）。
4. ⏳ **路线 C（`dsh plugin --profile web add`）未端到端验证**：
   包内 `dsh.bundle` 指向的 patch 语义未实测；**已验证可用的 `--patch` / bootstrap 脚本路线可作兜底**。
5. ✅ **BGM 素材可自己生成 + 已做成数字断言并真跑过一次**（2026-10-05）：
   - **素材来源**：独立工具 `kb_bgm` 走元气中转站 `suno-v4.5` 纯器乐模式，
     **按次约 0.49 元**。⚠️ **修正**：此前本节写"素材必须由用户提供、没有 agent 会生成音乐"，
     是漏看了 `元气中转站调用文档.md` 里的 `suno-v4.5`。三条硬约束已写进代码：
     **付费前必查价**（超 `max_price` 拒绝提交）、**绝不重复提交**（`task_id` 立刻落盘，
     再调即续轮询）、**402 就停**。`--dry-run` 只查价不花钱。
   - **长片**：Suno 单曲**上限 4 分钟**，而片子常 20 分钟 ⇒ `mix` 默认 `bgm_loop=true`
     （`-stream_loop -1` 循环到片长）；关掉循环才检查音乐是否够长。
   - **混音**：`volume=…:enable='between(t,a,b)'` + `afade` 淡入淡出，比 skill 原始的
     "BGM 分段音量 + acrossfade 拼接 + amix" 更短。
   - **质检**：6 项 BGM 断言（`bgm_source_exists` / `present` / `not_overpowering` /
     `true_peak_ok` / `covers_duration` / `has_fade_out`）。
   - **真跑记录**：生成 174.9s mp3（3.5MB，`cost=0.4896`）→ 循环补齐 600s 片长 →
     qc 报 `Δ+5.70 LU` → `bgm_overpowers_voice` → 按 cause 把 volume 0.12→0.08 重跑 →
     `Δ+3.40 LU`，qc 全过；抽 590s 处响度确认**音乐真铺到了片尾**。
   - ⚠️ **剩余边界（本质限制，不是缺口）**：**模型听不到声音**（DSH 无读音频工具），
     所以"响度是否合适"可以由数字判，"**听感是否舒服**"只能由人确认 ——
     交付时应如实标注"听感未经人工确认"。
6. ⏳ **HyperFrames 动效未真跑** `overlay-render`（本机 `课件实验/talking-head-project/hyperframes-ppt`
   有真实工程可试）。
7. ⏳ **`assemble` 的 xfade 递推式与 skill 原文有分歧**：见 `docs/contracts.md` D-1，
   已加"预测时长 vs 实测"校验防静默出错，但**需要你拍板用哪个式子**。
8. ✅ **子代理挂载已实测**（第五轮）—— `provider: spawn` 是 `ctx.subagents` **后端名**（不是 LLM 路由），
   模型由子代理**继承父代理**，无需配 `model`；已真派发成功。
