@echo off
setlocal EnableExtensions

set "PROJECT_DIR=%~dp0"
set "PORTABLE_MARKER=%PROJECT_DIR%.portable-install.json"
set "PORTABLE_PYTHON=%PROJECT_DIR%runtime\python.exe"
set "INSTALL_DIR=%PROJECT_DIR%.tools\danser-0.11.0"
set "DOWNLOAD_URL=https://github.com/Wieku/danser-go/releases/download/0.11.0/danser-0.11.0-win.zip"
set "EXPECTED_SHA256=749B2E66E36C3E2217910923802F08DE9BC1C0858FCB6FFAE861A6787FB21EEE"
set "NO_PAUSE=0"
if /I "%~1"=="/nopause" set "NO_PAUSE=1"

if not exist "%PORTABLE_MARKER%" goto not_portable
if not exist "%PORTABLE_PYTHON%" goto not_portable

if exist "%INSTALL_DIR%\danser-cli.exe" (
    if exist "%INSTALL_DIR%\danser-core.dll" (
        if exist "%INSTALL_DIR%\assets.dpak" (
            echo Danser 0.11.0 is already installed for Mapperatorinpainter.
            echo %INSTALL_DIR%\danser-cli.exe
            goto success
        )
    )
)

echo Downloading the official Danser 0.11.0 Windows release...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference = 'Stop';" ^
    "$archive = Join-Path ([System.IO.Path]::GetTempPath()) ('mapperatorinpainter-danser-' + [guid]::NewGuid().ToString('N') + '.zip');" ^
    "try {" ^
    "  Invoke-WebRequest -Uri '%DOWNLOAD_URL%' -OutFile $archive;" ^
    "  $stream = [System.IO.File]::OpenRead($archive); $sha = [System.Security.Cryptography.SHA256]::Create();" ^
    "  try { $actual = ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '') } finally { $sha.Dispose(); $stream.Dispose() };" ^
    "  if ($actual -ne '%EXPECTED_SHA256%') { throw ('Danser archive checksum mismatch. Expected %EXPECTED_SHA256%, got ' + $actual) };" ^
    "  New-Item -ItemType Directory -Force -Path '%INSTALL_DIR%' | Out-Null;" ^
    "  Expand-Archive -LiteralPath $archive -DestinationPath '%INSTALL_DIR%' -Force;" ^
    "} finally { Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue }"

if errorlevel 1 goto failed
if not exist "%INSTALL_DIR%\danser-cli.exe" goto incomplete
if not exist "%INSTALL_DIR%\danser-core.dll" goto incomplete
if not exist "%INSTALL_DIR%\assets.dpak" goto incomplete

echo.
echo Danser 0.11.0 is ready for Mapperatorinpainter previews.
echo %INSTALL_DIR%\danser-cli.exe
goto success

:not_portable
echo ERROR: This installer only works inside a Mapperatorinpainter portable release.
echo Re-extract the portable release, then run this installer from its root folder.
goto failed_no_message

:incomplete
echo ERROR: The verified archive did not contain a complete Danser runtime.
goto failed_no_message

:failed
echo.
echo ERROR: Danser installation failed. No unverified download will be used.

:failed_no_message
if "%NO_PAUSE%"=="0" pause
exit /b 1

:success
if "%NO_PAUSE%"=="0" pause
exit /b 0
