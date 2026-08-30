@echo off
setlocal

rem Always launch relative to this file, so the shortcut works from anywhere.
set "PROJECT_DIR=%~dp0"
set "APP_DIR=%PROJECT_DIR%Mapperatorinator"
set "PYTHONUTF8=1"

if not exist "%APP_DIR%\web-ui.py" (
    echo ERROR: Could not find "%APP_DIR%\web-ui.py".
    echo Keep this launcher in the Mapperatorinpainter repository folder.
    pause
    exit /b 1
)

pushd "%APP_DIR%"

rem Prefer a project virtual environment when one is available.
if exist "%PROJECT_DIR%.venv\Scripts\python.exe" (
    "%PROJECT_DIR%.venv\Scripts\python.exe" web-ui.py
    goto finished
)

if exist "%APP_DIR%\.venv\Scripts\python.exe" (
    "%APP_DIR%\.venv\Scripts\python.exe" web-ui.py
    goto finished
)

rem Otherwise use an installed Python 3.10, which Mapperatorinator requires.
where py >nul 2>&1
if not errorlevel 1 (
    py -3.10 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        py -3.10 web-ui.py
        goto finished
    )
)

where python >nul 2>&1
if not errorlevel 1 (
    python web-ui.py
    goto finished
)

echo ERROR: Python was not found.
echo Install Python 3.10 or create a .venv in the repository.
set "LAUNCH_EXIT=1"
goto cleanup

:finished
set "LAUNCH_EXIT=%ERRORLEVEL%"

:cleanup
popd
if not "%LAUNCH_EXIT%"=="0" (
    echo.
    echo Mapperatorinpainter exited with error code %LAUNCH_EXIT%.
    echo Review the messages above for the cause.
    pause
)
exit /b %LAUNCH_EXIT%
