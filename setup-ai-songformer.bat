@echo off
rem Optional AI engine for Windows: SongFormer (structure) + Beat This! (beats).
rem Creates a dedicated venv in .venv-songformer\ -- run.bat picks it up automatically.
rem Takes ~10-15 minutes and ~4 GB of disk. Needs git in PATH (git-scm.com).
rem
rem Versions are the combination verified on the dev server (Python 3.12,
rem torch 2.6.0, SongFormer @ SF_REF) -- keep in sync with setup-ai-songformer.sh.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "SF_REPO=https://github.com/ASLP-lab/SongFormer"
set "SF_REF=139b2aa3b14bd1c6d961d0994e9fc975f1ef7fd5"
if defined MUSLOOP_SONGFORMER_REF set "SF_REF=%MUSLOOP_SONGFORMER_REF%"
set "TORCH_PKGS=torch==2.6.0 torchaudio==2.6.0 torchvision==0.21.0"
set "DEPS=transformers==4.51.1 muq==0.1.0 beat_this==1.1.0 msaf==0.1.80 x_transformers==2.28.4 ema_pytorch==0.8.3 demucs==4.1.0 mir_eval==0.8.2 omegaconf==2.3.1 einops==0.8.2 loguru numpy librosa soundfile certifi"

echo == musloop AI setup: SongFormer + Beat This (Windows) ==

rem --- pick a Python torch 2.6 supports: 3.10-3.13, prefer 3.12 (py launcher) ---
set "PY="
for %%V in (3.12 3.11 3.13 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  where python >nul 2>nul && (
    python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>nul && set "PY=python"
  )
)
if not defined PY (
  echo ERROR: need Python 3.10-3.13 ^(3.12 recommended^) -- torch 2.6 has no wheels for other versions.
  echo        Install Python 3.12 from https://python.org ^(check "Add python.exe to PATH"^) and re-run.
  pause & exit /b 1
)
for /f "delims=" %%v in ('!PY! -c "import sys; print(sys.version.split()[0])"') do echo Python: !PY! ^(%%v^)

where git >nul 2>nul
if errorlevel 1 (
  echo ERROR: git not found in PATH. Install from https://git-scm.com
  pause & exit /b 1
)

set "VENVPY=.venv-songformer\Scripts\python.exe"

if exist "%VENVPY%" if exist ".venv-songformer\src\src\SongFormer\ckpts" (
  "%VENVPY%" -c "import beat_this, muq" >nul 2>nul && (
    echo Already installed ^(.venv-songformer^). To reinstall: rmdir /s /q .venv-songformer
    pause & exit /b 0
  )
)

if not exist "%VENVPY%" (
  echo [1/5] Creating venv in .venv-songformer ...
  !PY! -m venv .venv-songformer
  if errorlevel 1 ( echo ERROR: venv creation failed. & pause & exit /b 1 )
)

echo [2/5] Installing PyTorch 2.6.0 (this is the big download) ...
"%VENVPY%" -m pip install --quiet --upgrade pip
rem CUDA build if an NVIDIA GPU is present, CPU otherwise
where nvidia-smi >nul 2>nul
if errorlevel 1 (
  "%VENVPY%" -m pip install --quiet %TORCH_PKGS% --index-url https://download.pytorch.org/whl/cpu
) else (
  "%VENVPY%" -m pip install --quiet %TORCH_PKGS% --index-url https://download.pytorch.org/whl/cu126
)
if errorlevel 1 ( echo ERROR: PyTorch install failed. & pause & exit /b 1 )

echo [3/5] Installing dependencies (pinned) ...
rem torch==2.6.0 in the same command keeps pip from upgrading it
"%VENVPY%" -m pip install --quiet %DEPS% torch==2.6.0
if errorlevel 1 ( echo ERROR: dependency install failed. & pause & exit /b 1 )

echo [4/5] SongFormer sources @ %SF_REF:~0,7% + checkpoints (~1.4 GB) ...
if not exist ".venv-songformer\src\.git" (
  if exist ".venv-songformer\src" rmdir /s /q ".venv-songformer\src"
  git init -q .venv-songformer\src
  git -C .venv-songformer\src remote add origin %SF_REPO%
  git -C .venv-songformer\src fetch -q --depth 1 origin %SF_REF%
  if errorlevel 1 (
    echo   ^(pinned commit not fetchable -- using the default branch^)
    git -C .venv-songformer\src fetch -q --depth 1 origin HEAD
  )
  git -C .venv-songformer\src checkout -q FETCH_HEAD
)
git -C .venv-songformer\src submodule update --init --recursive
if not exist ".venv-songformer\src\src\third_party\musicfm\model" (
  echo ERROR: SongFormer submodules ^(musicfm/MuQ^) failed to clone. Check your network/git and re-run.
  pause & exit /b 1
)
pushd .venv-songformer\src\src\SongFormer
"%~dp0.venv-songformer\Scripts\python.exe" utils\fetch_pretrained.py
set "FETCH_ERR=!errorlevel!"
popd
if not "!FETCH_ERR!"=="0" ( echo ERROR: checkpoint download failed. & pause & exit /b 1 )

echo [5/5] Verifying ...
set "SONGFORMER_SRC=%~dp0.venv-songformer\src"
"%VENVPY%" -c "import os, torch; assert os.path.isdir(os.path.join(os.environ['SONGFORMER_SRC'],'src','SongFormer','ckpts')), 'checkpoints missing'; import beat_this, muq; print('OK: SongFormer + Beat This ready, torch', torch.__version__, 'device:', 'cuda' if torch.cuda.is_available() else 'cpu')"
if errorlevel 1 ( echo ERROR: verification failed. & pause & exit /b 1 )

echo.
echo Done. Start run.bat -- the SongFormer button will appear automatically.
echo.
echo Note: the second engine (All-In-One) is not supported on Windows:
echo its NATTEN dependency requires building from source with MSVC.
echo Use WSL2 (Ubuntu) if you need it -- setup-ai-allin1.sh works there.
pause
