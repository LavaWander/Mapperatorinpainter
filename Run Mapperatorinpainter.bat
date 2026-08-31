@echo off
setlocal EnableExtensions

rem Always launch relative to this file, so the portable folder can be moved.
set "PROJECT_DIR=%~dp0"
set "APP_DIR=%PROJECT_DIR%Mapperatorinator"
set "PORTABLE_PYTHON=%PROJECT_DIR%runtime\python.exe"
set "PORTABLE_MARKER=%PROJECT_DIR%.portable-install.json"
set "PYTHONUTF8=1"

if not exist "%APP_DIR%\web-ui.py" (
    echo ERROR: Could not find "%APP_DIR%\web-ui.py".
    echo Keep this launcher in the Mapperatorinpainter folder.
    pause
    exit /b 1
)

rem A release build owns its Python environment and checks for updates before launch.
if exist "%PORTABLE_MARKER%" (
    if not exist "%PORTABLE_PYTHON%" (
        echo ERROR: The portable Python runtime is missing.
        echo Re-extract the complete Mapperatorinpainter portable release.
        pause
        exit /b 1
    )
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%portable\Prepare-Portable.ps1" -ProjectRoot "%PROJECT_DIR%."
    if errorlevel 1 goto prepare_failed
    set PYTHON_EXE="%PORTABLE_PYTHON%"
    set "PATH=%PROJECT_DIR%runtime\ffmpeg\bin;%PATH%"
    set "HF_HOME=%PROJECT_DIR%model_cache\huggingface"
    set "TORCH_HOME=%PROJECT_DIR%model_cache\torch"
    set "XDG_CACHE_HOME=%PROJECT_DIR%model_cache"
    if not exist "%PROJECT_DIR%model_cache" mkdir "%PROJECT_DIR%model_cache" >nul 2>&1
    call :offer_danser
) else (
    rem Development checkout fallback. Portable releases never use system Python.
    if exist "%PROJECT_DIR%.venv\Scripts\python.exe" (
        set PYTHON_EXE="%PROJECT_DIR%.venv\Scripts\python.exe"
    ) else if exist "%APP_DIR%\.venv\Scripts\python.exe" (
        set PYTHON_EXE="%APP_DIR%\.venv\Scripts\python.exe"
    ) else (
        where py >nul 2>&1
        if not errorlevel 1 (
            py -3.10 -c "import sys" >nul 2>&1
            if not errorlevel 1 set "PYTHON_EXE=py -3.10"
        )
        if not defined PYTHON_EXE (
            where python >nul 2>&1
            if not errorlevel 1 set "PYTHON_EXE=python"
        )
    )
)

if not defined PYTHON_EXE (
    echo ERROR: Python was not found.
    echo Development checkouts require Python 3.10 or a .venv.
    pause
    exit /b 1
)

pushd "%APP_DIR%"
if exist "%PORTABLE_MARKER%" (
    %PYTHON_EXE% "%PROJECT_DIR%portable\launch.py" "%APP_DIR%\web-ui.py"
) else (
    %PYTHON_EXE% web-ui.py
)
set "LAUNCH_EXIT=%ERRORLEVEL%"
popd

if not "%LAUNCH_EXIT%"=="0" (
    echo.
    echo Mapperatorinpainter exited with error code %LAUNCH_EXIT%.
    echo Review the messages above for the cause.
    pause
)
exit /b %LAUNCH_EXIT%

:offer_danser
set "DANSER_DIR=%PROJECT_DIR%.tools\danser-0.11.0"
if exist "%DANSER_DIR%\danser-cli.exe" if exist "%DANSER_DIR%\danser-core.dll" if exist "%DANSER_DIR%\assets.dpak" exit /b 0
if exist "%PROJECT_DIR%.portable-state\danser-prompted" exit /b 0
if not exist "%PROJECT_DIR%.portable-state" mkdir "%PROJECT_DIR%.portable-state" >nul 2>&1
echo.
choice /C YN /N /M "Install optional Danser high-fidelity preview now? [Y/N] "
set "DANSER_CHOICE=%ERRORLEVEL%"
>"%PROJECT_DIR%.portable-state\danser-prompted" echo prompted
if "%DANSER_CHOICE%"=="1" call "%PROJECT_DIR%Install Danser Preview.bat" /nopause
exit /b 0

:prepare_failed
echo.
echo ERROR: Mapperatorinpainter portable setup could not be completed.
echo Review the message above, check your connection, and try again.
pause
exit /b 1
