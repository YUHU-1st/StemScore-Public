[CmdletBinding()]
param(
    [string]$ReleaseDirectory,
    [string]$Destination,
    [string]$MsstRoot,
    [string]$UvrRoot,
    [string]$TranscriptionRoot,
    [switch]$PlanOnly
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

if ([string]::IsNullOrWhiteSpace($ReleaseDirectory)) {
    $ReleaseDirectory = $PSScriptRoot
}
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path $env:LOCALAPPDATA 'StemScore'
}

$releaseRoot = [System.IO.Path]::GetFullPath($ReleaseDirectory)
$destinationRoot = [System.IO.Path]::GetFullPath($Destination)
$releaseManifestPath = Join-Path $releaseRoot 'release-manifest.json'
$runtimeManifestPath = Join-Path $releaseRoot 'runtime-profiles.json'
$selectorPath = Join-Path $releaseRoot 'Select-StemScoreRuntime.ps1'

foreach ($requiredFile in @($releaseManifestPath, $runtimeManifestPath, $selectorPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required deployment file not found: $requiredFile"
    }
}

$releaseManifest = Get-Content -LiteralPath $releaseManifestPath -Raw | ConvertFrom-Json

function Assert-ReleaseAsset {
    param([Parameter(Mandatory)]$Asset)

    $name = [string]$Asset.name
    if ([System.IO.Path]::GetFileName($name) -ne $name) {
        throw "Release manifest asset name must not contain a path: $name"
    }
    $path = Join-Path $releaseRoot $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Release asset not found: $path"
    }
    $file = Get-Item -LiteralPath $path
    if ($file.Length -ne [int64]$Asset.sizeBytes) {
        throw "Release asset size mismatch: $name"
    }
    $actualHash = Get-Sha256Hex -Path $path
    if ($actualHash -ne ([string]$Asset.sha256).ToLowerInvariant()) {
        throw "Release asset SHA-256 mismatch: $name"
    }
    return $file
}

