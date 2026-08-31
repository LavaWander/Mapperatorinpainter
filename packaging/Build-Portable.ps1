param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$outputRoot = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    [System.IO.Path]::GetFullPath($OutputDirectory)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
}
$pythonVersion = "3.10.11"
$torchVersion = "2.10.0"
$torchIndex = "https://download.pytorch.org/whl/cu130"
$pythonUrl = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip"
$getPipUrl = "https://bootstrap.pypa.io/get-pip.py"
$ffmpegUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
$requirementsSource = Join-Path $projectRoot "portable\portable-requirements.txt"
$temporaryParent = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$temporaryRoot = Join-Path $temporaryParent ("mapperatorinpainter-portable-build-" + [guid]::NewGuid().ToString("N"))
$releaseRoot = Join-Path $temporaryRoot "Mapperatorinpainter"
$runtimeRoot = Join-Path $releaseRoot "runtime"

function Copy-ReleaseSources {
    New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
    foreach ($name in @(
        "Mapperatorinator",
        "portable",
        "Run Mapperatorinpainter.bat",
        "Install Danser Preview.bat",
        "INPAINTING_BACKLOG.md",
        "CODEBASE_ORIENTATION.md"
    )) {
        $source = Join-Path $projectRoot $name
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination $releaseRoot -Recurse -Force
        }
    }
    Get-ChildItem -LiteralPath $projectRoot -Filter "M*_PROOF.md" -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $releaseRoot $_.Name) -Force
    }
    Get-ChildItem -LiteralPath $releaseRoot -Directory -Recurse -Force |
        Where-Object { $_.Name -in @("__pycache__", ".pytest_cache") } |
        Sort-Object FullName -Descending |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Join-Path $releaseRoot "inpaint_output") | Out-Null
}

function Install-EmbeddedPython {
    $pythonArchive = Join-Path $temporaryRoot "python.zip"
    $getPip = Join-Path $temporaryRoot "get-pip.py"
    Write-Host "Downloading Python $pythonVersion embedded runtime..."
    Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonArchive -TimeoutSec 120
    New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
    Expand-Archive -LiteralPath $pythonArchive -DestinationPath $runtimeRoot -Force

    $pthFile = Get-ChildItem -LiteralPath $runtimeRoot -Filter "python*._pth" -File | Select-Object -First 1
    if ($null -eq $pthFile) { throw "The embedded Python archive has no ._pth file." }
    $pthLines = Get-Content -LiteralPath $pthFile.FullName
    $pthLines = $pthLines | ForEach-Object { if ($_ -eq "#import site") { "import site" } else { $_ } }
    if ($pthLines -notcontains "Lib\site-packages") { $pthLines += "Lib\site-packages" }
    Set-Content -LiteralPath $pthFile.FullName -Value $pthLines -Encoding ASCII

    Write-Host "Bootstrapping pip and portable Python libraries..."
    Invoke-WebRequest -Uri $getPipUrl -OutFile $getPip -TimeoutSec 120
    $python = Join-Path $runtimeRoot "python.exe"
    & $python $getPip --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "Could not bootstrap pip." }
    & $python -m pip install "torch==$torchVersion" "torchaudio==$torchVersion" --index-url $torchIndex --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "Could not install CUDA PyTorch." }
    & $python -m pip install -r $requirementsSource --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "Could not install Mapperatorinpainter requirements." }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw "The built Python environment has incompatible packages." }

    $requirementsHash = (Get-FileHash -LiteralPath $requirementsSource -Algorithm SHA256).Hash
    [ordered]@{
        fingerprint = "$requirementsHash|torch=$torchVersion|index=$torchIndex"
        installed_at = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeRoot ".mapperatorinpainter-dependencies.json") -Encoding UTF8
}

function Install-PortableFfmpeg {
    $ffmpegArchive = Join-Path $temporaryRoot "ffmpeg.zip"
    $ffmpegExtract = Join-Path $temporaryRoot "ffmpeg"
    Write-Host "Downloading the portable FFmpeg runtime..."
    Invoke-WebRequest -Uri $ffmpegUrl -OutFile $ffmpegArchive -TimeoutSec 180
    Expand-Archive -LiteralPath $ffmpegArchive -DestinationPath $ffmpegExtract -Force
    $ffmpegExecutable = Get-ChildItem -LiteralPath $ffmpegExtract -Filter "ffmpeg.exe" -File -Recurse | Select-Object -First 1
    if ($null -eq $ffmpegExecutable) { throw "The FFmpeg archive did not contain ffmpeg.exe." }
    $destination = Join-Path $runtimeRoot "ffmpeg\bin"
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Get-ChildItem -LiteralPath $ffmpegExecutable.Directory.FullName -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
    }
}

function Write-ReleaseMarker {
    [ordered]@{
        version = $Version
        repository = "LavaWander/Mapperatorinpainter"
        built_at = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $releaseRoot ".portable-install.json") -Encoding UTF8
}

function Test-PortableRuntime {
    $python = Join-Path $runtimeRoot "python.exe"
    $applicationRoot = Join-Path $releaseRoot "Mapperatorinator"
    $probe = @'
import sys
from pathlib import Path
app = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(app))
import torch, torchaudio, transformers, flask, slider, rosu_pp_py, webview
from webview.platforms import qt
import PyQt5.QtWebEngineWidgets
import config, inference, inpainting
assert torch.__version__.startswith("2.10.0")
assert torch.version.cuda == "13.0"
'@
    & $python -c $probe $applicationRoot
    if ($LASTEXITCODE -ne 0) { throw "Portable Python/application import smoke test failed." }
    & (Join-Path $runtimeRoot "ffmpeg\bin\ffmpeg.exe") -version *> $null
    if ($LASTEXITCODE -ne 0) { throw "Portable FFmpeg smoke test failed." }
}

New-Item -ItemType Directory -Force -Path $temporaryRoot, $outputRoot | Out-Null
try {
    Copy-ReleaseSources
    Install-EmbeddedPython
    Install-PortableFfmpeg
    Write-ReleaseMarker
    Test-PortableRuntime

    $archiveName = "Mapperatorinpainter-$($Version.TrimStart('v'))-Portable.zip"
    $archivePath = Join-Path $outputRoot $archiveName
    if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $releaseRoot,
        $archivePath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $true
    )
    Write-Host "Portable release created: $archivePath"
} finally {
    $resolvedTemporary = [System.IO.Path]::GetFullPath($temporaryRoot)
    if ($resolvedTemporary.StartsWith($temporaryParent, [StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $resolvedTemporary).StartsWith("mapperatorinpainter-portable-build-")) {
        Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force -ErrorAction SilentlyContinue
    }
}
