# Musloop - FastAPI server: track upload, analysis, audio serving and UI.

from __future__ import annotations

import json
import logging
import re
import os
import shutil
import subprocess
import sys
import uuid

log = logging.getLogger("musloop")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s",
                    datefmt="%H:%M:%S")

from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Body
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .analysis import (analyze, analyze_deep_merge, loop_quality, detect_meter_for_beats, repair_grid,
                       beats_too_sparse, audio_beat_period, beat_coverage)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The library: one folder per track, named like the track (backend/library.py).
#   native: <repo>/library   MUSLOOP_LIBRARY overrides it
#   Docker: MUSLOOP_DATA=/data -> /data/library (the volume)
# App state (projects, music folders, temp files) lives in <library>/.musloop.
_DATA = os.environ.get("MUSLOOP_DATA") or os.environ.get("MUSSLOP_DATA")
LIBRARY_DIR = os.path.abspath(os.environ.get("MUSLOOP_LIBRARY")
                              or (os.path.join(_DATA, "library") if _DATA else os.path.join(BASE_DIR, "library")))
STATE_DIR = os.path.join(LIBRARY_DIR, ".musloop")
# where older versions kept everything flat (<id>.mp3 + <id>.json): moved
# into the library on start
LEGACY_DIR = os.path.abspath(_DATA) if _DATA else os.path.join(BASE_DIR, "uploads")
UPLOAD_DIR = STATE_DIR   # temp / helper files (ffmpeg shim, downloads)
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
os.makedirs(STATE_DIR, exist_ok=True)

from . import library as lib  # noqa: E402


