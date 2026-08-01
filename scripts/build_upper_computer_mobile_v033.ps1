$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

function Assert-NativeSuccess([string]$step, [int]$exitCode) {
    if ($exitCode -ne 0) {
        throw "$step failed with exit code $exitCode"
    }
}

python -m compileall upper_computer
Assert-NativeSuccess "Python compileall" $LASTEXITCODE

$hasPyInstaller = python -c "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('PyInstaller') else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: python -m pip install -r requirements-build.txt"
}

python -m PyInstaller --clean --noconfirm EchoGuardMobile.spec
Assert-NativeSuccess "Mobile PyInstaller build" $LASTEXITCODE

$displayVersion = python -c "from upper_computer.version import DISPLAY_VERSION; print(DISPLAY_VERSION)"
Assert-NativeSuccess "Read display version" $LASTEXITCODE
$distName = python -c "from upper_computer.version import MOBILE_DIST_NAME; print(MOBILE_DIST_NAME)"
Assert-NativeSuccess "Read Mobile release name" $LASTEXITCODE
$executable = Join-Path $repoRoot "dist\$distName\EchoGuard.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Missing Mobile executable: $executable"
}
Write-Host "EchoGuard Mobile $displayVersion build complete: dist\$distName\EchoGuard.exe"
