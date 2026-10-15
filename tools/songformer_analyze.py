#!/usr/bin/env python3
"""Runner for SongFormer + Beat This! in a separate venv.

Called from backend/main.py as a subprocess:
    .venv-songformer/bin/python tools/songformer_analyze.py input.mp3 output.json

Requires the SONGFORMER_SRC variable - path to a clone of the SongFormer
repository (its infer code is not packaged in pip). setup-ai-songformer.sh
puts the clone into .venv-songformer/src and sets everything up itself.

Output - JSON with bpm, beats, downbeats, segments (start/end/label).
"""
import json
import os
import subprocess
import sys
import tempfile


def pick_device() -> str:
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _ensure_beatthis_ckpt() -> None:
    """Pre-fetch the Beat This! checkpoint into the torch hub cache.

    beat_this downloads it via urllib on first use; on stock macOS Python
    that often dies with SSL: CERTIFICATE_VERIFY_FAILED (no system certs),
    and beat_this swallows the real error into a generic ValueError
    ('Could not load the checkpoint ... final0'). Download it ourselves
    with certifi and put it exactly where beat_this looks for it."""
    import torch.hub
    ckpt_dir = os.path.join(torch.hub.get_dir(), "checkpoints")
    dst = os.path.join(ckpt_dir, "beat_this-final0.ckpt")
    if os.path.exists(dst):
        return
    os.makedirs(ckpt_dir, exist_ok=True)
    url = "https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt"
    print("downloading Beat This! checkpoint (~80 MB, one time)...", file=sys.stderr)
    import ssl
    import urllib.request
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    tmp_path = dst + ".part"
    with urllib.request.urlopen(url, context=ctx, timeout=120) as r, \
         open(tmp_path, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp_path, dst)


def run_beats(audio: str, device: str):
    """Beat This!: beats and downbeats; tempo - from inter-beat intervals."""
    import numpy as np
    from beat_this.inference import File2Beats
    try:
        _ensure_beatthis_ckpt()
    except Exception as e:
        print(f"checkpoint pre-fetch failed ({e}); "
              "letting beat_this try its own download", file=sys.stderr)
    # dbn=False - without madmom
    dev = device if device != "mps" else "cpu"  # beat_this can have issues with mps
    f2b = File2Beats(checkpoint_path="final0", device=dev, dbn=False)
    beats, downbeats = f2b(audio)
    beats = [float(b) for b in beats]
    downbeats = [float(b) for b in downbeats]
    if len(beats) > 3:
        bpm = float(60.0 / np.median(np.diff(beats)))
    else:
        bpm = 120.0
    return bpm, beats, downbeats


def run_beats_fallback(audio: str):
    """librosa beat tracking when Beat This! is unavailable (e.g. its
    checkpoint could not be downloaded): worse downbeats, but the whole
    analysis should not die because of the beat model."""
    import librosa
    import numpy as np
    y, sr = librosa.load(audio, mono=True)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, trim=False)
    beats = [float(t) for t in librosa.frames_to_time(beat_frames, sr=sr)]
    bpm = float(np.atleast_1d(tempo)[0]) if np.size(tempo) else 120.0
    # placeholder 4/4 grid: the server re-estimates the meter from audio
    # when it sees "downbeats_assumed" (backend/analysis.py)
    downbeats = beats[::4]
    return bpm, beats, downbeats


