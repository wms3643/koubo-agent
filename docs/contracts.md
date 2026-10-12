# 交付契约（tools / artifacts / 回退矩阵）

本文件是**三方对齐的单一事实源**：内核实现了什么、主管要求子代理产出什么、
失败时回退到哪。改任何一处契约，必须同步改另外两处，否则系统会静默跑偏。

---

## 一、十一个工具的工具契约

⭐ **2026-10-05 起：一个确定性阶段 = 一个工具，不再有 `kb_run(stage=…)` 聚合入口，
也不再接受自由 `args` 数组。** 理由见本节末尾「为什么必须一阶段一工具」。

| 工具 | 性质 | 输入 | 输出（canonical JSON） | 会不会写盘 |
|---|---|---|---|---|
| `kb_status` | 只读 | `project` | `stages_done` / `next_stage` / `artifacts[]` / 规格 | 否 |
| `kb_plan_cut` | 只给证据 | `project` | `segment_count` / `stats` / 写入 `plan_context.json` | 写证据文件 |
| `kb_transcribe` | 执行（GPU 长任务） | `project`, `device?`(enum), `language?`, `background?` | 转写报告；产出 `transcript.json` + `.txt` + `.srt` | 是 |
| `kb_cut` | 执行 | `project`, `run?`, `background?` | 剪裁报告（含 `timeline` 计数 / `hallucination_cut`）；产出 `cut_blocks.json` / `timeline_map.json` / `run_cut.sh` / （`run:true`）`edited_final.mp4` | 是 |
| `kb_srt` | 执行 | `project` | 字幕报告（`subtitle_count` / `checks` / `off_block_segments[]`） | 是 |
| `kb_burn` | 执行 | `project`, **`mode`（必填，enum test/full）**, `background?` | 烧录报告 + 抓帧路径；`mode=full` 受护栏 | 是 |
| `kb_assemble` | 执行 | `project`, **`mode`（必填，enum assemble/mix）**, `spec?`, `dry_run?`, `background?` | 缝接/混音报告（`predicted_duration` / `actual_duration` / `checks`） | 是 |
| `kb_overlay` | 执行（慢） | `project`, `output?`, `background?` | `{ok, output, format:"mov/ProRes4444"}` | 是（MOV） |
| `kb_qc` | 只断言+取证 | `project`, **`target`（必填，enum）**, `frames_at`（number[]） | `checks[]`（每项带 `cause`）、`causes[]`、帧路径、`frame_failures[]` | 写报告+帧图 |
| `kb_bgm` | **执行 · 唯一会花钱** | `project`, `dry_run?`, `confirm_paid?`, `background?` | BGM 报告（含 `cost` / `refunded` / `task_id` / `bytes`） | 是（音频 + 报告） |
| `kb_override` | ⛔ **主管专用 · 例外放行** | `project`, **`action`（enum retranscribe / allow_hallucination_cut）**, **`reason`（必填 ≥10 字）**, `background?` | 对应阶段报告 + `override_action` / `override_reason` | 是 |

**共同约定**（全部工具一致，模型可直接依赖）：
- 退出码：`0` 通过 ／ `2` **业务性失败**（带 `cause`，可路由）／ `1` 环境或代码异常（需人工看 traceback）。
  插件把 `exit_code` 一并回带给模型，`ok=false` 时**必须先读 `cause`**。
- 所有 stage 的 stdout **只输出一个 JSON 对象**；中文在部分终端可能不显示，
  所以模型应以**写盘的报告文件**为准，不要依赖终端回显。
- 时间单位一律秒（float），字幕时间码 `HH:MM:SS,mmm`。
- 段索引一律 **0-based**，直连 `transcript.json` 下标。
- 长阶段（`background?` 参数）传 `true` 即走 DSH 作业运行时，返回 `{ok, background:true, job_id, ...}`；
  无 job controller 时**自动退化为同步**并带 `background_fallback_reason`。

> ⭐⭐ **为什么必须一阶段一工具（2026-10-05 复审结论）**
>
> DSH 的 `toolFilter` **只能按工具名过滤、不能按 stage 过滤**。
> 所以"一个工具承载 N 个 stage"等于**把 N 种能力绑在同一个权限开关上**。
> 改前的实测后果（`kb_run` 承载 9 个 stage）：
>
> | 谁 | 拿到了 `kb_run` | 于是它实际能做 |
> |---|---|---|
> | 字幕制作 | ✅ | 跑 `qc`（绕过 `kb_qc` 的必填 target）、`transcribe --force` 覆盖已确认逐字稿 |
> | 特效设计 | ✅ | 同上，外加渲染别人的 overlays |
> | 任何人 | ✅ | `args` 是**自由字符串数组** ⇒ 内核 CLI 新增任何开关都自动对它开放 |
>
> 为了补这个洞，只能在别处加护栏（`burn-full`）—— 而护栏的可见性依赖注册层级，
> 项目自己就实测过"桩里拦得住、真跑没拦"。**粒度选错之后，只能拿更脆的机制去垫。**
>
> 改成"一阶段一工具"后：
> - 谁能做什么 = `toolFilter` 白名单**直接**决定，不再靠提示词；
> - 自由 `args` 消失 ⇒ 没有"新开关自动开放"这类问题；
> - 例外开关（`--force` / `--allow-hallucination-cut`）只存在于 `kb_override`，
>   而它**不在任何子代理的白名单里** ⇒ 只有主管能拍板，且必须写明 `reason`。
>
> 代价：工具数 5 → 11。用 `kb_*` 前缀 + 一句话描述把选择成本压住（DSH 的 `deferLoading`
> 也可用于进一步降低提示词占用）。

**共同约定**（全部工具一致，模型可直接依赖）：
- 退出码：`0` 通过 ／ `2` **业务性失败**（带 `cause`，可路由）／ `1` 环境或代码异常（需人工看 traceback）。
  插件把 `exit_code` 一并回带给模型，`ok=false` 时**必须先读 `cause`**。
- 所有 stage 的 stdout **只输出一个 JSON 对象**；中文在部分终端可能不显示，
  所以模型应以**写盘的报告文件**为准，不要依赖终端回显。
- 时间单位一律秒（float），字幕时间码 `HH:MM:SS,mmm`。
- 段索引一律 **0-based**，直连 `transcript.json` 下标。

### 工具 ↔ 内核子命令 ↔ 阶段细节

