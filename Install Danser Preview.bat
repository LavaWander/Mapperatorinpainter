@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "INSTALL_DIR=%PROJECT_DIR%.tools\danser-0.11.0"
set "DOWNLOAD_URL=https://github.com/Wieku/danser-go/releases/download/0.11.0/danser-0.11.0-win.zip"
set "EXPECTED_SHA256=749B2E66E36C3E2217910923802F08DE9BC1C0858FCB6FFAE861A6787FB21EEE"

if exist "%INSTALL_DIR%\danser-cli.exe" (
    if exist "%INSTALL_DIR%\danser-core.dll" (
        if exist "%INSTALL_DIR%\assets.dpak" (
            echo Danser 0.11.0 is already installed for Mapperatorinpainter.
            echo %INSTALL_DIR%\danser-cli.exe
            pause
            exit /b 0
        )
    )
)

echo Downloading the official Danser 0.11.0 Windows release...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference = 'Stop';" ^
    "$archive = Join-Path ([System.IO.Path]::GetTempPath()) ('mapperatorinpainter-danser-' + [guid]::NewGuid().ToString('N') + '.zip');" ^
    "try {" ^
    "  Invoke-WebRequest -Uri '%DOWNLOAD_URL%' -OutFile $archive;" ^
    "  $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash;" ^
    "  if ($actual -ne '%EXPECTED_SHA256%') { throw ('Danser archive checksum mismatch. Expected %EXPECTED_SHA256%, got ' + $actual) };" ^
    "  New-Item -ItemType Directory -Force -Path '%INSTALL_DIR%' | Out-Null;" ^
    "  Expand-Archive -LiteralPath $archive -DestinationPath '%INSTALL_DIR%' -Force;" ^
    "} finally { Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue }"

if errorlevel 1 (
    echo.
    echo ERROR: Danser installation failed. No unverified download will be used.
    pause
    exit /b 1
)

if not exist "%INSTALL_DIR%\danser-cli.exe" goto incomplete
if not exist "%INSTALL_DIR%\danser-core.dll" goto incomplete
if not exist "%INSTALL_DIR%\assets.dpak" goto incomplete
goto installed

:incomplete
    echo ERROR: The verified archive did not contain a complete Danser runtime.
    pause
    exit /b 1

:installed
echo.
echo Danser 0.11.0 is ready for Mapperatorinpainter previews.
echo %INSTALL_DIR%\danser-cli.exe
pause
