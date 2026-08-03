$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

function Assert-NativeSuccess([string]$step, [int]$exitCode) {
    if ($exitCode -ne 0) {
        throw "$step failed with exit code $exitCode"
    }
}

$env:QT_QPA_PLATFORM = "offscreen"
python -m compileall -q upper_computer
Assert-NativeSuccess "Python compileall" $LASTEXITCODE

$testFiles = @(git ls-files "tests/test_*.py")
Assert-NativeSuccess "List tracked tests" $LASTEXITCODE
if ($testFiles.Count -eq 0) {
    throw "No tracked Python tests found"
}
python -m unittest -v @testFiles
Assert-NativeSuccess "Tracked Python test suite" $LASTEXITCODE

& (Join-Path $PSScriptRoot "build_upper_computer.ps1")
& (Join-Path $PSScriptRoot "build_upper_computer_mobile_v033.ps1")

$standardName = python -c "from upper_computer.version import STANDARD_DIST_NAME; print(STANDARD_DIST_NAME)"
Assert-NativeSuccess "Read standard release name" $LASTEXITCODE
$mobileName = python -c "from upper_computer.version import MOBILE_DIST_NAME; print(MOBILE_DIST_NAME)"
Assert-NativeSuccess "Read Mobile release name" $LASTEXITCODE
$releaseDir = Join-Path $repoRoot "releases"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null

$archives = @()
foreach ($name in @($standardName, $mobileName)) {
    $source = Join-Path $repoRoot "dist\$name"
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Missing packaged directory: $source"
    }
    $archive = Join-Path $releaseDir "$name.zip"
    Compress-Archive -Path (Join-Path $source "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    $archives += Get-Item -LiteralPath $archive
}

$checksumPath = Join-Path $releaseDir "SHA256SUMS.txt"
$checksumLines = foreach ($archive in $archives) {
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($archive.Name)"
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($checksumPath, [string[]]$checksumLines, $utf8NoBom)

Write-Host "Release packages created:"
$archives | ForEach-Object { Write-Host "  $($_.FullName)" }
Write-Host "  $checksumPath"
