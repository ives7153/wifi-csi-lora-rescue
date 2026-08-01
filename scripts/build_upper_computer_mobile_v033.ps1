$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

python -m compileall upper_computer
python -m unittest discover -s tests -p "test_*.py" -v

$hasPyInstaller = python -c "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('PyInstaller') else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: python -m pip install -r requirements-build.txt"
}

python -m PyInstaller --clean --noconfirm EchoGuardMobile.spec

$displayVersion = python -c "from upper_computer.version import DISPLAY_VERSION; print(DISPLAY_VERSION)"
$distName = python -c "from upper_computer.version import MOBILE_DIST_NAME; print(MOBILE_DIST_NAME)"
Write-Host "EchoGuard Mobile $displayVersion build complete: dist\$distName\EchoGuard.exe"
