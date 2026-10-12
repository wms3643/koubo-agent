# 把 koubo-agent-dsh 挂到你的 DSH 上（幂等，可重复执行）
#
# 做三件事：
#   1. 写凭据到 $DshHome\.credentials.yaml（version 1 + refs；密钥不进任何配置文件）
#   2. 把两条 patch 条目追加进 $DshHome\profiles\<profile>\cordis.patch.yml
#      · 元气中转站模型路由（llm-pi-ai）+ 默认模型指向它
#      · 本仓库的口播剪辑插件（11 个工具，一阶段一工具：kb_status / kb_plan_cut /
#        kb_transcribe / kb_cut / kb_srt / kb_burn / kb_assemble / kb_overlay /
#        kb_qc / kb_bgm / kb_override）
#   3. 打印验证命令
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\bootstrap-dsh.ps1 `
#     -DshHome "D:\dsh-home" -CredentialsDoc "$HOME\Desktop\元气中转站调用文档.md"
#
# 安全说明：
#   · 脚本从调用文档里**读取**密钥，只写进 DSH 的凭据存储；不会写进 cordis.patch.yml
#   · 凭据文件仅当前 OS 用户可读，但**agent 工具进程与你同用户**，所以它挡不住 agent
#   · 想撤销：删掉 profile 里带 KOUBO-BEGIN/KOUBO-END 标记的那一段，以及 .credentials.yaml 里那条 ref

param(
  [string]$DshHome = (Join-Path $HOME '.dsh'),
  [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
  [string]$CredentialsDoc = (Join-Path $HOME 'Desktop\元气中转站调用文档.md'),
  [string]$Profile = 'web',
  [string]$KeyRef = 'YUANQI_API_KEY'
)

$ErrorActionPreference = 'Stop'
$BEGIN = '# ==== KOUBO-BEGIN (bootstrap-dsh.ps1 生成，勿手改此区间) ===='
$END   = '# ==== KOUBO-END ===='

# ── 1) 凭据 ────────────────────────────────────────────────────────────
if (-not (Test-Path -LiteralPath $CredentialsDoc)) {
  Write-Error "找不到密钥文档：$CredentialsDoc`n用 -CredentialsDoc 指定路径。"
  exit 1
}
$text = Get-Content -LiteralPath $CredentialsDoc -Raw -Encoding UTF8
$m = [regex]::Match($text, 'sk-[A-Za-z0-9]+')
if (-not $m.Success) {
  Write-Error "在 $CredentialsDoc 里没找到 sk- 开头的密钥。"
  exit 1
}
$key = $m.Value

New-Item -ItemType Directory -Force -Path $DshHome | Out-Null
$credPath = Join-Path $DshHome '.credentials.yaml'
$credBody = "version: 1`nrefs:`n  $KeyRef`n"
# 用占位符再替换，避免把密钥写进脚本自身或临时文件
$credBody = $credBody -replace "$KeyRef`n", "$KeyRef`: $key`n"
[System.IO.File]::WriteAllText($credPath, $credBody, (New-Object System.Text.UTF8Encoding($false)))
Write-Output "已写凭据：$credPath（ref=$KeyRef，值长度 $($key.Length)，不回显）"

# ── 2) patch 条目（幂等） ──────────────────────────────────────────────
$profileDir = Join-Path $DshHome "profiles\$Profile"
if (-not (Test-Path -LiteralPath $profileDir)) {
  Write-Output "profile 目录不存在，先让它生成：dsh --profile $Profile --dump-config"
  & dsh --profile $Profile --dump-config | Out-Null
}
$patchPath = Join-Path $profileDir 'cordis.patch.yml'
if (-not (Test-Path -LiteralPath $patchPath)) {
  [System.IO.File]::WriteAllText($patchPath, "[]`n", (New-Object System.Text.UTF8Encoding($false)))
  Write-Output "已建空的 $patchPath"
}

$existing = Get-Content -LiteralPath $patchPath -Raw -Encoding UTF8
if ($existing -match [regex]::Escape($BEGIN)) {
  Write-Output "patch 已包含 koubo 区间，跳过追加（如需更新请先手动删除该区间）"
} else {
  $kbEntry   = (Join-Path $RepoRoot 'kb.py') -replace '\\', '/'
  $projects  = (Join-Path $RepoRoot 'projects') -replace '\\', '/'
  $entryFile = (Join-Path $RepoRoot 'plugin\src\index.js') -replace '\\', '/'
  $py         = 'C:/Users/lenovo/.workbuddy/binaries/python/envs/default/Scripts/python.exe'

  $block = @"
$BEGIN
- id: llm-pi-ai
  config:
    providers:
      yuanqi:
        displayName: 元气中转站
        apiKeyEnv: $KeyRef
        api: openai-completions
        baseURL: https://api.lk888.ai/v1
        defaultContextWindow: 262144
        defaultMaxTokens: 32768
        models:
          - id: doubao-seed-2-0-pro-260215
            name: Doubao Seed 2.0 Pro
          - id: deepseek-v4-pro
            name: DeepSeek V4 Pro
          - id: deepseek-v4-flash
            name: DeepSeek V4 Flash

- id: agent-default-model
  config:
    provider: yuanqi
    model: doubao-seed-2-0-pro-260215

- insert:
    # ⛔ name 必须是**入口文件**：写目录会让 ESM 抛 ERR_UNSUPPORTED_DIR_IMPORT，
    #    结果是插件静默不激活。诊断：node tools/check-plugin-import.mjs
    - id: plugin-koubo
      name: '$entryFile'
      config:
        python: '$py'
        kbEntry: '$kbEntry'
        projectsRoot: '$projects'
$END
"@
  Add-Content -LiteralPath $patchPath -Value $block -Encoding UTF8
  Write-Output "已追加 patch 区间到：$patchPath"
}

# ── 3) 下一步 ─────────────────────────────────────────────────────────
Write-Output ""
Write-Output "验证（不启动 harness、不调模型）："
Write-Output "  `$env:DSH_HOME = '$DshHome'"
Write-Output "  dsh --profile $Profile --dump-config | Select-String yuanqi,plugin-koubo"
Write-Output ""
Write-Output "真跑一次（会消耗 token）："
Write-Output "  dsh headless `"调用 kb_status 看 $RepoRoot\projects\_smoke 的 next_stage`""
Write-Output ""
Write-Output "挂 4 个子代理（可选）—— ⛔ DSH **没有**「子代理名册目录」，不要往 `$DshHome\subagents 拷 yaml："
Write-Output "  dsh --profile $Profile --patch `"$RepoRoot\profile\yuanqi-provider.patch.yml`" ``"
Write-Output "      --patch `"$RepoRoot\profile\cordis.patch.yml`" --patch `"$RepoRoot\agents\subagents.patch.yml`""
Write-Output "  （顺序：插件 patch 在前、子代理 patch 在后；详见 agents\README.md）"
