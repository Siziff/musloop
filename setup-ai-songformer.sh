#!/usr/bin/env bash
# Optional AI engine #2: SongFormer (structure, 2025) + Beat This! (beats, 2024).
# Creates a dedicated venv in .venv-songformer/ — run.sh picks it up automatically.
#
# Usage: ./setup-ai-songformer.sh            (Linux / macOS Apple Silicon)
#        PYTHON=python3.12 ./setup-ai-songformer.sh   to pick the interpreter
# Takes ~10 minutes and ~4 GB of disk (torch, MuQ, MusicFM, checkpoints).
# No NATTEN, no compilers needed — plain pip installs.
#
# Versions below are the combination verified on the dev server
# (Python 3.12, torch 2.6.0, SongFormer @ SF_REF). Bump them together.
set -e
cd "$(dirname "$0")"

VENV=".venv-songformer"
SF_REPO="https://github.com/ASLP-lab/SongFormer"
SF_REF="${MUSSLOP_SONGFORMER_REF:-139b2aa3b14bd1c6d961d0994e9fc975f1ef7fd5}"
TORCH_VER="2.6.0"; TA_VER="2.6.0"; TV_VER="0.21.0"
DEPS=(
  "transformers==4.51.1" "muq==0.1.0" "beat_this==1.1.0" "msaf==0.1.80"
  "x_transformers==2.28.4" "ema_pytorch==0.8.3" "demucs==4.1.0" "mir_eval==0.8.2"
  "omegaconf==2.3.1" "einops==0.8.2" "loguru" "numpy" "librosa" "soundfile" "certifi"
)

echo "== musslop AI setup: SongFormer + Beat This! =="

# --- pick a Python torch 2.6 supports (3.10-3.13; 3.12 preferred) ------------
pick_python() {
  local c
  for c in ${PYTHON:-} python3.12 python3.11 python3.13 python3.10 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}
PY="$(pick_python)" || {
  echo "ERROR: need Python 3.10-3.13 (3.12 recommended); torch $TORCH_VER has no wheels for other versions."
  echo "       macOS: brew install python@3.12   Linux: install python3.12 (+ python3.12-venv)"
  echo "       then re-run, or point to it: PYTHON=/path/to/python3.12 ./setup-ai-songformer.sh"
  exit 1; }
echo "Python: $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

OS="$(uname -s)"; ARCH="$(uname -m)"
if [ "$OS" = "Darwin" ]; then
  if [ "$ARCH" != "arm64" ]; then
    if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
      echo "ERROR: this shell runs under Rosetta (x86_64) on an Apple Silicon Mac."
      echo "       Open a native terminal (or: arch -arm64 bash ./setup-ai-songformer.sh)."
    else
      echo "ERROR: Intel Macs are not supported by the AI engines (PyTorch $TORCH_VER has"
      echo "       no x86_64 macOS builds). The base app (./setup.sh) works fine."
    fi
    exit 1
  fi
fi
command -v git >/dev/null || { echo "ERROR: git is required (macOS: xcode-select --install)"; exit 1; }

if [ -x "$VENV/bin/python" ] && [ -d "$VENV/src/src/SongFormer/ckpts" ] \
   && "$VENV/bin/python" -c "import beat_this, muq" 2>/dev/null; then
  echo "Already installed ($VENV). To reinstall: rm -rf $VENV"
  exit 0
fi

echo "[1/5] Creating venv in $VENV ..."
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip

echo "[2/5] Installing PyTorch $TORCH_VER (CUDA / MPS / CPU auto) ..."
if [ "$OS" = "Darwin" ]; then
  "$VENV/bin/python" -m pip install --quiet "torch==$TORCH_VER" "torchaudio==$TA_VER" "torchvision==$TV_VER"
elif command -v nvidia-smi >/dev/null 2>&1; then
  "$VENV/bin/python" -m pip install --quiet "torch==$TORCH_VER" "torchaudio==$TA_VER" "torchvision==$TV_VER" \
    --index-url https://download.pytorch.org/whl/cu126
else
  "$VENV/bin/python" -m pip install --quiet "torch==$TORCH_VER" "torchaudio==$TA_VER" "torchvision==$TV_VER" \
    --index-url https://download.pytorch.org/whl/cpu
fi

echo "[3/5] Installing dependencies (pinned) ..."
# the pins never touch torch: it stays at $TORCH_VER from the step above
"$VENV/bin/python" -m pip install --quiet "${DEPS[@]}" "torch==$TORCH_VER"

echo "[4/5] SongFormer sources @ ${SF_REF:0:7} + checkpoints (~1.4 GB) ..."
if [ ! -d "$VENV/src/.git" ]; then
  rm -rf "$VENV/src"
  git init -q "$VENV/src"
  git -C "$VENV/src" remote add origin "$SF_REPO"
  if ! git -C "$VENV/src" fetch -q --depth 1 origin "$SF_REF"; then
    echo "  (pinned commit not fetchable — using the default branch)"
    git -C "$VENV/src" fetch -q --depth 1 origin HEAD
  fi
  git -C "$VENV/src" checkout -q FETCH_HEAD
fi
git -C "$VENV/src" submodule update --init --recursive --depth 1 \
  || git -C "$VENV/src" submodule update --init --recursive
if [ ! -d "$VENV/src/src/third_party/musicfm/model" ]; then
  echo "ERROR: SongFormer submodules (musicfm/MuQ) failed to clone. Check network/git and re-run."
  exit 1
fi
VENV_PY="$(pwd)/$VENV/bin/python"
(cd "$VENV/src/src/SongFormer" && "$VENV_PY" utils/fetch_pretrained.py)

echo "[5/5] Verifying ..."
SONGFORMER_SRC="$(pwd)/$VENV/src" "$VENV/bin/python" - <<'EOF'
import os, torch
assert os.path.isdir(os.path.join(os.environ["SONGFORMER_SRC"], "src", "SongFormer", "ckpts")), "checkpoints missing"
import beat_this, muq
dev = 'cuda' if torch.cuda.is_available() else \
      'mps' if getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available() else 'cpu'
print(f"OK: SongFormer + Beat This! ready, torch {torch.__version__}, device: {dev}")
EOF

echo
echo "Done! Start ./run.sh — the SongFormer engine will appear automatically."
