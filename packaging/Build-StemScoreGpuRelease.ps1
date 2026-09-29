[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$')]
    [string]$Version,

    [string]$SourceApplication,
    [string]$OutputDirectory,
    [string]$ManifestPath,
    [string]$RuntimePayloadRoot,
    [switch]$PublicSafe,
    [switch]$PlanOnly
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem

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
if ([string]::IsNullOrWhiteSpace($SourceApplication)) {
    $SourceApplication = Join-Path $repositoryRoot 'dist\StemScore'
}
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repositoryRoot "release\StemScore-$Version-gpu"
}
if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $ManifestPath = Join-Path $PSScriptRoot 'runtime-profiles.json'
}

function Get-RelativeAssetRecord {
    param(
        [Parameter(Mandatory)][System.IO.FileInfo]$File,
        [Parameter(Mandatory)][string]$Kind,
        [string]$ProfileId
    )

    $record = [ordered]@{
        name = $File.Name
        kind = $Kind
        sizeBytes = $File.Length
        sha256 = Get-Sha256Hex -Path $File.FullName
    }
    if ($ProfileId) {
        $record.profileId = $ProfileId
    }
    return [pscustomobject]$record
}

function Split-ReleaseFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$OutputDirectory,
        [Parameter(Mandatory)][string]$BaseName,
        [Parameter(Mandatory)][int64]$PartSize
    )

    $source = [System.IO.File]::OpenRead($Path)
    try {
        $buffer = New-Object byte[] (8MB)
        $index = 1
        $parts = [System.Collections.Generic.List[System.IO.FileInfo]]::new()
        while ($source.Position -lt $source.Length) {
            $partPath = Join-Path $OutputDirectory ("{0}.part{1:D3}" -f $BaseName, $index)
            $target = [System.IO.File]::Create($partPath)
            try {
                $remaining = [Math]::Min($PartSize, $source.Length - $source.Position)
                while ($remaining -gt 0) {
                    $count = [Math]::Min([int64]$buffer.Length, $remaining)
                    $read = $source.Read($buffer, 0, [int]$count)
                    if ($read -le 0) { break }
                    $target.Write($buffer, 0, $read)
                    $remaining -= $read
                }
            } finally {
                $target.Dispose()
            }
            $parts.Add((Get-Item -LiteralPath $partPath))
            $index++
        }
        return $parts
    } finally {
        $source.Dispose()
    }
}

function Test-RuntimePayload {
    param([Parameter(Mandatory)][string]$Root)

    $required = @(
        'msst\env\python.exe',
        'msst\inference.py',
        'msst\pretrain\BS-Roformer-Resurrection.ckpt',
        'msst\pretrain\bs_roformer_karaoke_frazer_becruily.ckpt',
        'msst\pretrain\BS-Rofo-SW-Fixed.ckpt',
        'msst\pretrain\dereverb_echo_mbr_fused_0.5_v2_0.25_big_0.25_super.ckpt',
        'uvr\models\Demucs_Models\v3_v4_repo\htdemucs_6s.yaml',
        'uvr\models\Demucs_Models\v3_v4_repo\5c90dfd2-34c22ccb.th',
        'transcription\basic-pitch\.venv\Lib\site-packages\basic_pitch',
        'transcription\basic-pitch\.venv\Lib\site-packages\basic_pitch\saved_models\icassp_2022\nmp.onnx',
        'transcription\python-base\python.exe',
        'transcription\transkun-packages\transkun\pretrained\2.0.pt'
    )
    foreach ($relative in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $relative))) {
            throw "Runtime payload is missing required path: $relative"
        }
    }
}