def _find_ffmpeg() -> str:
    """ffmpeg: PATH -> local copy in the project -> binary from the pip package
    imageio-ffmpeg (works on Windows/macOS/Linux without manual installation)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for cand in (
        os.path.join(BASE_DIR, "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(BASE_DIR, "ffmpeg", "bin", "ffmpeg"),
        os.path.join(BASE_DIR, "ffmpeg.exe"),
    ):
        if os.path.exists(cand):
            return cand
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"  # last resort - maybe it shows up in PATH


FFMPEG = _find_ffmpeg()
# subprocesses (yt-dlp, demucs in the AI venv) need ffmpeg too. The bundled
# imageio-ffmpeg binary is named "ffmpeg-linux-x86_64-v7.0.2" (not "ffmpeg"),
# so putting its folder on PATH is not enough: expose it under the plain name
# in a small bin dir, and pass the exact path in MUSLOOP_FFMPEG.
os.environ["MUSLOOP_FFMPEG"] = FFMPEG
if os.path.dirname(FFMPEG) and os.path.basename(FFMPEG).split(".")[0] != "ffmpeg":
    _shim = os.path.join(UPLOAD_DIR, ".bin")
    try:
        os.makedirs(_shim, exist_ok=True)
        _link = os.path.join(_shim, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not os.path.exists(_link):
            if os.name == "nt":
                shutil.copy2(FFMPEG, _link)
            else:
                os.symlink(FFMPEG, _link)
        os.environ["PATH"] = _shim + os.pathsep + os.environ.get("PATH", "")
    except OSError:
        pass
elif os.path.dirname(FFMPEG):
    os.environ["PATH"] = (os.path.dirname(FFMPEG) + os.pathsep
                          + os.environ.get("PATH", ""))


def _venv_python(venv_dir: str) -> list[str]:
    """Candidate python paths inside a venv for Linux/macOS and Windows."""
    return [
        os.path.join(venv_dir, "bin", "python"),
        os.path.join(venv_dir, "Scripts", "python.exe"),
    ]


# Python from the venv with allin1 (torch+NATTEN). Priority: env variable ->
# local .venv-ai (created by ./setup-ai-allin1.sh).
def _find_deep_py() -> str | None:
    cands = [
        os.environ.get("MUSLOOP_DEEP_PY"),
        os.environ.get("MUSSLOP_DEEP_PY"),
        *_venv_python(os.path.join(BASE_DIR, ".venv-ai")),
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


# venv with SongFormer + Beat This! (created by ./setup-ai-songformer.sh).
# Besides python we need the path to a SongFormer source clone (the infer
# code is not packaged in pip).
def _find_songformer() -> tuple[str | None, str | None]:
    py_cands = [
        os.environ.get("MUSLOOP_SONGFORMER_PY"),
        os.environ.get("MUSSLOP_SONGFORMER_PY"),
        *_venv_python(os.path.join(BASE_DIR, ".venv-songformer")),
    ]
    src_cands = [
        os.environ.get("SONGFORMER_SRC"),
        os.path.join(BASE_DIR, ".venv-songformer", "src"),
    ]
    py = next((c for c in py_cands if c and os.path.exists(c)), None)
    src = next((c for c in src_cands if c and os.path.isdir(c or "")), None)
    if py and src and os.path.isdir(os.path.join(src, "src", "SongFormer", "ckpts")):
        return py, src
    return None, None


DEEP_PY = _find_deep_py()
DEEP_AVAILABLE = DEEP_PY is not None
SONGFORMER_PY, SONGFORMER_SRC = _find_songformer()
SONGFORMER_AVAILABLE = SONGFORMER_PY is not None

app = FastAPI(title="Musloop")



@app.middleware("http")
async def no_html_cache(request, call_next):
    """Never cache HTML: otherwise the browser may show a stale UI."""
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if "text/html" in ct:
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response

# id -> {"orig": path, "wav": path, "name": str, "mime": str}
TRACKS: dict[str, dict] = {}

ALLOWED_EXT = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".webm"}
MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".flac": "audio/flac", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".opus": "audio/ogg", ".webm": "audio/webm",
}


def _save(track: dict) -> None:
    lib.save(track)


def _sync_library() -> None:
    lib.sync(TRACKS, LIBRARY_DIR)


def _restore_tracks() -> None:
    """Move tracks of older versions into the library, then read it."""
    try:
        lib.migrate_legacy(LEGACY_DIR, LIBRARY_DIR, STATE_DIR)
    except Exception as e:  # noqa: BLE001
        log.warning("library: moving old tracks failed: %s", e)
    _sync_library()
    log.info("library: %d tracks in %s", len(TRACKS), LIBRARY_DIR)


def _wav(track: dict) -> str:
    """Analysis WAV (mono 22 kHz), re-created if it was deleted (cache/)."""
    w = track.get("wav") or lib.default_wav(track)
    if not os.path.exists(w):
        os.makedirs(os.path.dirname(w), exist_ok=True)
        try:
            _to_wav(track["orig"], w)
        except subprocess.CalledProcessError:
            raise HTTPException(400, "Failed to decode the file (ffmpeg)")
        track["wav"] = w
        _save(track)
    return w


def _add_track(src: str, name: str, move: bool = False, **extra) -> dict:
    """Put an audio file into the library as a new track folder."""
    ext = os.path.splitext(src)[1].lower()
    d = lib.new_dir(LIBRARY_DIR, name, incoming=True)
    orig = lib.audio_path(d, name, ext)
    try:
        (shutil.move if move else shutil.copyfile)(src, orig)
        meta = {"id": uuid.uuid4().hex[:12], "orig": orig, "name": name, "dir": d,
                "mime": MIME.get(ext, "application/octet-stream"),
                "uploaded_at": int(__import__("time").time()), **extra}
        meta["wav"] = lib.default_wav(meta)
        if meta["wav"] != orig:
            os.makedirs(os.path.dirname(meta["wav"]), exist_ok=True)
            _to_wav(orig, meta["wav"])
    except subprocess.CalledProcessError:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, "Failed to decode the file (ffmpeg)")
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    _save(meta)
    TRACKS[meta["id"]] = meta
    lib.done_incoming(d)
    return meta


def _to_wav(src: str, dst: str) -> None:
    """Transcode to WAV for librosa (the browser gets the original)."""
    subprocess.run(
        [FFMPEG, "-y", "-i", src, "-ac", "1", "-ar", "22050", dst],
        check=True, capture_output=True,
    )


_restore_tracks()


@app.get("/api/health")
def health():
    return {"status": "ok", "tracks": len(TRACKS),
            "deep_available": DEEP_AVAILABLE,
            "songformer_available": SONGFORMER_AVAILABLE}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported format: {ext}")

    import tempfile
    name = os.path.basename((file.filename or "").replace("\\", "/")) or f"track{ext}"
    with tempfile.NamedTemporaryFile(dir=STATE_DIR, suffix=ext, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
    try:
        meta = _add_track(tmp.name, name, move=True)
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)
    size = os.path.getsize(meta["orig"])
    log.info("upload: '%s' -> library/%s (%.1f MB)", name, os.path.basename(meta["dir"]), size / 1e6)
    return {"track_id": meta["id"], "name": meta["name"], "size": size}


@app.get("/api/tracks")
def list_tracks():
    """History of uploaded tracks (for re-opening with saved markup)."""
    _sync_library()   # folders added / removed by hand in the library
    items = []
    for tid, m in TRACKS.items():
        items.append({
            "track_id": tid,
            "name": m.get("name"),
            "uploaded_at": m.get("uploaded_at", 0),
            "has_markup": bool(m.get("markup")),
            "favorite": bool(m.get("favorite")),
            "duration": (m.get("markup") or {}).get("duration"),
            "n_segments": len((m.get("markup") or {}).get("segments", []) or []),
        })
    # library on top; within groups - newest first
    items.sort(key=lambda x: (not x["favorite"], -x["uploaded_at"]))
    return {"tracks": items}


@app.post("/api/favorite/{track_id}")
def toggle_favorite(track_id: str, body: dict = Body(...)):
    """Add/remove a track to/from the library (favorite=true/false)."""
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    track["favorite"] = bool(body.get("favorite"))
    _save(track)
    return {"ok": True, "favorite": track["favorite"]}


_AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".webm")


def clean_track_name(name, old: str | None = None) -> str:
    """Track name typed by the user: one line, no path separators or control
    characters, at most 200 characters. The old file extension is kept so
    exports keep their file names (the UI edits the name without it)."""
    import re
    n = re.sub(r"[\x00-\x1f\x7f/\\]+", " ", str(name or ""))
    n = re.sub(r"\s+", " ", n).strip().strip(".")[:200].strip()
    if not n:
        raise HTTPException(400, "Empty name")
    ext = os.path.splitext(old or "")[1]
    if ext.lower() in _AUDIO_EXTS and not n.lower().endswith(ext.lower()):
        n += ext
    return n


@app.post("/api/tracks/{track_id}/name")
def rename_track(track_id: str, body: dict = Body(...)):
    """Rename a track (library / history). Markup, scenes and projects are
    kept: they refer to the track by id."""
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    track["name"] = clean_track_name(body.get("name"), track.get("name"))
    if "favorite" in body:
        track["favorite"] = bool(body.get("favorite"))
    lib.rename_dir(track, LIBRARY_DIR)   # library/<new name>/<new name>.mp3
    _save(track)
    log.info("track %s renamed to %r", track_id, track["name"])
    return {"ok": True, "name": track["name"], "favorite": bool(track.get("favorite"))}


@app.post("/api/markup/{track_id}")
def save_markup(track_id: str, markup: dict = Body(...)):
    """Save markup (segments + downbeats/beats/tempo) next to the track."""
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    track["markup"] = markup
    _save(track)
    return {"ok": True}


@app.get("/api/markup/{track_id}")
def get_markup(track_id: str):
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    markup = track.get("markup")
    # markups saved before the beat-grid fix: neural beat lists with extra /
    # missing beats gave bars of 1..9 beats. Repair the grid once (parts and
    # scenes are kept), save it back.
    # v3: also holes where the beat tracker lost the beat (SongFormer runs)
    # v5: AI runs where the network lost the beat on most of the track: the
    # bar grid is rebuilt from the audio (parts and scenes are kept)
    if isinstance(markup, dict) and markup.get("beats") and (markup.get("grid_v") or 0) < 5 \
            and markup.get("engine") in ("deep", "songformer"):
        raw = track.get("deep_raw" if markup["engine"] == "deep" else "songformer_raw") or {}
        try:
            if raw.get("beats") is not None and markup.get("duration") and \
                    beat_coverage(raw["beats"], float(markup["duration"])) < 0.3:
                manual = markup.get("beats_per_bar") if (markup.get("meter") or {}).get("source") == "manual" else None
                r = analyze_deep_merge(_wav(track), raw, manual)
                markup = dict(markup)
                for k in ("beats", "downbeats", "tempo", "beats_per_bar", "meter", "beat_accents", "downbeats_auto"):
                    if k in r:
                        markup[k] = r[k]
                markup["grid_v"] = 5
                track["markup"] = markup
                _save(track)
                log.info("markup %s: neural beats lost, bar grid rebuilt from the audio (%d beats, %d bars, %.0f BPM)",
                         track_id, len(markup["beats"]), len(markup["downbeats"]), markup.get("tempo") or 0)
        except Exception as e:  # noqa: BLE001 - never block opening a track
            log.warning("markup %s: grid rebuild failed: %s", track_id, e)
    # v4: also beats marked only on bar starts / at half rate (no beat ticks)
    if isinstance(markup, dict) and markup.get("beats") and (markup.get("grid_v") or 0) < 5:
        try:
            period = None
            if beats_too_sparse(markup.get("beats"), markup.get("downbeats")):
                import librosa as _lr
                _y, _sr = _lr.load(_wav(track), sr=22050, mono=True)
                period = audio_beat_period(_y, _sr)
            fixed = repair_grid(markup.get("beats"), markup.get("downbeats"),
                                markup.get("beats_per_bar") if (markup.get("meter") or {}).get("source") == "manual" else None,
                                markup.get("duration"), period)
        except Exception as e:  # noqa: BLE001 - never block opening a track
            log.warning("markup %s: grid repair failed: %s", track_id, e)
            fixed = None
        markup = dict(markup)
        markup["grid_v"] = 5
        if fixed:
            log.info("markup %s: repaired beat grid (%d -> %d beats, %d bars, %d/bar)", track_id,
                     len(markup.get("beats") or []), len(fixed["beats"]), len(fixed["downbeats"]), fixed["beats_per_bar"])
            markup.update(fixed)
            meter = dict(markup.get("meter") or {})
            if meter.get("source") != "manual":
                meter.update(beats_per_bar=fixed["beats_per_bar"], detected=fixed["beats_per_bar"], phase=0)
            markup["meter"] = meter or None
        track["markup"] = markup
        _save(track)
    return {"markup": markup}


@app.get("/api/scenes")
def list_scenes():
    """All scenes of all tracks (for the cross-track scene list and hotkeys)."""
    items = []
    for tid, m in TRACKS.items():
        markup = m.get("markup") or {}
        segs = markup.get("segments") or []
        for sc in markup.get("scenes") or []:
            if not isinstance(sc, dict):
                continue
            idx = sc.get("segIndex")
            label = segs[idx].get("label") if isinstance(idx, int) and 0 <= idx < len(segs) else None
            items.append({**sc, "track_id": tid, "track_name": m.get("name"), "part_label": label})
    return {"scenes": items}


@app.delete("/api/tracks/{track_id}")
def delete_track(track_id: str):
    track = TRACKS.pop(track_id, None)
    if not track:
        raise HTTPException(404, "Track not found")
    lib.delete(track)
    return {"ok": True}


# ----------------------------- Music folders --------------------------------
# The user points musloop at folders on disk; we list the audio files inside
# and import them on demand (copy into uploads/ + analysis WAV), remembering
# the source path so a file is imported only once.
FOLDERS_PATH = os.path.join(STATE_DIR, "folders.json")


def _load_folders() -> list[str]:
    try:
        with open(FOLDERS_PATH) as f:
            data = json.load(f)
        return [p for p in data if isinstance(p, str)]
    except Exception:
        return []


def _save_folders(folders: list[str]) -> None:
    with open(FOLDERS_PATH, "w") as f:
        json.dump(folders, f, ensure_ascii=False)


def _source_index() -> dict[str, str]:
    """source path -> track_id for already imported files."""
    return {os.path.normpath(m["source"]): tid for tid, m in TRACKS.items() if m.get("source")}


def _scan_folder(root: str, limit: int = 2000) -> list[dict]:
    out = []
    src = _source_index()
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in ALLOWED_EXT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            tid = src.get(os.path.normpath(full))
            tags = _audio_tags(full)
            out.append({
                "path": full, "name": fn, "size": size,
                "rel": os.path.relpath(full, root),
                "track_id": tid,
                "has_markup": bool(TRACKS.get(tid, {}).get("markup")) if tid else False,
                "has_cover": _embedded_cover(full) is not None,
                **tags,
            })
            if len(out) >= limit:
                return out
    return out


@app.get("/api/folders")
def list_folders():
    folders = _load_folders()
    return {"folders": [{"path": p, "exists": os.path.isdir(p)} for p in folders]}


def _resolve_folder(raw: str) -> tuple[str | None, list[str]]:
    """Turn user input into an existing absolute directory path.

    Accepts absolute and relative paths, ~ , quotes around the path, Windows
    backslashes and drive letters (also when typed on a POSIX host), and
    trailing separators. Relative paths are tried against the current working
    directory, the project root and the user's home. Returns (path, tried)."""
    raw = (raw or "").strip().strip('"').strip("'").strip()
    # `file://` URLs (drag & drop from a file manager) -> plain path
    if raw.lower().startswith("file://"):
        from urllib.parse import unquote, urlparse
        raw = unquote(urlparse(raw).path)
        if os.name == "nt" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
            raw = raw[1:]
    if not raw:
        return None, []
    candidates: list[str] = []

    def add(p: str) -> None:
        p = os.path.normpath(p)
        if p not in candidates:
            candidates.append(p)

    variants = [raw]
    # Windows-style input on any host: backslashes -> os separator
    if "\\" in raw:
        variants.append(raw.replace("\\", os.sep))
    for v in variants:
        v = os.path.expandvars(os.path.expanduser(v))
        add(v)
        if not os.path.isabs(v):
            add(os.path.join(os.getcwd(), v))
            add(os.path.join(BASE_DIR, v))
            add(os.path.join(os.path.expanduser("~"), v))
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c), candidates
    return None, candidates


