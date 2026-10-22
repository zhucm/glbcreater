@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 goto setup_error

rem Edit these five settings before double-clicking this file.
set "INPUT_DIR=D:\github\glbcreater\images"
set "OUTPUT_DIR=D:\github\glbcreater\output"
set "WIDTH_CM=8"
set "THICKNESS_CM=0.5"
set "BLENDER_EXE=D:\Blender\blender.exe"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo Missing Python environment. See README.md for installation.
    goto setup_error
)
if not exist "%INPUT_DIR%\" (
    echo Input directory does not exist: "%INPUT_DIR%"
    goto setup_error
)
if not exist "%BLENDER_EXE%" (
    echo Blender does not exist: "%BLENDER_EXE%"
    goto setup_error
)
where node >nul 2>&1
if errorlevel 1 (
    echo Node.js is not on PATH.
    goto setup_error
)
if not exist "node_modules\meshoptimizer\package.json" (
    echo Node dependencies are missing. Run npm install in this directory.
    goto setup_error
)
if not exist "node_modules\three\package.json" (
    echo Node dependencies are missing. Run npm install in this directory.
    goto setup_error
)

echo Input: "%INPUT_DIR%"
echo Output: "%OUTPUT_DIR%"
echo Width: %WIDTH_CM% cm  Thickness: %THICKNESS_CM% cm
echo Blender: "%BLENDER_EXE%"
echo.
"%PYTHON_EXE%" -m glb_tools --input "%INPUT_DIR%" --output "%OUTPUT_DIR%" --width-cm "%WIDTH_CM%" --thickness-cm "%THICKNESS_CM%" --blender "%BLENDER_EXE%"
set "EXIT_CODE=%ERRORLEVEL%"
goto finish

:setup_error
set "EXIT_CODE=2"

:finish
echo.
if "%EXIT_CODE%"=="0" (echo Finished successfully.) else (echo Finished with exit code %EXIT_CODE%.)
if not defined GLB_TOOLS_NO_PAUSE pause
exit /b %EXIT_CODE%
