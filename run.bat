@echo off
rem Start musloop on Windows. First-time setup: setup.bat
rem Usage: run.bat [port]   (default 8801)
setlocal
cd /d "%~dp0"

set PORT=%1
if "%PORT%"=="" set PORT=8801

if not exist ".venv\Scripts\python.exe" (
  echo Dependencies are not installed. Run setup.bat first.
  pause
  exit /b 1
)

rem Load local settings from .env if present (KEY=VALUE lines)
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
  echo Loaded .env
)

rem AI engine venvs are auto-detected by the server itself (.venv-ai, .venv-songformer)

echo Starting: http://localhost:%PORT%   (close this window to stop)
".venv\Scripts\python.exe" -m uvicorn backend.main:app --host 0.0.0.0 --port %PORT%
pause
