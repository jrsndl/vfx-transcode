@echo off
rem Freezes vfx-transcode.py and vfx-transcode-tuner.py into dist\vfx-transcode\
rem
rem   build_exe.bat            build both, copy presets, docs and the tool folders
rem   build_exe.bat notools    same, but skip ffmpeg, oiio, ocio and fonts - they are large
rem
rem Result:
rem   dist\vfx-transcode\vfx-transcode.exe          one-file console exe
rem   dist\vfx-transcode\vfx-transcode-tuner.exe    windowed tuner, uses the _internal folder
rem   dist\vfx-transcode\presets.json, docs\, README.md, ffmpeg\, oiio\, ocio\, fonts\
rem The tuner runs the vfx-transcode.exe that sits next to it; both read ./presets.json and the
rem ./ffmpeg, ./oiio, ./ocio tool paths relative to the exe folder.

setlocal
cd /d "%~dp0"

set "OUT=dist\vfx-transcode"
set "WORK=build\pyinstaller"
set "COPY_TOOLS=1"
if /i "%~1"=="notools" set "COPY_TOOLS=0"

rem --- environment: uv if present, else python from PATH -----------------------------------
where uv >nul 2>nul
if errorlevel 1 goto :no_uv
echo === uv sync --extra tuner
uv sync --extra tuner
if errorlevel 1 goto :fail
set "PY=uv run python"
goto :env_ok

:no_uv
echo uv not found - using python from PATH. It needs pyinstaller and PySide6 installed.
set "PY=python"

:env_ok
%PY% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo PyInstaller is not available. Run: uv sync --extra tuner
    goto :fail
)
%PY% -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo PySide6 is not available. Run: uv sync --extra tuner
    goto :fail
)

rem --- clean ------------------------------------------------------------------------------------
if exist "%OUT%" rmdir /s /q "%OUT%"
if exist "build\cli" rmdir /s /q "build\cli"
if exist "build\tuner" rmdir /s /q "build\tuner"

rem --- vfx-transcode: one-file console exe, from vfx-transcode.spec ----------------------------
echo.
echo === building vfx-transcode.exe
%PY% -m PyInstaller --noconfirm --clean --distpath build\cli --workpath "%WORK%\cli" vfx-transcode.spec
if errorlevel 1 goto :fail
if not exist "build\cli\vfx-transcode.exe" goto :fail

rem --- tuner: one-folder windowed exe - starts faster than one-file with Qt inside --------------
echo.
echo === building vfx-transcode-tuner.exe
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed --noupx --name vfx-transcode-tuner ^
    --distpath build\tuner --workpath "%WORK%\tuner" --specpath build ^
    --paths "%CD%" "%CD%\vfx-transcode-tuner.py"
if errorlevel 1 goto :fail
if not exist "build\tuner\vfx-transcode-tuner\vfx-transcode-tuner.exe" goto :fail

rem --- assemble ---------------------------------------------------------------------------------
echo.
echo === assembling %OUT%
xcopy /e /i /q /y "build\tuner\vfx-transcode-tuner" "%OUT%" >nul
if errorlevel 1 goto :fail
copy /y "build\cli\vfx-transcode.exe" "%OUT%\" >nul
if errorlevel 1 goto :fail
copy /y presets.json "%OUT%\" >nul
copy /y README.md "%OUT%\" >nul
xcopy /e /i /q /y docs "%OUT%\docs" >nul

for %%D in (ffmpeg oiio ocio fonts) do (
    if exist "%%D\" (
        if "%COPY_TOOLS%"=="1" (
            echo     copying %%D
            xcopy /e /i /q /y "%%D" "%OUT%\%%D" >nul
        ) else (
            mkdir "%OUT%\%%D" 2>nul
            if exist "%%D\README.md" copy /y "%%D\README.md" "%OUT%\%%D\" >nul
        )
    )
)

echo.
echo === done: %OUT%
dir /b "%OUT%"
set "RC=0"
goto :end

:fail
echo.
echo === BUILD FAILED
set "RC=1"

:end
rem keep the window open when started by double-click
echo %CMDCMDLINE% | find /i "/c" >nul && pause
endlocal & exit /b %RC%
