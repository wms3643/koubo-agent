/**
 * dsh-plugin-koubo —— 把口播剪辑流水线的确定性内核暴露为 **11 个模型可见工具**
 * （一个确定性阶段／子命令对应一个工具），并挂两条**不可撤销的护栏**：
 * （①）烧全片前必须已有 10s 测试片；（②）付费生成 BGM 前必须先写好 edit/bgm.json 的风格描述。
 *
 * 设计要点（与 docs/architecture.md 一致）：
 *   1. **工具薄、内核厚**：本文件零业务逻辑，只做参数装配 + 子进程调用 + JSON 解析。
 *   2. ⭐ **一个阶段一个工具：不设 `kb_run(stage=…)` 聚合入口，也不接受自由 `args` 数组。**
 *      理由（2026-10-05 复审结论）：DSH 的 `toolFilter` **只能按工具名过滤、不能按 stage 过滤**
 *      ⇒ 一个工具承载 N 个 stage，等于把 N 种能力绑在**同一个权限开关**上。
 *      实测后果（改前）：字幕制作与特效设计都拿到 `kb_run`，于是它们能跑 `qc`
 *      （绕过 `kb_qc` 的必填 target）、能 `transcribe --force` 覆盖已确认的逐字稿、
 *      能渲染 overlays —— "只有谁做某事"全靠提示词和护栏补。
 *      改成"一阶段一工具"后，**谁能做什么由 toolFilter 白名单直接决定**。
 *      附带收益：自由 `args` 数组消失 ⇒ "内核新增任何开关都会自动对模型开放"这类问题
 *      从根上没有了；例外开关（`--force` / `--allow-hallucination-cut`）只存在于 `kb_override`，
 *      而它**不在任何子代理的白名单里**。
 *   3. **判断与执行分离**：`kb_plan_cut` 只产出证据；决策文件由剪裁策划写出后，
 *      `kb_cut` 才机械执行并断言越界。
 *   4. **失败即数据**：内核以退出码 2 表示"业务性失败"并回带 `cause`；工具原样交给模型，
 *      模型据此按「回退矩阵」路由，而不是笼统重做。
 *   5. **长阶段真后台**：走 `ctx.jobs.start`，输出进 job output ring，最终 JSON 报告
 *      经 `JobOutcome.result` 一次性交给模型。无 job controller 时**优雅退化为同步**。
 *   6. **护栏是 monotonic deny**：用 `ctx.tools.guard()`（在 `tools/pre-execute` 之后求值，
 *      后续监听器无法把拒绝翻回允许），落实 skill 第 7 条红线。
 *   7. **偏离红线的例外只走 `kb_override`**（重转写 `--force` / 放行"删幻觉段"），
 *      必须写明 `reason`（写进报告、可回溯）；它是**主管的工具**。
 *
 * 工具清单（11）与它对应的内核子命令：
 *   kb_status→status          kb_plan_cut→cut-plan      kb_transcribe→transcribe
 *   kb_cut→cut-apply          kb_srt→srt                kb_burn→burn-test|burn-full
 *   kb_assemble→assemble|mix  kb_overlay→overlay-render kb_qc→qc
 *   kb_bgm→bgm-generate       kb_override→transcribe --force ／ cut-apply --allow-hallucination-cut
 *
 * 接口依据（均取自本机安装的官方包 .d.ts，非推测）：
 *   · `ToolRuntime.guard(guard: ToolGuard): () => void`
 *     `ToolGuard = (exec: Readonly<ToolExecution>) => string | undefined`
 *   · `ToolExecutionInput = { callId, name, arguments, agent?, signal, ... }`
 *   · `Agent.id: SessionId`（可直接作 `JobSpec.owner`）
 *   · `JobRegistry.start(spec: JobSpec): JobId`（**同步**返回 id）
 *     `JobSpec = { kind, label, owner?, outputLimitBytes?, output?, run(job) => JobHooks }`
 *     `JobHandle = { id, append(text, {channel?}), updateProgress(line) }`
 *     `JobHooks = { cancel(reason?), done: Promise<JobOutcome> }`
 *     `JobOutcome = { status: 'completed'|'killed'|'failed', detail?, result? }`
 *
 * ⛔ 致命坑（官方 postmortem 0001）：本插件**绝不能** `export default`，
 *    否则 Loader 的 `unwrapExports` 会折叠模块、丢掉 `inject`。
 */

import { spawn } from 'node:child_process'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { defineTool } from '@deepseek-ai/dsh-tools'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 插件包根（含 package.json）：<repo>/plugin */
const PACKAGE_ROOT = path.resolve(HERE, '..')
/** 仓库根（kb.py 与 pipeline/ 所在）：<repo>。npm 安装时不存在，需靠 config 指定。 */
const REPO_ROOT = path.resolve(PACKAGE_ROOT, '..')

/**
 * 运行环境默认值。可用环境变量或 cordis.patch.yml 里的 config 覆盖：
 *   KB_PYTHON      —— 带 faster-whisper 的解释器（本机已验证路径见 skill）
 *   KB_ENTRY       —— kb.py 绝对路径
 *   KB_PROJECTS    —— 项目根目录（工具只允许操作它下面的项目）
 */