| 工具 | 子命令 | 是否慢 | 前置产物 | 产出 | 常见 cause |
|---|---|---|---|---|---|
| `kb_transcribe` | `transcribe` | 慢（GPU） | `input/raw.mp4` | `edit/transcript.json` + `.txt` + `.srt`（word 级时间戳） | `transcript_missing_timestamps`、**`transcript_exists`**（已有逐字稿则拒绝覆盖；覆盖只能由主管走 `kb_override`） |
| `kb_cut` | `cut-apply` | 中（`run:true` 才编码） | `edit/edit_decisions.json` | `edited_transcript.txt` / `cut_blocks.json` / **`timeline_map.json`（剪后时间轴）** / `run_cut.sh` / `edited_final.mp4` | `index_out_of_range`、`empty_keep_set`、`bad_spec`、**`hallucination_cut_forbidden`**（删了幻觉嫌疑段；放行只能由主管走 `kb_override`） |
| `kb_srt` | `srt` | 快 | `transcript.json` + `cut_blocks.json`（+ 可选 `edit_decisions.json` / `subtitle_fixes.json` 的 `text_fixes`） | `edit/subtitles/final.srt` + `srt_report.json`（含 `off_block_segments[]` 明细） | `empty_srt`、`bad_spec` |
| `kb_burn(mode=test)` | `burn-test` | 快 | `edited_final.mp4` + `final.srt` | `renders/subtitle_test_10s.mp4` + 抓帧（**至少 1 张**，否则 exit=2） | `missing_input`、`missing_srt` |
| `kb_burn(mode=full)` | `burn-full` | 中 | 同上 + **护栏要求已有测试片与抓帧** | `renders/subtitled_full.mp4` | 同上 + 时长不一致 |
| `kb_assemble(mode=assemble)` | `assemble` | 慢（`dry_run:true` 秒级） | `edit/assemble.json`（`segments[]` + 可选 **`overlays[]`**） | `renders/final.mp4`（已叠加动效层）；`dry_run:true` 时**不产文件**、只校验+预测时长 | `bad_spec`（含"转场比段长还长"）、`missing_input`、**`missing_overlay`** |
| `kb_assemble(mode=mix)` | `mix` | 中 | `edit/mix.json`（含 `bgm`、`bgm_volume_sections[]`、`bgm_fade_in/out`、**`bgm_loop`**） | 铺完 BGM 的 `final.mp4` | `bad_spec`、`missing_input`、`bgm_missing`、`bgm_silent`、`bgm_overpowers_voice`、`bgm_true_peak`、`bgm_abrupt_cut` |
| `kb_overlay` | `overlay-render` | 慢 | `overlays/index.html` + 本地 hyperframes | `overlays/renders/*.mov` | `missing_overlay_project`、`missing_hyperframes`、`hyperframes_render_failed` |
| `kb_override(action=retranscribe)` | `transcribe --force` | 慢 | —— | 覆盖 `transcript.json`（下游全部作废） | 缺 `reason` → `override_reason_required` |
| `kb_override(action=allow_hallucination_cut)` | `cut-apply --allow-hallucination-cut --run` | 中 | `edit/edit_decisions.json` | 放行删除集里的幻觉嫌疑段并执行裁切 | 同上 |

> ⭐ **`dry_run` 的语义（2026-10-05 修正）**：`kb_assemble(mode="assemble", dry_run=true)`
> 现在**真的不渲染** —— 只做前置校验（片段/叠加层存在、转场合法）+ 按时长递推**预测**输出时长，
> 不写任何文件。此前它只是把 `dry_run: true` 写进报告、ffmpeg 照跑（参数名骗人）。

### `kb_qc` 检查项的**作用域**（2026-10-05 修正）

`target` 决定检查项集合，**不能一套检查打天下**：

| 检查项 | subtitle_test | edited | subtitled | final |
|---|---|---|---|---|
| `streams_present` / `av_duration_equal` / `audio_no_clipping` / `no_black_frame_*` | ✅ | ✅ | ✅ | ✅ |
| `srt_no_bom` / `srt_has_entries` / `srt_no_trailing_symbol` / `srt_monotonic` | ✅ | ✅ | ✅ | ✅ |
| `srt_timeline_matches_video` / `edited_duration_vs_plan` | ⛔ **不做** | ✅ | ✅ | ✅ |
| `bgm_*` | ⛔ | ⛔ | ⛔ | ✅ |

> ⛔ **为什么 `subtitle_test` 必须排除那两项**：`final.srt` 是全片的（示例 1185.9s），
> 而测试片只有 10s。拿全片 SRT 去比测试片时长 ⇒ **恒定报 `srt_timeline_mismatch`**，
> 把 CP2「看图判测试片」污染成一个假 cause —— 而它的回退目标是"回剪裁策划"，
> 属最贵的一类。同理 `edited_duration_vs_plan` 也不能拿 `final.mp4` 去比
> （装配的 xfade 转场本来就会吃掉时长）：它优先量 `renders/edited_final.mp4`。

### 工具参数 JSON Schema 的**能力边界**（2026-10-05 实测，取自 `@deepseek-ai/dsh-tools`）

DSH 的 `defineTool` 收的是一套**简写 DSL**（`{type, required:true, description, enum, items, oneOf, additionalProperties}`），
真实发给模型的 JSON Schema 由 `parameterSchemaSpecToJsonSchema()` 投影。实测结论（`node tools/dump_schemas.mjs`）：

1. **投影是可靠的**：per-property 的 `required: true` 会被**上提**成顶层 `required: [...]`，
   属性里不再残留 `required`（否则就是非法 JSON Schema，严格网关会拒收）。
2. **只支持一个受限子集**：`type` / `oneOf`（恰好 2+ 分支）/ `properties` / `required` /
   `additionalProperties`（**只能是 boolean**）/ `items` / `enum` / `const`，外加
   `description` / `title` / `default` / `examples` 这几个**注解**（`default` 确实会出现在线上 schema 里）。
   ⚠️ `minLength` / `pattern` / `format` / `minimum` / `minItems` / `uniqueItems` **不在子集里**
   —— 写了会 **authorError 直接报错**（是"拒绝"，不是"静默不生效"）。所以**别指望用 JSON Schema 做格式校验**。
3. ⛔ **参数根对象天生是开放的**：`ParameterSchemaSpec` 的官方注释就是 *"The map itself is an implicit
   open object root"* ⇒ **无法**声明 `additionalProperties: false`。模型多传的字段（例如
   `kb_status(project, force: true)`）会被**静默忽略**，不会报 `INVALID_ARGS`。
   ⇒ **参数集合的约束没有 schema 层出口，只能写在插件代码里。**
   这解释了为什么"一阶段一工具"（§一开头）是**唯一**能收紧权限的做法：参数只能声明"有哪些"，
   声明不了"不许有别的东西"，所以**能少一个工具就少一个工具**、绝不能让一个工具承载多种能力。
   也是为什么例外开关（`--force` / `--allow-hallucination-cut`）要**藏进 `kb_override`**，
   而不是作为普通参数暴露 —— "靠 schema 限制"在这里根本不存在。
