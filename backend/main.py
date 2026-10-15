# Musslop - FastAPI server: track upload, analysis, audio serving and UI.

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import uuid

log = logging.getLogger("musslop")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s",
                    datefmt="%H:%M:%S")

from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Body
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .analysis import analyze, analyze_deep_merge, loop_quality

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
os.makedirs(UPLOAD_DIR, exist_ok=True)


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
# subprocesses (yt-dlp, demucs in the AI venv) look for ffmpeg in PATH - add it there
if os.path.dirname(FFMPEG):
    os.environ["PATH"] = (os.path.dirname(FFMPEG) + os.pathsep
                          + os.environ.get("PATH", ""))


def _venv_python(venv_dir: str) -> list[str]:
    """Candidate python paths inside a venv for Linux/macOS and Windows."""
    return [
        os.path.join(venv_dir, "bin", "python"),
        os.path.join(venv_dir, "Scripts", "python.exe"),
    ]


# Python from the venv with allin1 (torch+NATTEN). Priority: env variable ->
# local .venv-ai (created by ./setup-ai-allin1.sh) -> path on the dev server.
def _find_deep_py() -> str | None:
    cands = [
        os.environ.get("MUSSLOP_DEEP_PY"),
        *_venv_python(os.path.join(BASE_DIR, ".venv-ai")),
        "/workspace-SR008.fs2/mikheev-kandy/.envs/allin1/bin/python",
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
        os.environ.get("MUSSLOP_SONGFORMER_PY"),
        *_venv_python(os.path.join(BASE_DIR, ".venv-songformer")),
        "/workspace-SR008.fs2/mikheev-kandy/.envs/songformer/bin/python",
    ]
    src_cands = [
        os.environ.get("SONGFORMER_SRC"),
        os.path.join(BASE_DIR, ".venv-songformer", "src"),
        "/workspace-SR008.fs2/mikheev-kandy/.envs/songformer-src",
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

app = FastAPI(title="Musslop")



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


def _meta_path(track_id: str) -> str:
    return os.path.join(UPLOAD_DIR, f"{track_id}.json")


def _restore_tracks() -> None:
    """Restore the track registry after a server restart (from meta files)."""
    for fn in os.listdir(UPLOAD_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(UPLOAD_DIR, fn)) as f:
                meta = json.load(f)
            if os.path.exists(meta["orig"]) and os.path.exists(meta["wav"]):
                TRACKS[meta["id"]] = meta
        except Exception:
            pass


_restore_tracks()


def _to_wav(src: str, dst: str) -> None:
    """Transcode to WAV for librosa (the browser gets the original)."""
    subprocess.run(
        [FFMPEG, "-y", "-i", src, "-ac", "1", "-ar", "22050", dst],
        check=True, capture_output=True,
    )


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

    track_id = uuid.uuid4().hex[:12]
    orig_path = os.path.join(UPLOAD_DIR, f"{track_id}{ext}")
    with open(orig_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # WAV (mono/22050) - for server-side analysis only
    wav_path = os.path.join(UPLOAD_DIR, f"{track_id}.analysis.wav")
    if ext == ".wav":
        wav_path = orig_path
    else:
        try:
            _to_wav(orig_path, wav_path)
        except subprocess.CalledProcessError:
            os.remove(orig_path)
            raise HTTPException(400, "Failed to decode the file (ffmpeg)")

    meta = {"id": track_id, "orig": orig_path, "wav": wav_path,
            "name": file.filename, "mime": MIME.get(ext, "application/octet-stream"),
            "uploaded_at": int(__import__("time").time())}
    TRACKS[track_id] = meta
    with open(_meta_path(track_id), "w") as f:
        json.dump(meta, f)

    size = os.path.getsize(orig_path)
    log.info("upload: '%s' -> %s (%.1f MB)", file.filename, track_id, size / 1e6)
    return {"track_id": track_id, "name": file.filename, "size": size}


@app.get("/api/tracks")
def list_tracks():
    """History of uploaded tracks (for re-opening with saved markup)."""
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
    with open(_meta_path(track_id), "w") as f:
        json.dump(track, f)
    return {"ok": True, "favorite": track["favorite"]}


@app.post("/api/markup/{track_id}")
def save_markup(track_id: str, markup: dict = Body(...)):
    """Save markup (segments + downbeats/beats/tempo) next to the track."""
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    track["markup"] = markup
    with open(_meta_path(track_id), "w") as f:
        json.dump(track, f)
    return {"ok": True}


@app.get("/api/markup/{track_id}")
def get_markup(track_id: str):
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    return {"markup": track.get("markup")}


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
    for p in {track.get("orig"), track.get("wav"), _meta_path(track_id)}:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return {"ok": True}


# ----------------------------- Music folders --------------------------------
# The user points musslop at folders on disk; we list the audio files inside
# and import them on demand (copy into uploads/ + analysis WAV), remembering
# the source path so a file is imported only once.
FOLDERS_PATH = os.path.join(UPLOAD_DIR, "folders.json")


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
                           "your Terminal/Python to access this folder, then restart musslop" if mac else ""))
        parent = os.path.dirname(c.rstrip(os.sep)) or c
        if os.path.isdir(parent):
            try:
                os.listdir(parent)
            except PermissionError:
                return f"{parent} is not readable by the server (permissions)"
    host = platform.node()
    return (f"Tried: {'; '.join(tried[:4])}. Note: the path must exist on the computer where the "
            f"musslop server runs (this one is '{host}', {platform.system()}), not just in your browser.")


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
            result["path"] = filedialog.askdirectory(title="musslop — choose a music folder") or None
            root.destroy()
        except Exception as e:  # headless, no display, ...
            result["error"] = str(e)

    # tk must run on the main thread on macOS; uvicorn's worker thread is fine on
    # Linux/Windows, so try directly first and fall back to a subprocess.
    import platform
    if platform.system() == "Darwin":
        code = ("import tkinter as tk,sys\nfrom tkinter import filedialog\nr=tk.Tk();r.withdraw();"
                "r.attributes('-topmost',True);print(filedialog.askdirectory(title='musslop — choose a music folder') or '')")
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
    track_id = uuid.uuid4().hex[:12]
    orig_path = os.path.join(UPLOAD_DIR, f"{track_id}{ext}")
    shutil.copyfile(full, orig_path)
    wav_path = os.path.join(UPLOAD_DIR, f"{track_id}.analysis.wav")
    if ext == ".wav":
        wav_path = orig_path
    else:
        try:
            _to_wav(orig_path, wav_path)
        except subprocess.CalledProcessError:
            os.remove(orig_path)
            raise HTTPException(400, "Failed to decode the file (ffmpeg)")
    meta = {"id": track_id, "orig": orig_path, "wav": wav_path,
            "name": os.path.basename(full), "mime": MIME.get(ext, "application/octet-stream"),
            "uploaded_at": int(__import__("time").time()), "source": full}
    TRACKS[track_id] = meta
    with open(_meta_path(track_id), "w") as f:
        json.dump(meta, f)
    log.info("import from folder: '%s' -> %s", full, track_id)
    return {"track_id": track_id, "name": meta["name"], "existing": False}


