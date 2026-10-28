param()
$ErrorActionPreference = 'Stop'
$publicRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonExe = Join-Path $publicRoot '.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $publicRoot 'backend'
$publicSettings = (& $pythonExe -c 'from app.config import load_settings; import json; s=load_settings(); print(json.dumps({"port":s.port,"data_dir":str(s.data_dir),"origin":s.public_origin}))') | ConvertFrom-Json
if (-not $publicSettings.origin) { throw 'Configure sharing.public_origin in config.toml first.' }
$localUrl = 'http://127.0.0.1:' + $publicSettings.port
$logDirectory = Join-Path $publicSettings.data_dir 'logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$health = $null
try { $health = Invoke-RestMethod -Uri ($localUrl + '/api/v1/health') -TimeoutSec 3 } catch {}
if ($health) {
    if ($health.workspace_root -ne $publicRoot -or $health.data_dir -ne $publicSettings.data_dir) {
        throw 'A different application is using the configured port.'
    }
} else {
    Start-Process -FilePath $pythonExe -ArgumentList @('-m','app.launch') -WorkingDirectory $publicRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDirectory 'public-app.stdout.log') -RedirectStandardError (Join-Path $logDirectory 'public-app.stderr.log') | Out-Null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        try { $health = Invoke-RestMethod -Uri ($localUrl + '/api/v1/health') -TimeoutSec 2; break } catch {}
    }
    if (-not $health) { throw ('Application did not start; inspect ' + $logDirectory) }
}
$tunnelConfig = Join-Path $env:USERPROFILE '.cloudflared\config.yml'
$tunnelExe = (Get-Command cloudflared.exe -ErrorAction Stop).Source
& $tunnelExe tunnel --config $tunnelConfig ingress validate
if ($LASTEXITCODE -ne 0) { throw 'Tunnel configuration is invalid.' }
$tunnelProcess = Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" | Where-Object {
    $_.CommandLine -like '*run mediagent*' -or $_.CommandLine -like '*run a10b1bcc-eaed-4dbc-b12c-4151c9385c2e*'
} | Select-Object -First 1
if (-not $tunnelProcess) {
    $tunnelProcess = Start-Process -FilePath $tunnelExe -ArgumentList @('tunnel','--config',('"' + $tunnelConfig + '"'),'run','mediagent') -WorkingDirectory $publicRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDirectory 'public-tunnel.stdout.log') -RedirectStandardError (Join-Path $logDirectory 'public-tunnel.stderr.log') -PassThru
}
$tunnelProcessId = if ($tunnelProcess.Id) { $tunnelProcess.Id } else { $tunnelProcess.ProcessId }
[pscustomobject]@{application_pid=$health.process_id;tunnel_pid=$tunnelProcessId;url=$publicSettings.origin} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $publicSettings.data_dir 'public-runtime.json') -Encoding UTF8
Write-Output ('Shared website started: ' + $publicSettings.origin)
Write-Output 'Keep Windows online and start ComfyUI to enable generation.'
