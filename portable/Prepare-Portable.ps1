param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = [System.IO.Path]::GetFullPath($ProjectRoot)
$markerPath = Join-Path $root ".portable-install.json"
$pythonPath = Join-Path $root "runtime\python.exe"
$requirementsPath = Join-Path $root "portable\portable-requirements.txt"
$dependencyStatePath = Join-Path $root "runtime\.mapperatorinpainter-dependencies.json"
$repository = "LavaWander/Mapperatorinpainter"
$torchVersion = "2.10.0"
$torchIndex = "https://download.pytorch.org/whl/cu130"

if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
    throw "Portable installation marker is missing: $markerPath"
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Portable Python runtime is missing: $pythonPath"
}
if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
    throw "Portable requirements file is missing: $requirementsPath"
}

function Read-PortableMarker {
    try {
        return Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    } catch {
        throw "Portable installation marker is invalid: $($_.Exception.Message)"
    }
}

function Get-Sha256Hex([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "")
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Write-PortableMarker([string]$Version) {
    $data = [ordered]@{
        version = $Version
        repository = $repository
        updated_at = [DateTime]::UtcNow.ToString("o")
    }
    $data | ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding UTF8
}

function Get-LatestRelease {
    $headers = @{
        "Accept" = "application/vnd.github+json"
        "User-Agent" = "Mapperatorinpainter-Portable"
        "X-GitHub-Api-Version" = "2022-11-28"
    }
    $releases = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/$repository/releases?per_page=20" `
        -Headers $headers `
        -Method Get `
        -TimeoutSec 10
    return $releases | Where-Object { -not $_.draft } | Select-Object -First 1
}

function Copy-UpdateFiles([string]$sourceRoot) {
    $sourcePortable = Join-Path $sourceRoot "portable"
    if (Test-Path -LiteralPath $sourcePortable -PathType Container) {
        $destinationPortable = Join-Path $root "portable"
        New-Item -ItemType Directory -Force -Path $destinationPortable | Out-Null
        Get-ChildItem -LiteralPath $sourcePortable -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $destinationPortable -Recurse -Force
        }
    }

    foreach ($name in @("Install Danser Preview.bat", "INPAINTING_BACKLOG.md")) {
        $source = Join-Path $sourceRoot $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $root $name) -Force
        }
    }
    Get-ChildItem -LiteralPath $sourceRoot -Filter "M*_PROOF.md" -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $root $_.Name) -Force
    }
}

