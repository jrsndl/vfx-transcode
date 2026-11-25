@echo off
rem Freezes vfx-transcode.py, vfx-transcode-tuner.py and vfx-transcode-process.py into dist\vfx-transcode\
rem
rem   build_exe.bat            build all three, copy presets, docs and the tool folders
rem   build_exe.bat notools    same, but skip ffmpeg, oiio, ocio and fonts - they are large
rem
rem Result:
rem   dist\vfx-transcode\vfx-transcode.exe          one-file console exe
rem   dist\vfx-transcode\vfx-transcode-tuner.exe    windowed tuner, uses the _internal folder
rem   dist\vfx-transcode\vfx-transcode-process.exe  windowed process app, shares the _internal folder
rem   dist\vfx-transcode\presets.json, vfx-transcode.ini, docs\, README.md, ffmpeg\, oiio\, ocio\, fonts\
rem The tuner and the process app run the vfx-transcode.exe that sits next to them; all read
rem ./presets.json, ./vfx-transcode.ini and the ./ffmpeg, ./oiio, ./ocio tool paths relative to
rem the exe folder. An existing dist\vfx-transcode\vfx-transcode.ini (e.g. pointing to the studio
rem preferences) is kept; delete it to get the one from the repo.

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

rem --- clean (an edited vfx-transcode.ini in dist is set aside and put back) ---------------------
rem (a .keep left by a failed build is used, unless dist has an ini again)
if not exist build mkdir build
if exist "%OUT%\vfx-transcode.ini" copy /y "%OUT%\vfx-transcode.ini" "build\vfx-transcode.ini.keep" >nul
if exist "%OUT%" rmdir /s /q "%OUT%"
if exist "build\cli" rmdir /s /q "build\cli"
if exist "build\tuner" rmdir /s /q "build\tuner"
if exist "build\process" rmdir /s /q "build\process"

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

rem --- process app: one-folder windowed exe, merged into the tuner's folder (same Qt files) ------
echo.
echo === building vfx-transcode-process.exe
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed --noupx --name vfx-transcode-process ^
    --distpath build\process --workpath "%WORK%\process" --specpath build ^
    --paths "%CD%" "%CD%\vfx-transcode-process.py"
if errorlevel 1 goto :fail
if not exist "build\process\vfx-transcode-process\vfx-transcode-process.exe" goto :fail

rem --- assemble ---------------------------------------------------------------------------------
echo.
echo === assembling %OUT%
xcopy /e /i /q /y "build\tuner\vfx-transcode-tuner" "%OUT%" >nul
if errorlevel 1 goto :fail
xcopy /e /i /q /y "build\process\vfx-transcode-process" "%OUT%" >nul
if errorlevel 1 goto :fail
copy /y "build\cli\vfx-transcode.exe" "%OUT%\" >nul
if errorlevel 1 goto :fail
copy /y presets.json "%OUT%\" >nul
rem the ini may already point to the studio preferences: keep the one dist had, else the repo's
if exist "build\vfx-transcode.ini.keep" (
    copy /y "build\vfx-transcode.ini.keep" "%OUT%\vfx-transcode.ini" >nul
    if errorlevel 1 goto :fail
    del /q "build\vfx-transcode.ini.keep"
    echo     kept the existing vfx-transcode.ini
) else (
    copy /y vfx-transcode.ini "%OUT%\" >nul
)
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