def _folder_diagnosis(tried: list[str]) -> str:
    """Explain why none of the candidates is a usable directory."""
    import platform
    for c in tried:
        if os.path.exists(c) and not os.path.isdir(c):
            return f"{c} is a file, not a folder"
        if os.path.isdir(c):
            try:
                os.listdir(c)
            except PermissionError:
                mac = platform.system() == "Darwin"
                return (f"{c} exists but the server is not allowed to read it"
                        + (" — macOS: System Settings → Privacy & Security → Files and Folders → allow "
                           "your Terminal/Python to access this folder, then restart musloop" if mac else ""))
        parent = os.path.dirname(c.rstrip(os.sep)) or c
        if os.path.isdir(parent):
            try:
                os.listdir(parent)
            except PermissionError:
                return f"{parent} is not readable by the server (permissions)"
    host = platform.node()
    return (f"Tried: {'; '.join(tried[:4])}. Note: the path must exist on the computer where the "
            f"musloop server runs (this one is '{host}', {platform.system()}), not just in your browser.")


@app.post("/api/folders")
def add_folder(body: dict = Body(...)):
    raw = str(body.get("path") or "")
    path, tried = _resolve_folder(raw)
    if not path:
        raise HTTPException(400, "Folder not found. " + (_folder_diagnosis(tried) if tried else "Empty path."))
    try:
        os.listdir(path)
    except PermissionError:
        raise HTTPException(400, "Folder not found. " + _folder_diagnosis([path]))
    folders = _load_folders()
    if path not in folders:
        folders.append(path)
        _save_folders(folders)
    return {"ok": True, "path": path}


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _registered(path: str) -> str | None:
    for p in _load_folders():
        if _same_path(p, path):
            return p
    return None


