# koubo-agent · 口播剪辑多代理协作系统

本仓库包含两种编排实现：`plugin/` + `agents/` 是早期的 DeepSeek Harness 版本，
`langgraph_app/` 是后续的 LangGraph 版本。两者共用 `pipeline/` 和 `kb.py` 中的
视频处理内核。

LangGraph 版本由主管 agent 协调剪裁策划、字幕制作、特效设计和成片质检四个子 agent；
底层通过 FFmpeg、转录模型及 HyperFrames 处理视频。`langgraph_app/webapp/`
提供本地 Web 界面。运行时生成的项目视频、会话数据库和密钥不包含在仓库中。

下文主要记录 DeepSeek Harness 版本的设计和验证过程；LangGraph 实现见
[`langgraph_app/`](langgraph_app/) 与 [`docs/architecture.md`](docs/architecture.md)。

## LangGraph 离线验证

在安装项目运行依赖的 Python 环境中执行：

```bash
python -m pytest langgraph_app/tests -q -p no:cacheprovider
```

这组测试使用桩替代真实模型和视频编码。运行完整流程仍需自行配置 FFmpeg、
转录模型、HyperFrames 和模型网关密钥；本仓库不提供这些外部依赖或样片。

## DeepSeek Harness 版本

把 `koubo-video-pipeline` 那套口播剪辑流程，从"一个人从头做到尾"改造成
**主管 + 4 个专业子代理协作**的系统，跑在 DeepSeek Harness（DSH）上。
每个环节都有明确的 agent 负责 —— 子代理手里用的是确定性工具，但**责任始终落在具体的 agent 身上**。

> 核心判断：**6 道工序不该做成 6 个 agent**（会互相踩、上下文被撑爆），
> 但**每一道工序都要有明确的责任人**。所以：4 个专业子代理各领一段，
> 机械动作由**它们调用的工具**执行 —— 工具是执行力，责任在 agent。
> 详见 [`docs/architecture.md`](docs/architecture.md) §1。

## 现状一句话

**已经真跑通了**：DSH 用元气中转站（`api.lk888.ai`）作模型路由 → 插件加载 → 模型**直接调用** `kb_status`
拿到 `next_stage=2_apply` → 护栏在真实管线里拦住了违规的 `burn-full`。
唯一未做的是 HyperFrames 真渲染与 `dsh plugin add` 路线（见文末）。

**2026-10-05 做了一轮全量代码审查与加固**（不启动 harness、不花一分钱）：修掉 9 类真实缺陷，
其中 4 类属于"文档写了、代码里没有"或"断言看起来在跑、其实从未运行"——
包括**付费前查价失败会被静默放过**、**提交后不落盘 task_id 会重复扣费**、
**`kb_qc(subtitle_test)` 恒报假 cause**、**铁律第一条只在提示词里**。
逐条见 [`docs/contracts.md`](docs/contracts.md) §四 D-4 与 `docs/architecture.md` §7。

**同日还把工具层重构了**：删掉"一个工具承载 9 个阶段"的 `kb_run`，改成
**一阶段一工具（5 → 11 个）**。原因是 DSH 的 `toolFilter` **只能按工具名授权**，
聚合入口等于把 9 种能力绑在同一个权限开关上 —— 实测字幕制作/特效设计因此能跑质检、
能 `transcribe --force` 覆盖已确认逐字稿。现在**谁能做什么由白名单直接决定**，
自由 `args` 彻底消失，偏离红线的例外只走主管专属的 `kb_override`（强制写 `reason`）。
详见 [`docs/architecture.md`](docs/architecture.md) §7「第六轮·补2」。

---

## 快速开始

### 0. 前置
- Python（带 `faster-whisper 1.2.1` / `ctranslate2 4.8.1`）
- FFmpeg **full build（含 libass）**：`ffmpeg -version` 能看到 `--enable-libass`
- `faster-whisper-large-v3` 模型目录（本机 `D:/workbuddy工作目录/models/`）
- Node ≥18 + 一份已安装的 DSH（`npm i @deepseek-ai/dsh`）

### 1. 验证确定性内核（不依赖 DSH）
```bash
cd koubo-agent
python kb.py status --project projects/_smoke
```
退出码：`0` 通过 / `2` 业务性失败（带 `cause`）/ `1` 异常。

