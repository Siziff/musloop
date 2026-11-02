#!/usr/bin/env bash
# Start musloop. Usage: ./run.sh [port]  (or PORT=8801 ./run.sh)
# First-time setup: ./setup.sh
set -e
cd "$(dirname "$0")"
PORT="${1:-${PORT:-8801}}"
export PORT   # the app announces this port to the phone app (discovery)

# Use the local venv if present (created by ./setup.sh)
if [ -x ".venv/bin/python" ]; then
  PATH="$(pwd)/.venv/bin:$PATH"
fi

# Local secrets/settings: .env in the project root (gitignored).
# Example: HF_TOKEN=hf_xxxxx
if [ -f ".env" ]; then
  set -a; . ./.env; set +a
  echo "Loaded .env"
fi

# Free the port from stale processes (there may be several pids)
OLD=$(lsof -ti tcp:"$PORT" 2>/dev/null || ss -tlnp 2>/dev/null | grep ":$PORT " | grep -o 'pid=[0-9]*' | cut -d= -f2 | head -1)
if [ -n "$OLD" ]; then
  echo "Port $PORT is busy (pid: $(echo $OLD | tr '\n' ' ')) — stopping"
  for pid in $OLD; do kill "$pid" 2>/dev/null || true; done
  sleep 1
  # escalate if still alive
  STILL=$(lsof -ti tcp:"$PORT" 2>/dev/null || true)
  if [ -n "$STILL" ]; then
    for pid in $STILL; do kill -9 "$pid" 2>/dev/null || true; done
    sleep 1
  fi
  if [ -n "$(lsof -ti tcp:"$PORT" 2>/dev/null || true)" ]; then
    echo "ERROR: port $PORT is still busy — free it manually: lsof -ti tcp:$PORT | xargs kill -9"
    exit 1
  fi
fi

command -v ffmpeg >/dev/null || echo "NOTE: system ffmpeg not found — using bundled copy (imageio-ffmpeg)" 
python3 -c "import fastapi, uvicorn, librosa" 2>/dev/null || {
  echo "Dependencies are not installed. Run: ./setup.sh"; exit 1; }
# packages added in later versions (phone remote): install them after a git pull
if ! python3 -c "import websockets, segno" 2>/dev/null; then
  echo "Installing new dependencies (requirements.txt) ..."
  python3 -m pip install --quiet -r requirements.txt || { echo "Run: ./setup.sh"; exit 1; }
fi
if python3 -c "import yt_dlp" 2>/dev/null || command -v yt-dlp >/dev/null; then
  echo "URL import (yt-dlp): available"
else
  echo "URL import (yt-dlp): not installed (pip3 install yt-dlp)"
fi

# variables from before the rename (MUSSLOP_*) still work
: "${MUSLOOP_DEEP_PY:=${MUSSLOP_DEEP_PY:-}}"
: "${MUSLOOP_SONGFORMER_PY:=${MUSSLOP_SONGFORMER_PY:-}}"
[ -n "$MUSLOOP_DEEP_PY" ] && export MUSLOOP_DEEP_PY
[ -n "$MUSLOOP_SONGFORMER_PY" ] && export MUSLOOP_SONGFORMER_PY

# AI engine #1: All-In-One (./setup-ai-allin1.sh -> .venv-ai)
if [ -z "$MUSLOOP_DEEP_PY" ] && [ -x ".venv-ai/bin/python" ]; then
  export MUSLOOP_DEEP_PY="$(pwd)/.venv-ai/bin/python"
fi
if [ -n "$MUSLOOP_DEEP_PY" ] && [ -x "$MUSLOOP_DEEP_PY" ]; then
  echo "AI engine All-In-One: available"
else
  echo "AI engine All-In-One: not installed (./setup-ai-allin1.sh)"
fi
# AI engine #2: SongFormer + Beat This! (./setup-ai-songformer.sh -> .venv-songformer)
if [ -z "$MUSLOOP_SONGFORMER_PY" ] && [ -x ".venv-songformer/bin/python" ]; then
  export MUSLOOP_SONGFORMER_PY="$(pwd)/.venv-songformer/bin/python"
  export SONGFORMER_SRC="$(pwd)/.venv-songformer/src"
fi
if [ -n "$MUSLOOP_SONGFORMER_PY" ] && [ -x "$MUSLOOP_SONGFORMER_PY" ]; then
  echo "AI engine SongFormer: available"
else
  echo "AI engine SongFormer: not installed (./setup-ai-songformer.sh)"
fi

# JSX precompilation (optional: the built app.js ships in the repo).
# A rebuild is only needed after editing frontend/index.html.
if [ frontend/index.html -nt frontend/index.prod.html ] || [ frontend/remote.src.js -nt frontend/remote.js ] 2>/dev/null; then
  python3 tools/build.py 2>/dev/null && echo "Frontend rebuilt (app.js)" \
    || echo "WARNING: index.html is newer than the build and quickjs is missing (pip install quickjs) — falling back to dev mode with in-browser Babel"
fi

echo "Starting: http://localhost:$PORT (log: server.log)"
exec python3 -m uvicorn backend.main:app --host 0.0.0.0 --port "$PORT" 2>&1 | tee server.log
