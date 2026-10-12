/**
 * 诊断：DSH loader 到底用哪种 import 形态解析插件入口？
 *
 * 背景：`dsh --dump-config` 把 patch 里的相对 name 归一化成 **目录 URL**
 * （`file:///…/koubo-agent-dsh/plugin`）。若 loader 真按目录 URL 去 import，
 * 会因 ESM 不支持目录导入而失败 —— 表现为启动时那句
 *   `dsh: warning: 1 entry did not activate`
 *   `plugin-koubo (file:///…): failed to import`
 *
 * 本脚本把候选形态逐一试一遍，打印每个的真实错误码，用来决定 `name` 该写什么。
 *
 * 运行：node tools/check-plugin-import.mjs
 */

import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')
const PLUGIN_DIR = path.join(ROOT, 'plugin')
const PLUGIN_ENTRY = path.join(PLUGIN_DIR, 'src', 'index.js')

const encoded = (p) => pathToFileURL(p).href
const raw = (p) => 'file:///' + p.replace(/\\/g, '/')

const cases = [
  ['入口文件 URL（pathToFileURL，推荐）', encoded(PLUGIN_ENTRY)],
  ['入口文件 URL（未编码）', raw(PLUGIN_ENTRY)],
  ['目录 URL（pathToFileURL，loader 显示的形状）', encoded(PLUGIN_DIR)],
  ['目录 URL（未编码）', raw(PLUGIN_DIR)],
  ['相对入口（模拟 patch 文件旁的 ./plugin/src/index.js）', './plugin/src/index.js'],
]

let anyOk = false
for (const [label, spec] of cases) {
  try {
    const mod = await import(spec)
    anyOk = true
    console.log(`OK    ${label}`)
    console.log(`      → 导出: ${Object.keys(mod).join(', ')}`)
  } catch (e) {
    const code = e && e.code ? e.code : '(no code)'
    const first = String(e && e.message ? e.message : e).split('\n')[0]
    console.log(`FAIL  ${label}`)
    console.log(`      ${code}  ${first}`)
  }
}

console.log('')
console.log(anyOk
  ? '结论：至少有一种形态可用 —— 把 cordis.patch.yml 的 name 改成「入口文件」形式即可。'
  : '结论：全部失败，问题不在路径形态（可能是依赖解析）。')
