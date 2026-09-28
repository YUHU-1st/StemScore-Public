[CmdletBinding()]
param(
    [string]$ProfileId = 'rtx50-cu128',
    [string]$MsstRoot = (Join-Path $env:USERPROFILE 'StemScoreRuntime\MSST'),
    [string]$MsstEnvironmentRoot,
    [string]$UvrRoot = (Join-Path $env:USERPROFILE 'StemScoreRuntime\UVR'),
    [string]$TranscriptionRoot = (Join-Path $env:USERPROFILE 'StemScoreRuntime\transcription\basic-pitch'),
    [string]$OutputRoot,
    [string]$ManifestPath,
    [switch]$PlanOnly,
    [switch]$Force
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
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $repositoryRoot 'runtime-payloads'
}
if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $ManifestPath = Join-Path $PSScriptRoot 'runtime-profiles.json'
}
if ([string]::IsNullOrWhiteSpace($MsstEnvironmentRoot)) {
    $MsstEnvironmentRoot = Join-Path $MsstRoot 'env'
}
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Runtime manifest not found: $ManifestPath"
}
$profiles = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$runtimeProfile = $profiles.runtimeProfiles | Where-Object { $_.id -eq $ProfileId } | Select-Object -First 1
if ($null -eq $runtimeProfile) {
    throw "Runtime profile not found: $ProfileId"
}
$destination = Join-Path ([System.IO.Path]::GetFullPath($OutputRoot)) $ProfileId

$preparationPlan = [pscustomobject]@{
    ProfileId = $ProfileId
    Destination = $destination
    SourceMsstRoot = [System.IO.Path]::GetFullPath($MsstRoot)
    SourceMsstEnvironmentRoot = [System.IO.Path]::GetFullPath($MsstEnvironmentRoot)
    Torch = [string]$runtimeProfile.torch.version
    Torchvision = [string]$runtimeProfile.torch.torchvisionVersion
    Torchaudio = [string]$runtimeProfile.torch.torchaudioVersion
    Python = [string]$runtimeProfile.torch.pythonVersion
    Cuda = [string]$runtimeProfile.torch.cuda
    RequiredWheelArchitecture = [string]$runtimeProfile.torch.requiredWheelArchitecture
    WheelIndexUrl = [string]$runtimeProfile.torch.indexUrl
    PreparedEnvironmentMustMatchProfile = $true
    CoreModelWeightsPlanned = $true
    OptionalAnalysisModelsPlanned = $false
    TargetHardwareInferencePerformed = $false
}
if ($PlanOnly) {
    $preparationPlan
    return
}

if (Test-Path -LiteralPath $destination) {
    if (-not $Force) {
        throw "Runtime payload destination already exists: $destination"
    }
    Remove-Item -LiteralPath $destination -Recurse -Force
}
New-Item -ItemType Directory -Path $destination | Out-Null

function Copy-Tree {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Required source directory not found: $Source"
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    & robocopy $Source $Destination /E /COPY:DAT /DCOPY:DAT /R:2 /W:1 /NFL /NDL /NP /NJH /NJS | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "robocopy failed with exit code $LASTEXITCODE while copying $Source"
    }
}

function Copy-RequiredFile {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required source file not found: $Source"
    }
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

$msstDestination = Join-Path $destination 'msst'
$uvrDestination = Join-Path $destination 'uvr'
$transcriptionDestination = Join-Path $destination 'transcription'

Copy-Tree -Source $MsstEnvironmentRoot -Destination (Join-Path $msstDestination 'env')
Copy-RequiredFile -Source (Join-Path $MsstRoot 'inference.py') -Destination (Join-Path $msstDestination 'inference.py')
Copy-Tree -Source (Join-Path $MsstRoot 'utils') -Destination (Join-Path $msstDestination 'utils')
Copy-Tree -Source (Join-Path $MsstRoot 'models') -Destination (Join-Path $msstDestination 'models')

$msstFiles = @(
    'configs\BS-Roformer-Resurrection-Config.yaml',
    'configs\config_karaoke_frazer_becruily.yaml',
    'configs\BS-Rofo-SW-Fixed.yaml',
    'configs\config_dereverb_echo_mbr_v2.yaml',
    'pretrain\BS-Roformer-Resurrection.ckpt',
    'pretrain\bs_roformer_karaoke_frazer_becruily.ckpt',
    'pretrain\BS-Rofo-SW-Fixed.ckpt',
    'pretrain\dereverb_echo_mbr_fused_0.5_v2_0.25_big_0.25_super.ckpt'
)
foreach ($relative in $msstFiles) {
    Copy-RequiredFile -Source (Join-Path $MsstRoot $relative) -Destination (Join-Path $msstDestination $relative)
}