# ----------------------------- Projects (set lists) -------------------------
# A project groups tracks (and their scenes) for one session / level:
#   {"id", "name", "created_at", "updated_at",
#    "groups": [{"id", "name", "items": [{"id", "track_id", "scene_id"|null, "note"}]}]}
# Stored as uploads/projects/{id}.json; the markup (segments + scenes) stays
# with the track, the project only references it.
PROJECTS_DIR = os.path.join(UPLOAD_DIR, "projects")
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
        result = analyze(track["wav"], n_segments=n_segments,
                         beats_per_bar=beats_per_bar)
    log.info("analyze: %s done in %.1fs, %d parts, tempo %.1f, meter %s/%s",
             track_id, _time.time() - _t0, len(result["segments"]),
             result.get("tempo") or 0, result.get("beats_per_bar"),
             (result.get("meter") or {}).get("source"))

    result["track_id"] = track_id
    result["name"] = track["name"]
    return result


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
                tail = (proc.stderr or "")[-800:]
                raise HTTPException(500, f"AI analysis failed: {tail}")
            with open(out_json) as f:
                cached = json.load(f)
        finally:
            if os.path.exists(out_json):
                os.remove(out_json)
        track[cache_key] = cached
        with open(_meta_path(track["id"]), "w") as f:
            json.dump(track, f)
    return cached


