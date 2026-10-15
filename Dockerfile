# musloop — Docker image (core app: fast analysis, editor, player, export).
#
#   docker build -t musloop .
#   docker run -d --name musloop -p 8801:8801 -v musloop-data:/data musloop
#   -> http://localhost:8801
#
# Or: docker compose up -d   (see docker-compose.yml)
#
# Size: ~600 MB (python:3.12-slim ~120 MB + numpy/scipy/numba/librosa ~390 MB
# + a static ffmpeg ~80 MB). Debian's ffmpeg package would add ~450 MB of
# video/X11 libraries we don't need.
# Works on regular PCs and Macs: x86_64 and arm64 (Apple M-series Macs).
# The optional AI engines (SongFormer / All-In-One, several GB of PyTorch)
# are NOT included — install them natively with setup-ai-*.sh if you need them.

ARG PYTHON_VERSION=3.12

# ---------- build stage: dependencies into a self-contained venv ----------
FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /venv
COPY requirements.txt /tmp/requirements.txt
# ffmpeg: imageio-ffmpeg ships a static build (no system libraries needed) —
# link it onto PATH for the app and yt-dlp. Then trim what the runtime doesn't
# need: pip, test suites, bytecode caches.
RUN /venv/bin/pip install --upgrade pip \
 && /venv/bin/pip install -r /tmp/requirements.txt \
 && ln -s "$(/venv/bin/python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')" /venv/bin/ffmpeg \
 && /venv/bin/ffmpeg -hide_banner -version | head -1 \
 && /venv/bin/pip uninstall -y pip \
 && find /venv -type d \( -name tests -o -name __pycache__ \) -prune -exec rm -rf {} +

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim
RUN useradd --create-home --uid 1000 musloop \
 && mkdir -p /data && chown musloop:musloop /data

COPY --from=build /venv /venv
WORKDIR /app
COPY --chown=musloop:musloop backend ./backend
COPY --chown=musloop:musloop frontend ./frontend
COPY --chown=musloop:musloop LICENSE README.md ./

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MUSLOOP_DATA=/data \
    NUMBA_CACHE_DIR=/tmp/numba \
    PORT=8801

# the served page must be the prebuilt one (index.prod.html newer than index.html)
RUN touch frontend/index.prod.html frontend/app.js

USER musloop
VOLUME ["/data"]
EXPOSE 8801
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health', timeout=4)" || exit 1

CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
