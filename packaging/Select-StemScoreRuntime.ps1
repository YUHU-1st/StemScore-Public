[CmdletBinding()]
param(
    [string]$ManifestPath,
    [switch]$AsJson
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $ManifestPath = Join-Path $PSScriptRoot 'runtime-profiles.json'
}

function ConvertTo-DriverVersion {
    param([Parameter(Mandatory)][string]$Value)

    $match = [regex]::Match($Value, '^\s*(\d+(?:\.\d+){1,3})')
    if (-not $match.Success) {
        throw "Cannot parse NVIDIA driver version: $Value"
    }
    return [version]$match.Groups[1].Value
}

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Runtime manifest not found: $ManifestPath"
}

$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($null -eq $nvidiaSmi) {
    throw 'nvidia-smi was not found. Install an NVIDIA Windows driver first.'
}

$query = & $nvidiaSmi.Source '--query-gpu=index,name,compute_cap,driver_version' '--format=csv,noheader,nounits' 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "nvidia-smi query failed: $($query -join [Environment]::NewLine)"
}

$gpus = foreach ($line in @($query)) {
    $fields = ([string]$line).Trim() -split '\s*,\s*', 4
    if ($fields.Count -ne 4) {
        throw "Cannot parse nvidia-smi output: $line"
    }
    [pscustomobject]@{
        Index = [int]$fields[0]
        Name = $fields[1]
        ComputeCapability = $fields[2]
        DriverVersion = $fields[3]
    }
}

$selection = $null
foreach ($profile in @($manifest.runtimeProfiles | Sort-Object -Property @{ Expression = { [int]$_.priority }; Descending = $true })) {
    $gpu = $gpus | Where-Object { @($profile.computeCapabilities) -contains $_.ComputeCapability } | Select-Object -First 1
    if ($null -ne $gpu) {
        $selection = [pscustomobject]@{
            ProfileId = [string]$profile.id
            ProfileName = [string]$profile.displayName
            RuntimeDirectory = [string]$profile.runtimeDirectory
            TorchVersion = [string]$profile.torch.version
            CudaVersion = [string]$profile.torch.cuda
            WheelIndexUrl = [string]$profile.torch.indexUrl
            DriverPolicy = [string]$profile.driver.policy
            MinimumDriver = [string]$profile.driver.minimum
            GpuIndex = $gpu.Index
            GpuName = $gpu.Name
            ComputeCapability = $gpu.ComputeCapability
            DriverVersion = $gpu.DriverVersion
        }
        break
    }
}

if ($null -eq $selection) {
    $detected = ($gpus | ForEach-Object { "$($_.Name) (CC $($_.ComputeCapability))" }) -join '; '
    throw "No supported RTX 40/50 GPU was found. Detected: $detected"
}

$installedDriver = ConvertTo-DriverVersion $selection.DriverVersion
$minimumDriver = ConvertTo-DriverVersion $selection.MinimumDriver
if ($installedDriver -lt $minimumDriver) {
    $message = "GPU $($selection.GpuName) uses driver $($selection.DriverVersion); profile $($selection.ProfileId) requires driver $($selection.MinimumDriver) or newer."
    if ($selection.DriverPolicy -eq 'required') {
        throw $message
    }
    Write-Warning $message
}

if ($AsJson) {
    $selection | ConvertTo-Json -Depth 4
} else {
    $selection
}