### 2. 冒烟（已有实测数据，几十秒）
```bash
python kb.py cut-plan  --project projects/_smoke
python kb.py cut-apply --project projects/_smoke     # 不加 --run 则只生成脚本不编码
python kb.py srt       --project projects/_smoke
python kb.py qc        --project projects/_smoke --target final
```
实测（真实 573 段口播）：`cut-plan` 关键词命中 14 / 幻觉嫌疑 6 / >5s 空白 17 →
`cut-apply` 删 5 段留 568 段、**1328.46s → 1185.90s** →
`srt` 合并 18 处幻觉链 → **525 条**、无 BOM、句末符号 0 →
`qc` 负例 `exit=2 causes=[cut_duration_mismatch, srt_timeline_mismatch]`、正例 **10/10 PASS**。

### 3. 链接 DSH 依赖（Windows，一次性）
ESM **不认 `NODE_PATH`**，裸模块名必须在解析链上有真实 `node_modules`：
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\link-dsh-deps.ps1 `
  -DshNodeModules "D:\dsh-sandbox\node_modules"
```
⛔ 删联接**只能** `[System.IO.Directory]::Delete(path,$false)`；`Remove-Item -Recurse` 会删掉**目标**真实数据。

### 4. 离线验证（都不启动 harness、不调模型）
```bash
python tools/kernel_selftest.py     # 内核字段成形（时间轴 / off_block 明细 / 写回通道不污染决策）
python tools/kernel_guard_test.py   # 内核护栏与失败路由（幻觉段 / 越界 / qc 作用域 / dry-run / 付费三项）
python tools/bgm_mix_test.py        # 混音 + BGM 六项数字断言（真跑 ffmpeg）
python tools/bgm_generate_test.py   # BGM 生成走**本地桩服务**（零真钱，含"续轮询不重复扣费"）
python tools/overlay_sync_test.py   # 动效叠加合成（造真透明 MOV，抽帧比像素）
node tools/plugin_smoke.mjs         # 插件：导出形态 / 护栏拒放 / 后台作业 / 退化同步 / 超时上限
node tools/registry_smoke.mjs       # 挂**真实** ToolRuntime，断言 ctx.tools.schemas() 有 11 个工具、覆盖内核 12 个子命令
node tools/guard_real_test.mjs      # 真注册表上证明"钱花不出去"
node tools/check-plugin-import.mjs  # 诊断 loader 用哪种 import 形态（见下方「头号坑」）
```
实测（2026-10-05）：`kernel_selftest` 全绿 · `kernel_guard_test` **40/40** ·
`plugin_smoke` **89/89** · `registry_smoke` **22/22** · `guard_real_test` 全绿 ·
`bgm_mix` / `bgm_generate` / `overlay_sync` 全绿 · 导入形态诊断通过（入口文件 URL 可用，目录 URL 不可用）。

> ℹ️ **工具参数的 JSON Schema 有硬边界**（实测 `@deepseek-ai/dsh-tools`）：参数**根对象天生开放**，
> 写不了 `additionalProperties: false`，模型多传的字段会被**静默忽略**；且只支持一个受限子集
> （`minLength`/`pattern`/`format` 等写了会直接报错）。⇒ **参数集合的约束只能写在插件代码里**。
> 详见 [`docs/contracts.md`](docs/contracts.md) §一「工具参数 JSON Schema 的能力边界」。
> 另：`project` 必须落在 `config.projectsRoot` 之下（越界给 `cause=project_outside_root`）——
> 这条边界以前只是 `cordis.patch.yml` 里的一句注释，现在真的在代码里。

> ⚠️ **`node` 那几项依赖真实的 `node_modules`**。本仓库的 `node_modules` 是指向已安装 DSH 的
> **目录联接**；如果你在**沙箱化**的 shell 里跑（例如某些 IDE 的受限终端），遍历联接会得到
> `OSError [WinError 448] 无法遍历该路径，因为它包含不受信任的装入点`，而 `Get-ChildItem`
> 只是**返回 0 项、不报错** —— 很容易被误判成"依赖没装"。请在普通终端里跑；
> 实在不行就把 `node_modules` 真拷贝到临时目录再跑（拷贝 462MB 约 15 秒）。

