$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
Push-Location $project
try {
    & $python -c 'import fitdecode'
    if ($LASTEXITCODE -ne 0) { throw 'Install requirements.txt before running the dry run.' }
    & $python -m unittest discover -s tests -p 'test_*.py' -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; no Garmin request attempted.' }
    & $python .\garmin_sync.py --dry-run --save-plan
    if ($LASTEXITCODE -ne 0) { throw 'Read-only dry run stopped. No history update was attempted.' }
} finally { Pop-Location }
