#!/usr/bin/env bash
# One-command setup for musloop (core, no AI engines).
# Creates .venv/ and installs everything needed for ./run.sh.
#
# Usage: ./setup.sh
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "== musloop setup =="
$PY -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"' \
  || { echo "ERROR: Python 3.10+ required"; exit 1; }
if ! command -v ffmpeg >/dev/null; then
  echo "NOTE: system ffmpeg not found — a bundled copy (imageio-ffmpeg) will be used."
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/2] Creating venv in .venv ..."
  $PY -m venv .venv
fi
echo "[2/2] Installing dependencies ..."
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt
# dev only: rebuild frontend/app.js after editing index.html (optional)
[ "${MUSLOOP_DEV:-0}" = "1" ] && ./.venv/bin/python -m pip install --quiet -r requirements-dev.txt

./.venv/bin/python -c "import fastapi, uvicorn, librosa, sklearn, soundfile, multipart, mutagen, yt_dlp, imageio_ffmpeg, websockets, segno; print('OK: core dependencies ready, librosa', librosa.__version__)"

echo
echo "Done! Start the app:   ./run.sh"
echo "Optional AI engines:   ./setup-ai-songformer.sh   ./setup-ai-allin1.sh"
