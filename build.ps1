param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$taskDataPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'dist\kotonoha-renamer\data'))
$taskBackupPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ('build\portable-data-' + [guid]::NewGuid().ToString('N'))))
$taskWorkspacePrefix = [IO.Path]::GetFullPath($PSScriptRoot) + '\'
if (-not $taskDataPath.StartsWith($taskWorkspacePrefix, [StringComparison]::OrdinalIgnoreCase) -or
    -not $taskBackupPath.StartsWith($taskWorkspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Portable data backup escaped the workspace.'
}
try {
    & $Python -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw 'Static checks failed.' }
    & $Python -m ruff format --check .
    if ($LASTEXITCODE -ne 0) { throw 'Formatting check failed.' }
    $taskExePath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'dist\kotonoha-renamer\Kotonoha.exe'))
    if (Get-Process -Name Kotonoha -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $taskExePath }) {
        throw 'Close the portable GUI before rebuilding it.'
    }
    if (Test-Path -LiteralPath $taskDataPath) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $taskBackupPath) -Force | Out-Null
        Move-Item -LiteralPath $taskDataPath -Destination $taskBackupPath
    }
    & $Python -m PyInstaller --noconfirm --clean kotonoha.spec
    if ($LASTEXITCODE -ne 0) { throw 'Portable build failed.' }
    if (Test-Path -LiteralPath $taskBackupPath) {
        Move-Item -LiteralPath $taskBackupPath -Destination $taskDataPath
    }
    & $Python scripts/package-release.py
    if ($LASTEXITCODE -ne 0) { throw 'Portable packaging failed.' }
} finally {
    if ((Test-Path -LiteralPath $taskBackupPath) -and -not (Test-Path -LiteralPath $taskDataPath)) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $taskDataPath) -Force | Out-Null
        Move-Item -LiteralPath $taskBackupPath -Destination $taskDataPath
    }
    Pop-Location
}
