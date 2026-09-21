param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputPath) {
    $OutputPath = Join-Path (Split-Path $repoRoot -Parent) "video-insight-source-safe.zip"
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)

if (Test-Path -LiteralPath $OutputPath) {
    throw "输出文件已存在，请更换名称或先手动移走：$OutputPath"
}

Push-Location $repoRoot
try {
    # git archive only includes committed files. Untracked config.json, .env,
    # cookies, logs, downloaded videos and test artifacts can never enter it.
    & git archive --format=zip --output=$OutputPath HEAD
    if ($LASTEXITCODE -ne 0) { throw "git archive 执行失败" }
}
finally {
    Pop-Location
}

Write-Output "安全源码包已生成：$OutputPath"