function Join-ReleaseParts {
    param(
        [Parameter(Mandatory)][object[]]$Parts,
        [Parameter(Mandatory)][string]$Destination
    )

    $target = [System.IO.File]::Create($Destination)
    try {
        $buffer = New-Object byte[] (8MB)
        foreach ($part in $Parts) {
            $source = [System.IO.File]::OpenRead($part.FullName)
            try {
                while (($read = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $target.Write($buffer, 0, $read)
                }
            } finally {
                $source.Dispose()
            }
        }
    } finally {
        $target.Dispose()
    }
}

function Resolve-ExternalRuntime {
    $provided = @($MsstRoot, $UvrRoot, $TranscriptionRoot) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    if ($provided.Count -eq 0) {
        return $null
    }
    if ($provided.Count -ne 3) {
        throw 'MsstRoot, UvrRoot, and TranscriptionRoot must be provided together.'
    }

    $roots = [ordered]@{
        msst = [System.IO.Path]::GetFullPath($MsstRoot)
        uvr = [System.IO.Path]::GetFullPath($UvrRoot)
        transcription = [System.IO.Path]::GetFullPath($TranscriptionRoot)
    }
    $required = @(
        (Join-Path $roots.msst 'env\python.exe'),
        (Join-Path $roots.msst 'inference.py'),
        (Join-Path $roots.msst 'configs\BS-Roformer-Resurrection-Config.yaml'),
        (Join-Path $roots.msst 'configs\config_karaoke_frazer_becruily.yaml'),
        (Join-Path $roots.msst 'configs\BS-Rofo-SW-Fixed.yaml'),
        (Join-Path $roots.msst 'configs\config_dereverb_echo_mbr_v2.yaml'),
        (Join-Path $roots.msst 'pretrain\BS-Roformer-Resurrection.ckpt'),
        (Join-Path $roots.msst 'pretrain\bs_roformer_karaoke_frazer_becruily.ckpt'),
        (Join-Path $roots.msst 'pretrain\BS-Rofo-SW-Fixed.ckpt'),
        (Join-Path $roots.msst 'pretrain\dereverb_echo_mbr_fused_0.5_v2_0.25_big_0.25_super.ckpt'),
        (Join-Path $roots.uvr 'models\Demucs_Models\v3_v4_repo'),
        (Join-Path $roots.transcription '.venv\Scripts\python.exe'),
        (Join-Path $roots.transcription '.venv\Lib\site-packages\basic_pitch\saved_models\icassp_2022\nmp.onnx'),
        (Join-Path (Split-Path -Parent $roots.transcription) 'transkun-packages\transkun\pretrained\2.0.pt'),
        (Join-Path (Split-Path -Parent $roots.transcription) 'transkun-packages\transkun\pretrained\2.0.conf')
    )
    foreach ($path in $required) {
        if (-not (Test-Path -LiteralPath $path)) {
            throw "External runtime is missing required path: $path"
        }
    }
    return $roots
}

$runtimeConfigAsset = $releaseManifest.assets | Where-Object { $_.kind -eq 'runtime-config' } | Select-Object -First 1
$selectorAsset = $releaseManifest.assets | Where-Object { $_.kind -eq 'runtime-selector' } | Select-Object -First 1
if ($null -eq $runtimeConfigAsset -or $null -eq $selectorAsset) {
    throw 'Release manifest does not contain the runtime config and selector assets.'
}
Assert-ReleaseAsset $runtimeConfigAsset | Out-Null
Assert-ReleaseAsset $selectorAsset | Out-Null

$selection = & $selectorPath -ManifestPath $runtimeManifestPath
$coreAsset = $releaseManifest.assets | Where-Object { $_.kind -eq 'shared-core' } | Select-Object -First 1
$profileAsset = $releaseManifest.assets |
    Where-Object { $_.kind -eq 'runtime-profile' -and $_.profileId -eq $selection.ProfileId } |
    Select-Object -First 1
if ($null -eq $coreAsset -or $null -eq $profileAsset) {
    throw "Release manifest is missing the shared core or profile asset for $($selection.ProfileId)."
}

$profilePayload = $releaseManifest.profiles |
    Where-Object { $_.profileId -eq $selection.ProfileId } |
    Select-Object -First 1
if ($null -eq $profilePayload) {
    throw "Release manifest is missing profile payload metadata for $($selection.ProfileId)."
}

$coreFile = Assert-ReleaseAsset $coreAsset
$profileFile = Assert-ReleaseAsset $profileAsset
$runtimePartAssets = @(
    $releaseManifest.assets |
        Where-Object { $_.kind -eq 'runtime-payload-part' -and $_.profileId -eq $selection.ProfileId } |
        Sort-Object { [int]$_.partIndex }
)
$runtimePartFiles = @($runtimePartAssets | ForEach-Object { Assert-ReleaseAsset $_ })
$runtimeIncluded = [bool]$profilePayload.runtimeIncluded
$externalRuntime = Resolve-ExternalRuntime
$externalRuntimeReady = $null -ne $externalRuntime
if ($runtimeIncluded -and $runtimePartFiles.Count -ne [int]$profilePayload.partCount) {
    throw "Runtime payload part count mismatch for $($selection.ProfileId)."
}
if (-not $runtimeIncluded -and $runtimePartFiles.Count -gt 0) {
    throw "Runtime payload parts exist for a profile marked runtimeIncluded=false: $($selection.ProfileId)."
}
$plan = [pscustomobject]@{
    Destination = $destinationRoot
    SelectedProfile = $selection.ProfileId
    Gpu = $selection.GpuName
    DriverVersion = $selection.DriverVersion
    SharedCoreAsset = $coreFile.Name
    RuntimeProfileAsset = $profileFile.Name
    RuntimePayloadIncluded = $runtimeIncluded
    CoreModelWeightsIncluded = [bool]$profilePayload.coreModelWeightsIncluded
    OptionalAnalysisModelsIncluded = [bool]$profilePayload.optionalAnalysisModelsIncluded
    LaunchReady = [bool]($profilePayload.launchReady -or $externalRuntimeReady)
    ExpectedRuntimeDirectory = Join-Path $destinationRoot $selection.RuntimeDirectory
    ModelsDirectory = Join-Path $destinationRoot 'models'
    ExternalRuntimeRoots = $externalRuntime
}

if ($PlanOnly) {
    $plan
    return
}

if (Test-Path -LiteralPath $destinationRoot) {
    throw "Destination already exists; choose a new empty path: $destinationRoot"
}

$destinationParent = Split-Path -Parent $destinationRoot
if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
    New-Item -ItemType Directory -Path $destinationParent | Out-Null
}
$staging = Join-Path $destinationParent ('.stemscore-deploy-' + [guid]::NewGuid().ToString('N'))
$payloadRoot = Join-Path $staging 'StemScore'