4. 明确的 object 节点（如 `output.schema`）**必须**写 `additionalProperties`（boolean）—— 这是硬性要求。

**由此定下的两条工程规矩**：

- **要给模型"可选参数"，就给 `enum`/`required`/`type`；要给"受限参数集合"，只能写代码校验。**
- 工具描述里写"默认值"是有用的（`default` 注解也会进线上 schema），但**行为上的默认值仍由代码兜**。

### 超时（按阶段分档）

| 阶段 | 默认上限 | 说明 |
|---|---|---|
| `status` | 2 分钟 | 只读、秒级 |
| `cut-plan` / `srt` / `burn-test` / `qc` | 10 分钟 | 快阶段 —— 卡住时要**尽快**报 `timeout`，而不是白等一小时 |
| `bgm-generate` | 20 分钟 | 生成通常 1~3 分钟 |
| `cut-apply` / `burn-full` / `assemble` / `mix` / `overlay-render` | 30 分钟 | 编码级 |
| `transcribe` | 60 分钟 | GPU 级 |

覆盖方式：`config.timeouts.<stage>`（毫秒）> 上表 > `config.timeoutMs`（兜底）。
超时会**杀掉子进程**并回带 `cause=timeout` + `timeout_ms`。

### `kb_bgm`（独立工具，唯一会花钱）

| 入参 | 说明 |
|---|---|
| `project` | 项目目录 |
| `dry_run` | `true` = 只查价并返回计划，**不提交、不花钱** |
| `confirm_paid` | `true` = 确认**这次真花钱**（约 0.49 元）。⛔ 不传只返回价格与提示、**绝不提交** |
| `background` | 生成通常 1~3 分钟，建议 `true` |

前置：`edit/bgm.json`（`style` / `mv` / `output` / `max_price` / `poll_timeout`），
其中 `style` **必须 ≥20 字**（付费护栏会拦）。
产出：`bgm/bgm.mp3` + `edit/bgm_report.json`（含 `cost` / `refunded`）+ `bgm/task.json`。
常见 cause：`bgm_exists`（已有产物 ⇒ 直接复用）、`bgm_needs_confirm`（未确认付费，
**默认就会走到这里**）、`bgm_no_api_key`、`bgm_price_above_budget`、
**`bgm_pricing_unavailable`**（**查不到单价 ⇒ 拒绝提交**）、`bgm_insufficient_balance`、
`bgm_submit_failed`、`bgm_generate_failed`、`bgm_timeout`、`bgm_download_failed`。

> ⭐ **闸门顺序（2026-10-05 定稿，改动需同步测试）**：
> `续轮询 → bgm_exists（免费） → bgm_needs_confirm → bgm_pricing_unavailable → 提交`。
> 三条理由，缺一条都会退化成"静默花钱"或"误导模型"：
> 1. **`bgm_exists` 排在付费确认之前** —— 已有成品时正确答复是"直接去 mix"。
>    排在后面会让模型先撞到 `bgm_needs_confirm`，被引导去加 `--confirm-paid`（方向是错的）。
> 2. **`bgm_needs_confirm` 必须早于"查价是否可用"** —— 契约承诺"不传 `confirm_paid`
>    只返回价格与提示、绝不提交"，这是模型最常撞到的第一道门。
> 3. **`bgm_pricing_unavailable` 早于提交** —— 以前 `price=None`（pricing 报错/超时/
>    响应结构变了）会被**静默放过**，等于最该拦的那次（信息不全）恰恰没拦；
>    现在"查不到价"本身就是拒绝理由。同时 pricing 响应也走 `_unwrap`（兼容网关的
>    `data` 包装），否则预算闸门会静默失效。

> ⛔ **为什么付费闸门在内核而不是插件护栏**：`ctx.tools.guard()` 的可见性取决于它被注册到
> **哪一层**（`guardReason` 只查 `layers.global` 与 `exec.agent` 的 scope 链），
> 2026-10-05 实测出现过「桩里拦得住、真跑没拦住、真花了 0.4896 元」。
> ⇒ **手上有钱的动作，闸门放在可测、绕不过的内核里**。

### 后台化与护栏（2026-10-04 补齐，接口取自官方 .d.ts）

**后台化**：带 `background` 参数的工具传 `true` 即走 DSH 的作业运行时，长阶段不再占住一次工具调用。
带该参数的工具：`kb_transcribe` / `kb_cut` / `kb_burn` / `kb_assemble` / `kb_overlay` /
`kb_bgm` / `kb_override`。
- `JobSpec = { kind:'koubo', label, owner: exec.agent.id, outputLimitBytes, run(job) => JobHooks }`
- `run(job)` **同步**返回 `{ cancel, done }`；子进程 stdout/stderr 经
  `job.append(text, { channel:'stdout'|'stderr' })` 进入 output ring，供 `job_output` 实时读取。
- ⭐ **退出码 0 与 2 都记为 `status:'completed'`** —— `cause` 是**正常的领域结果**，
  不是基础设施故障；只有其它退出码或 spawn 失败才记 `'failed'`。
- 完整 JSON 报告放进 `JobOutcome.result`，模型在**结算后首次读取**时一次性拿到。
- 无 job controller 时 `ctx.jobs.start()` 会抛错 ⇒ 插件**自动退化为同步执行**，
  返回对象里带 `background:false` 与 `background_fallback_reason`（不会静默丢结果）。
- 这七个工具共用 `runStage()` **一份**实现（复制七遍必然漂移）。

**护栏（monotonic deny）**：由 `ctx.tools.guard()` 注册，在 `tools/pre-execute` **之后**求值；
guard 没有 "allow" 返回值 ⇒ 后续监听器**无法把拒绝翻回允许**。
- 规则①：`kb_burn(mode="full")` 只有在 `renders/subtitle_test_10s.mp4`
  **与** `edit/tmpimg/frame_*.png` 都存在时才放行（skill 红线第 7 条）。
- 规则②：`kb_bgm` **付费**调用前必须已有写好的 `edit/bgm.json`（`style` ≥20 字；`dry_run` 除外）。
- 拒绝时返回可读理由，并指引「先 `kb_burn(mode="test")` → `kb_qc(target="subtitle_test")` → 再重试」。
- ⚠️ **能力边界（诚实）**：护栏只覆盖**插件工具路径**。若模型绕过工具直接用 `bash` 调 ffmpeg，
  护栏不拦 —— **它是护栏，不是沙箱**。要真正不可绕过，须由部署层的 sandbox/权限策略承担。
  ℹ️ 不过 2026-10-05 拆工具后，"该不该做"这件事主要已由 `toolFilter` 白名单承担，
  护栏退为第二道（针对"顺序错了"而不是"没权限"）。