def _patched_infer(sf_dir: str) -> str:
    """The official infer.py hardcodes device = f"cuda:{rank}" — on Macs that
    silently means CPU-only (minutes instead of seconds). Write a patched copy
    next to it that picks cuda -> mps -> cpu (or MUSLOOP_SF_DEVICE)."""
    src_path = os.path.join(sf_dir, "infer", "infer.py")
    with open(src_path) as f:
        src = f.read()
    patched = src.replace(
        'device = f"cuda:{rank}"',
        'device = os.environ.get("MUSLOOP_SF_DEVICE") or ('
        'f"cuda:{rank}" if torch.cuda.is_available() else '
        '"mps" if getattr(torch.backends, "mps", None) '
        'and torch.backends.mps.is_available() else "cpu")',
    )
    out_path = os.path.join(sf_dir, "infer", "infer_musloop.py")
    # rewrite only when stale so repeated runs stay cheap
    try:
        with open(out_path) as f:
            if f.read() == patched:
                return out_path
    except OSError:
        pass
    with open(out_path, "w") as f:
        f.write(patched)
    return out_path


def run_structure(audio: str, src_dir: str, py: str):
    """SongFormer via a device-patched copy of the official infer.py.

    --debug runs inference inline (single process): with one file the
    multi-GPU queue machinery is pure overhead, and a crashed worker in the
    queue mode hangs the parent until the timeout instead of failing fast."""
    sf_dir = os.path.join(src_dir, "src", "SongFormer")
    infer_py = _patched_infer(sf_dir)
    with tempfile.TemporaryDirectory() as tmp:
        scp = os.path.join(tmp, "in.scp")
        with open(scp, "w") as f:
            f.write(os.path.abspath(audio) + "\n")
        out_dir = os.path.join(tmp, "out")
        os.makedirs(out_dir, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([
            os.path.join(src_dir, "src", "third_party"),
            sf_dir,
            env.get("PYTHONPATH", ""),
        ]).rstrip(os.pathsep)
        # some MusicFM/MuQ ops are not implemented on MPS — let torch fall
        # back to CPU per-op instead of crashing the run
        env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        proc = subprocess.run(
            [py, infer_py,
             "-i", scp, "-o", out_dir,
             "--model", "SongFormer",
             "--checkpoint", "SongFormer.safetensors",
             "--config_path", "SongFormer.yaml",
             "-gn", "1", "-tn", "1", "--debug"],
            cwd=sf_dir, env=env, capture_output=True, text=True, timeout=1800,
        )
        outs = [f for f in os.listdir(out_dir) if f.endswith(".json")]
        if not outs:
            sys.stderr.write(proc.stderr[-1500:] if proc.stderr else "no output")
            raise RuntimeError("SongFormer produced no output")
        with open(os.path.join(out_dir, outs[0])) as f:
            return json.load(f)


def main() -> None:
    audio, out_path = sys.argv[1], sys.argv[2]
    src_dir = os.environ.get("SONGFORMER_SRC")
    if not src_dir or not os.path.isdir(src_dir):
        sys.exit("SONGFORMER_SRC is not set or missing")

    device = pick_device()
    print(f"device: {device}", file=sys.stderr)

    raw_segments = run_structure(audio, src_dir, sys.executable)
    downbeats_assumed = False
    try:
        bpm, beats, downbeats = run_beats(audio, device)
    except Exception as e:
        print(f"Beat This! failed ({e}); falling back to librosa beats",
              file=sys.stderr)
        bpm, beats, downbeats = run_beats_fallback(audio)
        downbeats_assumed = True

    # SongFormer splits sections into same-labeled subsections (by phrase) -
    # merge adjacent same-named ones: loops need large parts
    merged = []
    for s in raw_segments:
        if merged and merged[-1]["label"] == s["label"]:
            merged[-1]["end"] = float(s["end"])
        else:
            merged.append({"label": str(s["label"]),
                           "start": float(s["start"]), "end": float(s["end"])})

    data = {
        "bpm": bpm,
        "beats": beats,
        "downbeats": downbeats,
        "downbeats_assumed": downbeats_assumed,
        "segments": merged,
        "segments_raw": [
            {"label": str(s["label"]), "start": float(s["start"]),
             "end": float(s["end"])} for s in raw_segments
        ],
    }
    with open(out_path, "w") as f:
        json.dump(data, f)
    print("OK")


if __name__ == "__main__":
    main()
