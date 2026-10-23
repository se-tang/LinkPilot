# 直装新版 WSL（官方 MSI）+ Ubuntu（需要管理员）
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File "D:\deepseek工作区\_LinkPilot-src\wsl-install-msi.ps1"
#
# 为什么不用 winget：winget 装 Microsoft.WSL 会先调旧的 wsl.exe --update，在这台机器上失败后
# 整个安装就没落盘。改走官方 GitHub release 的 x64 MSI（更小，且 msiexec /qn 静默可靠）。
#
# 步骤：下载 wsl.<ver>.x64.msi → msiexec /i /qn → 复核 wsl --version → wsl --install -d Ubuntu --no-launch
# 日志：logs\wsl-install-msi.log

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8
$env:WSL_UTF8 = '1'

$logDir = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'wsl-install-msi.log'
Set-Content -Path $log -Value "=== WSL MSI 安装 $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) ===" -Encoding UTF8

function Log($m) { $line = "$(Get-Date -Format 'HH:mm:ss')  $m"; Write-Host $line; Add-Content -Path $log -Value $line -Encoding UTF8 }
function Ok($m)   { Log "[OK] $m" }
function Warn($m) { Log "[!] $m" }
function Bad($m)  { Log "[X] $m" }

function Invoke-Native {   # 原生命令包装：不因 stderr 中断，返回退出码与输出
  param([string]$File, [string[]]$CmdArgs)
  Log "  > $File $($CmdArgs -join ' ')"
  $out = (& $File @CmdArgs 2>&1 | Out-String)
  $code = $LASTEXITCODE
  foreach ($l in ($out.Trim() -split "`r?`n")) { if ($l.Trim()) { Add-Content -Path $log -Value "      $($l.Trim())" -Encoding UTF8 } }
  return [pscustomobject]@{ Code = $code; Out = $out }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Bad '需要管理员权限。'; exit 1 }
Ok '管理员权限正常'

# ---- 1) 找 MSI 下载地址 ----
$ver = ''
$url = ''
$dest = Join-Path $env:TEMP 'wsl-install.msi'
if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
try {
  $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/microsoft/WSL/releases/latest' `
                           -Headers @{ 'User-Agent' = 'wsl-installer' } -TimeoutSec 30
  $ver = $rel.tag_name
  $asset = $rel.assets | Where-Object { $_.name -match '\.x64\.msi$' } | Select-Object -First 1
  if ($asset) { $url = $asset.browser_download_url }
} catch { Warn "查最新版本失败：$($_.Exception.Message)" }

if (-not $url) {
  # 查不到就用已知的 3.0.3 兜底
  $ver = '3.0.3'
  $url = "https://github.com/microsoft/WSL/releases/download/$ver/wsl.$ver.0.x64.msi"
  Warn "改用兜底地址：$url"
}
Ok "目标版本：WSL $ver"
Log "  下载地址：$url"

# ---- 2) 下载（大文件用 WebClient，带进度） ----
Log '正在下载（约 350MB，国内可能较慢，请耐心）...'
$sw = [Diagnostics.Stopwatch]::StartNew()
try {
  $wc = New-Object Net.WebClient
  $wc.Headers.Add('User-Agent', 'wsl-installer')
  $wc.DownloadFile($url, $dest)
  Ok "下载完成：$([math]::Round((Get-Item $dest).Length / 1MB, 1)) MB，用时 $([int]$sw.Elapsed.TotalSeconds) 秒"
} catch {
  Bad "下载失败：$($_.Exception.Message)"
  exit 2
}

# ---- 3) 静默安装 MSI ----
Log '正在安装（msiexec /qn，可能需要 1-3 分钟）...'
$r = Invoke-Native 'msiexec.exe' @('/i', $dest, '/qn', '/norestart', '/L*v', (Join-Path $logDir 'wsl-msi-verbose.log'))
if ($r.Code -in 0, 3010) {
  Ok "MSI 安装返回 $($r.Code)（3010 = 成功但需重启）"
} else {
  Bad "MSI 安装失败，返回 $($r.Code)，详见 logs\wsl-msi-verbose.log"
}

# ---- 4) 复核 ----
Start-Sleep -Seconds 8
$v = Invoke-Native 'wsl.exe' @('--version')
$pkg = Get-AppxPackage -Name 'Microsoft.WSL' -ErrorAction SilentlyContinue
Log "  Microsoft.WSL 包：$(if ($pkg) { $pkg.Version } else { '未注册' })"

# ---- 5) 装 Ubuntu ----
$list = Invoke-Native 'wsl.exe' @('-l', '-q')
if ($list.Out -match 'Ubuntu') {
  Ok 'Ubuntu 已安装'
} else {
  Log '正在安装 Ubuntu（这一步会下载发行版，几分钟）...'
  # 用 cmd 重定向 stdin，避免 wsl.exe 在非交互会话里等按键
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  $out = (& cmd.exe /c "wsl.exe --install -d Ubuntu --no-launch < NUL" 2>&1 | Out-String)
  $code = $LASTEXITCODE
  $ErrorActionPreference = $old
  foreach ($l in ($out.Trim() -split "`r?`n")) { if ($l.Trim()) { Add-Content -Path $log -Value "      $($l.Trim())" -Encoding UTF8 } }
  Log "  wsl --install 退出码=$code"
  $list2 = Invoke-Native 'wsl.exe' @('-l', '-q')
  if ($list2.Out -match 'Ubuntu') { Ok 'Ubuntu 安装完成' } else { Warn 'Ubuntu 可能未装成功，见上面输出' }
}

# ---- 6) 结论 ----
Log ''
Log '=== 结果 ==='
Log "  WSL : $($v.Out.Split("`n")[0])"
Log "  发行版 : $((Invoke-Native 'wsl.exe' @('-l','-q')).Out -replace "`r?`n", ' | ')"
Log ''
Log '下一步：在任意 PowerShell 执行  wsl -d Ubuntu  ，首次启动会要你设 Linux 用户名（填 deepseek）与密码。'
Log ''