---

## 二、产物契约（文件是唯一的跨代理交接面）

| 文件 | 谁产出 | 谁消费 | 关键字段 |
|---|---|---|---|
| `edit/transcript.json` | stage1 | stage2/3、剪裁策划 | 段数组 `{id,start,end,text,words[]}`（兼容 `{segments:[...]}` 包装） |
| `edit/plan_context.json` | stage2 `plan` | **剪裁策划** | `segments[{idx(0-based),start,end,dur,chars,cps,text,keywords[],suspect_hallucination}]`、`long_gaps[]`、`repeat_clusters[]`、`stats` |
| `edit/edit_decisions.json` | **剪裁策划** | stage2 `apply`、stage3 | `source`、`drop_segment_ids[]`(0-based)、`text_fixes{idx:文本}`、`notes` |
| `edit/subtitle_fixes.json` | **字幕制作** | stage3 `srt` | `text_fixes{idx:文本}`；与 decisions 的 `text_fixes` **合并**，同 idx 覆盖前者 |
| `edit/cut_blocks.json` | stage2 `apply` | stage3、stage5、qc | 每块 `edit_start/edit_end/orig_start/orig_end/first_seg/last_seg`（`first_seg` 为 **1-based**） |
| `edit/subtitles/final.srt` | stage3 | stage4 | UTF-8 **无 BOM**、句末无符号、时间单调 |
| `edit/qc_report.json` | stage6 | **审片子代理 + 主管** | `checks[]`、`causes[]`、`frames[]`、`junction_frames[]` |
| `edit/timeline_map.json` | **剪裁策划**（stage2 `cut-apply`） | **特效设计**、成片质检 | **剪后**时间轴：`segments[{source_start,source_end,edit_start,edit_end,text,words[{w,s,e}]}]`（一条记录两套坐标） |
| `edit/assemble.json` | **剪裁策划**（缝接 + 铺 BGM）／ **特效设计**（叠加动效层） | stage5 `assemble` / `mix` | `segments[{path,kind}]`、**`overlays[{path,start?,end?}]`**、`transition`、`output` |
| `edit/mix.json` | **剪裁策划** | stage5 `mix` | `video`、`bgm`、`bgm_volume_sections[]`、`bgm_fade_in`(默认0.5)、`bgm_fade_out`(默认1.5)、`bgm_loop`(默认true)、`output` |
| `edit/bgm.json` | **剪裁策划** | kb `bgm-generate` | `style`（风格描述）、`mv`、`sample_rate`、`bitrate`、`output`、`max_price`、`poll_timeout` |
| `bgm/bgm.*` | **剪裁策划**（调 `suno-v4.5` 生成，或用户提供） | stage5 `mix` | mp3，48kHz 立体声；**单曲上限 4 分钟** ⇒ 长片靠 `bgm_loop` 循环补齐 |
| `bgm/task.json` | kb `bgm-generate` 自写 | kb `bgm-generate`（**续轮询**依据） | `task_id`、`state`、`cost`、`refunded`。⚠️ **看到它就要续轮询，不要重新提交**（防重复扣费） |
| `overlays/index.html` | **特效设计** | kb `overlay-render` | 根元素须带 `data-composition-id="main"` + 宽高 |
| `overlays/renders/*.mov` | **特效设计**（调 `overlay-render`） | **特效设计**（调 `assemble` 叠加进成片） | ProRes4444 带 alpha，**已按剪辑后时间轴排布** |

> ⚠️ **`first_seg`/`last_seg` 是 1-based，`drop_segment_ids`/`idx` 是 0-based。**
> 这是本项目唯一允许混用两套编号的地方，因为 `cut_blocks.json` 沿用实战脚本的既有格式。
> 任何新增消费方都必须先看清用的是哪一套。

---

## 二·补、⭐ 音画同步机制（动效层怎么对上人物说话）

动效层要把"说到某个词"的时刻做成动画。**先明确一件事：全项目有且只有两份时间轴。**

| | 剪**前**时间轴 | 剪**后**时间轴 |
|---|---|---|
| 文件 | `edit/transcript.json` | **`edit/timeline_map.json`** |
| 谁产出 | **剪裁策划**（转写时建立） | **剪裁策划**（剪切完成时立即建立） |
| 坐标 | 原片秒（word 级） | 成片秒（段级 + 词级），**并同时带 `source_*` 原片坐标** |
| 条数 | 全片所有段（示例 573） | 保留段数（示例 **568**） |
| 回答什么 | "这句**原来**在哪" —— 剪辑判断、切点回退、追溯 | "动效该在**第几秒**" —— 音画同步 |
| 谁在用 | `剪裁策划` | `特效设计`、`成片质检` |

⭐ **两份时间轴都归剪裁策划** —— 因为时间轴是**剪辑决定的**（剪掉一段，后面全部前移）。
剪完片它就建立剪后时间轴 ⇒ **特效不必再等字幕阶段**（此前它"搭字幕阶段的车"，是个隐式耦合，现已解除）。

⚠️ **剪辑改变了时间轴**（实测素材 1328.46s → 1185.90s，删掉 142.56s）。
所以**剪前那份绝不能用于动效**：每过一个剪点，后面的时间就累积偏移 —— 越到后面错得越多。

> 📌 **为什么剪后时间轴归剪裁策划**：时间轴是**剪辑的直接结果** —— 剪掉一段，后面全部前移。
> 剪切完成时 `cut_blocks.json` 已给出完整映射，重映射所需的一切都已就绪，
> 所以这一步**天然属于剪辑**，而不是"顺手挂在字幕阶段"。
>
> 📌 **收益**：`特效设计` 只依赖剪辑、**不再依赖字幕** ⇒ 特效可与字幕线并行。

`timeline_map.json` 的条目形状（一条记录里同时给两套坐标，不必再去别的文件对照）：

```json
{"segment_count": 568,
 "source_duration": 1328.46, "edit_duration": 1185.9,
 "segments": [
   {"source_start": 16.5, "source_end": 20.44,
    "edit_start": 0.0,  "edit_end": 3.94,
    "text": "市场是商品经济的范畴",
    "words": [{"w": "市场", "s": 0.0, "e": 0.72}]}]}
```

`s` / `e` 是**剪辑后**的词级时间（动效要用它）；段级同时给两套坐标便于追溯与交叉验证。

