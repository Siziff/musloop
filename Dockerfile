# musslop — Docker image (core app: fast analysis, editor, player, export).
#
#   docker build -t musslop .
#   docker run -d --name musslop -p 8801:8801 -v musslop-data:/data musslop
#   -> http://localhost:8801
#
# Or: docker compose up -d   (see docker-compose.yml)
#
# Size: roughly 0.6-0.7 GB (python:3.12-slim ~120 MB + Python deps ~390 MB + ffmpeg).
# Works on x86_64 and arm64 (Apple Silicon, Raspberry Pi 4/5 64-bit).
# The optional AI engines (SongFormer / All-In-One, several GB of PyTorch)
# are NOT included — install them natively with setup-ai-*.sh if you need them.

ARG PYTHON_VERSION=3.12

# ---------- build stage: dependencies into a self-contained venv ----------
FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /venv
COPY requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install --upgrade pip \
 && /venv/bin/pip install -r /tmp/requirements.txt \
 # trim: the system ffmpeg is used instead of imageio-ffmpeg's 77 MB binary,
 # pip is not needed at runtime, test suites and bytecode caches are dead weight
 && rm -rf /venv/lib/python*/site-packages/imageio_ffmpeg/binaries/ffmpeg-* \
 && /venv/bin/pip uninstall -y pip \
 && find /venv -type d \( -name tests -o -name __pycache__ \) -prune -exec rm -rf {} +

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim
# ffmpeg: decoding uploads, export, yt-dlp; --no-install-recommends keeps it lean
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 musslop \
 && mkdir -p /data && chown musslop:musslop /data

COPY --from=build /venv /venv
WORKDIR /app
COPY --chown=musslop:musslop backend ./backend
COPY --chown=musslop:musslop frontend ./frontend
COPY --chown=musslop:musslop LICENSE README.md ./

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MUSSLOP_DATA=/data \
    NUMBA_CACHE_DIR=/tmp/numba \
    PORT=8801

# the served page must be the prebuilt one (index.prod.html newer than index.html)
RUN touch frontend/index.prod.html frontend/app.js

USER musslop
VOLUME ["/data"]
EXPOSE 8801
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health', timeout=4)" || exit 1

CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