def _deep_analyze(track: dict, beats_per_bar: int | None = None) -> dict:
    """allin1 in a separate venv (subprocess); the result is cached in meta."""
    cached = _run_engine(track, "deep_raw", "deep_analyze.py", DEEP_PY)
    result = analyze_deep_merge(track["wav"], cached, beats_per_bar)
    result["engine"] = "deep"
    return result


def _songformer_analyze(track: dict, beats_per_bar: int | None = None) -> dict:
    """SongFormer + Beat This! in its own venv; merged with our loop metrics."""
    cached = _run_engine(track, "songformer_raw", "songformer_analyze.py",
                         SONGFORMER_PY, {"SONGFORMER_SRC": SONGFORMER_SRC})
    result = analyze_deep_merge(track["wav"], cached, beats_per_bar)
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
            safe = os.path.join(UPLOAD_DIR, f"{track_id}.playback.wav")
            try:
                subprocess.run(
                    [FFMPEG, "-y", "-vn", "-i", track["orig"],
                     "-ac", "2", "-ar", "44100", "-acodec", "pcm_s16le", safe],
                    check=True, capture_output=True,
                )
            except subprocess.CalledProcessError:
                raise HTTPException(500, "Transcode failed (ffmpeg)")
            track["safe_wav"] = safe
            with open(_meta_path(track_id), "w") as f:
                json.dump(track, f)
        return FileResponse(safe, media_type="audio/wav")
    return FileResponse(track["orig"], media_type=track["mime"])


@app.post("/api/loopability/{track_id}")
def loopability(track_id: str, segments: list[dict] = Body(...)):
    """Recompute loop quality for manually edited boundaries."""
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    return {"loopability": loop_quality(track["wav"], segments)}


