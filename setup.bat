@echo off
rem One-command setup for musslop on Windows (core, no AI engines).
rem Creates .venv\ and installs everything needed for run.bat.
rem ffmpeg is bundled automatically via the imageio-ffmpeg package.
setlocal
cd /d "%~dp0"

echo == musslop setup (Windows) ==

rem Prefer the "py" launcher (installed by python.org): "python" may be the
rem Microsoft Store stub that only opens the Store.
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo ERROR: Python 3.10+ not found. Install it from https://python.org
  echo        and check "Add python.exe to PATH" in the installer.
  echo        ^(The Microsoft Store "python" alias does not count.^)
  pause
  exit /b 1
)
for /f "delims=" %%v in ('%PY% -c "import sys; print(sys.version.split()[0])"') do echo Python: %PY% ^(%%v^)

if not exist ".venv\Scripts\python.exe" (
  echo [1/2] Creating venv in .venv ...
  %PY% -m venv .venv
  if errorlevel 1 ( echo ERROR: venv creation failed. & pause & exit /b 1 )
)

echo [2/2] Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo ERROR: dependency installation failed. See messages above.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, librosa, sklearn, soundfile, multipart, mutagen, yt_dlp, imageio_ffmpeg; print('OK: core dependencies ready (ffmpeg bundled:', imageio_ffmpeg.get_ffmpeg_exe(), ')')"
if errorlevel 1 (
  echo ERROR: verification failed.
  pause
  exit /b 1
)

echo.
echo Done! Start the app:   run.bat
echo Optional AI engine:   setup-ai-songformer.bat
pause