const DEFAULTS = {
  python: process.env.KB_PYTHON
    || 'C:/Users/lenovo/.workbuddy/binaries/python/envs/default/Scripts/python.exe',
  kbEntry: process.env.KB_ENTRY || path.join(REPO_ROOT, 'kb.py'),
  projectsRoot: process.env.KB_PROJECTS || path.join(REPO_ROOT, 'projects'),
  // `timeoutMs` 是**兜底**上限（未知 stage / 没在下面列出的 stage 用它）。
  // 单阶段可用 `config.timeouts.<stage>` 覆盖（毫秒）。
  timeoutMs: 60 * 60 * 1000,
  timeouts: {},
  ringLimitBytes: 64 * 1024,
}

/**
 * 每个阶段的**默认**超时上限（毫秒）。
 *
 * ⚠️ 为什么不能只用一个全局 60 分钟：快阶段（`qc` / `status`）万一卡住
 * （ffmpeg 探针挂死、大目录 IO 卡住），模型要**白等一小时**才拿到 `cause=timeout`；
 * 而转写本来就是几十分钟级的，上限必须给足。两头差距太大，一个数字照顾不了。
 */
const STAGE_TIMEOUT_MS = {
  status: 2 * 60 * 1000,
  'cut-plan': 10 * 60 * 1000,
  srt: 10 * 60 * 1000,
  'burn-test': 10 * 60 * 1000,
  qc: 10 * 60 * 1000,
  'bgm-generate': 20 * 60 * 1000,
  'cut-apply': 30 * 60 * 1000,
  'burn-full': 30 * 60 * 1000,
  assemble: 30 * 60 * 1000,
  mix: 30 * 60 * 1000,
  'overlay-render': 30 * 60 * 1000,
  transcribe: 60 * 60 * 1000,
}

/** 该 stage 的超时上限：`config.timeouts.<stage>` > `STAGE_TIMEOUT_MS` > `config.timeoutMs`。 */
function stageLimit(cfg, stage) {
  const override = cfg.timeouts && cfg.timeouts[stage]
  if (Number(override) > 0) return Number(override)
  return STAGE_TIMEOUT_MS[stage] || cfg.timeoutMs
}

export const name = 'dsh-plugin-koubo'
// `jobs` 是 Core 服务（官方 dsh-tool-jobs 的 inject 同样是 ["tools","jobs",...]）。
// 若某个精简 composition 没有 job controller，start() 会抛错 —— 我们捕获后退化为同步执行，
// 不会让整个插件树启动失败。
export const inject = ['tools', 'jobs']

/** 从 stdout 里取最后一个 JSON 对象（内核所有 stage 都以 JSON 作为唯一出口）。 */
function parseReport(stdout) {
  const text = (stdout || '').trim()
  if (!text) return null
  const start = text.lastIndexOf('\n{')
  const candidate = start >= 0 ? text.slice(start + 1) : text
  try {
    return JSON.parse(candidate)
  } catch {
    const first = text.indexOf('{')
    if (first < 0) return null
    try { return JSON.parse(text.slice(first)) } catch { return null }
  }
}

/** 统一构造内核子进程的参数与环境。 */
function kernelSpawnArgs(cfg, stage, project, extraArgs) {
  return {
    command: cfg.python,
    args: [cfg.kbEntry, stage, '--project', project, ...extraArgs],
    options: {
      cwd: cfg.projectsRoot || undefined,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    },
  }
}

/**
 * 项目是否落在 `cfg.projectsRoot` 之内（是则返回 null，否则返回违规说明）。
 *
 * ⚠️ **`projectsRoot` 此前只被当作子进程的 `cwd`，没有任何包含校验** ——
 * 而 `profile/cordis.patch.yml` 的注释写着"项目根目录：kb_* 工具只允许操作它下面的项目"。
 * 也就是说：**一条被文档承诺的边界，在代码里根本不存在**（与"铁律只写在提示词里"同类）。
 * 现在补上：越界直接拒绝并给出 cause，而不是让内核去任意目录 `mkdir -p` 再写文件。
 */
function checkProjectsRoot(cfg, project) {
  const root = cfg.projectsRoot
  if (!root || typeof project !== 'string' || project.trim() === '') return null
  const resolvedRoot = path.resolve(root)
  const resolved = path.resolve(project)
  const rel = path.relative(resolvedRoot, resolved)
  const inside = rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel))
  if (inside) return null
  return {
    ok: false,
    cause: 'project_outside_root',
    error: `项目目录 ${resolved} 不在允许的根 ${resolvedRoot} 之内`,
    fix: '把项目放在 projectsRoot 下面；确实要在别处工作，请改 config.projectsRoot'
      + '（或设环境变量 KB_PROJECTS）后重启 harness。',
  }
}

