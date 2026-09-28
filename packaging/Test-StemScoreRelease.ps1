[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Version,
    [string]$DistDirectory
)

$ErrorActionPreference = 'Stop'

function Get-Sha256Hex {
    param([Parameter(Mandatory)][string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}
$repositoryRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($DistDirectory)) {
    $DistDirectory = Join-Path $repositoryRoot 'dist'
}
$application = Join-Path $DistDirectory 'StemScore'
$executable = Join-Path $application 'StemScore.exe'
$archive = Join-Path $DistDirectory "StemScore-$Version-win64.zip"
$checksum = "$archive.sha256"

foreach ($path in @($executable, $archive, $checksum)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Release artifact is missing: $path"
    }
}

$expectedHash = ((Get-Content -LiteralPath $checksum -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
$actualHash = Get-Sha256Hex -Path $archive
if ($expectedHash -ne $actualHash) {
    throw 'Portable ZIP SHA-256 does not match its checksum file.'
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($archive)
try {
    $entryNames = @($zip.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
    $requiredSuffixes = @(
        '/StemScore.exe',
        '/_internal/stemscore/model_catalog.json',
        '/_internal/stemscore/runtime/analyze_music.py',
        '/_internal/stemscore/runtime/analyze_clap.py',
        '/_internal/stemscore/web/models.html',
        '/THIRD_PARTY_NOTICES.md'
    )
    foreach ($suffix in $requiredSuffixes) {
        if (-not ($entryNames | Where-Object { ('/' + $_).EndsWith($suffix, [StringComparison]::OrdinalIgnoreCase) })) {
            throw "Portable ZIP is missing required entry: $suffix"
        }
    }
} finally {
    $zip.Dispose()
}

$process = Start-Process -FilePath $executable -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 4
if ($process.HasExited) {
    throw "Frozen application exited during smoke test with code $($process.ExitCode)."
}
Stop-Process -Id $process.Id -Force

[pscustomobject]@{
    Version = $Version
    Archive = $archive
    Sha256 = $actualHash
    RequiredEntries = $requiredSuffixes.Count
    FrozenStartup = 'passed'
}