> ⚠️ **它与字幕不强行 1:1**：时间轴按「实际说出的每一句」（示例 568 条），
> 字幕按显示需要合并幻觉链后（示例 525 条）。**特效按前者更准**（它要贴的是真实发声时刻）。

**三条铁律**：

1. ⛔ **绝不能用 `edit/transcript.json` 的时间** —— 那是剪辑**前**的时间轴，每过一个剪点就累积偏移
   （实测素材被删掉 142.56s）。拿它做动画会「越到后面错得越多」。
2. `剪裁策划` 在剪切完成那一刻**调用工具**建立剪后时间轴（`common.build_timeline()`，唯一实现）——
   **不让 LLM 自己算**。确定性计算由代码执行、责任归 agent，这是本项目一贯的分工。
3. 渲染出的 MOV 已按剪辑后时间轴排布 ⇒ `assemble` 合成时 **`overlay=0:0` 从 0 点对齐即可**，
   **不需要任何时间偏移**。同步是「渲染时就对齐」，不是「合成时再修」。

> 回归验证：`tools/kernel_selftest.py`（时间轴单调 / 词级嵌套在句区间内 / 条数 == 保留段数）
> ＋ `tools/overlay_sync_test.py`（造真透明 MOV，抽帧比像素，证明叠加生效且 alpha 正确）。

---

## 二·补2、⭐ BGM（谁负责、素材从哪来、为什么是数字断言）

### 责任

**`剪裁策划` 负责 BGM** —— 它是 `mix` 这一步的执行者，摊在它身上有两个硬理由：

1. **BGM 的分段音量必须跟着「章节边界」走**，而章节边界是它自己缝接出来的产物。换一个 agent 做，
   就得先跨 agent 交接一遍边界数据，而对方还不懂这些边界是怎么来的。
2. `assemble`（缝接）与 `mix`（铺 BGM）在**同一条编码链**上（画面定型之后、叠加动效之前）。
   拆给两个 agent 会导致 base 视频被编两遍。

### 素材从哪来：**默认自己生成**（2026-10-05 修正）

`mix.json` 的 `bgm` 只是**路径**。素材有三种来源，优先走第 1 种：

| 来源 | 谁做 | 说明 |
|---|---|---|
| ① **自己生成**（默认） | 剪裁策划调 `kb_bgm` | 走元气中转站 `suno-v4.5` 纯器乐模式，**按次约 0.49 元** |
| ② 用户指定曲目 | 用户给文件/链接 | 用户明确要某首曲子时用这个 |
| ③ 复用已生成的 | 剪裁策划 | `bgm/` 下已有可用曲目时直接用，不重复付费 |

> ⚠️ **修正记录**：本节原先写"没有任何 agent 会生成音乐、素材必须由用户提供"。
> 那是我漏看了 `元气中转站调用文档.md` 里的 `suno-v4.5`（文档只重点写了图像/视频 + TTS，
> 音乐那条在"已验证过的常用模型"列表里）。**能力是有的，所以不该把活推给用户。**

**`bgm-generate` 的三条硬约束**（都写进代码，不是口号）：

1. ⛔ **付费前必查价**：先 `GET /skills/models/suno-v4.5/pricing`；
   超过 `max_price`（默认 1 元）**直接拒绝提交**，返回 `bgm_price_above_budget`。
   `--dry-run` 只查价不提交、不花钱。
2. ⛔ **绝不重复提交**：提交后**立刻把 `task_id` 落盘**到 `bgm/task.json`；
   再次调用时若发现有未完成任务，**续轮询**而不是重新提交（否则重复扣费）。
   超时不代表失败 —— 返回 `bgm_timeout`，再调一次就是续轮询。
3. ⛔ **402 就停**：余额不足返回 `bgm_insufficient_balance`，**不重试**。

**长片怎么办**：Suno 单曲**最长 4 分钟**，口播片常 20 分钟 ⇒ `mix` 默认 `bgm_loop=true`
（`-stream_loop -1` 循环输入再 `atrim` 到片长）。关掉循环才会检查"音乐是否够长"。

> ⚠️ 用环境变量传密钥，**不落进任何配置文件**：`LK_API_KEY` → `YUANQI_API_KEY`
> → `LK_API_KEY_FILE` 指向的文件 → 默认读桌面那份明文文档。`LK_BASE_URL` 可覆盖网关地址
> （自测时指向本地桩服务）。

### 为什么 BGM 的响度必须测成数字、不能交给 agent 判断

⛔ **质检员听不到声音。** DSH 只有 `read_image`，**没有读音频的工具** ⇒
"BGM 到底有没有进去""有没有盖住人声""片尾是不是被硬切"这三件事，
**没有任何 agent 能靠听来判断**。硬要派给 agent，它只能瞎猜。

所以它们被做成了 `stage6_qc._bgm_checks()` 的**数字断言**，做法是
**把混音产物与混音前的纯人声对比**（`mix.json` 同时记录了 `video` 与 `output`，两者时间轴一致，
响度差就是 BGM 的贡献量）：

| 检查项 | 判据 | 失败 cause |
|---|---|---|
| `bgm_source_exists` | 素材文件存在 | `bgm_missing`（停下问人） |
| `bgm_present` | ΔLUFS ≥ 0.3（BGM 真进了混音） | `bgm_silent` |
| `bgm_not_overpowering` | ΔLUFS ≤ 4.0（不抢戏、人声清晰） | `bgm_overpowers_voice` |
| `bgm_true_peak_ok` | 真峰值 ≤ −1.0 dBFS | `bgm_true_peak` |
| `bgm_covers_duration` | BGM 时长 ≥ 片长 − 淡出 | `bgm_abrupt_cut` |
| `bgm_has_fade_out` | `bgm_fade_out > 0` | `bgm_abrupt_cut` |

> ⚠️ 后两项**不去猜混音后的听感**（那需要"听"），而是**直接查素材与参数**：
> 音乐比片长短就会中途断掉；没设淡出就会被硬切。这两件都是确定性事实。
>
> 回归验证：`tools/bgm_mix_test.py` —— 真跑 `kb.py mix` → `kb.py qc`，
> 五个用例（正常 / 过响 / 静音 / 素材缺失 / 铺不满）。

---

## 三、回退矩阵（主管裁决的唯一依据）

**原则：工具能确定性修好的，绝不叫 LLM 重做；只有"判断错了"才回退到子代理。**

