$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

python -m compileall upper_computer

$hasPyInstaller = python -c "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('PyInstaller') else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: python -m pip install -r requirements-build.txt"
}

python -m PyInstaller --clean --noconfirm EchoGuard.spec

$distName = python -c "from upper_computer.version import STANDARD_DIST_NAME; print(STANDARD_DIST_NAME)"
Write-Host "EchoGuard build complete: dist\$distName\EchoGuard.exe"
