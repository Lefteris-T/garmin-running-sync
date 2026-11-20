$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
if (-not (Test-Path -LiteralPath $runtime)) {
    throw 'Python 3.13 was not found at its standard per-user path. Install it first, or report its installation path.'
}
& $runtime --version
if ($LASTEXITCODE -ne 0) { throw 'Python could not start.' }
$venv = Join-Path $projectDir '.venv'
if (-not (Test-Path -LiteralPath $venv)) {
    & $runtime -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
$python = Join-Path $venv 'Scripts\python.exe'
@'
import sys, ssl, json, platform
assert sys.platform == "win32"
assert sys.prefix != sys.base_prefix
print("Windows virtual environment verified")
print(platform.python_version())
print(ssl.OPENSSL_VERSION)
'@ | & $python -
if ($LASTEXITCODE -ne 0) { throw 'Python verification failed.' }
& $python -m pip --version
if ($LASTEXITCODE -ne 0) { throw 'pip verification failed.' }