| 失败 cause | 现象 | 回退目标 | 成本 |
|---|---|---|---|
| `srt_bom` | SRT 带 BOM → libass 全片无字幕 | 让字幕制作重跑 `kb_srt` | 极低 |
| `srt_trailing_symbol` | 句末带标点 | 重跑 `srt` | 极低 |
| `srt_timeline_overlap` | 字幕时间重叠 | 重跑 `srt` | 极低 |
| `cut_duration_mismatch` | 实测时长 ≠ 保留块计划 | 回 `剪裁策划` 复核保留块 | 中 |
| `srt_timeline_mismatch` | 字幕整体与画面脱节 | 回 `剪裁策划` | 中 |
| `junction_glitch` | 切点吞字/跳帧 | 回剪裁，把切点回退 0.1~0.3s 重跑 `cut-apply` | 中 |
| `audio_clipping` | 削顶爆音 | 同 `junction_glitch` | 中 |
| `av_desync` | 音视频轨时长不一致 | 同 `junction_glitch` | 中 |
| `subtitle_occlusion` | 字幕挡脸 | 改 `force_style`（先调 `MarginV`，**越大越往上**）后重烧 | 低 |
| `black_frame` | 首尾黑帧 | 回 **剪裁策划**（重跑缝接） | 中 |
| `missing_overlay` | 动效层文件缺失 | 回 **特效设计**（重跑 `overlay-render`） | 低 |
| `bgm_needs_confirm` | 付费操作未确认 | **不是错误**：先 `args:["--dry-run"]` 看价，确认要花再加 `args:["--confirm-paid"]` | — |
| `transcript_exists` | 已有逐字稿，转写拒绝覆盖 | **不是错误**：直接复用（复用 573 段那份）。确要重转才显式传 `args:["--force"]`，并清楚下游全部要重跑 | — |
| `bgm_missing` | BGM 素材文件不存在 | 回 **剪裁策划**：先跑 `bgm-generate` 生成；用户已指定曲目则取回文件 | 低（约 0.49 元） |
| `bgm_no_api_key` | 找不到网关密钥 | 回 **剪裁策划**：设 `LK_API_KEY` / `LK_API_KEY_FILE`；或请用户提供曲目 | — |
| `bgm_price_above_budget` | 单价超 `max_price` | 回 **剪裁策划**：调预算或改用用户提供的曲目（**不许硬顶着花钱**） | — |
| `bgm_insufficient_balance` | 网关余额不足（402） | **停下升级人工**：充值或改用现成曲目。⛔ **禁止反复提交** | — |
| `bgm_timeout` | 生成任务超时（未 final） | 回 **剪裁策划**：**再调一次即续轮询**（不会重复扣费），别重新提交 | 低 |
| `bgm_generate_failed` / `bgm_submit_failed` / `bgm_download_failed` | 生成/提交/下载失败 | 回 **剪裁策划**：先看 `bgm/task.json` 与响应体；确认未创建任务才可重试 | 低 |
| `bgm_silent` | 混音后与纯人声几乎无差 ⇒ BGM 没进去 | 回 **剪裁策划** 复核 `mix.json` 参数重跑 `mix` | 低 |
| `bgm_overpowers_voice` | BGM 抢戏、人声不清晰 | 回 **剪裁策划** 下调对应 section 的 `volume` 重跑 `mix` | 低 |
| `bgm_true_peak` | 真峰值超 −1 dBFS，易削顶 | 回 **剪裁策划** 整体降音量重跑 `mix` | 低 |
| `bgm_abrupt_cut` | 音乐中途断掉 / 片尾被硬切 | 回 **剪裁策划**：换够长的音乐、或补 `bgm_fade_out` 重跑 `mix` | 低 |
| `qc_probe_failed` | 质检探针没返回可解析结果 | **不得当作通过**；先修探针（多半是 ffmpeg 统计被读到 stdout 而非 stderr） | — |
| `missing_stream` / `spec_mismatch` | 缺流 / 规格不符 | **停下升级人工**，禁止自动重试 | — |
| `hyperframes_render_failed` | 动效渲染失败 | 回 `特效设计`（多半是根元素缺 `data-composition-id` 或用了 Google Fonts `<link>`） | 中 |

**三·补 · 2026-10-05 补齐的 cause（此前只存在于代码、矩阵里没有 ⇒ 主管查不到回退目标）**

| 失败 cause | 现象 | 回退目标 | 成本 |
|---|---|---|---|
| `hallucination_cut_forbidden` | `cut-apply` 发现删除集里含幻觉嫌疑段（时长 ≤0.5s 却 ≥6 字） | 回 **剪裁策划**：把它们从 `drop_segment_ids` 移除（字幕层会自动合并）；确需删除须由主管显式传 `args:["--allow-hallucination-cut"]`，该决定会记进 `cut_report.json` | 极低（改文件重跑） |
| `bad_spec` | 规格/决策文件写错（非法 `drop_segment_ids` 元素、非数字 `text_fixes` 键、缺 `video`/`bgm`、`transition` 超过段长…） | 回 **产出该文件的子代理** 修文件后重跑对应 stage（工具重跑，确定性） | 极低 |
| `empty_keep_set` | 删除集覆盖了全部片段 | 回 **剪裁策划** 复核保留块（不可能没人说话） | 低 |
| `index_out_of_range` | 删除下标越界（多半把 0-based 写成了 1-based） | 回 **剪裁策划** 改下标重跑 | 极低 |
| `srt_empty` | 重映射后一条字幕都不剩 | 回 **剪裁策划**（保留块判断出了问题），不要手写 SRT | 中 |
| `bgm_pricing_unavailable` | 查不到 `suno-v4.5` 单价 ⇒ 拒绝提交 | 回 **剪裁策划**：确认网关可达与密钥权限，先 `--dry-run` 复查看价；⛔ 不许靠调高预算绕过 | — |
| `qc_probe_failed` | 质检探针没返回可解析结果（含 A/V 轨级 duration 缺失、抓帧失败、`volumedetect`/`ebur128` 无输出） | **不得当作通过**；先修探针（多半是 ffmpeg 统计被读到 stdout 而非 stderr），再重跑 `kb_qc` | — |
| `timeout` | 同步工具调用超过该阶段上限（见 §一「超时（按阶段分档）」）被杀 | 长阶段改用 `background:true` 走作业运行时；或调大 `config.timeouts.<stage>` / `config.timeoutMs` 后重试同一个 stage | — |
| `project_outside_root` | `project` 不在插件配置的 `projectsRoot` 之下 | 回 **对应子代理** 把项目放到根下；确需在别处工作则改 `config.projectsRoot`（或 `KB_PROJECTS`）后重启 harness。⛔ 不是"重做一遍专业判断" | 极低 |
| `kernel_missing` / `python_missing` | 找不到 `kb.py` / 找不到带 faster-whisper 的解释器 | **停下升级人工**：改 `config.kbEntry` / `config.python`（或 `KB_ENTRY` / `KB_PYTHON`） | — |
| `transcript_bad_shape` | 逐字稿不是非空段列表 | 回 **剪裁策划**：先跑 `kb_transcribe` | 低 |
| `override_reason_required` | 调 `kb_override` 时 `reason` 少于 10 字 | 补上理由再调（**例外必须是一次有记录的决定**） | 极低 |
| `missing_file` | 决策/规格文件不存在 | 回 **对应子代理**：先产出该文件 | 极低 |
| `kernel_exception` | 内核代码/环境异常（**不该出现**；出现即 bug） | **停下升级人工**，附 `traceback_tail`。⚠️ 决策文件写错**不该**走到这里 —— 那应报 `bad_spec` | — |
| `kernel_crashed` / `kernel_business_failure` | 内核未返回可解析 JSON | 看 `stdout_tail`/`stderr_tail`；多为环境问题（Python/ffmpeg 缺失）⇒ 升级人工 | — |
| `aborted` | 调用被 `exec.signal` 中止（用户打断） | 用 `kb_status` 看断点，按 `next_stage` 续跑 | — |

