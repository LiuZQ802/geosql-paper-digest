# 每天在本机运行文献日报（Windows 计划任务调用）
# - 出版社请求走本机直连出口（学校授权），能拿到 Elsevier / Springer 付费全文
# - 跑完把加密后的数据推送到 GitHub，触发网页部署
# - 云端 11:00 的任务看到当天已由本机跑过，就不再重复运行
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
New-Item -ItemType Directory -Force "$Root\logs" | Out-Null
$Today = Get-Date -Format "yyyy-MM-dd"
$Log = "$Root\logs\$Today.log"
function Log($msg) { "$(Get-Date -Format 'HH:mm:ss') $msg" | Tee-Object -FilePath $Log -Append }

# 读取本机密钥（.env.local，不进 git）
if (-not (Test-Path "$Root\.env.local")) { Log "缺少 .env.local，退出"; exit 1 }
Get-Content "$Root\.env.local" -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process") }
}

# 本机代理（Clash）在线就用它访问 arXiv / GitHub 等；出版社接口在代码里强制直连
$proxyUp = Test-NetConnection -ComputerName 127.0.0.1 -Port 7890 -InformationLevel Quiet -WarningAction SilentlyContinue
if ($proxyUp) { $env:HTTP_PROXY = "http://127.0.0.1:7890"; $env:HTTPS_PROXY = "http://127.0.0.1:7890" }
Log "start (proxy: $proxyUp)"

git pull --rebase --quiet 2>&1 | ForEach-Object { Log "git: $_" }

& "D:\miniconda3\python.exe" -X utf8 -m scholar.main 2>&1 | ForEach-Object { Log $_ }
if ($LASTEXITCODE -ne 0) { Log "pipeline failed (exit $LASTEXITCODE)，交给云端 11:00 补跑"; exit 1 }

Set-Content -Path "$Root\state\last_local_run.txt" -Value $Today -Encoding ASCII
git add web/data state 2>&1 | Out-Null
git commit --quiet -m "digest: $Today (local)" 2>&1 | ForEach-Object { Log "git: $_" }
git push --quiet 2>&1 | ForEach-Object { Log "git: $_" }
Log "done"
