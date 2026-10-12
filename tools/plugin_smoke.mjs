/**
 * 插件自检（回归测试）：不启动整个 Harness，用桩 ctx 验证
 *   1) 插件能被加载、导出形态正确、注册出**预期的 11 个工具**（一阶段一工具）；
 *   2) 护栏（烧全片前置条件）在**该拒时拒、该放时放**；
 *   3) 后台作业路径真能跑起来（start → run(handle) → done 携带 JSON 报告）；
 *   4) 无 job controller 时优雅退化为同步执行；
 *   5) 同步路径真能调到确定性内核并拿到报告（失败以 cause 回传，不抛异常）；
 *   6) 每阶段超时上限是**真上限**（卡死时杀进程并回带 cause=timeout）；
 *   7) 工具参数的 JSON Schema 形状与边界（含"参数根是开放对象"这条框架事实）；
 *   8) **权限矩阵**：`agents/subagents.patch.yml` 的白名单与设计一致
 *      （尤其：`kb_override` / `kb_bgm` 不得出现在不该出现的子代理里）。
 *
 * 运行：
 *   node tools/plugin_smoke.mjs
 *
 * 前置：plugin/node_modules 是指向已安装 DSH 依赖的目录联接（见 README「本地开发」）。
 * 退出码 0 = 全部通过；1 = 有不通过项。
 */

import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'

// ⚠️ 中文路径必须用 fileURLToPath：`new URL(import.meta.url).pathname` 会返回
// 百分号编码路径（AI%E5%AD%A6%E4%B9%A0），导致 ERR_MODULE_NOT_FOUND。
const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')
const ENTRY = path.join(ROOT, 'plugin', 'src', 'index.js')
const SMOKE_PROJECT = path.join(ROOT, 'projects', '_smoke')
const ALLOW_PROJECT = path.join(ROOT, 'projects', '_guard_allow')