### 护栏（防烧钱，主管必须硬执行）
- ⛔ **动效层不得默认跳过**：`特效设计` 是**独立的专业岗**，不是可选扩展。
  只有用户**明确要求**出无动效版才可省略，且须在交付说明里注明"本片未含动效层"。
- `retake_budget = 5`：单个阶段累计回退不超过 5 次。
- ⛔ `cross_agent_loop_budget = 1`：**跨 agent 回路每片只允许 1 次**。
  即 `字幕制作` 报「跨删除区句子必须救回」→ 回 `剪裁策划` 改保留块，
  整片只能发生一次；第 2 次停下报告用户。
  代价说明：保留块一旦变更，字幕全部失效、`edited_final.mp4` 需重新编码 ——
  **比工具级重跑贵一个数量级**。
  该预算**独立于** `retake_budget`：后者按阶段计，管不住跨 agent 回路。
- **同一 cause 连续 2 次未解决 → 停止重试，向用户报告**（不是第三次重试）。
- 任何"更贵"的回退（重转写、重新生图/生视频）**必须先问用户**，这是一条红线：
  skill 规定"偏离先报批 —— 报告冲突 → 列至少 2 个选项 → 等用户拍板"。
- 交付前**必须**先出 10s 测试片并抓帧确认，禁止"差不多直接烧全片"。

---

## 四、偏离登记（技能要求 vs 本实现的冲突，须用户拍板）

### D-1 · xfade offset 递推式（3 段及以上）
- **skill 原文**：`acc = acc + 前段时长; off = acc - 0.5`。
- **本实现**：`off_k = 已累积输出时长 − T`（官方 xfade 语义；offset 相对第一路时间轴）。
  skill 的式子在第 2 段场景与之一致（`off = d0 - T`），但从第 3 段起会**偏移一个 T**，
  转场会落到累积输出的末尾之外。
- **防错措施**：生成后**预测总时长并与实测比对**，偏差 > 0.5s 即报 `duration_close=false`，
  不会静默出片。
- **待用户裁决**：若你确认 skill 式子在你手上出片正确，请把递推式改回；否则沿用本实现。

### D-2 · SRT 编码（无 BOM vs utf-8-sig）
- **skill**：⛔ 必须 **UTF-8 无 BOM**（带 BOM → libass 报 `Invalid data` → 全片无字幕）。
- **`WORKBUDDY_口播剪辑工具说明.md`**：写 "UTF-8 with BOM（Windows 软件更稳定识别）"。
- **本实现**：按 skill 走**无 BOM**（实战脚本 `build_srt_final.py` 也是无 BOM），
  并在 `srt_report.json` 里把 `has_bom` 作为断言项暴露出来，冲突可被立刻发现。
- **待用户裁决**：那份说明文档的这条建议会直接导致烧录失败，建议修正。

### D-3 · 字幕样式 `MarginV`
- skill 用 `MarginV=26`，说明文档用 `MarginV=20`。本实现按 **skill 的 26**，
  并允许审片子代理在发现遮挡时**只调这一个参数**重烧（不必重做整条流水线）。

### D-4 · 本轮（2026-10-05）审查后的**实现变更登记**

不是"与 skill 冲突"，而是"我们改了自己的行为"。列在这里以便回溯。

