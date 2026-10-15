#!/usr/bin/env bash
# Optional AI components setup (All-In-One structure analyzer).
# Creates a dedicated venv in .venv-ai/ — run.sh picks it up automatically.
#
# Usage: ./setup-ai-allin1.sh            (Linux; macOS Apple Silicon builds NATTEN from source)
#        PYTHON=python3.12 ./setup-ai-allin1.sh   to pick the interpreter
# Takes ~10 minutes and ~2.5 GB of disk (torch, demucs, madmom, allin1).
#
# Verified combination (dev server): Python 3.12, torch 2.6.0+cu126,
# NATTEN 0.17.5, allin1 1.1.0, madmom @ MADMOM_REF. NATTEN 0.17.5 ships
# wheels only for cp310-cp312, hence the Python range below.
set -e
cd "$(dirname "$0")"

VENV=".venv-ai"
MADMOM_REF="27f032e8947204902c675e5e341a3faf5dc86dae"
ALLIN1_VER="1.1.0"

echo "== musslop AI setup: All-In-One =="

pick_python() {
  local c
  for c in ${PYTHON:-} python3.12 python3.11 python3.10 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}
PY="$(pick_python)" || {
  echo "ERROR: All-In-One needs Python 3.10-3.12 (3.12 recommended): NATTEN 0.17.5 has no"
  echo "       builds for newer Pythons. Install python3.12 and re-run, or:"
  echo "       PYTHON=/path/to/python3.12 ./setup-ai-allin1.sh"
  exit 1; }
echo "Python: $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" != "arm64" ]; then
  echo "ERROR: Intel Macs (or a Rosetta shell) are not supported: PyTorch 2.6 has no x86_64 macOS builds."
  exit 1
fi
command -v git >/dev/null || { echo "ERROR: git is required (madmom is installed from GitHub)"; exit 1; }

if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import allin1" 2>/dev/null; then
  echo "Already installed ($VENV). To reinstall: rm -rf $VENV"
  exit 0
fi

echo "[1/4] Creating venv in $VENV ..."
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip

echo "[2/4] Installing PyTorch (CPU/MPS on macOS, CUDA if available) ..."
OS="$(uname -s)"
# Pin torch 2.6: allin1 needs NATTEN 0.17.x which is built for torch<=2.6
if [ "$OS" = "Darwin" ]; then
  "$VENV/bin/pip" install --quiet torch==2.6.0 torchaudio==2.6.0
else
  # Linux: CUDA build when nvidia-smi is present, CPU otherwise
  if command -v nvidia-smi >/dev/null 2>&1; then
    "$VENV/bin/pip" install --quiet torch==2.6.0 torchaudio==2.6.0 \
      --index-url https://download.pytorch.org/whl/cu126
  else
    "$VENV/bin/pip" install --quiet torch==2.6.0 torchaudio==2.6.0 \
      --index-url https://download.pytorch.org/whl/cpu
  fi
fi

echo "[3/4] Installing madmom, allin1 and a compatible NATTEN ..."
"$VENV/bin/pip" install --quiet "git+https://github.com/CPJKU/madmom@$MADMOM_REF"
"$VENV/bin/pip" install --quiet "allin1==$ALLIN1_VER" "demucs==4.1.0" "torch==2.6.0"

# allin1 pulls a fresh natten (0.21+) that dropped the needed API — replace with 0.17.5.
# IMPORTANT: --no-deps, otherwise pip upgrades torch to latest and breaks everything
PYTAG=$("$VENV/bin/python" -c "import sys; print(f'cp{sys.version_info.major}{sys.version_info.minor}')")
if [ "$OS" = "Linux" ] && command -v nvidia-smi >/dev/null 2>&1; then
  "$VENV/bin/pip" install --quiet --force-reinstall --no-deps \
    "https://github.com/SHI-Labs/NATTEN/releases/download/v0.17.5/natten-0.17.5%2Btorch260cu126-${PYTAG}-${PYTAG}-linux_x86_64.whl" \
    || { echo "ERROR: NATTEN wheel did not match — see https://whl.natten.org"; exit 1; }