const EXPECTED = [
  'kb_status', 'kb_plan_cut', 'kb_transcribe', 'kb_cut', 'kb_srt', 'kb_burn',
  'kb_assemble', 'kb_overlay', 'kb_qc', 'kb_bgm', 'kb_override',
]
const failures = []
const ok = (cond, label) => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}`)
  if (!cond) failures.push(label)
}

console.log(`entry: ${ENTRY}`)
const mod = await import(pathToFileURL(ENTRY).href)

// ── 1. 导出形态（官方 postmortem 0001：绝不能有 default）──
ok(mod.name === 'dsh-plugin-koubo', `name export = ${mod.name}`)
ok(Array.isArray(mod.inject) && mod.inject.includes('tools') && mod.inject.includes('jobs'),
   `inject = ${JSON.stringify(mod.inject)}`)
ok(!('default' in mod), 'NO export default（否则 Loader 会吃掉 inject）')
ok(typeof mod.apply === 'function', 'apply 是函数')
ok(typeof mod.burnFullGuard === 'function', 'burnFullGuard 可被单独测试')
ok(typeof mod.bgmSpendGuard === 'function', 'bgmSpendGuard 可被单独测试')

// ── 2. 桩 ctx：捕获注册的工具 / 护栏 / 作业规格 ──
const registered = []
const guards = []
let jobSpec = null
let jobsStartThrows = false

const ctx = {
  tools: {
    register: (t) => registered.push(t),
    guard: (g) => { guards.push(g); return () => {} },
  },
  jobs: {
    start: (spec) => {
      if (jobsStartThrows) throw new Error('no attached job controller')
      jobSpec = spec
      return 'koubo-1'
    },
  },
}
mod.apply(ctx, {})

ok(registered.length === EXPECTED.length, `注册工具数 = ${registered.length}（期望 ${EXPECTED.length}）`)
for (const n of EXPECTED) ok(registered.some((t) => t.name === n), `含工具 ${n}`)
ok(guards.length === 2, `注册护栏数 = ${guards.length}（期望 2：烧全片 + 付费）`)
console.log('    keys of tool def: ' + Object.keys(registered[0] || {}).join(', '))

const guard = guards[0]

// ── 3. 护栏：该放 / 该拒 ──
ok(guard({ name: 'kb_status', arguments: { project: SMOKE_PROJECT } }) === undefined,
   '护栏对非 kb_burn 调用放行')
ok(guard({ name: 'kb_burn', arguments: { project: SMOKE_PROJECT, mode: 'test' } }) === undefined,
   '护栏对 mode=test 放行')
ok(typeof guard({ name: 'kb_burn', arguments: { project: SMOKE_PROJECT, mode: 'full' } }) === 'string',
   '护栏拦截没有 10s 测试片的 mode=full')
ok(typeof guard({ name: 'kb_burn', arguments: { mode: 'full' } }) === 'string',
   '护栏拦截缺少 project 的 mode=full')

// 造一个"已出测试片 + 已有抓帧"的项目 → 应放行
try {
  mkdirSync(path.join(ALLOW_PROJECT, 'renders'), { recursive: true })
  mkdirSync(path.join(ALLOW_PROJECT, 'edit', 'tmpimg'), { recursive: true })
  writeFileSync(path.join(ALLOW_PROJECT, 'renders', 'subtitle_test_10s.mp4'), 'stub')
  writeFileSync(path.join(ALLOW_PROJECT, 'edit', 'tmpimg', 'frame_1.5.png'), 'stub')
  ok(guard({ name: 'kb_burn', arguments: { project: ALLOW_PROJECT, mode: 'full' } }) === undefined,
     '护栏在有测试片+抓帧时放行 mode=full')
} finally {
  rmSync(ALLOW_PROJECT, { recursive: true, force: true })
}

if (!existsSync(SMOKE_PROJECT)) {
  console.log('SKIP  内核调用（projects/_smoke 不存在，先跑 README 里的冒烟步骤）')
} else {
  const exec = { signal: undefined, agent: { id: 'sess-smoke' } }
  const kbCut = registered.find((t) => t.name === 'kb_cut')
  const kbStatus = registered.find((t) => t.name === 'kb_status')
  const kbQc = registered.find((t) => t.name === 'kb_qc')
  const kbBurn = registered.find((t) => t.name === 'kb_burn')

  // ── 4. 同步路径 → 内核 ──
  const st = await kbStatus.execute({ project: SMOKE_PROJECT }, exec)
  ok(st && st.ok === true, `kb_status ok=true（segment_count=${st && st.segment_count}）`)
  ok(st && st.next_stage, `kb_status 给出 next_stage=${st && st.next_stage}`)
  ok(st && st.exit_code === 0, `exit_code 透传 = ${st && st.exit_code}`)

  const qc = await kbQc.execute({ project: SMOKE_PROJECT, target: 'final' }, exec)
  ok(qc && typeof qc.ok === 'boolean', `kb_qc 返回 ok=${qc && qc.ok}`)
  ok(Array.isArray(qc && qc.causes), `kb_qc 回带 causes=${JSON.stringify(qc && qc.causes)}`)
  ok(qc && qc.exit_code === (qc.ok ? 0 : 2),
     `kb_qc 退出码与 ok 一致（exit=${qc && qc.exit_code}）`)

  // kb_burn(mode=test) 需要 edited_final.mp4 + final.srt；_smoke 没有前者 ⇒ 预期 missing_input
  const bad = await kbBurn.execute({ project: SMOKE_PROJECT, mode: 'test' }, exec)
  ok(bad && bad.ok === false && typeof bad.cause === 'string',
     `失败以 cause 回传（kb_burn(mode=test) → ok=${bad && bad.ok}, cause=${bad && bad.cause}）`)

  // ── 5. 后台作业路径：start → run(handle) → done ──
  //    用 kb_cut（不传 run ⇒ 只写决策/时间轴，不跑 ffmpeg）作为长阶段代表。
  jobSpec = null
  const bg = await kbCut.execute(
    { project: SMOKE_PROJECT, background: true }, exec)
  ok(bg && bg.background === true && bg.job_id === 'koubo-1',
     `background:true 立即返回 job_id=${bg && bg.job_id}`)
  ok(jobSpec && jobSpec.kind === 'koubo', `JobSpec.kind = ${jobSpec && jobSpec.kind}`)
  ok(jobSpec && jobSpec.owner === 'sess-smoke',
     `JobSpec.owner 取自 exec.agent.id = ${jobSpec && jobSpec.owner}`)
  ok(jobSpec && typeof jobSpec.run === 'function', 'JobSpec.run 是函数')

  if (jobSpec) {
    const appended = []
    const progresses = []
    const handle = {
      id: 'koubo-1',
      append: (t, o) => appended.push({ t, channel: o && o.channel }),
      updateProgress: (l) => progresses.push(l),
    }
    const hooks = jobSpec.run(handle)
    ok(typeof hooks.cancel === 'function', 'run() 返回 cancel')
    ok(hooks.done && typeof hooks.done.then === 'function', 'run() 返回 done Promise')
    const outcome = await hooks.done
    ok(outcome.status === 'completed', `作业结算 status = ${outcome.status}`)
    ok(typeof outcome.result === 'string', 'outcome.result 是字符串（JSON 报告）')
    let parsedResult = null
    try { parsedResult = JSON.parse(outcome.result) } catch { /* 下面断言 */ }
    ok(parsedResult && parsedResult.stage === 'cut-apply',
       `outcome.result 可解析且 stage=${parsedResult && parsedResult.stage}`)
    ok(appended.length > 0, `输出已 append 进 ring（${appended.length} 块）`)
    ok(progresses.length > 0, `updateProgress 已被调用（${progresses.length} 次）`)
    hooks.cancel() // 幂等：作业已结束，不应抛错
    ok(true, 'cancel() 在作业结束后调用不抛错')
  }

  // ── 6. 无 job controller → 优雅退化为同步 ──
  jobsStartThrows = true
  const fb = await kbCut.execute({ project: SMOKE_PROJECT, background: true }, exec)
  jobsStartThrows = false
  ok(fb && fb.background === false && typeof fb.background_fallback_reason === 'string',
     `start 抛错时退化为同步（background=${fb && fb.background}）`)
  ok(fb && fb.stage === 'cut-apply', '退化路径仍拿到内核报告（stage=cut-apply）')
}

// ── 7. 花钱动作被隔离 + 付费护栏 + **不再有聚合入口**（2026-10-05 拆工具）──
{
  const kbBgm = registered.find((t) => t.name === 'kb_bgm')
  ok(!!kbBgm, 'kb_bgm 已注册（唯一会花钱的工具，单独命名以便按工具名授权）')
  const bgmProps = (kbBgm && kbBgm.parameters && kbBgm.parameters.properties) || {}
  ok('dry_run' in bgmProps,
     `kb_bgm 暴露 dry_run（参数：${Object.keys(bgmProps).join(',')}）`)
  ok('confirm_paid' in bgmProps,
     'kb_bgm 暴露 confirm_paid（付费是两步：不确认就不提交）')

  // ⭐ 核心：聚合入口 `kb_run` 已删除 —— 一个工具承载 N 个阶段 = N 种能力共用一个权限开关
  ok(!registered.some((t) => t.name === 'kb_run'),
     'kb_run 聚合入口已删除（一阶段一工具）')

  // 自由 `args` 数组必须彻底消失（它是"内核新增任何开关自动对模型开放"的根源）
  const freeArgs = registered.filter((t) => {
    const p = (t.parameters && t.parameters.properties) || {}
    return Object.prototype.hasOwnProperty.call(p, 'args')
  }).map((t) => t.name)
  ok(freeArgs.length === 0, `没有任何工具暴露自由 args 数组（实际：${freeArgs.join(',') || '无'}）`)

  // 例外开关只能出现在 kb_override 上，不得混进普通工具的参数
  const withForce = registered.filter((t) => {
    const p = (t.parameters && t.parameters.properties) || {}
    return 'force' in p || 'allow_hallucination_cut' in p
  }).map((t) => t.name)
  ok(withForce.length === 0,
     `重转写/放行删幻觉段不暴露成普通参数（实际：${withForce.join(',') || '无'}）`)
  const ov = registered.find((t) => t.name === 'kb_override')
  ok(!!ov && !!(ov.parameters.properties.action.enum || []).includes('retranscribe')
     && !!(ov.parameters.properties.action.enum || []).includes('allow_hallucination_cut'),
     'kb_override 承载两个例外动作')
  ok((ov.parameters.required || []).includes('reason'), 'kb_override 强制要求 reason（例外必须留痕）')

  // 付费护栏三态：无 spec → 拦；style 太短 → 拦；dry_run → 放行
  const noSpec = path.join(ROOT, 'projects', '_no_such_project')
  ok(typeof mod.bgmSpendGuard({ name: 'kb_bgm', arguments: { project: noSpec } }) === 'string',
     '保护栏：没有 edit/bgm.json 时拒绝付费调用')
  ok(mod.bgmSpendGuard({ name: 'kb_bgm', arguments: { project: noSpec, dry_run: true } }) === undefined,
     '护栏：dry_run（不花钱）时放行')
  const gdir = path.join(ROOT, 'projects', '_spend_guard', 'edit')
  mkdirSync(gdir, { recursive: true })
  writeFileSync(path.join(gdir, 'bgm.json'), JSON.stringify({ style: '太短' }), 'utf8')
  ok(typeof mod.bgmSpendGuard({
       name: 'kb_bgm', arguments: { project: path.join(ROOT, 'projects', '_spend_guard') },
     }) === 'string', '护栏：style 太短（<20 字）时拒绝花钱')
  writeFileSync(path.join(gdir, 'bgm.json'),
    JSON.stringify({ style: 'Instrumental only, no vocals, calm restrained underscore, loop-friendly.' }),
    'utf8')
  ok(mod.bgmSpendGuard({
       name: 'kb_bgm', arguments: { project: path.join(ROOT, 'projects', '_spend_guard') },
     }) === undefined, '护栏：风格写清楚后放行')
  ok(mod.bgmSpendGuard({ name: 'kb_burn', arguments: { mode: 'full' } }) === undefined,
     '两条护栏互不串扰（kb_bgm 护栏不看别的工具）')
  rmSync(path.join(ROOT, 'projects', '_spend_guard'), { recursive: true, force: true })
}

// ── 8. 同步调用超时：每阶段上限必须是**真上限**（曾经是死配置）──
//    长阶段（transcribe / burn-full）卡死时，工具调用不能永久挂住 —— 必须到点杀进程
//    并回带可路由的 cause=timeout。这里用 `timeouts.srt = 1ms` 制造确定的超时。
{
  const reg2 = []
  const ctx2 = {
    tools: { register: (t) => reg2.push(t), guard: () => () => {} },
    jobs: { start: () => 'unused' },
  }
  mod.apply(ctx2, { timeouts: { srt: 1 } })
  const kbSrt2 = reg2.find((t) => t.name === 'kb_srt')
  const t0 = Date.now()
  const res = await kbSrt2.execute(
    { project: path.join(ROOT, 'projects', '_smoke') }, { signal: undefined })
  const ms = Date.now() - t0
  ok(res && res.cause === 'timeout',
    `超过该阶段上限时回带 cause=timeout（cause=${res && res.cause}, ${ms}ms）`)
  ok(res && res.timeout_ms === 1, `回带 timeout_ms=${res && res.timeout_ms}`)
  ok(ms < 10_000, `超时后立即返回、没有挂住（${ms}ms）`)
}

// ── 9. 工具参数 JSON Schema 的形状与边界（2026-10-05 审查后新增）──
{
  const byName = (n) => registered.find((t) => t.name === n)
  const props = (n) => (byName(n).parameters && byName(n).parameters.properties) || {}
  const req = (n) => byName(n).parameters.required || []

  // ① DSH 的参数根对象**天生是开放的**（ParameterSchemaSpec 注释："implicit open
  //    object root"），无法声明 additionalProperties:false。这里把这条框架事实**钉住**，
  //    免得以后误以为"多传的参数会被拒绝"。
  for (const n of EXPECTED) {
    ok(!('additionalProperties' in byName(n).parameters),
      `${n} 的参数根是开放的（框架定死，无法用 additionalProperties 收紧）`)
  }
  // ② 编造的参数会被**静默忽略**（不是报错）—— 所以任何"参数集合"的约束只能写在插件代码里
  const kbStatus3 = byName('kb_status')
  const ignored = await kbStatus3.execute(
    { project: SMOKE_PROJECT, force: true, nonsense: 42 }, { signal: undefined })
  ok(ignored && ignored.ok === true,
    '编造的参数被静默忽略（证明开放根是框架行为，需靠代码自守）')

  // ③ kb_qc.target 必填（选错目标会拿到不同检查项集合 —— CP2 要 subtitle_test）
  ok(req('kb_qc').includes('target'), `kb_qc.target 是必填（required=${req('kb_qc').join(',')}）`)
  ok(props('kb_qc').target.default === 'final', 'kb_qc.target 带 default 注解')
  // ④ frames_at 建模成数组（内核 CLI 是逗号分隔字符串，在 execute 里 join）
  ok(props('kb_qc').frames_at.type === 'array', `frames_at 是数组（${props('kb_qc').frames_at.type}）`)
  ok(props('kb_qc').frames_at.items.type === 'number', 'frames_at.items 是 number')
  const kbQc3 = byName('kb_qc')
  const qcArr = await kbQc3.execute(
    { project: SMOKE_PROJECT, target: 'subtitle_test', frames_at: [1, 2.5] },
    { signal: undefined })
  ok(qcArr && qcArr.cause === 'missing_input',
    `frames_at 数组被正确转成 CLI（拿到 ${qcArr && qcArr.cause}，说明参数已通到内核）`)

  // ⑤ projectsRoot 边界：越界必须被拒绝（cordis.patch.yml 一直声称有这条边界）
  const outside = await kbStatus3.execute(
    { project: path.resolve(ROOT, '..', 'not-under-projects-root') }, { signal: undefined })
  ok(outside && outside.cause === 'project_outside_root',
    `projectsRoot 之外的 project 被拒绝（cause=${outside && outside.cause}）`)
  const inside = await kbStatus3.execute({ project: SMOKE_PROJECT }, { signal: undefined })
  ok(inside && inside.ok === true, 'projectsRoot 之内的 project 正常放行')

  // ⑥ 一阶段一工具：**每个内核子命令恰好被一个工具覆盖**（不多不少）
  const COVERAGE = {
    status: 'kb_status', 'cut-plan': 'kb_plan_cut', transcribe: 'kb_transcribe',
    'cut-apply': 'kb_cut', srt: 'kb_srt', 'burn-test': 'kb_burn',
    'burn-full': 'kb_burn', assemble: 'kb_assemble', mix: 'kb_assemble',
    'overlay-render': 'kb_overlay', qc: 'kb_qc', 'bgm-generate': 'kb_bgm',
  }
  const missing = Object.entries(COVERAGE).filter(([cmd, tool]) => {
    const t = byName(tool)
    if (!t) return true
    const p = (t.parameters && t.parameters.properties) || {}
    // 多 mode 工具：确认那个 mode 值真的被 enum 收下
    if (cmd === 'burn-full') return !(p.mode && (p.mode.enum || []).includes('full'))
    if (cmd === 'mix') return !(p.mode && (p.mode.enum || []).includes('mix'))
    return false
  }).map(([cmd]) => cmd)
  ok(missing.length === 0, `内核 12 个子命令都被工具覆盖（缺：${missing.join(',') || '无'}）`)
}

// ── 10. 权限矩阵：子代理白名单必须与设计一致（工具名 = 权限边界）──
{
  const yml = readFileSync(path.join(ROOT, 'agents', 'subagents.patch.yml'), 'utf8')
  const matrix = {}
  const re = /toolName:\s*([\w-]+)([\s\S]*?)(?=toolName:|$)/g
  let m
  while ((m = re.exec(yml)) !== null) {
    const allowPart = m[2].split('persona:')[0]
    matrix[m[1]] = [...allowPart.matchAll(/^\s*-\s+([A-Za-z0-9_]+)\s*$/gm)].map((x) => x[1])
  }
  const who = (tool) => Object.entries(matrix).filter(([, a]) => a.includes(tool)).map(([k]) => k)

  ok(Object.keys(matrix).length === 4, `解析出 4 个子代理白名单（实际 ${Object.keys(matrix).length}）`)
  ok(who('kb_override').length === 0,
     `⛔ kb_override 不在任何子代理白名单里（实际：${who('kb_override').join(',') || '无'}）`)
  ok(who('kb_bgm').join() === 'delegate_transcript_editor',
     `kb_bgm 只授给剪裁策划（实际：${who('kb_bgm').join(',') || '无'}）`)
  ok(who('kb_qc').join() === 'delegate_qc_inspector',
     `kb_qc 只授给成片质检（实际：${who('kb_qc').join(',') || '无'}）`)
  ok(who('kb_overlay').join() === 'delegate_overlay_designer',
     `kb_overlay 只授给特效设计（实际：${who('kb_overlay').join(',') || '无'}）`)
  ok(who('kb_transcribe').join() === 'delegate_transcript_editor',
     `kb_transcribe 只授给剪裁策划（实际：${who('kb_transcribe').join(',') || '无'}）`)
  ok(who('kb_burn').join() === 'delegate_subtitle_maker',
     `kb_burn 只授给字幕制作（实际：${who('kb_burn').join(',') || '无'}）`)
  ok(who('kb_srt').join() === 'delegate_subtitle_maker',
     `kb_srt 只授给字幕制作（实际：${who('kb_srt').join(',') || '无'}）`)
}

console.log('')
if (failures.length) {
  console.log(`${failures.length} 项不通过：`)
  for (const f of failures) console.log(`  - ${f}`)
  process.exit(1)
}
console.log('插件自检全部通过')