function Install-ReleaseUpdate($release) {
    $temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("mapperatorinpainter-update-" + [guid]::NewGuid().ToString("N"))
    $archivePath = Join-Path $temporaryRoot "release.zip"
    $extractPath = Join-Path $temporaryRoot "extracted"
    $backupPath = Join-Path $root (".update-backup-" + [guid]::NewGuid().ToString("N"))
    $currentApp = Join-Path $root "Mapperatorinator"
    $appMoved = $false

    New-Item -ItemType Directory -Force -Path $temporaryRoot, $extractPath | Out-Null
    try {
        Write-Host "Downloading Mapperatorinpainter $($release.tag_name)..."
        Invoke-WebRequest -Uri $release.zipball_url -Headers @{ "User-Agent" = "Mapperatorinpainter-Portable" } -OutFile $archivePath -TimeoutSec 120
        Expand-Archive -LiteralPath $archivePath -DestinationPath $extractPath -Force
        $sourceRoot = Get-ChildItem -LiteralPath $extractPath -Directory | Where-Object {
            Test-Path -LiteralPath (Join-Path $_.FullName "Mapperatorinator\web-ui.py") -PathType Leaf
        } | Select-Object -First 1
        if ($null -eq $sourceRoot) {
            throw "The release archive does not contain a valid Mapperatorinpainter application."
        }

        New-Item -ItemType Directory -Force -Path $backupPath | Out-Null
        Move-Item -LiteralPath $currentApp -Destination (Join-Path $backupPath "Mapperatorinator")
        $appMoved = $true
        Move-Item -LiteralPath (Join-Path $sourceRoot.FullName "Mapperatorinator") -Destination $currentApp
        $previousLogs = Join-Path $backupPath "Mapperatorinator\logs"
        if (Test-Path -LiteralPath $previousLogs -PathType Container) {
            Copy-Item -LiteralPath $previousLogs -Destination $currentApp -Recurse -Force
        }
        Copy-UpdateFiles $sourceRoot.FullName
        Write-PortableMarker ([string]$release.tag_name)
        Remove-Item -LiteralPath $backupPath -Recurse -Force
        $appMoved = $false
        Write-Host "Updated to Mapperatorinpainter $($release.tag_name)."
    } catch {
        if ($appMoved) {
            if (Test-Path -LiteralPath $currentApp) {
                Remove-Item -LiteralPath $currentApp -Recurse -Force
            }
            $backupApp = Join-Path $backupPath "Mapperatorinator"
            if (Test-Path -LiteralPath $backupApp) {
                Move-Item -LiteralPath $backupApp -Destination $currentApp
            }
        }
        throw
    } finally {
        if (Test-Path -LiteralPath $temporaryRoot) {
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $backupPath) {
            Remove-Item -LiteralPath $backupPath -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Test-ForUpdate {
    if ($env:MAPPERATORINPAINTER_SKIP_UPDATE -eq "1") {
        return
    }
    try {
        $release = Get-LatestRelease
        if ($null -eq $release) {
            Write-Host "No published Mapperatorinpainter release was found; continuing offline."
            return
        }
        $marker = Read-PortableMarker
        $currentVersion = [string]$marker.version
        if ($currentVersion.Trim().TrimStart("v") -ieq ([string]$release.tag_name).Trim().TrimStart("v")) {
            return
        }

        Write-Host "A new Mapperatorinpainter release is available: $($release.tag_name) (installed: $currentVersion)."
        $answer = Read-Host "Install it before starting? [Y/N]"
        if ($answer -match "(?i)^(y|yes)$") {
            Install-ReleaseUpdate $release
        }
    } catch {
        Write-Warning "Update check/install failed; the installed release will be used. $($_.Exception.Message)"
    }
}

function Get-DependencyFingerprint {
    $requirementsHash = Get-Sha256Hex $requirementsPath
    return "$requirementsHash|torch=$torchVersion|index=$torchIndex"
}

function Test-Dependencies([string]$fingerprint) {
    if (-not (Test-Path -LiteralPath $dependencyStatePath -PathType Leaf)) {
        return $false
    }
    try {
        $state = Get-Content -LiteralPath $dependencyStatePath -Raw | ConvertFrom-Json
        if ([string]$state.fingerprint -ne $fingerprint) {
            return $false
        }
    } catch {
        return $false
    }

    $probe = @'
import importlib.util, sys
required = ('torch', 'torchaudio', 'transformers', 'hydra', 'flask', 'webview', 'slider', 'rosu_pp_py')
missing = [name for name in required if importlib.util.find_spec(name) is None]
raise SystemExit(0 if sys.version_info[:2] == (3, 10) and not missing else 1)
'@
    & $pythonPath -c $probe *> $null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    & $pythonPath -m pip check *> $null
    return $LASTEXITCODE -eq 0
}

function Install-Dependencies([string]$fingerprint) {
    Write-Host "Installing or repairing Mapperatorinpainter's portable libraries..."
    $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
    $env:PIP_NO_INPUT = "1"
    & $pythonPath -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Could not update pip in the portable runtime." }
    & $pythonPath -m pip install "torch==$torchVersion" "torchaudio==$torchVersion" --index-url $torchIndex
    if ($LASTEXITCODE -ne 0) { throw "Could not install the portable CUDA PyTorch runtime." }
    & $pythonPath -m pip install -r $requirementsPath
    if ($LASTEXITCODE -ne 0) { throw "Could not install Mapperatorinpainter's Python libraries." }
    & $pythonPath -m pip check
    if ($LASTEXITCODE -ne 0) { throw "The portable Python environment has incompatible libraries." }

    [ordered]@{
        fingerprint = $fingerprint
        installed_at = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath $dependencyStatePath -Encoding UTF8
}

Test-ForUpdate
$dependencyFingerprint = Get-DependencyFingerprint
if (-not (Test-Dependencies $dependencyFingerprint)) {
    Install-Dependencies $dependencyFingerprint
}
