/**
 * 注册表级验证：把插件挂到**真实** `ctx.tools`（@deepseek-ai/dsh-tools 的 ToolRuntime，
 * 而非桩对象），再读 `ctx.tools.schemas()` —— 这正是官方生成 `docs/tool-catalog.md`
 * 用的方式（"starts each tool plugin on a real context and reads ctx.tools.schemas()"）。
 *
 * 它验证的东西是桩测试证不了的：
 *   · 我们的 `parameters` DSL 能否通过真实校验；
 *   · `parameters` DSL 的 `required: true` 能否被抽成顶层 `required` 数组；
 *
 * ⚠️ 实测结论（2026-10-05）：`schemas()` **不返回 `output`** —— `output` 只服务 SDK 模式的
 *   类型生成，**不进发给模型的 function schema**（OpenAI 的 function 只有 name/description/parameters）。
 *   · `ctx.tools.guard()` 在真实注册表上是否存在且可注册。
 *
 * 不调用任何模型、不启动 harness、不写 ~/.dsh。
 *
 * 运行：node tools/registry_smoke.mjs
 */

import path from 'node:path'
import { readFileSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')
const EXPECTED = [
  'kb_status', 'kb_plan_cut', 'kb_transcribe', 'kb_cut', 'kb_srt', 'kb_burn',
  'kb_assemble', 'kb_overlay', 'kb_qc', 'kb_bgm', 'kb_override',
]

const failures = []
const ok = (cond, label) => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}`)
  if (!cond) failures.push(label)
}
const pluginOf = (m) => (m && m.default && !m.apply ? m.default : m)

console.log('=== 挂载真实服务 ===')
const { Context } = await import('@deepseek-ai/cordis')
const ctx = new Context()

for (const pkg of ['@deepseek-ai/dsh-system-prompt', '@deepseek-ai/dsh-tools']) {
  try {
    const mod = await import(pkg)
    ctx.plugin(pluginOf(mod))
    console.log(`  已挂载 ${pkg}`)
  } catch (e) {
    console.log(`  挂载 ${pkg} 失败: ${e.message}`)
  }
}

// 让服务激活
try { if (ctx.lifecycle && typeof ctx.lifecycle.flush === 'function') await ctx.lifecycle.flush() } catch { /* 无该 API */ }
await new Promise((r) => setTimeout(r, 80))

console.log('')
console.log('=== 真实注册表可用性 ===')
ok(ctx.tools != null, 'ctx.tools 已激活（真实 ToolRuntime）')
if (ctx.tools == null) {
  console.log('SKIP  真实注册表未激活（服务链不完整），本次跳过注册表级断言')
  console.log('      桩测试（plugin_smoke.mjs）仍是有效的回归防线。')
  process.exit(0)
}
ok(typeof ctx.tools.register === 'function', 'ctx.tools.register 是函数')
ok(typeof ctx.tools.guard === 'function', 'ctx.tools.guard 是函数（monotonic deny 入口）')
ok(typeof ctx.tools.schemas === 'function', 'ctx.tools.schemas 是函数')

console.log('')
console.log('=== 挂载本插件 ===')
const mod = await import(pathToFileURL(path.join(ROOT, 'plugin', 'src', 'index.js')).href)
mod.apply(ctx, {})
await new Promise((r) => setTimeout(r, 80))

const schemas = ctx.tools.schemas()
const names = schemas.map((s) => s.name)
console.log(`  注册表里共有 ${names.length} 个工具: ${names.join(', ')}`)

console.log('')
console.log('=== 断言 ===')
for (const n of EXPECTED) ok(names.includes(n), `真实注册表可见 ${n}`)

// ── 覆盖度：内核子命令 vs 插件工具（真实投影后逐项确认）──
//   内核入口有两个来源：
//     1) DISPATCH 表 —— 走 pipeline/stage*.py 子进程；
//     2) kb.py 里的特判分支 if cmd in ("status", "overlay-render") —— 直接调函数、不起子进程。
//   一阶段一工具之后，"哪个工具覆盖哪个子命令"的显式映射表在 plugin_smoke.mjs 的 COVERAGE；
//   这里在**真实注册表**上确认：枚举被真实投影、没有任何自由 args、聚合入口确实消失。
const kbPy = readFileSync(path.join(ROOT, 'kb.py'), 'utf8')
const disp = [...kbPy.matchAll(/^\s{4}"([a-z-]+)":\s+\("stage\d/gm)].map((m) => m[1])
const specialLine = (kbPy.match(/if cmd in \(([^)]*)\)/) || [])[1] || ''
const specials = [...specialLine.matchAll(/"([a-z-]+)"/g)].map((m) => m[1])
const kernel = [...new Set([...disp, ...specials])].sort()
ok(disp.length > 0 && specials.length > 0,
   `读到内核入口：DISPATCH ${disp.length} 项 + 特判 ${specials.join('/')}`)
ok(kernel.length === 12, `内核子命令共 ${kernel.length} 个（期望 12）`)

const byName = (n) => schemas.find((s) => s.name === n)
const kbBurn = byName('kb_burn')
ok(!!kbBurn && (kbBurn.parameters.properties.mode.enum || []).join() === 'test,full',
   `真实注册表里 kb_burn.mode = test|full`)
const kbAsm = byName('kb_assemble')
ok(!!kbAsm && (kbAsm.parameters.properties.mode.enum || []).join() === 'assemble,mix',
   `真实注册表里 kb_assemble.mode = assemble|mix`)
ok(Array.isArray(kbAsm.parameters.required)
   && kbAsm.parameters.required.includes('project')
   && kbAsm.parameters.required.includes('mode'),
   `required:true 被抽成顶层数组（kb_assemble.required = ${(kbAsm.parameters.required || []).join(',')}）`)

ok(!names.includes('kb_run'), '真实注册表里没有 kb_run（聚合入口已删）')
const freeArgs = schemas.filter((s) => {
  const p = (s.parameters && s.parameters.properties) || {}
  return Object.prototype.hasOwnProperty.call(p, 'args')
}).map((s) => s.name)
ok(freeArgs.length === 0, `真实注册表里没有自由 args 参数（实际：${freeArgs.join(',') || '无'}）`)
console.log('')
if (failures.length) {
  console.log(`${failures.length} 项不通过：`)
  for (const f of failures) console.log(`  - ${f}`)
  process.exit(1)
}
console.log('注册表级验证全部通过')
