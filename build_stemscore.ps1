param(
    [string]$Version
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv312\Scripts\python.exe'
$web = Join-Path $root 'stemscore\web'
$runtime = Join-Path $root 'stemscore\runtime'
$modelCatalog = Join-Path $root 'stemscore\model_catalog.json'

if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = (& $python -c "import stemscore; print(stemscore.__version__)").Trim()
}

& $python -m pytest -q
& $python -m PyInstaller --noconfirm --clean --windowed --onedir --name StemScore --add-data "$web;stemscore\web" --add-data "$runtime;stemscore\runtime" --add-data "$modelCatalog;stemscore" (Join-Path $root 'run_stemscore.py')

$packageDocs = Join-Path $root 'dist\StemScore\docs'
New-Item -ItemType Directory -Path $packageDocs -Force | Out-Null
Copy-Item -Path (Join-Path $root 'docs\*.md') -Destination $packageDocs -Force
Copy-Item -LiteralPath (Join-Path $root 'THIRD_PARTY_NOTICES.md') -Destination (Join-Path $root 'dist\StemScore\THIRD_PARTY_NOTICES.md') -Force
Copy-Item -LiteralPath (Join-Path $root 'tools\setup_stemscore_runtime.ps1') -Destination (Join-Path $root 'dist\StemScore\setup_runtime.ps1') -Force
$packageTraining = Join-Path $root 'dist\StemScore\training'
$packageValidation = Join-Path $root 'dist\StemScore\validation'
New-Item -ItemType Directory -Path $packageTraining,$packageValidation -Force | Out-Null
$validationDocs = @(Get-ChildItem -Path (Join-Path $root 'validation\*.md') -File -ErrorAction SilentlyContinue)
if ($validationDocs.Count -gt 0) {
    Copy-Item -LiteralPath $validationDocs.FullName -Destination $packageValidation -Force
}

$portable = Join-Path $root "dist\StemScore-$Version-win64.zip"
Compress-Archive -LiteralPath (Join-Path $root 'dist\StemScore') -DestinationPath $portable -Force
$portableHash = Get-FileHash -Algorithm SHA256 $portable
$checksum = Join-Path $root "dist\StemScore-$Version-win64.zip.sha256"
"$($portableHash.Hash.ToLowerInvariant())  StemScore-$Version-win64.zip" | Set-Content -LiteralPath $checksum -Encoding ascii
$portableHash

