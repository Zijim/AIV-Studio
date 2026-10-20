param([string]$DataDirectory)
$ErrorActionPreference = 'Stop'
$workbenchRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workbenchPython = Join-Path $workbenchRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $workbenchPython)) { throw 'Run scripts/setup.ps1 before creating the shortcut.' }
$workbenchDesktop = [Environment]::GetFolderPath('Desktop')
$workbenchName = -join ([char[]]@(0x6620, 0x4F5C, 0x5DE5, 0x4F5C, 0x53F0))
$workbenchLinkPath = Join-Path $workbenchDesktop ($workbenchName + '.lnk')
$workbenchShell = New-Object -ComObject WScript.Shell
$workbenchShortcut = $workbenchShell.CreateShortcut($workbenchLinkPath)
$workbenchShortcut.TargetPath = $workbenchPython
$workbenchShortcut.Arguments = '"' + (Join-Path $PSScriptRoot 'launch_workbench.py') + '"'
if ($DataDirectory) {
    $workbenchDataPath = [IO.Path]::GetFullPath($DataDirectory)
    $workbenchShortcut.Arguments += ' --data-directory "' + $workbenchDataPath + '"'
}
$workbenchShortcut.WorkingDirectory = $workbenchRoot
$workbenchShortcut.Description = 'Start AIV Studio and open the workbench in your browser.'
$workbenchShortcut.IconLocation = (Join-Path $PSScriptRoot 'workbench.ico') + ',0'
$workbenchShortcut.WindowStyle = 7
$workbenchShortcut.Save()
Write-Output $workbenchLinkPath
