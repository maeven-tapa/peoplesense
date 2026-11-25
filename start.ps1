$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Install the virtual environment and requirements first. See README.md.'
}
Set-Location -LiteralPath $PSScriptRoot
& $taskPython manage.py migrate
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $taskPython manage.py bootstrap_appliance
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $taskPython manage.py runserver 127.0.0.1:8000
