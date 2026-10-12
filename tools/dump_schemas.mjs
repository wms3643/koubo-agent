/**
 * 打印**真实**的工具 JSON Schema —— 即 `ctx.tools.schemas()` 的产物。
 *
 * 为什么要跑它而不是看源码：源码里的 `parameters` 用的是一套简写 DSL
 * （`{ type, required, description, enum, items, properties }`），
 * 最终发给模型的 JSON Schema 是这套 DSL **被真实投影**出来的结果。
 * 只有跑真实注册表才能看到投影后的形状（required 数组、items、additionalProperties 等）。
 *
 * 用法：
 *   node tools/dump_schemas.mjs            # 全部工具
 *   node tools/dump_schemas.mjs kb_run     # 只看一个
 *   node tools/dump_schemas.mjs kb_run wire # 只看「发给网关的形态」（模拟 pi-ai 的 toolsOf）
 */

import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')
const pluginOf = (m) => (m && m.default && !m.apply ? m.default : m)

const { Context } = await import('@deepseek-ai/cordis')
const ctx = new Context()
for (const pkg of ['@deepseek-ai/dsh-system-prompt', '@deepseek-ai/dsh-tools']) {
  ctx.plugin(pluginOf(await import(pkg)))
}
try { if (ctx.lifecycle && typeof ctx.lifecycle.flush === 'function') await ctx.lifecycle.flush() } catch { /* 无该 API */ }
await new Promise((r) => setTimeout(r, 80))

const mod = await import(pathToFileURL(path.join(ROOT, 'plugin', 'src', 'index.js')).href)
mod.apply(ctx, {})
await new Promise((r) => setTimeout(r, 80))

const MODES = new Set(['wire'])
// 允许 `node dump_schemas.mjs wire`（不写工具名，只看全部的发出去形态）
const want = MODES.has(process.argv[2]) ? undefined : process.argv[2]
const mode = MODES.has(process.argv[2]) ? process.argv[2] : process.argv[3]
let schemas = ctx.tools.schemas()
if (want) schemas = schemas.filter((s) => s.name === want)

if (mode === 'wire') {
  // 复刻 @deepseek-ai/dsh-llm-pi-ai 的 toolsOf()：只取 name / description / parameters
  schemas = schemas.map((t) => ({ name: t.name, description: t.description, parameters: t.parameters }))
  console.log('// 发给网关的 tools 字段（pi-ai toolsOf 的等价物）')
}

console.log(JSON.stringify(schemas, null, 2))