function preflight(cfg, project) {
  const outside = checkProjectsRoot(cfg, project)
  if (outside) return outside
  if (!existsSync(cfg.kbEntry)) {
    return {
      ok: false,
      cause: 'kernel_missing',
      error: `找不到确定性内核入口 ${cfg.kbEntry}`,
      fix: '把 kbEntry 指向 kb.py 的绝对路径（在 cordis.patch.yml 的 config 里，'
         + '或设环境变量 KB_ENTRY）',
    }
  }
  if (!existsSync(cfg.python)) {
    return {
      ok: false,
      cause: 'python_missing',
      error: `找不到解释器 ${cfg.python}`,
      fix: '把 python 指向带 faster-whisper 的 python.exe（config 或环境变量 KB_PYTHON）',
    }
  }
  return null
}

/**
 * 同步调用确定性内核。总是 resolve（不 reject），把"失败"表达成带 cause 的普通值，
 * 让模型看得到原因、而不是拿到一个笼统异常。
 */
function runKernel(cfg, stage, args, signal) {
  return new Promise((resolve) => {
    const bad = preflight(cfg, args.project)
    if (bad) { resolve(bad); return }

    const { command, args: argv, options } = kernelSpawnArgs(cfg, stage, args.project, args.extra)
    const child = spawn(command, argv, options)

    let stdout = ''
    let stderr = ''
    let settled = false
    let timer = null
    const finish = (value) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      resolve(value)
    }

    // ⚠️ `cfg.timeoutMs` 以前**定义了却从未使用**（死配置）：长阶段（transcribe /
    //    burn-full / assemble）一旦卡死，工具调用就永久挂住 —— 模型侧永远等不到返回，
    //    既不会得到 cause，也无法回退。这里把它落成真正的上限（按 stage 分档，
    //    见 STAGE_TIMEOUT_MS）：到点杀子进程、返回**可路由的** `timeout`（不是让它悬着）。
    const limit = stageLimit(cfg, stage)
    if (limit > 0) {
      timer = setTimeout(() => {
        try { child.kill() } catch { /* 已退出 */ }
        finish({
          ok: false,
          cause: 'timeout',
          timeout_ms: limit,
          error: `内核执行超过 ${limit} ms 被杀（stage=${stage}）。`
            + '长阶段可用 background:true 走作业运行时；确需更久请调 '
            + `config.timeouts.${stage}（或 config.timeoutMs）。`,
          stdout_tail: stdout.slice(-2000),
          stderr_tail: stderr.slice(-1000),
        })
      }, limit)
      // 不让这个定时器把 Node 事件循环吊住（子进程自己会吊住；纯测试场景更干净）
      if (typeof timer.unref === 'function') timer.unref()
    }

    child.stdout.on('data', (d) => { stdout += d.toString('utf8') })
    child.stderr.on('data', (d) => { stderr += d.toString('utf8') })
    child.on('error', (e) => finish({
      ok: false, cause: 'spawn_failed', error: String((e && e.message) || e),
    }))
    child.on('close', (code) => {
      const parsed = parseReport(stdout)
      if (parsed) { finish({ ...parsed, exit_code: code }); return }
      finish({
        ok: code === 0,
        cause: code === 2 ? 'kernel_business_failure' : 'kernel_crashed',
        exit_code: code,
        error: '内核未返回可解析的 JSON 报告',
        stdout_tail: stdout.slice(-4000),
        stderr_tail: stderr.slice(-2000),
      })
    })

    if (signal) {
      const onAbort = () => {
        try { child.kill() } catch { /* 已退出 */ }
        finish({ ok: false, cause: 'aborted', error: '调用被中止（exec.signal 触发）' })
      }
      if (signal.aborted) onAbort()
      else signal.addEventListener('abort', onAbort, { once: true })
    }
  })
}

/**
 * 把一次内核调用注册成后台作业。
 *
 * 语义选择：内核退出码 0（通过）与 2（业务性失败，带 cause）都算作业 `completed` ——
 * 因为 `cause` 是**正常的领域结果**，不是基础设施故障；只有其它退出码/ spawn 失败才算 `failed`。
 * 完整 JSON 报告放进 `JobOutcome.result`，模型在结算后首次读取时一次性拿到。
 */
function startKernelJob(ctx, cfg, stage, project, extraArgs, exec) {
  const bad = preflight(cfg, project)
  if (bad) throw new Error(`${bad.cause}: ${bad.error}`)

  const owner = exec && exec.agent && typeof exec.agent.id === 'string' ? exec.agent.id : undefined
  const { command, args: argv, options } = kernelSpawnArgs(cfg, stage, project, extraArgs)

  return ctx.jobs.start({
    kind: 'koubo',
    label: `koubo ${stage} · ${path.basename(project)}`,
    ...(owner ? { owner } : {}),
    outputLimitBytes: cfg.ringLimitBytes,
    run(job) {
      let child = null
      let stdout = ''
      let stderr = ''
      let settled = false

      const done = new Promise((resolve) => {
        const finish = (outcome) => { if (!settled) { settled = true; resolve(outcome) } }
        try {
          child = spawn(command, argv, options)
        } catch (e) {
          finish({ status: 'failed', detail: `spawn 失败: ${(e && e.message) || e}` })
          return
        }
        job.updateProgress(`阶段 ${stage} 执行中`)
        child.stdout.on('data', (d) => {
          const s = d.toString('utf8'); stdout += s
          job.append(s, { channel: 'stdout' })
        })
        child.stderr.on('data', (d) => {
          const s = d.toString('utf8'); stderr += s
          job.append(s, { channel: 'stderr' })
        })
        child.on('error', (e) => finish({
          status: 'failed', detail: String((e && e.message) || e),
        }))
        child.on('close', (code) => {
          const parsed = parseReport(stdout)
          const payload = parsed || {
            ok: code === 0,
            cause: code === 2 ? 'kernel_business_failure' : 'kernel_crashed',
            exit_code: code,
            error: '内核未返回可解析的 JSON 报告',
            stdout_tail: stdout.slice(-4000),
            stderr_tail: stderr.slice(-2000),
          }
          finish({
            status: (code === 0 || code === 2) ? 'completed' : 'failed',
            detail: `exit ${code} · ok=${payload.ok}`
              + (payload.cause ? ` · cause=${payload.cause}` : ''),
            result: JSON.stringify(payload, null, 2),
          })
        })
      })

      return {
        cancel: () => { try { if (child) child.kill() } catch { /* 已退出 */ } },
        done,
      }
    },
  })
}

