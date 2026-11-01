param([switch]$Voice)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$tempRoot = Join-Path $projectRoot 'data\tmp'
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
try {
    $env:TEMP = $tempRoot
    $env:TMP = $tempRoot
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        & python -m venv (Join-Path $projectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed' }
    }
    & $pythonPath -m pip install --no-cache-dir setuptools==80.9.0
    if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed' }
    $lockName = if ($Voice) { 'requirements-voice.lock' } else { 'requirements-wechat.lock' }
    & $pythonPath -m pip install --no-cache-dir -r (Join-Path $projectRoot $lockName)
    if ($LASTEXITCODE -ne 0) { throw 'Runtime dependency installation failed' }
    & $pythonPath -m pip install --no-cache-dir --no-build-isolation -e $projectRoot
    if ($LASTEXITCODE -ne 0) { throw 'Wechat Reader installation failed' }
    & $pythonPath -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency compatibility check failed' }
    & $pythonPath (Join-Path $projectRoot 'scripts\acceptance.py')
    if ($LASTEXITCODE -ne 0) { throw 'Acceptance failed' }
} finally {
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
}