New-Item -ItemType Directory -Path $payloadRoot | Out-Null
try {
    $expandedCore = Join-Path $staging 'expanded-core'
    Expand-Archive -LiteralPath $coreFile.FullName -DestinationPath $expandedCore
    $expectedCoreRoot = Join-Path $expandedCore 'StemScore'
    if (-not (Test-Path -LiteralPath (Join-Path $expectedCoreRoot 'StemScore.exe') -PathType Leaf)) {
        throw 'The shared core archive does not contain StemScore/StemScore.exe.'
    }
    Move-Item -LiteralPath $expectedCoreRoot -Destination (Join-Path $payloadRoot 'app')

    $deploymentDirectory = Join-Path $payloadRoot 'deployment'
    Expand-Archive -LiteralPath $profileFile.FullName -DestinationPath $deploymentDirectory
    Copy-Item -LiteralPath $runtimeManifestPath -Destination (Join-Path $payloadRoot 'runtime-profiles.json')
    Copy-Item -LiteralPath $selectorPath -Destination (Join-Path $payloadRoot 'Select-StemScoreRuntime.ps1')
    $runtimeDirectory = Join-Path $payloadRoot $selection.RuntimeDirectory
    [void][System.IO.Directory]::CreateDirectory($runtimeDirectory)
    if ($runtimeIncluded) {
        $runtimeArchive = Join-Path $staging ("$($selection.ProfileId)-runtime.zip")
        Join-ReleaseParts -Parts $runtimePartFiles -Destination $runtimeArchive
        $runtimeArchiveFile = Get-Item -LiteralPath $runtimeArchive
        if ($runtimeArchiveFile.Length -ne [int64]$profilePayload.archiveSizeBytes) {
            throw "Reassembled runtime archive size mismatch for $($selection.ProfileId)."
        }
        $runtimeArchiveHash = Get-Sha256Hex -Path $runtimeArchive
        if ($runtimeArchiveHash -ne ([string]$profilePayload.archiveSha256).ToLowerInvariant()) {
            throw "Reassembled runtime archive SHA-256 mismatch for $($selection.ProfileId)."
        }
        Expand-Archive -LiteralPath $runtimeArchive -DestinationPath $runtimeDirectory
    }
    $modelsDirectory = Join-Path $payloadRoot 'models'
    [void][System.IO.Directory]::CreateDirectory($modelsDirectory)
    $writeProbe = Join-Path $modelsDirectory ('.write-test-' + [guid]::NewGuid().ToString('N'))
    [System.IO.File]::WriteAllText($writeProbe, 'write-test')
    [System.IO.File]::Delete($writeProbe)

    $state = [ordered]@{
        schemaVersion = 1
        selectedProfile = $selection.ProfileId
        gpu = $selection.GpuName
        computeCapability = $selection.ComputeCapability
        driverVersion = $selection.DriverVersion
        sharedCoreInstalled = $true
        runtimeDirectoryCreated = $true
        runtimePayloadIncluded = $runtimeIncluded
        coreModelWeightsIncluded = [bool]$profilePayload.coreModelWeightsIncluded
        optionalAnalysisModelsIncluded = [bool]$profilePayload.optionalAnalysisModelsIncluded
        modelsDirectoryWritable = $true
        launchReady = [bool]($profilePayload.launchReady -or $externalRuntimeReady)
        expectedRuntimeDirectory = $selection.RuntimeDirectory
        externalRuntimeRoots = $externalRuntime
    }
    $state | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $payloadRoot 'deployment-state.json') -Encoding utf8
    Move-Item -LiteralPath $payloadRoot -Destination $destinationRoot
    Get-Content -LiteralPath (Join-Path $destinationRoot 'deployment-state.json') -Raw | ConvertFrom-Json
} finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
}