/** 统一的输出投影：canonical 值就是内核的 JSON 报告，render 负责给人看。 */
const REPORT_OUTPUT = {
  schema: { type: 'object', additionalProperties: true },
  render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
}

const PROJECT_PARAM = {
  type: 'string',
  required: true,
  description: '项目目录绝对路径，**必须在插件配置的 projectsRoot 之下**'
    + '（越界会被拒绝，cause=project_outside_root）；目录不存在时由内核创建。'
    + '转写阶段需要里面有 input/raw.mp4。',
}

/**
 * ⛔ **这里没有 `kb_run` / `STAGE_VALUES` / `LONG_STAGES` 了** —— 它们是"一个工具承载
 * N 个阶段"的产物，而那种设计把 N 种能力绑在同一个权限开关上（`toolFilter` 只能按工具名过滤）。
 * 现在每个阶段是一个独立工具，见文件头的「工具清单（11）」。
 */

// ───────────────────────────────────────────────────────────────────────────
//  护栏：烧全片之前必须已有 10s 测试片 + 抓帧证据（skill 红线第 7 条）
// ───────────────────────────────────────────────────────────────────────────
function checkTestArtifacts(project) {
  const testVideo = path.join(project, 'renders', 'subtitle_test_10s.mp4')
  if (!existsSync(testVideo)) {
    return { ok: false, why: `未找到测试片 ${testVideo}` }
  }
  const framesDir = path.join(project, 'edit', 'tmpimg')
  let frames = 0
  try {
    if (existsSync(framesDir)) {
      frames = readdirSync(framesDir).filter((f) => /^frame_.*\.png$/i.test(f)).length
    }
  } catch { /* 读不到按 0 处理 */ }
  if (frames === 0) {
    return { ok: false, why: `未找到抓帧证据 ${framesDir}/frame_*.png` }
  }
  return { ok: true, frames }
}

export function burnFullGuard(execution) {
  if (!execution || execution.name !== 'kb_burn') return undefined
  const args = execution.arguments
  if (!args || typeof args !== 'object' || args.mode !== 'full') return undefined
  const project = typeof args.project === 'string' ? args.project : ''
  if (!project) return 'kb_burn(mode="full") 缺少 project，无法核对 10s 测试片。'

  const probe = checkTestArtifacts(project)
  if (probe.ok) return undefined
  return '⛔ 红线拦截（koubo skill 第7条：烧录前必出 10s 测试片并抓帧确认）：'
    + `${probe.why}。请先执行 kb_burn(mode="test")，`
    + '再用 kb_qc(target="subtitle_test") 与抓帧图确认字幕真烧进、不挡脸、时间对齐，'
    + '然后重试 kb_burn(mode="full")。'
}

/**
 * 花钱的护栏：`kb_bgm` 会真扣费（约 0.49 元/次），所以**必须先有写好的风格描述**。
 *
 * 为什么要有这条：
 *   `edit/bgm.json` 由剪裁策划用 `write` 显式写出（含风格、输出路径、预算）。
 *   没有这个文件就直接调 `kb_bgm`，等于用一个默认风格盲花钱 —— 不允许。
 *   干跑（`dry_run=true`）只查价、不花钱，所以不受此限。
 */
export function bgmSpendGuard(execution) {
  if (!execution || execution.name !== 'kb_bgm') return undefined
  const args = execution.arguments
  if (!args || typeof args !== 'object') return undefined
  if (args.dry_run) return undefined
  const project = typeof args.project === 'string' ? args.project : ''
  if (!project) return 'kb_bgm 缺少 project。'

  const spec = path.join(project, 'edit', 'bgm.json')
  if (!existsSync(spec)) {
    return '⛔ 拦住：这是**付费**操作（约 0.49 元/次），必须先写 ' + spec
      + '（至少含 "style" 风格描述与 "output"）。'
      + '若只是想先看价格，用 kb_bgm({project, dry_run:true}) —— 那次不花钱。'
  }
  try {
    const spec0 = JSON.parse(readFileSync(spec, 'utf8'))
    const style = String(spec0.style || '').trim()
    if (style.length < 20) {
      return `⛔ 拦住：${spec} 里的 style 太短（${style.length} 字），`
        + '付费前请把音乐风格描述写清楚（要写成能约束模型的句子，例如无人声/克制/留空间给人声）。'
    }
  } catch (e) {
    return `⛔ 拦住：${spec} 不是合法 JSON（${String((e && e.message) || e)}），付费前请先修正。`
  }
  return undefined
}