@app.post("/api/folders/pick")
def pick_folder():
    """Open the OS folder dialog on the server machine (works when the browser
    runs on the same computer). Returns {"path": ...} or {"path": null}."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        raise HTTPException(501, "Folder dialog is not available on this server (no tkinter)")
    result: dict = {}

    def run():
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            result["path"] = filedialog.askdirectory(title="musloop — choose a music folder") or None
            root.destroy()
        except Exception as e:  # headless, no display, ...
            result["error"] = str(e)

    # tk must run on the main thread on macOS; uvicorn's worker thread is fine on
    # Linux/Windows, so try directly first and fall back to a subprocess.
    import platform
    if platform.system() == "Darwin":
        code = ("import tkinter as tk,sys\nfrom tkinter import filedialog\nr=tk.Tk();r.withdraw();"
                "r.attributes('-topmost',True);print(filedialog.askdirectory(title='musloop — choose a music folder') or '')")
        try:
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
            result["path"] = out.stdout.strip() or None
        except Exception as e:
            result["error"] = str(e)
    else:
        run()
    if result.get("error"):
        raise HTTPException(501, f"Folder dialog failed: {result['error']}")
    return {"path": result.get("path")}


@app.delete("/api/folders")
def remove_folder(path: str = Query(...)):
    folders = [p for p in _load_folders() if not _same_path(p, path)]
    _save_folders(folders)
    return {"ok": True}


@app.get("/api/folders/scan")
def scan_folder(path: str = Query(...)):
    reg = _registered(path)
    if not reg or not os.path.isdir(reg):
        raise HTTPException(404, "Folder not registered")
    return {"path": reg, "files": _scan_folder(reg)}


def _in_registered_folder(full: str) -> bool:
    full = os.path.normpath(full)
    for root in _load_folders():
        if not os.path.isdir(root):
            continue
        try:
            r = os.path.normcase(os.path.normpath(root))
            if os.path.commonpath([os.path.normcase(full), r]) == r:
                return True
        except ValueError:
            pass
    return False


def _embedded_cover(path: str) -> tuple[bytes, str] | None:
    """Return (bytes, mime) of the embedded artwork, if any (mutagen)."""
    try:
        import mutagen
        from mutagen.id3 import ID3
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4
        from mutagen.oggvorbis import OggVorbis
        from mutagen.oggopus import OggOpus
    except Exception:
        return None
    try:
        f = mutagen.File(path)
        if f is None:
            return None
        tags = getattr(f, "tags", None)
        # MP3 / AIFF / WAV with ID3
        if tags is not None and hasattr(tags, "getall"):
            pics = tags.getall("APIC")
            if pics:
                pic = sorted(pics, key=lambda p: (getattr(p, "type", 0) != 3))[0]
                return pic.data, pic.mime or "image/jpeg"
        if isinstance(f, FLAC) and f.pictures:
            pic = sorted(f.pictures, key=lambda p: (p.type != 3))[0]
            return pic.data, pic.mime or "image/jpeg"
        if isinstance(f, MP4):
            covr = f.tags.get("covr") if f.tags else None
            if covr:
                c = covr[0]
                mime = "image/png" if getattr(c, "imageformat", None) == 14 else "image/jpeg"
                return bytes(c), mime
        if isinstance(f, (OggVorbis, OggOpus)):
            b64 = f.get("metadata_block_picture")
            if b64:
                import base64
                from mutagen.flac import Picture
                pic = Picture(base64.b64decode(b64[0]))
                return pic.data, pic.mime or "image/jpeg"
    except Exception:
        return None
    return None


def _audio_tags(path: str) -> dict:
    """title / artist / duration from tags (best effort)."""
    out: dict = {}
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        if f is None:
            return out
        if f.info is not None and getattr(f.info, "length", None):
            out["duration"] = round(float(f.info.length), 1)
        for k in ("title", "artist", "album"):
            v = f.get(k)
            if v:
                out[k] = str(v[0])
    except Exception:
        pass
    return out


@app.get("/api/folders/cover")
def folder_cover(path: str = Query(...)):
    if not _in_registered_folder(path) or not os.path.isfile(path):
        raise HTTPException(404, "File not found")
    cov = _embedded_cover(path)
    if not cov:
        raise HTTPException(404, "No embedded cover")
    from fastapi.responses import Response
    data, mime = cov
    return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/folders/stream")
def folder_stream(path: str = Query(...)):
    """Serve the original file straight from the folder for a quick listen
    (no import, no analysis). Browsers handle range requests via FileResponse."""
    if not _in_registered_folder(path) or not os.path.isfile(path):
        raise HTTPException(404, "File not found")
    ext = os.path.splitext(path)[1].lower()
    return FileResponse(path, media_type=MIME.get(ext, "application/octet-stream"))


@app.post("/api/folders/import")
def import_from_folder(body: dict = Body(...)):
    """Import one file from a registered folder (or return the existing track)."""
    full = os.path.normpath(str(body.get("path") or ""))
    folders = _load_folders()

    def inside(root: str) -> bool:
        try:
            return os.path.commonpath([os.path.normcase(full), os.path.normcase(os.path.normpath(root))]) \
                == os.path.normcase(os.path.normpath(root))
        except ValueError:  # different drives on Windows
            return False

    if not any(inside(root) for root in folders if os.path.isdir(root)):
        raise HTTPException(400, "File is outside the registered folders")
    if not os.path.isfile(full):
        raise HTTPException(404, "File not found")
    existing = _source_index().get(os.path.normpath(full))
    if existing and existing in TRACKS:
        return {"track_id": existing, "name": TRACKS[existing]["name"], "existing": True}

    ext = os.path.splitext(full)[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported format: {ext}")
    meta = _add_track(full, os.path.basename(full), source=full)
    log.info("import from folder: '%s' -> library/%s", full, os.path.basename(meta["dir"]))
    return {"track_id": meta["id"], "name": meta["name"], "existing": False}


# ----------------------------- Projects (set lists) -------------------------
# A project groups tracks (and their scenes) for one session / level:
#   {"id", "name", "created_at", "updated_at",
#    "groups": [{"id", "name", "items": [{"id", "track_id", "scene_id"|null, "note"}]}]}
# Stored as library/.musloop/projects/{id}.json; the markup (segments + scenes) stays
# with the track, the project only references it.
PROJECTS_DIR = os.path.join(STATE_DIR, "projects")
os.makedirs(PROJECTS_DIR, exist_ok=True)


def _project_path(pid: str) -> str:
    return os.path.join(PROJECTS_DIR, f"{pid}.json")


def _load_project(pid: str) -> dict | None:
    p = _project_path(pid)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _save_project(proj: dict) -> None:
    proj["updated_at"] = int(__import__("time").time())
    with open(_project_path(proj["id"]), "w") as f:
        json.dump(proj, f, ensure_ascii=False)


def _clean_project(body: dict, proj: dict) -> dict:
    """Validate/normalize an incoming project body onto `proj` (in place)."""
    if "name" in body:
        proj["name"] = str(body.get("name") or "")[:120]
    if "groups" in body:
        groups = []
        for g in body.get("groups") or []:
            if not isinstance(g, dict):
                continue
            items = []
            for it in g.get("items") or []:
                if not isinstance(it, dict) or not it.get("track_id"):
                    continue
                items.append({
                    "id": str(it.get("id") or uuid.uuid4().hex[:8]),
                    "track_id": str(it["track_id"]),
                    "scene_id": (str(it["scene_id"]) if it.get("scene_id") else None),
                    "note": str(it.get("note") or "")[:300],
                })
            groups.append({
                "id": str(g.get("id") or uuid.uuid4().hex[:8]),
                "name": str(g.get("name") or "")[:120],
                "items": items,
            })
        proj["groups"] = groups
    return proj


def _project_summary(proj: dict) -> dict:
    n_items = sum(len(g.get("items", [])) for g in proj.get("groups", []))
    return {"id": proj["id"], "name": proj.get("name", ""),
            "created_at": proj.get("created_at", 0), "updated_at": proj.get("updated_at", 0),
            "n_groups": len(proj.get("groups", [])), "n_items": n_items}


@app.get("/api/projects")
def list_projects():
    items = []
    for fn in os.listdir(PROJECTS_DIR):
        if fn.endswith(".json"):
            proj = _load_project(fn[:-5])
            if proj:
                items.append(_project_summary(proj))
    items.sort(key=lambda x: -x["updated_at"])
    return {"projects": items}


@app.post("/api/projects")
def create_project(body: dict = Body(...)):
    pid = uuid.uuid4().hex[:10]
    proj = {"id": pid, "name": "", "groups": [],
            "created_at": int(__import__("time").time())}
    _clean_project(body, proj)
    if not proj["name"]:
        proj["name"] = "Project"
    _save_project(proj)
    return proj


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    proj = _load_project(pid)
    if not proj:
        raise HTTPException(404, "Project not found")
    # enrich items with the current track name / scene name / availability
    for g in proj.get("groups", []):
        for it in g.get("items", []):
            tr = TRACKS.get(it["track_id"])
            it["track_name"] = tr.get("name") if tr else None
            it["available"] = tr is not None
            it["scene_name"] = None
            if tr and it.get("scene_id"):
                for sc in ((tr.get("markup") or {}).get("scenes") or []):
                    if isinstance(sc, dict) and sc.get("id") == it["scene_id"]:
                        it["scene_name"] = sc.get("name")
    return proj


@app.put("/api/projects/{pid}")
def update_project(pid: str, body: dict = Body(...)):
    proj = _load_project(pid)
    if not proj:
        raise HTTPException(404, "Project not found")
    _clean_project(body, proj)
    _save_project(proj)
    return {"ok": True, "updated_at": proj["updated_at"]}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    p = _project_path(pid)
    if not os.path.exists(p):
        raise HTTPException(404, "Project not found")
    os.remove(p)
    return {"ok": True}


@app.get("/api/analyze/{track_id}")
def analyze_track(track_id: str,
                  n_segments: int | None = Query(None, ge=2, le=24),
                  engine: str = Query("fast"),
                  beats_per_bar: int | None = Query(None, ge=2, le=7)):
    """beats_per_bar: None = auto-detect the meter (recommended), otherwise a
    manual override (2..7 beats per bar)."""
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")

    log.info("analyze: %s engine=%s", track_id, engine)
    import time as _time
    _t0 = _time.time()
    if engine == "deep":
        if not DEEP_AVAILABLE:
            raise HTTPException(503, "Deep analysis unavailable: install All-In-One (setup-ai-allin1.sh; Windows — WSL2 only)")
        result = _deep_analyze(track, beats_per_bar)
    elif engine == "songformer":
        if not SONGFORMER_AVAILABLE:
            raise HTTPException(503, "SongFormer unavailable: run setup-ai-songformer.sh (Windows: setup-ai-songformer.bat) and restart the server")
        result = _songformer_analyze(track, beats_per_bar)
    else:
        result = analyze(_wav(track), n_segments=n_segments,
                         beats_per_bar=beats_per_bar)
    log.info("analyze: %s done in %.1fs, %d parts, tempo %.1f, meter %s/%s",
             track_id, _time.time() - _t0, len(result["segments"]),
             result.get("tempo") or 0, result.get("beats_per_bar"),
             (result.get("meter") or {}).get("source"))

    result["track_id"] = track_id
    result["name"] = track["name"]
    return result


def _total_ram_gb() -> float | None:
    """RAM available to this process (container limit if any), in GB."""
    try:
        for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            if os.path.exists(p):
                v = open(p).read().strip()
                if v.isdigit() and int(v) < 1 << 60:
                    return int(v) / 1024**3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except (ValueError, OSError, AttributeError):
        return None


# Rough peak memory of the neural engines on CPU (measured): SongFormer
# ~5 GB for a 1-minute track, ~10 GB for 3-4 minutes, ~25 GB for 7+ minutes;
# Demucs layers ~2 GB. On a GPU most of it sits in video memory.
_OOM_MARKERS = ("cannot allocate memory", "out of memory", "memoryerror",
                "defaultcpuallocator", "std::bad_alloc", "killed")


def _is_oom(returncode: int | None, stderr: str) -> bool:
    """Process ran out of memory: the kernel / Docker killed it (SIGKILL,
    exit -9 / 137 with no message) or torch reported a failed allocation."""
    if returncode in (-9, 137):
        return True
    low = (stderr or "").lower()
    return any(m in low for m in _OOM_MARKERS)


def _oom_message(what: str) -> str:
    """Starts with "[oom:<GB>]" so the UI can show a localized message."""
    ram = _total_ram_gb()
    have = f" This computer / container has about {ram:.0f} GB." if ram else ""
    return (f"[oom:{ram:.0f}] " if ram else "[oom:] ") + (f"Not enough memory for {what}.{have} On CPU the structure analysis needs "
            "about 10 GB for a typical 3-4 minute track and up to ~25 GB for tracks "
            "of 7 minutes or longer. Docker Desktop: Settings → Resources → Advanced → Memory limit "
            "(raise the limit and restart the container). Or use the fast analysis, "
            "or a machine with an NVIDIA GPU.")


def _run_engine(track: dict, cache_key: str, script: str, py: str,
                extra_env: dict | None = None) -> dict:
    """Shared AI-runner launch via subprocess with caching in the track meta."""
    cached = track.get(cache_key)
    if cached:
        log.info("engine %s: using cached result for %s", script, track["id"])
    if not cached:
        log.info("engine %s: running for %s (this can take minutes on CPU; "
                 "progress in this terminal)", script, track["id"])
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            out_json = tmp.name
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        try:
            proc = subprocess.run(
                [py, os.path.join(base, "tools", script),
                 track["orig"], out_json],
                capture_output=True, text=True, timeout=1800, env=env,
            )
            if proc.returncode != 0:
                if _is_oom(proc.returncode, proc.stderr):
                    log.warning("engine %s: out of memory on %s (exit %s)",
                                script, track["id"], proc.returncode)
                    raise HTTPException(507, _oom_message("the neural structure analysis"))
                tail = (proc.stderr or "")[-800:]
                raise HTTPException(500, f"AI analysis failed: {tail}")
            with open(out_json) as f:
                cached = json.load(f)
        finally:
            if os.path.exists(out_json):
                os.remove(out_json)
        track[cache_key] = cached
        _save(track)
    return cached


def _deep_analyze(track: dict, beats_per_bar: int | None = None) -> dict:
    """allin1 in a separate venv (subprocess); the result is cached in meta."""
    cached = _run_engine(track, "deep_raw", "deep_analyze.py", DEEP_PY)
    result = analyze_deep_merge(_wav(track), cached, beats_per_bar)
    result["engine"] = "deep"
    return result


def _songformer_analyze(track: dict, beats_per_bar: int | None = None) -> dict:
    """SongFormer + Beat This! in its own venv; merged with our loop metrics."""
    cached = _run_engine(track, "songformer_raw", "songformer_analyze.py",
                         SONGFORMER_PY, {"SONGFORMER_SRC": SONGFORMER_SRC})
    result = analyze_deep_merge(_wav(track), cached, beats_per_bar)
    result["engine"] = "songformer"
    return result


@app.get("/api/audio/{track_id}")
def get_audio(track_id: str, transcode: int = Query(0)):
    """Serve the original (compressed) file to the browser - faster download.
    ?transcode=1 - transcode to WAV 44.1kHz stereo: needed when
    decodeAudioData cannot handle the original (e.g. FLAC with cover art
    decodes incompletely)."""
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if transcode:
        safe = track.get("safe_wav")
        if not safe or not os.path.exists(safe):
            safe = os.path.join(track["dir"], "cache", "playback.wav")
            os.makedirs(os.path.dirname(safe), exist_ok=True)
            try:
                subprocess.run(
                    [FFMPEG, "-y", "-vn", "-i", track["orig"],
                     "-ac", "2", "-ar", "44100", "-acodec", "pcm_s16le", safe],
                    check=True, capture_output=True,
                )
            except subprocess.CalledProcessError:
                raise HTTPException(500, "Transcode failed (ffmpeg)")
            track["safe_wav"] = safe
            _save(track)
        return FileResponse(safe, media_type="audio/wav")
    return FileResponse(track["orig"], media_type=track["mime"])


@app.post("/api/meter/{track_id}")
def detect_meter(track_id: str, body: dict = Body(...)):
    """Detect the meter for an existing beat grid without re-segmenting
    (for markups saved before meter detection). Body: {beats, downbeats,
    engine}; neural engines trust their downbeats."""
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    try:
        beats = [float(x) for x in (body.get("beats") or [])]
        downbeats = [float(x) for x in (body.get("downbeats") or [])]
    except (TypeError, ValueError):
        raise HTTPException(400, "beats/downbeats must be numbers")
    if len(beats) < 8:
        raise HTTPException(400, "Not enough beats to detect the meter")
    neural = body.get("engine") in ("deep", "songformer")
    return detect_meter_for_beats(_wav(track), beats, downbeats, neural)


@app.post("/api/loopability/{track_id}")
def loopability(track_id: str, segments: list[dict] = Body(...)):
    """Recompute loop quality for manually edited boundaries."""
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    return {"loopability": loop_quality(_wav(track), segments)}


STEM_NAMES = ("drums", "bass", "other", "vocals")
STEM_JOBS: dict[str, dict] = {}  # track_id -> {"status", "progress", "error"}


def _stem_dir(track: dict) -> str:
    return os.path.join(track["dir"], "stems")


def _stems_done(stem_dir: str) -> bool:
    return all(os.path.exists(os.path.join(stem_dir, f"{n}.wav"))
               for n in STEM_NAMES)


def _run_demix(track_id: str, orig: str, stem_dir: str) -> None:
    """Background task: demucs with progress parsing from stderr."""
    import re as _re
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    job = STEM_JOBS[track_id]
    try:
        proc = subprocess.Popen(
            [(DEEP_PY or SONGFORMER_PY),
             os.path.join(base, "tools", "demix.py"), orig, stem_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        # demucs updates its progress bar via \r - read char by char
        buf = ""
        tail: list[str] = []   # last stderr lines (errors) for the log / message
        while True:
            ch = proc.stderr.read(1)
            if ch == "" and proc.poll() is not None:
                break
            if ch in ("\r", "\n"):
                m = _re.search(r"(\d+)%\|", buf)
                if m:
                    p = int(m.group(1))
                    if p >= job.get("_logged", -10) + 10:
                        log.info("stems %s: %d%%", track_id, p)
                        job["_logged"] = p
                    job["progress"] = p
                elif buf.strip():
                    tail = (tail + [buf.strip()])[-15:]
                buf = ""
            else:
                buf += ch
        proc.wait(timeout=3600)
        if proc.returncode != 0 or not _stems_done(stem_dir):
            err = "\n".join(tail)
            log.warning("stems %s: failed (exit %s): %s", track_id, proc.returncode,
                        " | ".join(tail[-4:])[-600:])
            job["status"] = "error"
            if _is_oom(proc.returncode, err):
                job["error"] = _oom_message("splitting the track into layers").replace("[oom:", "[oom-stems:", 1).replace(
                    "the structure analysis needs about 10 GB for a typical 3-4 minute track "
                    "and up to ~25 GB for tracks of 7 minutes or longer",
                    "the layer split needs about 2-3 GB")
            else:
                job["error"] = "Separation failed: " + (tail[-1][:200] if tail else "see server.log")
            return
        track = TRACKS.get(track_id)
        if track is not None:
            track["stems"] = stem_dir
            _save(track)
        job["status"] = "done"
        job["progress"] = 100
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)[:300]


@app.post("/api/stems/{track_id}")
def make_stems(track_id: str):
    """Start stem separation (background). Status - GET /api/stems/{id}/status."""
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if not (DEEP_AVAILABLE or SONGFORMER_AVAILABLE):
        raise HTTPException(503, "Stems unavailable: install an AI engine "
                                 "(setup-ai-songformer or setup-ai-allin1)")

    stem_dir = _stem_dir(track)
    if _stems_done(stem_dir):
        STEM_JOBS[track_id] = {"status": "done", "progress": 100, "error": None}
        return {"ok": True, "status": "done", "stems": list(STEM_NAMES)}

    job = STEM_JOBS.get(track_id)
    if job and job.get("status") == "running":
        return {"ok": True, "status": "running", "progress": job.get("progress", 0)}

    STEM_JOBS[track_id] = {"status": "running", "progress": 0, "error": None}
    import threading
    threading.Thread(target=_run_demix, args=(track_id, track["orig"], stem_dir),
                     daemon=True).start()
    return {"ok": True, "status": "running", "progress": 0}


@app.get("/api/stems/{track_id}/status")
def stems_status(track_id: str):
    track = TRACKS.get(track_id)
    if track and _stems_done(_stem_dir(track)):
        return {"status": "done", "progress": 100}
    job = STEM_JOBS.get(track_id)
    if not job:
        return {"status": "none", "progress": 0}
    return {"status": job["status"], "progress": job.get("progress", 0),
            "error": job.get("error")}


@app.get("/api/stems/{track_id}/{stem}")
def get_stem(track_id: str, stem: str):
    if stem not in STEM_NAMES:
        raise HTTPException(404, "No such stem")
    track = TRACKS.get(track_id)
    path = os.path.join(_stem_dir(track), f"{stem}.wav") if track else ""
    if not path or not os.path.exists(path):
        raise HTTPException(404, "Stem is not ready")
    return FileResponse(path, media_type="audio/wav")


import shutil as _shutil


def _find_ytdlp() -> list[str] | None:
    """Command to launch yt-dlp: module in the current python -> binary in
    PATH -> module in python3 from PATH (common case: pip install --user)."""
    try:
        import yt_dlp  # noqa: F401
        return [os.sys.executable, "-m", "yt_dlp"]
    except ImportError:
        pass
    exe = _shutil.which("yt-dlp")
    if exe:
        return [exe]
    py3 = _shutil.which("python3")
    if py3 and py3 != os.sys.executable:
        chk = subprocess.run([py3, "-c", "import yt_dlp"], capture_output=True)
        if chk.returncode == 0:
            return [py3, "-m", "yt_dlp"]
    return None


def _js_runtime_args() -> list[str]:
    """YouTube makes downloaders solve a JavaScript challenge. yt-dlp needs a
    JS runtime for it: the deno binary installed with musloop (requirements),
    passed by path so it works even when the venv is not on PATH (run.bat,
    Docker, IDE)."""
    try:
        import deno  # pip package with the official Deno binary
        path = deno.find_deno_bin()
        if path and os.path.exists(path):
            return ["--js-runtimes", f"deno:{path}"]
    except Exception:  # noqa: BLE001 - not installed: yt-dlp looks in PATH itself
        pass
    return []


@app.post("/api/import_url")
def import_url(body: dict = Body(...)):
    """Import a track from a URL (YouTube and anything yt-dlp supports).
    Download progress is streamed to the server log (terminal)."""
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL")

    ytdlp = _find_ytdlp()
    if not ytdlp:
        raise HTTPException(
            503, "yt-dlp is not installed. Run: pip install yt-dlp "
                 "and restart the server")

    log.info("import_url: start %s (yt-dlp: %s)", url, " ".join(ytdlp))
    track_id = uuid.uuid4().hex[:12]
    out_tpl = os.path.join(STATE_DIR, f"dl-{track_id}.%(ext)s")

    # extra yt-dlp options from .env, e.g. MUSLOOP_YTDLP_ARGS="--proxy http://127.0.0.1:7890 -4"
    import shlex
    extra = shlex.split(os.environ.get("MUSLOOP_YTDLP_ARGS", ""))
    # stream yt-dlp output to the log line by line ([download] xx.x% of ...)
    proc = subprocess.Popen(
        ytdlp + ["-x", "--audio-format", "mp3",
         "--audio-quality", "0", "--no-playlist", "--max-filesize", "200M",
         "--newline", "--force-overwrites",
         # flaky VPN / Wi-Fi: retry instead of failing on the first dropped connection
         "--retries", "10", "--fragment-retries", "10", "--extractor-retries", "5",
         "--socket-timeout", "30", *_js_runtime_args(), *extra,
         "-o", out_tpl, "--print", "after_move:filepath",
         "--print", "title", url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    lines: list[str] = []
    last_pct = -10
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        lines.append(line)
        if line.startswith("[download]") and "%" in line:
            # print roughly every 10%, don't spam the terminal
            try:
                pct = float(line.split("%")[0].split()[-1])
                if pct - last_pct >= 10 or pct >= 100:
                    log.info("import_url: %s", line)
                    last_pct = pct
            except ValueError:
                pass
        else:
            log.info("import_url: %s", line)
    ret = proc.wait(timeout=600)
    if ret != 0:
        tail = "\n".join(lines[-4:])
        low = tail.lower()
        if any(k in low for k in ("failed to resolve", "nodename nor servname", "name or service not known",
                                  "getaddrinfo")):
            hint = ("This computer could not look up the site's address (DNS). The internet / VPN "
                    "connection dropped — check it and try again. ")
        elif any(k in low for k in ("ssl", "unable to download", "timed out", "no route to host",
                                    "connection reset", "connection refused")):
            hint = ("The connection to the site was cut off. Try again; if it keeps happening, "
                    "check your VPN / network (some networks block YouTube). A proxy can be set "
                    "with MUSLOOP_YTDLP_ARGS in .env, see README. ")
        elif any(k in low for k in ("js runtime", "javascript runtime", "challenge solver", "wiki/ejs",
                                    "page needs to be reloaded", "n challenge", "signature solving")):
            hint = ("YouTube changed its download check and this yt-dlp can't pass it. Update it: "
                    "run ./setup.sh (Windows: setup.bat) and restart musloop. ")
        elif re.search(r"\bsign in\b|confirm your age|age[- ]restricted|not a bot|cookies", low):
            hint = ("YouTube wants a signed-in browser for this video (age limit or bot check). Add to "
                    ".env: MUSLOOP_YTDLP_ARGS=\"--cookies-from-browser chrome\" (or firefox / safari / edge, "
                    "the browser where you're signed in to YouTube) and restart musloop. ")
        elif "yt-dlp -u" in low or "extract" in low:
            hint = "Try updating yt-dlp: run ./setup.sh (Windows: setup.bat) and restart musloop. "
        else:
            hint = ""
        log.error("import_url: FAILED\n%s", "\n".join(lines[-10:]))
        raise HTTPException(400, f"Download failed. {hint}Details: {tail[-300:]}")

    # --print outputs the title and path; filter out service [lines]
    printed = [l for l in lines if not l.startswith("[")]
    title = printed[0] if printed else "track"
    orig_path = os.path.join(STATE_DIR, f"dl-{track_id}.mp3")
    if not os.path.exists(orig_path):
        raise HTTPException(500, "File did not appear after download")
    log.info("import_url: downloaded '%s' (%.1f MB)", title, os.path.getsize(orig_path) / 1e6)
    try:
        try:
            nice = clean_track_name(title)
        except HTTPException:
            nice = "track"
        meta = _add_track(orig_path, f"{nice}.mp3", move=True, source_url=url)
    except HTTPException as e:
        if e.status_code == 400 and "decode" in str(e.detail):
            raise HTTPException(400, "Failed to decode the downloaded audio")
        raise
    finally:
        if os.path.exists(orig_path):
            os.remove(orig_path)
    return {"track_id": meta["id"], "name": meta["name"]}


def _content_disposition(filename: str) -> str:
    """attachment header that survives non-ASCII names (Cyrillic, CJK...):
    HTTP headers are latin-1, so send an ASCII fallback plus the RFC 5987
    filename* with the real UTF-8 name (all browsers use filename*)."""
    from urllib.parse import quote
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "").strip() or "musloop.zip"
    if ascii_name.startswith(".") or ascii_name.startswith("_"):
        ascii_name = "musloop" + ascii_name
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


@app.post("/api/export/{track_id}")
def export_loops(track_id: str, body: dict | list = Body(...)):
    """Cut the track into loops at the boundaries and return a zip.

    Body: either a bare list of segments (legacy) or
    {"segments": [...], "scenes": [...], "tempo": float, "downbeats": [...],
     "tails": bool} — with the extended form the zip also contains a
    manifest.json describing loops, transition points and scenes (for game
    engines), plus optional *_tail.wav files (post-exit tails).

    We cut the original file (full quality, 44.1kHz stereo 16bit), not the
    mono analysis WAV. ZIP_STORED: PCM barely compresses with deflate,
    while the attempt takes a lot of time.
    """
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if isinstance(body, list):
        segments, scenes, tempo, downbeats, tails, target = body, [], None, [], False, "generic"
        crossfade = 0.0
    else:
        segments = body.get("segments") or []
        scenes = body.get("scenes") or []
        tempo = body.get("tempo")
        downbeats = body.get("downbeats") or []
        tails = bool(body.get("tails"))
        target = str(body.get("target") or "generic")   # generic | foundry
        crossfade = float(body.get("crossfade") or 0.0)
    meter_bpb, phrase_bars = 4, 4
    if isinstance(body, dict):
        try:
            meter_bpb = int(body.get("beats_per_bar") or 4)
            phrase_bars = int(body.get("phrase_bars") or 4)
        except (TypeError, ValueError):
            raise HTTPException(400, "beats_per_bar / phrase_bars must be integers")
        if not 1 <= meter_bpb <= 16 or not 1 <= phrase_bars <= 32:
            raise HTTPException(400, "beats_per_bar / phrase_bars out of range")
    if not segments:
        raise HTTPException(400, "Empty segment list")
    if target == "foundry":
        tails = True  # the Foundry module uses post-exit tails

    import io
    import re
    import tempfile
    import zipfile
    import soundfile as sf

    # decode the original at full quality (once, ~1-2s per track)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            [FFMPEG, "-y", "-i", track["orig"], "-ac", "2", "-ar", "44100",
             "-acodec", "pcm_s16le", tmp_path],
            check=True, capture_output=True,
        )
        y, sr = sf.read(tmp_path, always_2d=True, dtype="int16")
    except subprocess.CalledProcessError:
        raise HTTPException(500, "Failed to decode the original (ffmpeg)")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    TAIL_SEC = 1.2
    manifest_loops = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for i, s in enumerate(segments):
            start, end = float(s["start"]), float(s["end"])
            a, b = int(start * sr), int(end * sr)
            a, b = max(0, a), min(len(y), b)
            if b - a < sr // 10:
                continue
            wav_io = io.BytesIO()
            sf.write(wav_io, y[a:b], sr, format="WAV", subtype="PCM_16")
            label = re.sub(r"[^\w\-]+", "_", str(s.get("label", f"part{i+1}")))
            fname = f"{i+1:02d}_{label}.wav"
            zf.writestr(fname, wav_io.getvalue())

            entry = {
                "index": i,
                "name": s.get("label", f"part{i+1}"),
                "file": fname,
                "start_sec": round(start, 4),
                "end_sec": round(end, 4),
                "duration_sec": round((b - a) / sr, 4),
                "sample_rate": sr,
                "loop": s.get("loop", True) is not False,
                # loop repeat may start later than the file start (loopStart)
                "loop_start_sample": 0,
                "loop_end_sample": b - a,
                "stinger": s.get("stinger") or None,
            }
            ls = s.get("loopStart")
            if isinstance(ls, (int, float)) and start < float(ls) < end - 0.5:
                entry["loop_start_sample"] = int((float(ls) - start) * sr)
            # phrase / bar grid inside the loop (for "soon" transitions)
            if downbeats:
                bars = [round(float(d) - start, 4) for d in downbeats if start <= float(d) < end]
                entry["downbeats_sec"] = bars
                entry["transition_points_sec"] = bars[::phrase_bars] if bars else []
            if tails:
                ta, tb = b, min(len(y), b + int(TAIL_SEC * sr))
                if tb - ta > sr // 20:
                    t_io = io.BytesIO()
                    sf.write(t_io, y[ta:tb], sr, format="WAV", subtype="PCM_16")
                    tname = f"{i+1:02d}_{label}_tail.wav"
                    zf.writestr(tname, t_io.getvalue())
                    entry["tail_file"] = tname
                    entry["tail_sec"] = round((tb - ta) / sr, 4)
            manifest_loops.append(entry)

        manifest = {
            "format": "musloop-loops",
            "version": 1,
            "track": track.get("name"),
            "bpm": tempo,
            "beats_per_bar": meter_bpb,
            "phrase_bars": phrase_bars,
            "sample_rate": sr,
            "channels": int(y.shape[1]),
            "loops": manifest_loops,
            "scenes": [
                {
                    "name": sc.get("name"),
                    "loop_index": sc.get("segIndex"),
                    "cue": sc.get("cueMode", "natural"),
                    "layers": sc.get("layers"),
                    "rate": sc.get("rate", 1),
                    "crossfade_sec": sc.get("crossfade", 0),
                    "reverb": sc.get("reverb", 0),
                    "hotkey": sc.get("hotkey"),
                }
                for sc in scenes
                if isinstance(sc, dict)
            ],
            "notes": (
                "loop_start_sample/loop_end_sample are relative to the file. "
                "downbeats_sec are bar starts (beats_per_bar beats per bar); "
                "transition_points_sec are every phrase_bars-th bar. "
                "cue: natural = switch at loop end, soon = at the next transition point, "
                "now = crossfade immediately. tail_file (if present) is the audio that "
                "follows the loop end in the original track — play it over the next loop's start."
            ),
        }
        base = os.path.splitext(track["name"] or "loops")[0]
        slug = re.sub(r"[^\w\-]+", "_", base).strip("_") or "track"
        manifest["slug"] = slug
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

        if target == "foundry":
            # A ready-to-import Foundry VTT Playlist document (right-click the
            # Playlists sidebar -> Import Data). Every loop = one PlaylistSound
            # with repeat on; scenes become extra playlists that point at the
            # same files. Paths assume the zip is unpacked into
            # <Foundry Data>/musloop/<slug>/ (what the musloop-foundry module
            # does on "Import ZIP").
            root = f"musloop/{slug}"
            fade_ms = int(max(0.0, crossfade) * 1000)
            def sound(i: int, L: dict, name: str | None = None) -> dict:
                return {
                    "name": name or L["name"],
                    "path": f"{root}/{L['file']}",
                    "channel": "music",
                    "playing": False,
                    "pausedTime": None,
                    "repeat": bool(L["loop"]),
                    "volume": 0.8,
                    "fade": fade_ms,
                    "sort": i * 100000,
                    "flags": {"musloop": {"loop_index": L["index"], "stinger": L.get("stinger"),
                                          "tail_file": (f"{root}/{L['tail_file']}" if L.get("tail_file") else None)}},
                }
            playlists = [{
                "name": base,
                "description": "Exported from musloop — each sound is a seamless loop. "
                               "Loops marked repeat=false are build-ups (play once).",
                "mode": -1,            # DISABLED: GM picks a sound, it loops on its own
                "channel": "music",
                "playing": False,
                "fade": fade_ms,
                "sorting": "m",
                "sounds": [sound(i, L) for i, L in enumerate(manifest_loops)],
                "flags": {"musloop": {"manifest": f"{root}/manifest.json", "slug": slug, "bpm": tempo}},
            }]
            if manifest["scenes"]:
                by_index = {L["index"]: L for L in manifest_loops}
                playlists.append({
                    "name": f"{base} — scenes",
                    "description": "musloop scenes: one entry per scene, pointing at that scene's loop.",
                    "mode": -1, "channel": "music", "playing": False, "fade": fade_ms, "sorting": "m",
                    "sounds": [sound(i, by_index[sc["loop_index"]], sc["name"])
                               for i, sc in enumerate(manifest["scenes"]) if sc.get("loop_index") in by_index],
                    "flags": {"musloop": {"manifest": f"{root}/manifest.json", "slug": slug}},
                })
            # Foundry's "Import Data" takes ONE document per file
            zf.writestr("foundry-playlist.json", json.dumps(playlists[0], indent=2, ensure_ascii=False))
            if len(playlists) > 1:
                zf.writestr("foundry-playlist-scenes.json", json.dumps(playlists[1], indent=2, ensure_ascii=False))
            zf.writestr("README-foundry.txt", (
                "musloop -> Foundry VTT\n"
                "======================\n"
                f"1. Unpack this zip into <your Foundry user data>/Data/musloop/{slug}/\n"
                "   (so that the .wav files sit next to this README).\n"
                "   Tip: with the musloop-foundry module installed, use its 'Import ZIP' button instead.\n"
                "2. Without the module: in Foundry, Playlists sidebar -> right-click -> Import Data ->\n"
                "   choose foundry-playlist.json (and foundry-playlist-scenes.json if present).\n"
                "   Each sound is a seamless loop; click one to play it, click another to switch\n"
                "   (Foundry crossfades by the playlist 'fade' setting).\n"
                "3. With the module: open the musloop desk (music-note button in the scene controls),\n"
                "   pick this track -> transitions land on loop/phrase boundaries, scenes get hotkeys,\n"
                "   and every connected player hears the same thing.\n"
            ))
    buf.seek(0)

    base = os.path.splitext(track["name"] or "loops")[0]
    suffix = "_foundry" if target == "foundry" else "_loops"
    from fastapi.responses import Response
    return Response(
        buf.read(), media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(f"{base}{suffix}.zip")},
    )


@app.get("/")
def index():
    """Serve the precompiled page (app.js) if it is built and newer than the
    source; otherwise the dev version with Babel in the browser (slower on
    weak machines/connections). Rebuild: python3 tools/build.py"""
    prod = os.path.join(FRONTEND_DIR, "index.prod.html")
    dev = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(prod) and os.path.getmtime(prod) >= os.path.getmtime(dev):
        return FileResponse(prod, media_type="text/html")
    return FileResponse(dev, media_type="text/html")


# phone remote (/remote, /ws/host, /ws/remote) + network access rules
from . import remote as _remote  # noqa: E402
_remote.register(app)

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