$uvrRepositorySource = Join-Path $UvrRoot 'models\Demucs_Models\v3_v4_repo'
$uvrRepositoryDestination = Join-Path $uvrDestination 'models\Demucs_Models\v3_v4_repo'
Copy-RequiredFile -Source (Join-Path $uvrRepositorySource 'htdemucs_6s.yaml') -Destination (Join-Path $uvrRepositoryDestination 'htdemucs_6s.yaml')
Copy-RequiredFile -Source (Join-Path $uvrRepositorySource '5c90dfd2-34c22ccb.th') -Destination (Join-Path $uvrRepositoryDestination '5c90dfd2-34c22ccb.th')
Copy-Tree -Source $TranscriptionRoot -Destination (Join-Path $transcriptionDestination 'basic-pitch')
$transkunSource = Join-Path (Split-Path -Parent $TranscriptionRoot) 'transkun-packages'
Copy-Tree -Source $transkunSource -Destination (Join-Path $transcriptionDestination 'transkun-packages')

$sourceVenvConfig = Join-Path $TranscriptionRoot '.venv\pyvenv.cfg'
if (-not (Test-Path -LiteralPath $sourceVenvConfig -PathType Leaf)) {
    throw "Basic Pitch pyvenv.cfg not found: $sourceVenvConfig"
}
$sourceVenvText = Get-Content -LiteralPath $sourceVenvConfig -Raw
$homeMatch = [regex]::Match($sourceVenvText, '(?m)^home\s*=\s*(.+?)\s*$')
if (-not $homeMatch.Success) {
    throw 'Basic Pitch pyvenv.cfg does not declare its base Python home.'
}
$pythonBaseSource = $homeMatch.Groups[1].Value.Trim()
$pythonBaseDestination = Join-Path $transcriptionDestination 'python-base'
Copy-Tree -Source $pythonBaseSource -Destination $pythonBaseDestination

$python = Join-Path $msstDestination 'env\python.exe'
$basicPitchPython = Join-Path $pythonBaseDestination 'python.exe'
$basicPitchPackages = Join-Path $transcriptionDestination 'basic-pitch\.venv\Lib\site-packages'
$transkunPackages = Join-Path $transcriptionDestination 'transkun-packages'

function Get-BasePackageVersion {
    param([Parameter(Mandatory)][string]$Value)
    return ($Value -split '\+', 2)[0]
}

$currentTorchJson = & $python -c "import json, torch, importlib.metadata as m; print(json.dumps({'torch':torch.__version__,'torchvision':m.version('torchvision'),'torchaudio':m.version('torchaudio'),'cuda':torch.version.cuda}))"
if ($LASTEXITCODE -ne 0) { throw 'Source MSST Python runtime does not contain the expected PyTorch components.' }
$currentTorch = $currentTorchJson | ConvertFrom-Json
$torchStackMismatch =
    (Get-BasePackageVersion ([string]$currentTorch.torch)) -ne [string]$runtimeProfile.torch.version -or
    (Get-BasePackageVersion ([string]$currentTorch.torchvision)) -ne [string]$runtimeProfile.torch.torchvisionVersion -or
    (Get-BasePackageVersion ([string]$currentTorch.torchaudio)) -ne [string]$runtimeProfile.torch.torchaudioVersion -or
    [string]$currentTorch.cuda -ne [string]$runtimeProfile.torch.cuda

if ($torchStackMismatch) {
    throw "MSST environment does not match profile $ProfileId. Supply a prepared environment with -MsstEnvironmentRoot."
}

$runtimeInfoJson = & $python -c "import json, torch, torchvision, torchaudio, importlib.util as u, sys; mods=['demucs','librosa','transformers','soundfile']; device={'name':None,'capability':None,'error':None}; available=torch.cuda.is_available();
if available:
 try: device={'name':torch.cuda.get_device_name(0),'capability':'.'.join(map(str,torch.cuda.get_device_capability(0))),'error':None}
 except Exception as exc: device['error']=str(exc)
