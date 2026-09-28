param(
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'StemScore\transcription\basic-pitch'),
    [Parameter(Mandatory)]
    [string]$MsstRoot
)

$ErrorActionPreference = 'Stop'

$uv = (Get-Command uv -ErrorAction Stop).Source
$environment = Join-Path $RuntimeRoot '.venv'
$python = Join-Path $environment 'Scripts\python.exe'
$basicPitch = Join-Path $environment 'Scripts\basic-pitch.exe'
$basicPitchModel = Join-Path $environment 'Lib\site-packages\basic_pitch\saved_models\icassp_2022\nmp.onnx'
$transkunPackages = Join-Path (Split-Path -Parent $RuntimeRoot) 'transkun-packages'
$msstPython = Join-Path $MsstRoot 'env\python.exe'
if (-not (Test-Path -LiteralPath $msstPython -PathType Leaf)) {
    throw "MSST Python not found: $msstPython"
}

New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $python)) {
    & $uv venv --python 3.11 $environment
}

& $uv pip install --python $python 'basic-pitch==0.4.0' 'onnxruntime==1.23.2' 'setuptools==80.9.0'
& $python -c "from basic_pitch.inference import predict; print('Basic Pitch import OK')"
if ($LASTEXITCODE -ne 0) {
    throw 'Basic Pitch import validation failed.'
}
& $basicPitch --help
if ($LASTEXITCODE -ne 0) {
    throw 'Basic Pitch CLI validation failed.'
}
if (-not (Test-Path -LiteralPath $basicPitchModel)) {
    throw 'Basic Pitch ONNX model validation failed.'
}

New-Item -ItemType Directory -Path $transkunPackages -Force | Out-Null
& $uv pip install --python $msstPython --target $transkunPackages --link-mode copy --no-deps `
    'transkun==2.0.1' 'moduleconf==0.1.4' 'pretty-midi==0.2.11.post0' 'pydub==0.25.1' `
    'soxr==1.0.0' 'sox==1.5.0' 'mido==1.3.3' 'importlib-resources==6.5.2'
$env:PYTHONPATH = $transkunPackages
& $msstPython -m transkun.transcribe -h | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'TransKun V2 import validation failed.'
}

$lock = & $uv pip freeze --python $python
$lockPath = Join-Path $RuntimeRoot 'requirements.lock.txt'
$lock | Set-Content -LiteralPath $lockPath -Encoding utf8
Get-FileHash -Algorithm SHA256 $lockPath
Get-FileHash -Algorithm SHA256 $basicPitchModel
Get-FileHash -Algorithm SHA256 (Join-Path $transkunPackages 'transkun\pretrained\2.0.pt')

Write-Host 'Basic Pitch and TransKun are ready.'
Write-Host 'No MSST or UVR separation checkpoint was downloaded. Supply models you are licensed to use, then bind the three runtime roots with Deploy-StemScoreGpuPackage.ps1.'

