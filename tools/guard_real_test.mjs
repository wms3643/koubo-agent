/**
 * 真实注册表·防花钱验证 —— 在**真 ToolRuntime** 上执行工具，断言"钱花不出去"。
 *
 * 背景（2026-10-05 真实踩坑，代价 0.4896 元）：
 *   我在插件里用 `ctx.tools.guard()` 做"付费前必须写好风格"的护栏。桩测试（我手写的假 ctx）
 *   显示它拦得住；但真 harness 里**没拦住，直接提交并扣了费**。
 *   读 dsh-tools 源码后定位：`guardReason(exec)` 只查 `layers.global` 与
 *   `exec.agent` 的 scope 链，而 `guard()` 是 `this.layers.effect(this.ctx, …)` ——
 *   **挂在调用它的那个 ctx 的层上**。插件拿到的 ctx 是插件 scope，于是护栏落在
 *   一个任何 agent 的链都到不了的层里。（官方 `childCtx.tools.guard(...)` 那个例子是
 *   **故意的 scoped 用法**，只想管住它自己派生的子代理；照抄到全局策略上是错的。）
 *
 * 结论与对策：
 *   ⛔ **不要把手上有钱的动作押在 guard 的作用域语义上** —— 那是我无法稳定验证的东西。
 *   ⇒ 把闸门放进**内核**（`pipeline/stage7_bgm.py` 的 `bgm_needs_confirm` 等），
 *     再用**这个脚本**在真实注册表上证明"不带确认就绝不提交"。内核是确定性的、可测的、
 *     而且**绕不过** —— 不管护栏在哪个 scope。
 *
 * 本脚本不花钱：全部用例要么 `dry_run`、要么就是**故意不带确认**（正因如此才不会提交）。
 *
 * 运行：node tools/guard_real_test.mjs
 */

import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')
const TMP = path.join(ROOT, 'projects', '_guard_real')
const NO_SPEC = path.join(TMP, 'nospec')
const WITH_SPEC = path.join(TMP, 'withspec')

const failures = []
const ok = (cond, label) => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}`)
  if (!cond) failures.push(label)
}
const pluginOf = (m) => (m && m.default && !m.apply ? m.default : m)

// 离线化：网关指向必然连不通的地址 ⇒ 就算哪条路漏了也不会真花钱
process.env.LK_BASE_URL = 'http://127.0.0.1:1/v1'

console.log('=== 挂载真实服务 ===')
const { Context } = await import('@deepseek-ai/cordis')
const ctx = new Context()
for (const pkg of ['@deepseek-ai/dsh-system-prompt', '@deepseek-ai/dsh-tools']) {
  try { ctx.plugin(pluginOf(await import(pkg))) } catch (e) { console.log(`  挂载 ${pkg} 失败: ${e.message}`) }
}
try { if (ctx.lifecycle && typeof ctx.lifecycle.flush === 'function') await ctx.lifecycle.flush() } catch { /* ignore */ }
await new Promise((r) => setTimeout(r, 80))

if (ctx.tools == null || typeof ctx.tools.execute !== 'function') {
  console.log('SKIP  真实注册表不可用，跳过')
  process.exit(0)
}

const mod = await import(pathToFileURL(path.join(ROOT, 'plugin', 'src', 'index.js')).href)
mod.apply(ctx, {})
await new Promise((r) => setTimeout(r, 80))

rmSync(TMP, { recursive: true, force: true })
mkdirSync(path.join(NO_SPEC, 'edit'), { recursive: true })
mkdirSync(path.join(WITH_SPEC, 'edit'), { recursive: true })
writeFileSync(path.join(WITH_SPEC, 'edit', 'bgm.json'), JSON.stringify({
  style: 'Instrumental only, no vocals, calm restrained underscore, loop-friendly.',
  output: 'bgm/bgm.mp3',
}), 'utf8')

let n = 0
const call = async (name, args) => {
  const res = await ctx.tools.execute({
    callId: `guard-real-${++n}`, name, arguments: args,
    signal: new AbortController().signal,
  })
  return JSON.stringify(res ?? {})
}

console.log('')
console.log('=== ① 内核级付费闸门（真正管用的那道）===')
const r1 = await call('kb_bgm', { project: WITH_SPEC })
console.log('  kb_bgm（风格已写好，但**没确认付费**）→ ' + r1.slice(0, 220))
ok(r1.includes('bgm_needs_confirm'),
   '真注册表上：不带 confirm_paid ⇒ 内核返回 bgm_needs_confirm，**没有提交**')

const r2 = await call('kb_bgm', { project: WITH_SPEC, dry_run: true })
console.log('  kb_bgm（dry_run）→ ' + r2.slice(0, 180))
ok(r2.includes('dry_run'), 'dry_run 放行（只查价、不花钱）')

console.log('')
console.log('=== ② 烧全片护栏（kb_burn mode=full）===')
const r3 = await call('kb_burn', { project: NO_SPEC, mode: 'full' })
const hitGuard = r3.includes('红线拦截')
const hitKernel = r3.includes('missing_input')
console.log(`  kb_burn(mode=full, 无测试片) → 护栏命中=${hitGuard} 落到内核=${hitKernel}`)
if (!hitGuard) {
  console.log('  ⚠️ 注意：本次护栏未命中，请求落到了内核（`missing_input`）。')
  console.log('     这说明 guard 的可见性依赖它被注册到哪一层 —— 所以本项目的**花钱与红线')
  console.log('     不只靠 guard**：内核 side 一律自带闸门（见 ① 与 transcript_exists / bgm_exists）。')
}

console.log('')
console.log('=== ③ 内核幂等闸（已有产物不再花钱）===')
mkdirSync(path.join(WITH_SPEC, 'bgm'), { recursive: true })
writeFileSync(path.join(WITH_SPEC, 'bgm', 'bgm.mp3'), Buffer.alloc(40_000, 1))
const r4 = await call('kb_bgm', { project: WITH_SPEC, confirm_paid: true })
console.log('  kb_bgm（已有产物 + 已确认付费）→ ' + r4.slice(0, 200))
ok(r4.includes('bgm_exists'), '已有产物时被内核幂等闸拦住（不会重复扣费）')

rmSync(TMP, { recursive: true, force: true })

console.log('')
if (failures.length) {
  console.log(`${failures.length} 项不通过：`)
  for (const f of failures) console.log(`  - ${f}`)
  process.exit(1)
}
console.log('真实注册表防花钱验证全部通过')
