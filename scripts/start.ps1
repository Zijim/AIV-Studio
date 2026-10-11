param([string]$DataDirectory)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($DataDirectory) { $env:AIV_DATA_DIR = [IO.Path]::GetFullPath($DataDirectory) }
$env:PYTHONPATH = Join-Path $taskRoot 'backend'
Push-Location $taskRoot
try {
    & (Join-Path $taskRoot '.venv\Scripts\python.exe') -m app.launch
} finally { Pop-Location }