/** 长阶段通用的 `background` 参数（转写/烧全片/缝接/混音/动效/付费生成都建议后台跑）。 */
const BACKGROUND_PARAM = {
  type: 'boolean',
  description: 'true=作为后台作业执行（分钟级长阶段推荐：返回 job_id，用 job_output 读输出）；'
    + '默认 false=同步等待。无 job controller 时自动退化为同步执行。',
}

/**
 * 跑一个确定性阶段：`background` 为真时走作业运行时，起作业失败则**优雅退化**为同步执行。
 *
 * ⚠️ 九个工具共用这一条路径 —— 复制九遍必然漂移（这一轮审查里已经见过太多"两处各写一份"的后果：
 * `remap()` 两份、描述与实现不一致、护栏与内核各判一次）。所以这里只留一份实现。
 */
async function runStage(ctx, cfg, stage, project, extra, exec, background) {
  if (background) {
    try {
      const jobId = startKernelJob(ctx, cfg, stage, project, extra, exec)
      return {
        ok: true,
        background: true,
        job_id: jobId,
        kind: 'koubo',
        stage,
        project,
        note: `已作为后台作业启动（stage=${stage}）。用 job_output 读 ${jobId} 的实时输出；`
          + '结算后首次读取会带回 result（即该阶段的完整 JSON 报告）。',
      }
    } catch (e) {
      const res = await runKernel(cfg, stage, { project, extra }, exec.signal)
      return {
        ...res,
        background: false,
        background_fallback_reason: String((e && e.message) || e),
      }
    }
  }
  return runKernel(cfg, stage, { project, extra }, exec.signal)
}