### 5. 一次性挂到 DSH 上
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\bootstrap-dsh.ps1 `
  -DshHome "D:\dsh-home" -CredentialsDoc "$HOME\Desktop\元气中转站调用文档.md"
```
脚本会（幂等）：
1. 把密钥写进 `$DshHome\.credentials.yaml`（`version: 1` + `refs`）—— **密钥不进任何配置文件**
2. 把两条 patch 追加进 `$DshHome\profiles\web\cordis.patch.yml`：
   - **模型路由**：元气中转站作为 `llm-pi-ai` 的一条 `openai-completions` 路由（`apiKeyEnv` 引用凭据）
   - **插件**：`plugin-koubo`（11 个工具：`kb_status` / `kb_plan_cut` / `kb_transcribe` / `kb_cut` /
     `kb_srt` / `kb_burn` / `kb_assemble` / `kb_overlay` / `kb_qc` / `kb_bgm` / `kb_override`）

不跑脚本也行 —— 用 `--patch` 直接叠加（本机已验证）：
```bash
dsh --profile web --patch profile/yuanqi-provider.patch.yml --patch profile/cordis.patch.yml \
  --patch agents/subagents.patch.yml --dump-config
```

### 6. 真跑一次
```bash
dsh headless --patch profile/yuanqi-provider.patch.yml --patch profile/cordis.patch.yml \
  --patch agents/subagents.patch.yml \
  "调用 kb_status 看 projects/_smoke 的 next_stage"
```
实测输出：模型**直接调用** `kb_status` 工具（不是绕道 shell），返回 **`2_apply`**。

### 7. 挂 4 个子代理（可选）

```bash
dsh --profile web \
  --patch profile/yuanqi-provider.patch.yml \
  --patch profile/cordis.patch.yml \
  --patch agents/subagents.patch.yml
```

❗ **DSH 没有「名册目录」**（本机 296 个官方包里没有 `dsh-subagent-library`）：
子代理 = 挂一个 `@deepseek-ai/dsh-tool-subagent` 实例，配置就在 `agents/subagents.patch.yml`。

- `provider` 是 **`ctx.subagents` 后端名**（`spawn`），**不是 LLM 路由** —— 别填 `yuanqi`；
- 模型**跟随父代理继承**，所以**不需要配** `agentOptions`；
- **patch 顺序有讲究**：插件在前（先注册 `kb_*`），子代理在后 ——
  因为 `toolFilter.allow` 里的工具名在实例化时校验，未知名字会 fail-loud。

最后把 `agents/director.md` 作为会话的系统提示词载入。

---

## ⛔ 头号坑：插件"静默不激活"

`cordis.patch.yml` 里 `name` **必须指向入口文件，不能指向包目录**：
```yaml
- insert:
    - id: plugin-koubo
      name: '../plugin/src/index.js'    # ✅ 入口文件
      # name: '../plugin'               # ❌ 目录 → ERR_UNSUPPORTED_DIR_IMPORT
```
写目录时 loader 会归一化成目录 URL，ESM 不支持目录导入，结果是**整个插件不激活、工具全部消失**，
而启动日志里只有一行极易忽略的：
```
dsh: warning: 1 entry did not activate
plugin-koubo (file:///…): failed to import
```
第一次真跑 headless 时就是这样被抓出来的 —— 模型因为找不到 `kb_status`，绕道用 `pwsh` 执行脚本，
甚至还去读了我的插件源码。**诊断脚本：`node tools/check-plugin-import.mjs`。**

---

## 已验证到哪一步（诚实分级）

| 层级 | 验证方式 | 结果 |
|---|---|---|
| 确定性内核 | 真实 573 段口播跑阶段 2→3→6 | ✅ 见 §2 数字 |
| **内核护栏与失败路由** | `kernel_guard_test.py` 40 项（幻觉段/越界/坏文件/qc 作用域/dry-run/付费三项） | ✅ 全绿 |
| 插件代码 | 桩 ctx 自检 89 项（含**权限矩阵**断言） | ✅ 全绿 |
| 工具注册 | 真实 ToolRuntime + `ctx.tools.schemas()` | ✅ **11 个工具**，覆盖内核 12 个子命令，无自由 `args` |
| 配置合成 | `dsh --dump-config`（真实 loader） | ✅ 零报错，两处 patch 正确落地 |
| **完整 harness** | **`dsh headless` 真调模型** | ✅ **模型直接调用 `kb_status` → `2_apply`** |
| **红线护栏** | **headless 里真让它调 `burn-full`** | ✅ **被拒绝**，模型无法绕过 |
| 模型路由 | 元气中转站 `GET /v1/models` + 实跑 | ✅ HTTP 200 / 180 模型 / 真实对话成功 |
| 后台作业 | 桩 self-check 真跑 `start→run→done` | ⚠️ 真实 harness 里未单独验（未触发长阶段） |
| `dsh plugin add` 路线 | — | ⏳ 未验（`--patch` 路线已可用并推荐） |
| HyperFrames 动效 | — | ⏳ 未真跑 `overlay-render` |

