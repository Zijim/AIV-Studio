param([string]$DataDirectory)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($DataDirectory) { $env:AIV_DATA_DIR = [IO.Path]::GetFullPath($DataDirectory) }
$env:PYTHONPATH = Join-Path $taskRoot 'backend'
$runtimeJson = & (Join-Path $taskRoot '.venv\Scripts\python.exe') -c 'from app.config import load_settings; import json; s=load_settings(); print(json.dumps({"port":s.port,"data_dir":str(s.data_dir)}))'
$runtime = $runtimeJson | ConvertFrom-Json
$url = 'http://127.0.0.1:' + $runtime.port
try { $health = Invoke-RestMethod -Uri ($url + '/api/v1/health') -TimeoutSec 3 } catch { Write-Host ('No service is listening at ' + $url); return }
if ($health.workspace_root -ne $taskRoot -or $health.data_dir -ne $runtime.data_dir) { throw 'Instance identity mismatch.' }
$session = Invoke-RestMethod -Uri ($url + '/api/v1/session') -SessionVariable web
$headers = @{ Origin=$url; 'X-AIV-CSRF'=$session.csrf_token }
$null = Invoke-RestMethod -Uri ($url + '/api/v1/system/shutdown') -Method Post -Headers $headers -WebSession $web
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-Process -Id $health.process_id -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 300
}
Write-Host 'Shutdown requested. ComfyUI accepted jobs continue; the next startup reconciles their results.'
