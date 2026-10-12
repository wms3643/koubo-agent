# 建立本地依赖联接（Windows，免管理员）
#
# 为什么需要：
#   plugin/ 与 tools/ 里的 JS 要从 **已安装的 DSH** 解析 `@deepseek-ai/dsh-tools`
#   这类裸模块名。ESM **不认** NODE_PATH，所以必须在解析链上真实存在 node_modules。
#   用「目录联接（Junction）」指向 DSH 安装目录即可，零复制、不污染仓库。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\link-dsh-deps.ps1 -DshNodeModules "D:\dsh-sandbox\node_modules"
#
# 卸载：删联接只能这样删，⛔ 绝不能用 Remove-Item -Recurse（会删掉**目标**里的真实数据）
#   [System.IO.Directory]::Delete("<repo>\node_modules", $false)
#   [System.IO.Directory]::Delete("<repo>\plugin\node_modules", $false)

param(
  [Parameter(Mandatory = $true)][string]$DshNodeModules,
  [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot)
)

if (-not (Test-Path -LiteralPath $DshNodeModules)) {
  Write-Error "目标不存在：$DshNodeModules（先在一个临时目录 npm i @deepseek-ai/dsh，或指向已有的 DSH 安装）"
  exit 1
}

$targets = @(
  (Join-Path $RepoRoot 'node_modules'),          # 服务 tools/ 下的脚本
  (Join-Path $RepoRoot 'plugin\node_modules')    # 服务 plugin/src 下的插件本体
)

foreach ($p in $targets) {
  if (Test-Path -LiteralPath $p) {
    $it = Get-Item -LiteralPath $p -Force
    Write-Output ("跳过（已存在，LinkType=" + $it.LinkType + "）：" + $p)
    continue
  }
  New-Item -ItemType Junction -Path $p -Target $DshNodeModules | Out-Null
  Write-Output ("已创建 Junction：" + $p + " -> " + $DshNodeModules)
}

Write-Output ""
Write-Output "验证：node tools\registry_smoke.mjs"