| 编号 | 变更 | 为什么 |
|---|---|---|
| D-4.1 | `cut-apply` **新增内核级拒绝** `hallucination_cut_forbidden`，例外须 `--allow-hallucination-cut` | 铁律第一条此前**只写在提示词里**（README 却声称"写进代码"）。删幻觉段会把说话声切断，是历史效果差的根因 ⇒ 变成可执行断言 |
| D-4.2 | `assemble --dry-run` **真的不渲染** | 此前只是把 `dry_run:true` 写进报告、ffmpeg 照跑（参数名骗人） |
| D-4.3 | `kb_qc` 的 `srt_timeline_matches_video` / `edited_duration_vs_plan` **按 target 收敛** | 见 §一「`kb_qc` 检查项的作用域」；此前 `subtitle_test` 会恒报假 `srt_timeline_mismatch` |
| D-4.4 | `bgm-generate` 新增 `bgm_pricing_unavailable`，闸门顺序改为 `exists → needs_confirm → pricing → 提交` | "付费前必查价"此前只要查价失败就**静默跳过**；且"已有成品"的免费结论排在付费确认之后会误导模型 |
| D-4.5 | 提交后**先落盘 `task_id` 再判成败** | HTTP 非 200 但任务已创建时，旧逻辑不落盘 ⇒ 下次重复提交 ⇒ **重复扣费** |
| D-4.6 | 决策/规格文件写错一律报 `bad_spec`（退出码 2） | 此前非法 `text_fixes` 键会崩成 `kernel_exception`（退出码 1，"需人工看栈"），而它明明是文件问题、可自动路由 |
| D-4.7 | `read_json` 容忍 UTF-8 BOM | `edit_decisions.json` / `subtitle_fixes.json` 由 agent 或人手写，Windows 编辑器很容易带 BOM |
| D-4.8 | `av_duration_equal` **恒出现**；拿不到轨级 duration 时报 `qc_probe_failed` | 此前会从检查列表里**静默消失** —— 正是本项目最忌讳的"断言静默失效" |
| D-4.9 | 同步工具调用落地 `config.timeoutMs`（默认 60 分钟）→ `cause=timeout` | 该配置此前**定义了从未使用**；长阶段卡死会让工具调用永久挂住 |
| D-4.10 | `build_timeline` 返回 `(segments, stats)`，`timeline_map.json` 带 `stats` | 契约说"条数 == 保留段数"，但空文本/缺时间戳/越界段会被跳过，此前**静默** |
| D-4.11 | `kb_qc.target` 由"可选、默认 final"改为 **`required: true`**（保留 `default` 注解） | 选错目标会拿到**完全不同的检查项集合**，CP2 要的正是 `subtitle_test`；静默默认 final 太容易踩 |
| D-4.12 | `kb_qc.frames_at` 由 `type:'string'`（逗号分隔）改为 **`array`/`items:number`** | 语义是"一串时间点"，用字符串建模会诱导模型传数组；插件里兼容旧式字符串 |
| D-4.13 | 新增 `checkProjectsRoot()`：`project` 必须在 `config.projectsRoot` 之内，否则 `cause=project_outside_root` | `projectsRoot` 此前**只被当作子进程 cwd**，没有任何包含校验；而 `cordis.patch.yml` 的注释写着"kb_* 工具只允许操作它下面的项目" —— **承诺的边界不在代码里** |
| D-4.14 | 超时改为**按阶段分档**（`STAGE_TIMEOUT_MS` + `config.timeouts.<stage>` 覆盖） | 一个全局 60 分钟照顾不了两头：`qc` 卡死要白等一小时，`transcribe` 又必须给足 |
| D-4.15 | `kb_bgm` 的 `bgm_exists` hint 不再教模型传 `--force` | 那个开关**没有暴露成参数**（防重复付费），hint 指向了一个模型根本走不通的路 |
| D-4.16 | 工具描述去掉 `+ '' +` 空串拼接、补 `\n` 分段；`kb_qc` 描述同步上一轮的作用域修改 | 描述是发给模型的，拼成一段难读；`kb_qc` 描述此前与实现不一致（"SRT 超出时长"已按 target 收敛） |
| D-4.17 | ⭐ **删掉 `kb_run`，改成一阶段一工具**（5 个工具 → 11 个）；`args` 自由数组彻底消失 | 一个工具承载 9 个 stage = 9 种能力共用一个权限开关。实测字幕制作/特效设计因此能跑 `qc`、能 `transcribe --force`、能渲染 overlays；且内核任何新开关都自动对它们开放 |
| D-4.18 | 新增 `kb_override`（`retranscribe` / `allow_hallucination_cut`，强制 `reason`），**只授给主管** | 例外开关此前作为自由 `args` 对每个拿 `kb_run` 的代理开放。现在它是独立工具名，而 `toolFilter` 只能按工具名过滤 —— 这才是真隔离 |
| D-4.19 | 烧全片护栏改认 `kb_burn(mode="full")` | 原规则挂在 `kb_run(stage="burn-full")` 上；拆工具后必须跟着改，否则护栏静默失效 |
| D-4.20 | `on/off` 边界进回归：`plugin_smoke` §10 解析 `subagents.patch.yml` 断言权限矩阵 | 白名单是唯一真正的权限边界，属于**必须被测试守住**的东西（尤其"`kb_override` 不在任何子代理里"） |

---

## 五、子代理契约（谁负责哪一段）

**划分依据：谁最懂这个产物，就归谁** —— 每个子代理领**一整段完整职责**（从输入到交付），不切成碎片。

| 子代理 | 专业 | **工具白名单（= 权限边界）** | 输入 | 完整产出 | 它要判断的事 |
|---|---|---|---|---|---|
| `剪裁策划` | 剪辑 | `kb_status` `kb_plan_cut` `kb_transcribe` `kb_cut` `kb_assemble` `kb_bgm`（+ read/write） | `input/raw.mp4`、`edit/plan_context.json`、`edit/bgm.json`（自己写） | `edit_decisions.json`、`cut_blocks.json`、**`timeline_map.json`（剪后时间轴）**、`edited_final.mp4`、**`bgm/bgm.mp3`（自己生成）**、**`assembled.mp4`（缝接 + BGM 的成片骨架）** | 哪几段该消失（幻觉段**绝不能**删）；片段怎么缝、BGM 用什么风格 |
| `字幕制作` | 字幕 | `kb_status` `kb_srt` `kb_burn`（+ read/write） | `transcript.json`、`cut_blocks.json` | `subtitle_fixes.json`、`final.srt`、`subtitle_test_10s.mp4`、`subtitled_full.mp4` | 断句是否破铁律、跨删除区句子该救还是该弃、文本是否还有错 |
| `特效设计` | 视觉包装 | `kb_status` `kb_overlay` `kb_assemble`（+ read/write/edit） | **`edit/timeline_map.json`（剪后时间轴）** | `overlays/index.html`、`overlays/renders/*.mov`、**`renders/final.mp4`** | 动效放哪、放什么、不遮谁、何时进出 |
| `成片质检` | 质量把关 | `kb_status` `kb_qc`（+ read/read_image/write） | `renders/*.mp4`、帧图、`qc_report.json` | 裁决建议（**不写产物**） | 字幕是否真烧进、是否挡脸、接缝是否跳帧、**动效层是否遮脸/压字幕/掉帧** ⇒ 能否交付 |
| **主管** | 编排 | **全部**（含 `kb_override`）；不碰专业产物 | —— | —— | 排程、派发、按 §三 裁决回退、交付（**不碰专业产物**） |

> ⛔ **`kb_override` 只给主管**：它是唯一承载"偏离红线例外"的工具
> （重转写 `--force`、放行删幻觉段），且强制要求 `reason`。
> 回归测试 `tools/plugin_smoke.mjs` §10 会解析本文件的 YAML **断言它不在任何子代理白名单里** ——
> 这条边界不是靠自觉，是靠测试守住的。

**两条硬规矩**：

1. **文件归属不交叉**。`edit_decisions.json` 只由 `剪裁策划` 写；
   `subtitle_fixes.json` 只由 `字幕制作` 写。交叉写会造成**不可恢复的产物丢失**
   （覆盖 decisions 会连带抹掉整个删除集）。
2. **子代理只报 `cause`，不自行回退**。回退目标一律由主管按 §三 裁决 ——
   避免"每个子代理都以为自己该重做一遍"。
3. **`text_fixes` 分工不重叠**：`剪裁策划` 只改**影响它判断删留**的文本；
   `字幕制作` 负责**全部纯文本纠错**（专名 / 数字 / 同音字）。
   合并规则"字幕覆盖剪裁"只是兜底，不是分工。

> ⚠️ 能力边界：`toolFilter` 只能按**工具名**过滤、不能按路径过滤，所以第 1 条目前是
> **提示词约束 + 文件分离**，不是硬隔离。要硬隔离须在插件 `tools/pre-execute` 里按
> `exec` 的目标参数判定（见 §一 护栏那节的能力边界说明）。
