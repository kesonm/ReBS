[CmdletBinding()]
param(
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectDirectory = $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$frontendDirectory = Join-Path $projectDirectory 'frontend'
$frontendEntry = Join-Path $frontendDirectory 'dist\index.html'

if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Python environment not found: $pythonExecutable. From ReBS, run: python -m venv .venv"
}

Push-Location -LiteralPath $projectDirectory
try {
    Write-Host 'Checking pinned Python dependencies...'
    $dependencyCheck = @'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

missing = []
for line in Path('requirements.txt').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    package, expected = line.split('==', 1)
    try:
        actual = version(package)
    except PackageNotFoundError:
        actual = 'missing'
    if actual != expected:
        missing.append(f'{package}: {actual}; required {expected}')
if missing:
    print('\n'.join(missing))
    sys.exit(1)
import akshare, fastapi, uvicorn
'@
    & $pythonExecutable -c $dependencyCheck
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Installing Python dependencies...'
        & $pythonExecutable -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) {
            throw 'Python dependency installation failed. See the pip error above.'
        }
    }

    if ($Rebuild -or -not (Test-Path -LiteralPath $frontendEntry -PathType Leaf)) {
        $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
        if ($null -eq $npmCommand) {
            throw 'npm.cmd was not found. Install Node.js and reopen the terminal before building the frontend.'
        }

        Push-Location -LiteralPath $frontendDirectory
        try {
            if (-not (Test-Path -LiteralPath 'node_modules' -PathType Container)) {
                Write-Host 'Installing frontend dependencies...'
                if (Test-Path -LiteralPath 'package-lock.json' -PathType Leaf) {
                    & $npmCommand.Source ci
                }
                else {
                    & $npmCommand.Source install
                }
                if ($LASTEXITCODE -ne 0) {
                    throw 'Frontend dependency installation failed. See the npm error above.'
                }
            }

            Write-Host 'Building frontend...'
            & $npmCommand.Source run build
            if ($LASTEXITCODE -ne 0) {
                throw 'Frontend build failed. See the build error above.'
            }
        }
        finally {
            Pop-Location
        }
    }

    Write-Host ''
    Write-Host 'ReBS: http://127.0.0.1:8000'
    Write-Host 'The address is ready after Uvicorn reports startup complete. Press Ctrl+C to stop.'
    & $pythonExecutable -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
    if ($LASTEXITCODE -ne 0) {
        throw 'The server stopped with an error. Check the output above; no existing service was stopped.'
    }
}
finally {
    Pop-Location
}
