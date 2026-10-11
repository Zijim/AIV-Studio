param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $taskRoot
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw '无法创建Python环境，请使用Python 3.12。' }
    }
    & '.\.venv\Scripts\python.exe' -m pip install --require-hashes -r backend\requirements.lock
    if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败。' }
    Push-Location frontend
    try {
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
    } finally { Pop-Location }
    Write-Host '环境已准备。运行 powershell -ExecutionPolicy Bypass -File scripts/start.ps1'
} finally { Pop-Location }