export function apply(ctx, config = {}) {
  const cfg = { ...DEFAULTS, ...config }

  // ── 护栏注册（两条，都是 monotonic deny）──
  // guard 在 `tools/pre-execute` 之后求值，且没有 "allow" 返回值 ⇒ 后面的监听器
  // 无法把拒绝翻回允许。
  //
  // ⛔ **必须挂到 root/global 层**（2026-10-05 实测踩坑）：
  //    `dsh-tools` 的 `guardReason(exec)` 只查两处 —— `layers.global` 与
  //    `exec.agent` 的 scope 链；而 `guard()` 的实现是 `this.layers.effect(this.ctx, …)`，
  //    **挂在调用它的那个 ctx 的层上**。官方那个 `childCtx.tools.guard(...)` 是
  //    **故意的 scoped 用法**（只想管住它自己派生的子代理）；照抄到"全局策略"上，
  //    护栏就落进插件自己的 scope，任何 agent 的链都查不到它。
  //    对照实验（`tools/guard_real_test.mjs`）：
  //      插件 scope 注册 → 护栏命中 = false（请求直接落到内核）
  //      root 注册      → 护栏命中 = true
  const guardCtx = ctx.root && ctx.root.tools ? ctx.root : ctx
  const guards = [
    guardCtx.tools.guard(burnFullGuard),
    guardCtx.tools.guard(bgmSpendGuard),
  ]
  try {
    if (typeof ctx.effect === 'function') ctx.effect(() => guards.forEach((d) => d()))
  } catch { /* 没有 effect 机制时保持注册即可 */ }

  // ── 工具 1：项目状态（快、只读；断点续跑与回退路由都要先问它）──
  ctx.tools.register(defineTool({
    name: 'kb_status',
    description:
      '读取口播剪辑项目的当前进度：哪些产物已存在、原片与成片的时长/分辨率规格、'
      + '上轮质检结论与失败 cause、下一步该做哪个阶段。'
      + '每次接续一个项目、或任何回退之前，都先调用它，不要凭记忆猜进度。',
    parameters: { project: PROJECT_PARAM },
    output: REPORT_OUTPUT,
    async execute(args, exec) {
      return runKernel(cfg, 'status', { project: args.project, extra: [] }, exec.signal)
    },
  }))

  // ── 工具 2：剪裁证据（只给证据，不给结论）──
  ctx.tools.register(defineTool({
    name: 'kb_plan_cut',
    description:
      '阶段2 前置：扫描 edit/transcript.json，把每一段拆成带 0-based 序号、起止时间、'
      + '字数、字/秒、必删关键词命中、幻觉嫌疑标记、长空白与重复文本簇的**证据表**，'
      + '写入 edit/plan_context.json 并返回摘要。\n'
      + '本工具**只提供证据，不做任何删除决定**。',
    parameters: { project: PROJECT_PARAM },
    output: REPORT_OUTPUT,
    async execute(args, exec) {
      return runKernel(cfg, 'cut-plan', { project: args.project, extra: [] }, exec.signal)
    },
  }))

  // ── 工具 3：转写（贵 + GPU；归剪裁策划）──
  ctx.tools.register(defineTool({
    name: 'kb_transcribe',
    description:
      '对 input/raw.mp4 做语音转写（faster-whisper large-v3），产出 edit/transcript.json '
      + '+ .txt + .srt，含 **word 级时间戳**（后续字幕重映射与音画同步都依赖它）。\n'
      + '⛔ GPU 长任务（十几分钟到半小时）⇒ **建议 background:true**。\n'
      + '⛔ 已有逐字稿时内核以 cause=transcript_exists 拒绝覆盖（覆盖会让剪辑决策/剪后时间轴/字幕全部作废）。\n'
      + '真要重转是"更贵的动作"，**不由本工具提供**：须由主管走 kb_override(action="retranscribe")。',
    parameters: {
      project: PROJECT_PARAM,
      device: { type: 'string', enum: ['cuda', 'cpu'], default: 'cuda',
                description: '推理设备，默认 cuda' },
      language: { type: 'string', default: 'zh', description: '语言，默认 zh' },
      background: BACKGROUND_PARAM,
    },
    output: REPORT_OUTPUT,
    async execute(args, exec) {
      const extra = ['--device', args.device || 'cuda', '--language', args.language || 'zh']
      return runStage(ctx, cfg, 'transcribe', args.project, extra, exec, args.background)
    },
  }))

  // ── 工具 4：落地剪裁（归剪裁策划）──
  ctx.tools.register(defineTool({
    name: 'kb_cut',
    description:
      '按 edit/edit_decisions.json **机械落地**删除集：断言下标越界 → 按 gap≤5s 合并保留块 → '
      + '建立**剪后时间轴** edit/timeline_map.json（特效设计靠它做人声对齐）→ run:true 时编码 '
      + 'renders/edited_final.mp4。\n'
      + '⛔ 删除集是 **0-based**，直连 transcript.json 下标。\n'
      + '⛔ 删到幻觉嫌疑段（时长≤0.5s 却≥6字）会被内核以 cause=hallucination_cut_forbidden 拒绝 —— '
      + '本工具**没有**放行开关（那是主管的 kb_override）；正确做法是把那些下标从删除集里移除，'
      + '它们只在字幕层合并。',
    parameters: {
      project: PROJECT_PARAM,
      run: { type: 'boolean', default: false,
             description: 'true=立即执行 FFmpeg 裁切（较慢，会产出 edited_final.mp4）；'
               + 'false=只产出决策/剪后时间轴/run_cut.sh' },
      background: BACKGROUND_PARAM,
    },
    output: REPORT_OUTPUT,
    async execute(args, exec) {
      const extra = args.run ? ['--run'] : []
      return runStage(ctx, cfg, 'cut-apply', args.project, extra, exec, args.background)
    },
  }))

  // ── 工具 5：字幕生成（归字幕制作）──
  ctx.tools.register(defineTool({
    name: 'kb_srt',
    description:
      '按保留块把逐字稿重映射成 edit/subtitles/final.srt（+ edit/srt_report.json）。\n'
      + '铁律：**字幕行 = Whisper 保留段本身**（不碎段合并、不机械折行、句末禁一切符号）；'
      + '重映射容差 0.6 **绝不 clamp**；单调化只推后不砍前。\n'
      + '文本纠错写 edit/subtitle_fixes.json 后复跑本工具即可（内核会与 edit_decisions.json 的 '
      + 'text_fixes 合并，字幕优先）。',
    parameters: { project: PROJECT_PARAM },
    output: REPORT_OUTPUT,
    async execute(args, exec) {
      return runKernel(cfg, 'srt', { project: args.project, extra: [] }, exec.signal)
    },
  }))

  // ── 工具 6：烧录字幕（归字幕制作；mode=full 受红线护栏）──
  ctx.tools.register(defineTool({
    name: 'kb_burn',
    description:
      '把 final.srt 烧进画面（libass；force_style 已按 skill 固定，MarginV=26）。\n'
      + 'mode="test" → 只烧前 10s，产出 renders/subtitle_test_10s.mp4 + edit/tmpimg/frame_*.png；\n'
      + 'mode="full" → 烧全片 renders/subtitled_full.mp4。\n'
      + '⛔ mode="full" 受**不可撤销护栏**约束：必须已有 10s 测试片**与**抓帧证据，否则直接被拒。'
      + '正确顺序：kb_burn(mode="test") → kb_qc(target="subtitle_test") + 逐张看图 → kb_burn(mode="full")。',
    parameters: {
      project: PROJECT_PARAM,
      mode: { type: 'string', required: true, default: 'test', enum: ['test', 'full'],
              description: 'test=只烧前 10s 并抓帧；full=烧全片（需先有测试片与抓帧证据）' },
      background: BACKGROUND_PARAM,
    },
    output: REPORT_OUTPUT,
    async execute(args, exec) {
      const stage = (args.mode || 'test') === 'full' ? 'burn-full' : 'burn-test'
      return runStage(ctx, cfg, stage, args.project, [], exec, args.background)
    },
  }))

  // ── 工具 7：缝接 / 铺 BGM（归剪裁策划；叠加动效层的合成也走它）──
  ctx.tools.register(defineTool({
    name: 'kb_assemble',
    description:
      'mode="assemble"：按 edit/assemble.json 做 xfade 缝接（spec 可含 overlays[] 透明动效层，'
      + '那一步由特效设计调用）；mode="mix"：按 edit/mix.json 给已缝接的片子铺 BGM。\n'
      + 'dry_run:true（仅 assemble）只做前置校验 + 按同一递推式**预测**输出时长，**不跑 ffmpeg、不产文件**。\n'
      + '产出 renders/final.mp4；缝接后 predicted vs actual 偏差 >0.5s 会报 duration_close=false。',
    parameters: {
      project: PROJECT_PARAM,
      mode: { type: 'string', required: true, default: 'assemble', enum: ['assemble', 'mix'],
              description: 'assemble=xfade 缝接（可叠加动效层）；mix=铺 BGM' },
      spec: { type: 'string',
              description: '规格文件（项目内相对路径）。默认 assemble→edit/assemble.json、'
                + 'mix→edit/mix.json' },
      dry_run: { type: 'boolean', default: false,
                 description: 'true=只校验与预测、不产文件（仅 mode=assemble 有效）' },
      background: BACKGROUND_PARAM,
    },
    output: REPORT_OUTPUT,
    async execute(args, exec) {
      const mode = (args.mode || 'assemble') === 'mix' ? 'mix' : 'assemble'
      const spec = args.spec || (mode === 'mix' ? 'edit/mix.json' : 'edit/assemble.json')
      const extra = ['--spec', spec]
      if (mode === 'assemble' && args.dry_run) extra.push('--dry-run')
      return runStage(ctx, cfg, mode, args.project, extra, exec, args.background)
    },
  }))

  // ── 工具 8：渲染透明叠加层（归特效设计）──
  ctx.tools.register(defineTool({
    name: 'kb_overlay',
    description:
      '把 overlays/index.html 这个 HyperFrames 工程渲染成**带 alpha 的 ProRes4444 MOV**'
      + '（内核已固定 --format mov，不可改）。\n'
      + '⛔ 六条致命红线：根元素必须带 data-composition-id="main" + data-width/height；'
      + 'GSAP 只能走 CDN（file:// 被 Chromium 禁）；纯 CSS 模式禁 Google Fonts <link>；'
      + '动画时间必须取自 **edit/timeline_map.json（剪后时间轴）**，绝不能用 transcript.json 的时间。\n'
      + '产出 overlays/renders/<output>；MOV 已按剪后时间轴排布 ⇒ 合成时从 0 点对齐，不需要时间偏移。',
    parameters: {
      project: PROJECT_PARAM,
      output: { type: 'string', default: 'overlay.mov',
                description: '输出文件名（落在 overlays/renders/ 下），默认 overlay.mov' },
      background: BACKGROUND_PARAM,
    },
    output: REPORT_OUTPUT,
    async execute(args, exec) {
      const extra = ['--output', args.output || 'overlay.mov']
      return runStage(ctx, cfg, 'overlay-render', args.project, extra, exec, args.background)
    },
  }))

  // ── 工具 5：BGM 生成（**唯一会花钱的工具**，故单独命名以便按工具名授权）──
  ctx.tools.register(defineTool({
    name: 'kb_bgm',
    description:
      '调元气中转站 suno-v4.5 生成无歌词 BGM（纯器乐）。\n'
      + '⛔ 这是**唯一会扣费的工具，约 0.49 元/次**。付费是两步：先 dry_run:true 看价（不花钱），'
      + '确认后再带 confirm_paid:true 提交；不带确认只会返回价格、绝不提交。'
      + '已有成品（bgm_exists）或已有未完成任务时不会重复扣费。\n'
      + '风格描述写在 edit/bgm.json 的 style 里（需 ≥20 字）。'
      + '产出 bgm/bgm.mp3 + edit/bgm_report.json。',
    parameters: {
      project: PROJECT_PARAM,
      dry_run: { type: 'boolean',
                 description: 'true=只查价并返回计划，不提交、不花钱；默认 false。'
                   + '⚠️ 若同时传了 confirm_paid，**dry_run 优先**（只看价、不提交）' },
      confirm_paid: { type: 'boolean',
                      description: '确认这次**真花钱**（约 0.49 元/次）。不传只会拿到价格与提示，'
                                   + '不会提交。想先看价用 dry_run:true。' },
      background: { type: 'boolean',
                    description: 'true=作为后台作业执行（生成通常 1~3 分钟）；默认 false=同步等待' },
    },
    output: REPORT_OUTPUT,
    async execute(args, exec) {
      const extra = ['--spec', 'edit/bgm.json']
      if (args.dry_run) extra.push('--dry-run')
      if (args.confirm_paid) extra.push('--confirm-paid')

      if (args.background) {
        try {
          const jobId = startKernelJob(ctx, cfg, 'bgm-generate', args.project, extra, exec)
          return {
            ok: true,
            background: true,
            job_id: jobId,
            kind: 'koubo',
            stage: 'bgm-generate',
            project: args.project,
            paid: !args.dry_run,
            note: `已作为后台作业启动（${args.dry_run ? '干跑，不花钱' : '会扣费，约 0.49 元'}）。`
                + `用 job_output 读 ${jobId} 的实时输出；结算后首次读取会带回完整 JSON 报告。`,
          }
        } catch (e) {
          const res = await runKernel(
            cfg, 'bgm-generate', { project: args.project, extra }, exec.signal)
          return {
            ...res,
            background: false,
            background_fallback_reason: String((e && e.message) || e),
          }
        }
      }

      return runKernel(cfg, 'bgm-generate', { project: args.project, extra }, exec.signal)
    },
  }))

  // ── 工具 4：质检（确定性断言 + 帧/音量证据；主观判断交给视觉子代理）──
  ctx.tools.register(defineTool({
    name: 'kb_qc',
    description:
      '对成片或测试片做确定性质检。返回 checks[]（每项含是否通过 + cause）、失败 cause 列表，'
      + '以及抓帧图路径（供视觉子代理看图）。\n'
      + '检查项：音视频流是否齐全、A/V 轨时长是否一致、是否削顶爆音、首尾黑帧、'
      + 'SRT 是否带 BOM / 句末带符号 / 时间单调 / 条数非空。\n'
      + '⚠️ **检查项随 target 变化**：`srt_timeline_matches_video`（字幕末条 vs 片长）与 '
      + '`edited_duration_vs_plan`（实测 vs 保留块计划）**只在全片目标**（edited / subtitled / final）'
      + '上做 —— 拿全片 SRT 去比 10s 测试片会恒定误报。BGM 相关检查只在 target=final。\n'
      + '本工具**只判定、不修复**：ok=false 时把 causes[] 交主管裁决回退目标。',
    parameters: {
      project: PROJECT_PARAM,
      target: { type: 'string', required: true, default: 'final',
                enum: ['subtitle_test', 'edited', 'subtitled', 'final'],
                description: '质检对象。**必填**（选错目标会拿到不同的检查项集合）：'
                  + 'subtitle_test=10s 测试片（CP2 看图用）；edited=剪切后未烧字幕；'
                  + 'subtitled=烧完字幕未装配；final=最终成片（含 BGM 检查）' },
      frames_at: { type: 'array', items: { type: 'number' },
                   description: '抓帧时间点（秒）数组，如 [1.0, 5, 9]；默认 [1.0]。'
                     + '（兼容传逗号分隔字符串）' },
    },
    output: REPORT_OUTPUT,
    async execute(args, exec) {
      const extra = ['--target', args.target || 'final']
      // 时间点是**数组**（schema 上是 array/number）；兼容旧式逗号分隔字符串。
      const points = Array.isArray(args.frames_at)
        ? args.frames_at.join(',')
        : args.frames_at
      if (points) extra.push('--frames-at', String(points))
      return runKernel(cfg, 'qc', { project: args.project, extra }, exec.signal)
    },
  }))

  // ── 工具 11：主管专用例外（偏离红线的两个动作；**不在任何子代理的白名单里**）──
  ctx.tools.register(defineTool({
    name: 'kb_override',
    description:
      '⛔ **主管专用**：放行"偏离红线"的例外。子代理的 toolFilter 里**没有**本工具。\n'
      + 'action="retranscribe" → 强制重转写（覆盖已有逐字稿）。代价极高：剪辑决策、剪后时间轴、'
      + '字幕全部作废，必须重跑整条下游。\n'
      + 'action="allow_hallucination_cut" → 放行"删除幻觉嫌疑段"（默认被内核拒绝，因为那些段在音频上'
      + '其实是一整句连贯的话，删时间轴会把说话声切断）。会连同 --run 一起执行裁切。\n'
      + '⛔ 两个动作都必须填 reason（≥10 字）。例外必须是一次**有记录的决定**，'
      + 'reason 会随工具返回值记录在会话日志里。',
    parameters: {
      project: PROJECT_PARAM,
      action: { type: 'string', required: true,
                enum: ['retranscribe', 'allow_hallucination_cut'],
                description: '要放行的例外动作' },
      reason: { type: 'string', required: true,
                description: '为什么这次例外成立（≥10 字）。太少会被拒（cause=override_reason_required）' },
      background: BACKGROUND_PARAM,
    },
    output: REPORT_OUTPUT,
    async execute(args, exec) {
      const reason = String(args.reason || '').trim()
      if (reason.length < 10) {
        return {
          ok: false,
          cause: 'override_reason_required',
          error: `kb_override 必须写明 reason（≥10 字，实际 ${reason.length} 字）——`
            + '例外必须是一次有记录的决定，不允许无理由放行。',
        }
      }
      const built = args.action === 'retranscribe'
        ? { stage: 'transcribe', extra: ['--force'] }
        : { stage: 'cut-apply', extra: ['--allow-hallucination-cut', '--run'] }
      const res = await runStage(
        ctx, cfg, built.stage, args.project, built.extra, exec, args.background)
      return { ...res, override_action: args.action, override_reason: reason }
    },
  }))
}