function Assert-PublicSafeApplication {
    param([Parameter(Mandatory)][string]$Root)

    $forbiddenExtensions = @('.ckpt', '.pt', '.pth', '.th', '.onnx', '.safetensors', '.gguf')
    $forbidden = @(
        Get-ChildItem -LiteralPath $Root -Recurse -File |
            Where-Object { $forbiddenExtensions -contains $_.Extension.ToLowerInvariant() }
    )
    if ($forbidden.Count -gt 0) {
        $relative = $forbidden | ForEach-Object { $_.FullName.Substring($Root.Length).TrimStart('\') }
        throw "Public release application contains model-weight files:`n$($relative -join "`n")"
    }
}

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Runtime manifest not found: $ManifestPath"
}
if (-not (Test-Path -LiteralPath $SourceApplication -PathType Container)) {
    throw "Shared application directory not found: $SourceApplication"
}
if ($PublicSafe -and -not [string]::IsNullOrWhiteSpace($RuntimePayloadRoot)) {
    throw 'PublicSafe releases cannot include RuntimePayloadRoot.'
}
if ($PublicSafe) {
    Assert-PublicSafeApplication -Root ([System.IO.Path]::GetFullPath($SourceApplication))
}

$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$entrypoint = Join-Path $SourceApplication $manifest.application.sharedCore.entrypoint
if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
    throw "Shared application entrypoint not found: $entrypoint"
}

$assetLimit = [int64]$manifest.release.githubAssetLimitBytes
$coreAssetName = ([string]$manifest.application.sharedCore.archiveNameTemplate).Replace('{version}', $Version)
$plannedAssets = @(
    $coreAssetName
    foreach ($profile in $manifest.runtimeProfiles) {
        "StemScore-$Version-win64-$($profile.id)-profile.zip"
    }
    'Deploy-StemScoreGpuPackage.ps1'
    'Select-StemScoreRuntime.ps1'
    'runtime-profiles.json'
    'release-manifest.json'
    'SHA256SUMS.txt'
)
if ($PublicSafe) {
    $plannedAssets += @('PUBLIC_RELEASE_README.md', 'THIRD_PARTY_NOTICES.md', 'setup_runtime.ps1')
}

if ($PlanOnly) {
    $runtimeProfiles = @()
    foreach ($profile in $manifest.runtimeProfiles) {
        $runtimeSource = if ([string]::IsNullOrWhiteSpace($RuntimePayloadRoot)) { $null } else { Join-Path $RuntimePayloadRoot $profile.id }
        $runtimePresent = $null -ne $runtimeSource -and (Test-Path -LiteralPath $runtimeSource -PathType Container)
        if ($runtimePresent) {
            Test-RuntimePayload -Root $runtimeSource
        }
        $runtimeProfiles += [pscustomobject]@{
            ProfileId = [string]$profile.id
            RuntimePayloadIncluded = $runtimePresent
            CoreModelWeightsIncluded = $runtimePresent
            OptionalAnalysisModelsIncluded = $false
        }
    }
    [pscustomobject]@{
        Version = $Version
        SourceApplication = (Resolve-Path -LiteralPath $SourceApplication).Path
        OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
        RuntimePayloadsIncluded = [bool]($runtimeProfiles | Where-Object RuntimePayloadIncluded)
        CoreModelWeightsIncluded = [bool]($runtimeProfiles | Where-Object CoreModelWeightsIncluded)
        OptionalAnalysisModelsIncluded = $false
        LaunchReady = [bool]($runtimeProfiles | Where-Object RuntimePayloadIncluded)
        RuntimeRepairAvailable = [bool]$PublicSafe
        PublicSafe = [bool]$PublicSafe
        DeploymentEntrypoint = 'Deploy-StemScoreGpuPackage.ps1'
        PlannedAssets = $plannedAssets
        Profiles = $runtimeProfiles
    }
    return
}

if (Test-Path -LiteralPath $OutputDirectory) {
    $existing = Get-ChildItem -LiteralPath $OutputDirectory -Force
    if ($existing.Count -gt 0) {
        throw "Output directory must not exist or must be empty: $OutputDirectory"
    }
} else {
    New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
}

$selectorPath = Join-Path $PSScriptRoot 'Select-StemScoreRuntime.ps1'
if (-not (Test-Path -LiteralPath $selectorPath -PathType Leaf)) {
    throw "Runtime selector script not found: $selectorPath"
}
$deployerPath = Join-Path $PSScriptRoot 'Deploy-StemScoreGpuPackage.ps1'
if (-not (Test-Path -LiteralPath $deployerPath -PathType Leaf)) {
    throw "Deployment script not found: $deployerPath"
}

$staging = Join-Path $OutputDirectory ('.staging-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $staging | Out-Null

try {
    $assets = [System.Collections.Generic.List[object]]::new()
    $profilePayloads = [System.Collections.Generic.List[object]]::new()

    $releaseDeployerPath = Join-Path $OutputDirectory 'Deploy-StemScoreGpuPackage.ps1'
    $releaseSelectorPath = Join-Path $OutputDirectory 'Select-StemScoreRuntime.ps1'
    $releaseRuntimeManifestPath = Join-Path $OutputDirectory 'runtime-profiles.json'
    Copy-Item -LiteralPath $deployerPath -Destination $releaseDeployerPath
    Copy-Item -LiteralPath $selectorPath -Destination $releaseSelectorPath
    Copy-Item -LiteralPath $ManifestPath -Destination $releaseRuntimeManifestPath
    $assets.Add((Get-RelativeAssetRecord -File (Get-Item -LiteralPath $releaseDeployerPath) -Kind 'deployment-entrypoint'))
    $assets.Add((Get-RelativeAssetRecord -File (Get-Item -LiteralPath $releaseSelectorPath) -Kind 'runtime-selector'))
    $assets.Add((Get-RelativeAssetRecord -File (Get-Item -LiteralPath $releaseRuntimeManifestPath) -Kind 'runtime-config'))

    if ($PublicSafe) {
        $publicReadmeSource = Join-Path $repositoryRoot 'docs\StemScore-Public-Install.md'
        $noticesSource = Join-Path $repositoryRoot 'THIRD_PARTY_NOTICES.md'
        $setupSource = Join-Path $repositoryRoot 'tools\setup_stemscore_runtime.ps1'
        foreach ($source in @($publicReadmeSource, $noticesSource, $setupSource)) {
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
                throw "Public release document not found: $source"
            }
        }
        $publicReadmePath = Join-Path $OutputDirectory 'PUBLIC_RELEASE_README.md'
        $noticesPath = Join-Path $OutputDirectory 'THIRD_PARTY_NOTICES.md'
        $setupPath = Join-Path $OutputDirectory 'setup_runtime.ps1'
        Copy-Item -LiteralPath $publicReadmeSource -Destination $publicReadmePath
        Copy-Item -LiteralPath $noticesSource -Destination $noticesPath
        Copy-Item -LiteralPath $setupSource -Destination $setupPath
        $assets.Add((Get-RelativeAssetRecord -File (Get-Item -LiteralPath $publicReadmePath) -Kind 'public-install-guide'))
        $assets.Add((Get-RelativeAssetRecord -File (Get-Item -LiteralPath $noticesPath) -Kind 'third-party-notices'))
        $assets.Add((Get-RelativeAssetRecord -File (Get-Item -LiteralPath $setupPath) -Kind 'transcription-runtime-setup'))
    }

    $coreAssetPath = Join-Path $OutputDirectory $coreAssetName
    Compress-Archive -LiteralPath $SourceApplication -DestinationPath $coreAssetPath -CompressionLevel Optimal
    $coreFile = Get-Item -LiteralPath $coreAssetPath
    if ($coreFile.Length -ge $assetLimit) {
        throw "Shared application package reaches the GitHub per-asset limit and must be split: $($coreFile.FullName)"
    }
    $assets.Add((Get-RelativeAssetRecord -File $coreFile -Kind 'shared-core'))

    foreach ($profile in $manifest.runtimeProfiles) {
        $profileStage = Join-Path $staging $profile.id
        New-Item -ItemType Directory -Path $profileStage | Out-Null
        Copy-Item -LiteralPath $ManifestPath -Destination (Join-Path $profileStage 'runtime-profiles.json')
        Copy-Item -LiteralPath $selectorPath -Destination (Join-Path $profileStage 'Select-StemScoreRuntime.ps1')
        Copy-Item -LiteralPath $deployerPath -Destination (Join-Path $profileStage 'Deploy-StemScoreGpuPackage.ps1')

        $runtimeSource = if ([string]::IsNullOrWhiteSpace($RuntimePayloadRoot)) { $null } else { Join-Path $RuntimePayloadRoot $profile.id }
        $runtimeIncluded = $null -ne $runtimeSource -and (Test-Path -LiteralPath $runtimeSource -PathType Container)
        if ($runtimeIncluded) {
            Test-RuntimePayload -Root $runtimeSource
        }

        $profileDocument = [ordered]@{
            schemaVersion = 1
            artifactKind = if ($runtimeIncluded) { 'runtime-profile-with-payload' } else { 'lightweight-profile-metadata' }
            profile = $profile
            sharedCore = [ordered]@{
                archive = $coreAssetName
                entrypoint = $manifest.application.sharedCore.entrypoint
            }
            runtimePayloadIncluded = $runtimeIncluded
            coreModelWeightsIncluded = $runtimeIncluded
            optionalAnalysisModelsIncluded = $false
            launchReady = $runtimeIncluded
            runtimeRepairAvailable = [bool]$PublicSafe
        }
        $profileDocument | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $profileStage 'runtime-profile.json') -Encoding utf8

        $payloadDescription = if ($runtimeIncluded) { 'A matching offline runtime payload is included as release parts.' } elseif ($PublicSafe) { 'This public package contains no prebuilt runtime or model weights. Install the client first; StemScore offers one-click repair from pinned permissively licensed public sources when a runtime is needed.' } else { 'This is a lightweight runtime profile. It does not include a runtime payload.' }
        $readme = @"
StemScore $Version - $($profile.displayName)

$payloadDescription
Core MSST, Demucs, Basic Pitch, and TransKun model weights included: $runtimeIncluded
Optional CLAP and local text-generation analysis models included: False
Required shared application package: $coreAssetName
Expected runtime directory: $($profile.runtimeDirectory)
Run Select-StemScoreRuntime.ps1 to verify the GPU, driver, and selected profile.
Deploy-StemScoreGpuPackage.ps1 verifies all release parts and installs the matching runtime when present.
For a public package, an existing runtime is optional. The deployed app can repair the missing runtime in-app; legacy users may pass -MsstRoot and -TranscriptionRoot, with -UvrRoot optional for compatibility.
See the RTX 40 / RTX 50 deployment document under docs for the full procedure.
"@
        $readme | Set-Content -LiteralPath (Join-Path $profileStage 'README.txt') -Encoding utf8

        $profileAssetName = "StemScore-$Version-win64-$($profile.id)-profile.zip"
        $profileAssetPath = Join-Path $OutputDirectory $profileAssetName
        Compress-Archive -Path (Join-Path $profileStage '*') -DestinationPath $profileAssetPath -CompressionLevel Optimal
        $profileFile = Get-Item -LiteralPath $profileAssetPath
        if ($profileFile.Length -ge $assetLimit) {
            throw "Runtime profile package reaches the GitHub per-asset limit: $($profileFile.FullName)"
        }
        $assets.Add((Get-RelativeAssetRecord -File $profileFile -Kind 'runtime-profile' -ProfileId $profile.id))

        $payloadRecord = [ordered]@{
            profileId = [string]$profile.id
            runtimeIncluded = $runtimeIncluded
            coreModelWeightsIncluded = $runtimeIncluded
            optionalAnalysisModelsIncluded = $false
            launchReady = $runtimeIncluded
            runtimeRepairAvailable = [bool]$PublicSafe
            archiveSizeBytes = 0
            archiveSha256 = $null
            partCount = 0
        }
        if ($runtimeIncluded) {
            $runtimeArchiveName = "StemScore-$Version-win64-$($profile.id)-runtime.zip"
            $runtimeArchive = Join-Path $staging $runtimeArchiveName
            # Compress-Archive is extremely slow and memory-hungry on the 60k+ file Python runtime.
            # ZipFile streams the directory and still produces a normal ZIP that Expand-Archive can read.
            [System.IO.Compression.ZipFile]::CreateFromDirectory(
                $runtimeSource,
                $runtimeArchive,
                [System.IO.Compression.CompressionLevel]::Fastest,
                $false
            )
            $runtimeFile = Get-Item -LiteralPath $runtimeArchive
            $runtimeHash = Get-Sha256Hex -Path $runtimeArchive
            $parts = @(Split-ReleaseFile -Path $runtimeArchive -OutputDirectory $OutputDirectory -BaseName $runtimeArchiveName -PartSize ([int64]$manifest.release.recommendedPartSizeBytes))
            $partCount = $parts.Count
            for ($partIndex = 0; $partIndex -lt $partCount; $partIndex++) {
                $asset = Get-RelativeAssetRecord -File $parts[$partIndex] -Kind 'runtime-payload-part' -ProfileId $profile.id
                $asset | Add-Member -NotePropertyName partIndex -NotePropertyValue ($partIndex + 1)
                $asset | Add-Member -NotePropertyName partCount -NotePropertyValue $partCount
                $assets.Add($asset)
            }
            $payloadRecord.archiveSizeBytes = $runtimeFile.Length
            $payloadRecord.archiveSha256 = $runtimeHash
            $payloadRecord.partCount = $partCount
        }
        $profilePayloads.Add([pscustomobject]$payloadRecord)
    }

    $releaseManifestPath = Join-Path $OutputDirectory 'release-manifest.json'
    $releaseDocument = [ordered]@{
        schemaVersion = 1
        application = $manifest.application.id
        version = $Version
        createdUtc = [DateTime]::UtcNow.ToString('o')
        publicSafe = [bool]$PublicSafe
        runtimeRepairAvailable = [bool]$PublicSafe
        runtimePayloadsIncluded = [bool]($profilePayloads | Where-Object runtimeIncluded)
        coreModelWeightsIncluded = [bool]($profilePayloads | Where-Object coreModelWeightsIncluded)
        optionalAnalysisModelsIncluded = $false
        deploymentEntrypoint = 'Deploy-StemScoreGpuPackage.ps1'
        payloads = [ordered]@{
            sharedCoreIncluded = $true
            runtimeIncluded = [bool]($profilePayloads | Where-Object runtimeIncluded)
            coreModelWeightsIncluded = [bool]($profilePayloads | Where-Object coreModelWeightsIncluded)
            optionalAnalysisModelsIncluded = $false
            launchReady = [bool]($profilePayloads | Where-Object launchReady)
            runtimeRepairAvailable = [bool]$PublicSafe
        }
        profiles = $profilePayloads
        githubAssetLimitBytes = $assetLimit
        recommendedPartSizeBytes = [int64]$manifest.release.recommendedPartSizeBytes
        assets = $assets
    }
    $releaseDocument | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $releaseManifestPath -Encoding utf8

    $checksumTargets = @(
        Get-ChildItem -LiteralPath $OutputDirectory -File |
            Where-Object { $_.Name -ne 'SHA256SUMS.txt' } |
            Sort-Object Name
    )
    $checksumLines = foreach ($file in $checksumTargets) {
        if ($file.Length -ge $assetLimit) {
            throw "Every GitHub Release asset must be strictly smaller than 2 GiB: $($file.FullName)"
        }
        $hash = Get-Sha256Hex -Path $file.FullName
        "$hash  $($file.Name)"
    }
    $checksumLines | Set-Content -LiteralPath (Join-Path $OutputDirectory 'SHA256SUMS.txt') -Encoding ascii

    Get-ChildItem -LiteralPath $OutputDirectory -File | Sort-Object Name | Select-Object Name, Length
} finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
}
