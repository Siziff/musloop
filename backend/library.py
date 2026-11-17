"""Track storage: one folder per track in the library, named like the track.

    library/
      Village Consort/
        Village Consort.mp3     the original audio (what the browser plays)
        track.json              name, markup, scenes, AI results (paths relative)
        cache/analysis.wav      mono 22 kHz for the analysis (re-created if missing)
        cache/playback.wav      only for files the browser can't decode
        stems/drums.wav ...     layers (if made)
      .musloop/                 app state: projects, music folders, temp files

A track folder can be copied to another computer (cache/ can be left out).
Audio files dropped into the library root, or folders with an audio file and
no track.json, become tracks on the next library scan.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid

log = logging.getLogger("musloop")

AUDIO_EXT = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".webm"}
MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".flac": "audio/flac", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".opus": "audio/ogg", ".webm": "audio/webm",
}
META = "track.json"
INCOMING = ".incoming"   # marker while musloop is still putting files in a new folder
PATH_KEYS = ("orig", "wav", "safe_wav", "stems")
LOCK = threading.RLock()
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def fs_name(name: str) -> str:
    """Folder / file name for a track name: the name without the audio
    extension, without characters Windows or macOS refuse, no leading dot
    (hidden), at most 100 characters."""
    name = str(name or "")
    base, ext = os.path.splitext(name)
    if ext.lower() not in AUDIO_EXT:
        base = name
    s = unicodedata.normalize("NFC", base)
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', " ", s)
    s = re.sub(r"\s+", " ", s).strip().lstrip(". ").rstrip(". ")
    s = s[:100].rstrip(". ")
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s += "_"
    return s or "track"


def _free_path(root: str, base: str, current: str | None = None) -> str:
    """root/base, or root/base (2), (3)... if that name is taken. The track's
    own current folder counts as free (renaming to the same name / case)."""
    try:
        taken = {n.lower() for n in os.listdir(root)}
    except OSError:
        taken = set()
    cur = os.path.basename(current).lower() if current else None
    for i in range(1, 10000):
        cand = base if i == 1 else f"{base} ({i})"
        if cand.lower() == cur:
            return os.path.join(root, cand)
        if cand.lower() not in taken:
            return os.path.join(root, cand)
    raise RuntimeError("no free folder name")


def new_dir(root: str, name: str, incoming: bool = False) -> str:
    """Create the folder for a new track (unique name). incoming=True marks
    it as being filled, so a library scan doesn't take it for a hand-added
    folder (see done_incoming)."""
    with LOCK:
        for _ in range(100):
            path = _free_path(root, fs_name(name))
            try:
                os.mkdir(path)
            except FileExistsError:
                continue
            if incoming:
                open(os.path.join(path, INCOMING), "w").close()
            return path
    raise RuntimeError("could not create a track folder")


def done_incoming(d: str) -> None:
    try:
        os.remove(os.path.join(d, INCOMING))
    except OSError:
        pass


def _being_filled(d: str) -> bool:
    """A folder musloop is still writing (marker younger than 10 minutes;
    an older one is left over from a crash and ignored)."""
    p = os.path.join(d, INCOMING)
    try:
        return time.time() - os.path.getmtime(p) < 600
    except OSError:
        return False


def audio_path(d: str, name: str, ext: str) -> str:
    return os.path.join(d, fs_name(name) + ext)


def default_wav(track: dict) -> str:
    if os.path.splitext(track["orig"])[1].lower() == ".wav":
        return track["orig"]
    return os.path.join(track["dir"], "cache", "analysis.wav")


def save(track: dict) -> None:
    """Write track.json atomically, paths relative to the track folder."""
    d = track["dir"]
    out = {k: v for k, v in track.items() if k != "dir"}
    for k in PATH_KEYS:
        v = out.get(k)
        if isinstance(v, str) and v and os.path.isabs(v):
            try:
                rel = os.path.relpath(v, d)
            except ValueError:   # another drive (Windows)
                continue
            if not rel.startswith(".."):
                out[k] = rel.replace(os.sep, "/")
    tmp = os.path.join(d, f".{META}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(d, META))


def _audio_files(d: str) -> list[str]:
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    return [os.path.join(d, n) for n in names
            if not n.startswith(".") and os.path.splitext(n)[1].lower() in AUDIO_EXT
            and os.path.isfile(os.path.join(d, n))]


def _new_meta(d: str, orig: str, name: str) -> dict:
    ext = os.path.splitext(orig)[1].lower()
    try:
        mtime = int(os.path.getmtime(orig))
    except OSError:
        mtime = int(time.time())
    meta = {"id": uuid.uuid4().hex[:12], "orig": orig, "name": name,
            "mime": MIME.get(ext, "application/octet-stream"), "uploaded_at": mtime, "dir": d}
    meta["wav"] = default_wav(meta)
    return meta


def load_dir(d: str) -> dict | None:
    """Read a track folder. Without track.json: the first audio file in it
    becomes a new track (named after the folder)."""
    p = os.path.join(d, META)
    meta = None
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as e:  # noqa: BLE001
            log.warning("library: can't read %s (%s), skipped", p, e)
            return None
        if not isinstance(meta, dict):
            return None
    if meta is None:
        files = _audio_files(d)
        if not files:
            return None
        meta = _new_meta(d, files[0], os.path.basename(d) + os.path.splitext(files[0])[1])
        save(meta)
        log.info("library: new track from folder '%s'", os.path.basename(d))
        return meta
    meta["dir"] = d
    for k in PATH_KEYS:
        v = meta.get(k)
        if not isinstance(v, str) or not v:
            continue
        if not os.path.isabs(v):
            meta[k] = os.path.normpath(os.path.join(d, v))
        elif not os.path.exists(v) and os.path.exists(os.path.join(d, os.path.basename(v))):
            meta[k] = os.path.join(d, os.path.basename(v))
    changed = False
    if not meta.get("orig") or not os.path.exists(meta["orig"]):
        files = _audio_files(d)
        if not files:
            log.warning("library: no audio file in '%s', skipped", os.path.basename(d))
            return None
        meta["orig"] = files[0]
        changed = True
    if not meta.get("id"):
        meta["id"] = uuid.uuid4().hex[:12]
        changed = True
    if not meta.get("name"):
        meta["name"] = os.path.basename(d) + os.path.splitext(meta["orig"])[1]
        changed = True
    ext = os.path.splitext(meta["orig"])[1].lower()
    meta.setdefault("mime", MIME.get(ext, "application/octet-stream"))
    meta.setdefault("uploaded_at", int(os.path.getmtime(meta["orig"])))
    if not meta.get("wav") or not os.path.exists(meta["wav"]):
        meta["wav"] = default_wav(meta)   # re-created on first use
    if meta.get("stems") and not os.path.isdir(meta["stems"]):
        meta.pop("stems", None)
    if changed:
        save(meta)
    return meta


def sync(tracks: dict, root: str) -> None:
    """Bring the registry in line with the library folder: new folders and
    loose audio files become tracks, moved folders are followed, deleted ones
    disappear. Folders musloop already knows are not re-read."""
    with LOCK:
        by_dir = {os.path.normcase(t["dir"]): tid for tid, t in tracks.items()}
        seen: set[str] = set()
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
        except OSError:
            return
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_file() and os.path.splitext(e.name)[1].lower() in AUDIO_EXT:
                    # an audio file dropped into the library root: own folder
                    d = new_dir(root, e.name)
                    dst = audio_path(d, e.name, os.path.splitext(e.name)[1])
                    shutil.move(e.path, dst)
                    meta = _new_meta(d, dst, e.name)
                    save(meta)
                    tracks[meta["id"]] = meta
                    seen.add(meta["id"])
                    log.info("library: new track '%s'", e.name)
                    continue
                if not e.is_dir():
                    continue
            except OSError as ex:
                log.warning("library: %s: %s", e.name, ex)
                continue
            known = by_dir.get(os.path.normcase(e.path))
            if known and os.path.exists(os.path.join(e.path, META)):
                seen.add(known)
                continue
            if _being_filled(e.path):
                continue
            meta = load_dir(e.path)
            if not meta:
                continue
            tid = meta["id"]
            other = tracks.get(tid)
            if tid in seen or (other and os.path.normcase(other["dir"]) != os.path.normcase(e.path)
                               and os.path.isdir(other["dir"])):
                # a copied folder: the copy becomes a separate track
                meta["id"] = uuid.uuid4().hex[:12]
                save(meta)
                tid = meta["id"]
            tracks[tid] = meta
            seen.add(tid)
        for tid in [t for t in tracks if t not in seen]:
            if not os.path.isdir(tracks[tid]["dir"]):
                log.info("library: '%s' is gone from the library folder", tracks[tid].get("name"))
                tracks.pop(tid, None)


def rename_dir(track: dict, root: str) -> None:
    """Rename the track folder and its audio file after the track name.
    Best effort: if the OS refuses (file in use on Windows), the old folder
    name stays and only the name in the library changes."""
    with LOCK:
        d = track["dir"]
        target = _free_path(root, fs_name(track["name"]), current=d)
        try:
            if target != d:
                os.rename(d, target)
                for k in PATH_KEYS:
                    v = track.get(k)
                    if isinstance(v, str) and v.startswith(d + os.sep):
                        track[k] = target + v[len(d):]
                track["dir"] = target
            old = track["orig"]
            new = audio_path(target, track["name"], os.path.splitext(old)[1])
            if os.path.normcase(new) != os.path.normcase(old) or os.path.basename(new) != os.path.basename(old):
                if os.path.exists(new) and os.path.normcase(new) != os.path.normcase(old):
                    return
                os.rename(old, new)
                if track.get("wav") == old:
                    track["wav"] = new
                if track.get("safe_wav") == old:
                    track["safe_wav"] = new
                track["orig"] = new
        except OSError as e:
            log.warning("library: couldn't rename the folder of '%s' (%s); the old folder name stays",
                        track.get("name"), e)


def _inside(p: str, d: str) -> bool:
    return os.path.normcase(os.path.abspath(p)).startswith(os.path.normcase(os.path.abspath(d)) + os.sep)


def delete(track: dict) -> None:
    """Remove the track's own files (audio, track.json, cache, stems) and the
    folder if nothing else is left in it."""
    with LOCK:
        d = track["dir"]
        for p in {track.get("orig"), track.get("wav"), track.get("safe_wav"), os.path.join(d, META)}:
            if p and os.path.isfile(p) and _inside(p, d):
                try:
                    os.remove(p)
                except OSError:
                    pass
        for sub in ("cache", "stems"):
            shutil.rmtree(os.path.join(d, sub), ignore_errors=True)
        try:
            os.rmdir(d)
        except OSError:
            if os.path.isdir(d):
                log.info("library: kept folder '%s' (it has other files)", os.path.basename(d))


def _ids_in(root: str) -> dict[str, str]:
    """track id -> folder, from the track.json files in the library."""
    out: dict[str, str] = {}
    try:
        entries = list(os.scandir(root))
    except OSError:
        return out
    for e in entries:
        if e.name.startswith(".") or not e.is_dir():
            continue
        try:
            with open(os.path.join(e.path, META), encoding="utf-8") as f:
                tid = json.load(f).get("id")
            if tid:
                out[tid] = e.path
        except Exception:  # noqa: BLE001
            continue
    return out


def migrate_legacy(legacy: str, root: str, state: str) -> None:
    """Move tracks from the old flat folder (uploads/<id>.mp3 + <id>.json)
    into track folders. Files are moved, not copied; the old json files are
    kept in <legacy>/.migrated/ as a backup. An interrupted run continues
    where it stopped (the folder is marked with the track id first)."""
    if not legacy or not os.path.isdir(legacy):
        return
    if os.path.normcase(os.path.abspath(legacy)) == os.path.normcase(os.path.abspath(root)):
        return
    names = [n for n in sorted(os.listdir(legacy)) if n.endswith(".json") and n != "folders.json"]
    backup = os.path.join(legacy, ".migrated")
    ids = _ids_in(root) if names else {}
    moved = 0

    def find(v):
        if not isinstance(v, str) or not v:
            return None
        if os.path.exists(v) and _inside(v, legacy):
            return v
        c = os.path.join(legacy, os.path.basename(v))
        return c if os.path.exists(c) else None

    def move_into(src, dst):
        if src and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        return dst if os.path.exists(dst) else None

    for fn in names:
        p = os.path.join(legacy, fn)
        try:
            with open(p, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(meta, dict) or not meta.get("id") or "orig" not in meta:
            continue
        tid = meta["id"]
        src = find(meta.get("orig"))
        d = ids.get(tid)
        if not src and not d:
            log.warning("library: old track '%s' (%s) has no audio file, not moved", meta.get("name"), tid)
            continue
        try:
            name = meta.get("name") or os.path.basename(meta["orig"])
            ext = os.path.splitext(meta["orig"])[1].lower() or ".mp3"
            base = {k: v for k, v in meta.items() if k not in PATH_KEYS}
            if not d:
                d = new_dir(root, name)
                ids[tid] = d
                stub = {**base, "dir": d, "orig": audio_path(d, name, ext)}
                stub["wav"] = default_wav(stub)
                save(stub)      # the folder now belongs to this track id
            new = {**base, "dir": d}
            same_wav = os.path.basename(str(meta.get("wav") or "")) == os.path.basename(meta["orig"])
            orig = move_into(src, audio_path(d, name, ext))
            if not orig:
                files = _audio_files(d)
                if not files:
                    raise RuntimeError("audio file missing")
                orig = files[0]
            new["orig"] = orig
            if same_wav:
                new["wav"] = orig
            else:
                w = move_into(find(meta.get("wav")) or find(os.path.join(legacy, f"{tid}.analysis.wav")),
                              os.path.join(d, "cache", "analysis.wav"))
                new["wav"] = w or default_wav(new)
            sw = move_into(find(meta.get("safe_wav")) or find(os.path.join(legacy, f"{tid}.playback.wav")),
                           os.path.join(d, "cache", "playback.wav"))
            if sw:
                new["safe_wav"] = sw
            st = move_into(find(meta.get("stems")) or find(os.path.join(legacy, f"{tid}.stems")),
                           os.path.join(d, "stems"))
            if st:
                new["stems"] = st
            save(new)
            os.makedirs(backup, exist_ok=True)
            os.replace(p, os.path.join(backup, fn))
            moved += 1
        except Exception as e:  # noqa: BLE001 - one bad track must not stop the rest
            log.warning("library: moving '%s' (%s) failed: %s", meta.get("name"), tid, e)
    # app state: projects and the list of music folders
    src = os.path.join(legacy, "projects")
    if os.path.isdir(src):
        dst_dir = os.path.join(state, "projects")
        os.makedirs(dst_dir, exist_ok=True)
        for n in os.listdir(src):
            if not os.path.exists(os.path.join(dst_dir, n)):
                shutil.move(os.path.join(src, n), os.path.join(dst_dir, n))
        try:
            os.rmdir(src)
        except OSError:
            pass
    src = os.path.join(legacy, "folders.json")
    if os.path.isfile(src) and not os.path.exists(os.path.join(state, "folders.json")):
        shutil.move(src, os.path.join(state, "folders.json"))
    if moved:
        log.info("library: moved %d tracks from %s to %s (old metadata kept in %s; "
                 "the old folder can be deleted)", moved, legacy, root, backup)