else
  # macOS/CPU: source build (needs a compiler; on mac — Xcode CLT)
  if [ "$OS" = "Darwin" ] && ! xcode-select -p >/dev/null 2>&1; then
    echo "ERROR: Xcode Command Line Tools required: xcode-select --install"; exit 1
  fi
  # cmake/ninja come as pip packages inside the venv (no brew needed)
  "$VENV/bin/pip" install --quiet cmake ninja
  export PATH="$(pwd)/$VENV/bin:$PATH"
  echo "  (building NATTEN 0.17.5 from source, 2-5 minutes...)"
  "$VENV/bin/pip" install --quiet --force-reinstall --no-deps --no-cache-dir \
    --no-build-isolation "natten==0.17.5" \
    || { echo "ERROR: NATTEN build failed. Check your compiler (clang/gcc)"; exit 1; }
fi

# safety net: if anything moved torch off 2.6 — put it back
TV=$("$VENV/bin/python" -c "import torch; print(torch.__version__)" 2>/dev/null | cut -d+ -f1)
if [ "$TV" != "2.6.0" ]; then
  echo "  (torch drifted to $TV — re-pinning 2.6.0)"
  if [ "$OS" = "Linux" ] && command -v nvidia-smi >/dev/null 2>&1; then
    "$VENV/bin/pip" install --quiet --force-reinstall --no-deps torch==2.6.0 torchaudio==2.6.0 \
      --index-url https://download.pytorch.org/whl/cu126
  else
    "$VENV/bin/pip" install --quiet --force-reinstall --no-deps torch==2.6.0 torchaudio==2.6.0
  fi
  # NATTEN may have been built against another torch — rebuild from scratch
  if ! "$VENV/bin/python" -c "import natten" 2>/dev/null; then
    echo "  (rebuilding NATTEN for torch 2.6, no cache...)"
    "$VENV/bin/pip" install --quiet --force-reinstall --no-deps --no-cache-dir \
      --no-build-isolation "natten==0.17.5" \
      || { echo "ERROR: NATTEN rebuild failed"; exit 1; }
  fi
fi

# allin1 may need the old natten API — compatibility shim
SITE=$("$VENV/bin/python" -c "import site; print(site.getsitepackages()[0])")

# natten 0.17.5 bug: on CPU-only machines it calls torch.cuda.get_device_capability
# at import time and crashes ("Torch not compiled with CUDA enabled") — patch it
if [ -f "$SITE/natten/utils/misc.py" ]; then
  "$VENV/bin/python" - <<'EOF'
import site, os
site_dir = site.getsitepackages()[0]
p = os.path.join(site_dir, "natten", "utils", "misc.py")
s = open(p).read()
old = "def get_device_cc("
guard = '''def get_device_cc(device_index=None):
    import torch
    if not torch.cuda.is_available():
        return 0
    return _get_device_cc_orig(device_index)


def _get_device_cc_orig('''
if "_get_device_cc_orig" not in s and old in s:
    s = s.replace(old, guard, 1)
    open(p, "w").write(s)
    print("  (patched natten CPU import bug)")
EOF
fi

if ! "$VENV/bin/python" -c "from natten.functional import natten1dav" 2>/dev/null \
   && "$VENV/bin/python" -c "from natten.functional import na1d_qk" 2>/dev/null; then
  cat > "$SITE/natten_compat.py" <<'EOF'
from natten.functional import na1d_qk, na1d_av, na2d_qk, na2d_av
def natten1dqkrpb(q, k, rpb, ks, dil): return na1d_qk(q, k, ks, dil, rpb=rpb)
def natten1dav(a, v, ks, dil): return na1d_av(a, v, ks, dil)
def natten2dqkrpb(q, k, rpb, ks, dil): return na2d_qk(q, k, ks, dil, rpb=rpb)
def natten2dav(a, v, ks, dil): return na2d_av(a, v, ks, dil)
EOF
  sed -i.bak 's/from natten.functional import natten1dav, natten1dqkrpb, natten2dav, natten2dqkrpb/from natten_compat import natten1dav, natten1dqkrpb, natten2dav, natten2dqkrpb/' \
    "$SITE/allin1/models/dinat.py" && rm -f "$SITE/allin1/models/dinat.py.bak"
  echo "  (NATTEN compatibility shim applied)"
fi

echo "[4/4] Verifying ..."
"$VENV/bin/python" - <<'EOF'
import allin1, torch
dev = 'cuda' if torch.cuda.is_available() else \
      'mps' if getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available() else 'cpu'
print(f"OK: allin1 works, device: {dev}")
EOF

echo
echo "Done! Start ./run.sh — the AI split button will appear automatically."