---

## 目录结构

```
koubo-agent/
├─ kb.py                     统一入口（唯一 CLI；DSH 侧只 shell 它）
├─ pipeline/                 确定性内核（纯 Python，零 LLM）stage1..7
├─ plugin/src/index.js       DSH 工具插件（**一阶段一工具，11 个** + 2 条 monotonic 护栏 + 后台作业）
├─ agents/director.md        ★ 主管编排提示词
├─ agents/subagents.patch.yml  4 个子代理挂载层：剪裁策划 / 字幕制作 / 特效设计 + 成片质检
├─ docs/architecture.md      分层、回退设计、DSH 能力映射、实测事实、未完成项
├─ docs/contracts.md         工具契约 / 产物契约 / ★回退矩阵 / 偏离登记
├─ profile/cordis.patch.yml         插件部署层（含「必须指向入口文件」的坑）
├─ profile/yuanqi-provider.patch.yml 元气中转站模型路由 + 默认模型
├─ tools/plugin_smoke.mjs      桩自检（39 项）
├─ tools/registry_smoke.mjs    真实注册表验证（11 项）
├─ tools/check-plugin-import.mjs 导入形态诊断（防"静默不激活"）
├─ tools/link-dsh-deps.ps1     建依赖联接
├─ tools/bootstrap-dsh.ps1     一键挂到 DSH（幂等）
├─ tools/kernel_selftest.py    内核回归自检（含"跨删除区句子"合成用例）
├─ tools/kernel_guard_test.py  ★内核护栏与失败路由回归（40 项；只花时间不花钱）
├─ tools/overlay_sync_test.py  动效叠加合成（造真透明 MOV，抽帧比像素；8 项）
├─ tools/bgm_mix_test.py       BGM 混音与质检（6 用例／16 项；含"探针完整性"守门）
├─ tools/bgm_generate_test.py  BGM 生成（suno-v4.5）★ 本地桩服务，17 项，含"付费闸门/续轮询不重复扣费"
├─ tools/guard_real_test.mjs  ★ 真注册表防花钱验证（内核闸门 / 幂等闸）
└─ projects/                 各条片子的工作目录
```

## 两个铁律（写进代码，不是文档口号）

1. **删段 0-based + 越界即拒**（`index_out_of_range`）；**幻觉段（时长 ≤0.5s 却 ≥6 字）
   时间轴绝不裁**，只在字幕层合并。判定由 `common.is_suspect_hallucination()` 统一提供
   （plan 与 apply 共用，规则不会漂移），`cut-apply` **见到就拒绝**：
   `cause=hallucination_cut_forbidden`。例外必须由主管显式传
   `args:["--allow-hallucination-cut"]`，且会记进 `cut_report.json` ——
   **例外是一次有记录的决定，不是静默的默认行为。**
   ⚠️ 2026-10-05 之前这条**只写在提示词里**（本文档却说"写进代码"）；现已真的落到断言上，
   回归见 `tools/kernel_guard_test.py` 的 A 组。
2. **字幕行 = Whisper 保留段本身**。不做碎段合并、不做机械折行、句末禁一切符号、
   重映射容差 0.6 绝不 clamp、单调化只推后不砍前。重映射只有**一份实现**
   （`common.remap_time()`），字幕与剪后时间轴共用 —— 两处各写一份必然漂移。

## 与 skill 的关系

本仓库是 `koubo-video-pipeline` skill 的**工程化外壳**：8 条红线被翻译成断言与提示词约束；
第 7 条（烧全片前必出 10s 测试片）已升级为**不可撤销护栏**，并在真实管线里验过拦截。
skill 里两处与另一份文档冲突、一处公式在 3 段以上存疑 ——
**登记在 [`docs/contracts.md`](docs/contracts.md) §四「偏离登记」，等你拍板**。