print(json.dumps({'python':sys.version,'python_version':f'{sys.version_info.major}.{sys.version_info.minor}','torch':torch.__version__,'torchvision':torchvision.__version__,'torchaudio':torchaudio.__version__,'cuda':torch.version.cuda,'cuda_available':available,'arch_list':torch.cuda.get_arch_list(),'device':device,'imports':{m:bool(u.find_spec(m)) for m in mods}}))"
if ($LASTEXITCODE -ne 0) { throw 'Copied MSST Python runtime failed validation.' }
$runtimeInfo = $runtimeInfoJson | ConvertFrom-Json
if (-not $runtimeInfo.imports.demucs -or -not $runtimeInfo.imports.librosa -or -not $runtimeInfo.imports.transformers) {
    throw 'Copied MSST Python runtime is missing a required StemScore package.'
}
if ((Get-BasePackageVersion ([string]$runtimeInfo.torch)) -ne [string]$runtimeProfile.torch.version) {
    throw "Unexpected torch version in copied runtime: $($runtimeInfo.torch)"
}
if ((Get-BasePackageVersion ([string]$runtimeInfo.torchvision)) -ne [string]$runtimeProfile.torch.torchvisionVersion) {
    throw "Unexpected torchvision version in copied runtime: $($runtimeInfo.torchvision)"
}
if ((Get-BasePackageVersion ([string]$runtimeInfo.torchaudio)) -ne [string]$runtimeProfile.torch.torchaudioVersion) {
    throw "Unexpected torchaudio version in copied runtime: $($runtimeInfo.torchaudio)"
}
if ([string]$runtimeInfo.cuda -ne [string]$runtimeProfile.torch.cuda) {
    throw "Unexpected CUDA runtime in copied runtime: $($runtimeInfo.cuda)"
}
if ([string]$runtimeInfo.python_version -ne [string]$runtimeProfile.torch.pythonVersion) {
    throw "Unexpected Python version in copied runtime: $($runtimeInfo.python_version)"
}
$requiredArchitecture = 'sm_' + ([string]$runtimeProfile.torch.requiredWheelArchitecture).Replace('.', '')
if (@($runtimeInfo.arch_list) -notcontains $requiredArchitecture) {
    throw "Copied runtime does not contain the required CUDA wheel architecture: $requiredArchitecture"
}

& $python (Join-Path $msstDestination 'inference.py') --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Copied MSST inference entrypoint failed validation.' }

$oldPythonPathForBasicPitch = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $basicPitchPackages
    & $basicPitchPython -m basic_pitch.predict --help | Out-Null
} finally {
    $env:PYTHONPATH = $oldPythonPathForBasicPitch
}
if ($LASTEXITCODE -ne 0) { throw 'Copied Basic Pitch runtime failed validation.' }
$oldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $transkunPackages
    & $python -m transkun.transcribe -h | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Copied TransKun runtime failed validation.' }
} finally {
    $env:PYTHONPATH = $oldPythonPath
}

$checkpointRecords = foreach ($relative in $msstFiles | Where-Object { $_ -like 'pretrain\*' }) {
    $file = Get-Item -LiteralPath (Join-Path $msstDestination $relative)
    [ordered]@{
        path = $relative.Replace('\', '/')
        sizeBytes = $file.Length
        sha256 = Get-Sha256Hex -Path $file.FullName
    }
}

$manifest = [ordered]@{
    schemaVersion = 1
    profileId = $ProfileId
    createdUtc = [DateTime]::UtcNow.ToString('o')
    target = [ordered]@{
        computeCapabilities = @($runtimeProfile.computeCapabilities)
        torch = [string]$runtimeProfile.torch.version
        torchvision = [string]$runtimeProfile.torch.torchvisionVersion
        torchaudio = [string]$runtimeProfile.torch.torchaudioVersion
        cuda = [string]$runtimeProfile.torch.cuda
        requiredWheelArchitecture = [string]$runtimeProfile.torch.requiredWheelArchitecture
        wheelIndexUrl = [string]$runtimeProfile.torch.indexUrl
    }
    runtime = $runtimeInfo
    checkpoints = $checkpointRecords
    payloadContents = [ordered]@{
        coreModelWeightsIncluded = $true
        optionalAnalysisModelsIncluded = $false
    }
    validation = [ordered]@{
        scope = 'Copied runtime imports, pinned package versions, CUDA wheel version, architecture list, Basic Pitch, and TransKun.'
        targetHardwareInferencePerformed = $false
        note = 'Preparing a payload does not constitute RTX 40 or RTX 50 hardware inference validation.'
    }
    localOnly = $true
    redistributionNote = 'Contains locally sourced model weights. Review every upstream model license before public redistribution.'
}
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $destination 'runtime-manifest.json') -Encoding utf8

$files = Get-ChildItem -LiteralPath $destination -Recurse -File
$size = ($files | Measure-Object Length -Sum).Sum
[pscustomobject]@{
    ProfileId = $ProfileId
    Destination = $destination
    FileCount = $files.Count
    SizeGiB = [math]::Round($size / 1GB, 3)
    Torch = $runtimeInfo.torch
    Torchvision = $runtimeInfo.torchvision
    Torchaudio = $runtimeInfo.torchaudio
    Cuda = $runtimeInfo.cuda
    CudaAvailable = $runtimeInfo.cuda_available
    TargetHardwareInferencePerformed = $false
}