STEM_NAMES = ("drums", "bass", "other", "vocals")
STEM_JOBS: dict[str, dict] = {}  # track_id -> {"status", "progress", "error"}


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
                buf = ""
            else:
                buf += ch
        proc.wait(timeout=3600)
        if proc.returncode != 0 or not _stems_done(stem_dir):
            job["status"] = "error"
            job["error"] = "Separation failed (see server.log)"
            return
        track = TRACKS.get(track_id)
        if track is not None:
            track["stems"] = stem_dir
            with open(_meta_path(track_id), "w") as f:
                json.dump(track, f)
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

    stem_dir = os.path.join(UPLOAD_DIR, f"{track_id}.stems")
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
    stem_dir = os.path.join(UPLOAD_DIR, f"{track_id}.stems")
    if _stems_done(stem_dir):
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
    path = os.path.join(UPLOAD_DIR, f"{track_id}.stems", f"{stem}.wav")
    if not os.path.exists(path):
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
    out_tpl = os.path.join(UPLOAD_DIR, f"{track_id}.%(ext)s")

    # stream yt-dlp output to the log line by line ([download] xx.x% of ...)
    proc = subprocess.Popen(
        ytdlp + ["-x", "--audio-format", "mp3",
         "--audio-quality", "0", "--no-playlist", "--max-filesize", "200M",
         "--newline", "--force-overwrites",
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
        if "ssl" in low or "unable to download" in low or "timed out" in low:
            hint = ("The server appears to have no access to YouTube (network/firewall). "
                    "Run musslop locally on a machine with internet access. ")
        elif "sign in" in low or "age" in low:
            hint = "The video requires sign-in / age confirmation. "
        elif "yt-dlp -u" in low or "extract" in low:
            hint = "Try updating yt-dlp: pip install -U yt-dlp. "
        else:
            hint = ""
        log.error("import_url: FAILED\n%s", "\n".join(lines[-10:]))
        raise HTTPException(400, f"Download failed. {hint}Details: {tail[-300:]}")

    # --print outputs the title and path; filter out service [lines]
    printed = [l for l in lines if not l.startswith("[")]
    title = printed[0] if printed else "track"
    orig_path = os.path.join(UPLOAD_DIR, f"{track_id}.mp3")
    if not os.path.exists(orig_path):
        raise HTTPException(500, "File did not appear after download")
    log.info("import_url: downloaded '%s' -> %s (%.1f MB)", title, orig_path,
             os.path.getsize(orig_path) / 1e6)

    wav_path = os.path.join(UPLOAD_DIR, f"{track_id}.analysis.wav")
    try:
        _to_wav(orig_path, wav_path)
    except subprocess.CalledProcessError:
        os.remove(orig_path)
        raise HTTPException(400, "Failed to decode the downloaded audio")

    meta = {"id": track_id, "orig": orig_path, "wav": wav_path,
            "name": f"{title}.mp3", "mime": "audio/mpeg",
            "uploaded_at": int(__import__("time").time())}
    TRACKS[track_id] = meta
    with open(_meta_path(track_id), "w") as f:
        json.dump(meta, f)
    return {"track_id": track_id, "name": meta["name"]}


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
            "format": "musslop-loops",
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
            # <Foundry Data>/musslop/<slug>/ (what the musslop-foundry module
            # does on "Import ZIP").
            root = f"musslop/{slug}"
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
                    "flags": {"musslop": {"loop_index": L["index"], "stinger": L.get("stinger"),
                                          "tail_file": (f"{root}/{L['tail_file']}" if L.get("tail_file") else None)}},
                }
            playlists = [{
                "name": base,
                "description": "Exported from musslop — each sound is a seamless loop. "
                               "Loops marked repeat=false are build-ups (play once).",
                "mode": -1,            # DISABLED: GM picks a sound, it loops on its own
                "channel": "music",
                "playing": False,
                "fade": fade_ms,
                "sorting": "m",
                "sounds": [sound(i, L) for i, L in enumerate(manifest_loops)],
                "flags": {"musslop": {"manifest": f"{root}/manifest.json", "slug": slug, "bpm": tempo}},
            }]
            if manifest["scenes"]:
                by_index = {L["index"]: L for L in manifest_loops}
                playlists.append({
                    "name": f"{base} — scenes",
                    "description": "musslop scenes: one entry per scene, pointing at that scene's loop.",
                    "mode": -1, "channel": "music", "playing": False, "fade": fade_ms, "sorting": "m",
                    "sounds": [sound(i, by_index[sc["loop_index"]], sc["name"])
                               for i, sc in enumerate(manifest["scenes"]) if sc.get("loop_index") in by_index],
                    "flags": {"musslop": {"manifest": f"{root}/manifest.json", "slug": slug}},
                })
            # Foundry's "Import Data" takes ONE document per file
            zf.writestr("foundry-playlist.json", json.dumps(playlists[0], indent=2, ensure_ascii=False))
            if len(playlists) > 1:
                zf.writestr("foundry-playlist-scenes.json", json.dumps(playlists[1], indent=2, ensure_ascii=False))
            zf.writestr("README-foundry.txt", (
                "musslop -> Foundry VTT\n"
                "======================\n"
                f"1. Unpack this zip into <your Foundry user data>/Data/musslop/{slug}/\n"
                "   (so that the .wav files sit next to this README).\n"
                "   Tip: with the musslop-foundry module installed, use its 'Import ZIP' button instead.\n"
                "2. Without the module: in Foundry, Playlists sidebar -> right-click -> Import Data ->\n"
                "   choose foundry-playlist.json (and foundry-playlist-scenes.json if present).\n"
                "   Each sound is a seamless loop; click one to play it, click another to switch\n"
                "   (Foundry crossfades by the playlist 'fade' setting).\n"
                "3. With the module: open the musslop desk (music-note button in the scene controls),\n"
                "   pick this track -> transitions land on loop/phrase boundaries, scenes get hotkeys,\n"
                "   and every connected player hears the same thing.\n"
            ))
    buf.seek(0)

    base = os.path.splitext(track["name"] or "loops")[0]
    suffix = "_foundry" if target == "foundry" else "_loops"
    from fastapi.responses import Response
    return Response(
        buf.read(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{base}{suffix}.zip"'},
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


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
