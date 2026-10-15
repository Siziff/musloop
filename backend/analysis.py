# Musloop - audio analysis: beats, bars, structural segmentation.
#
# Theory the segmentation is based on:
#  1. Beat tracking: onset envelope + dynamic programming (Ellis, 2007).
#  2. Downbeats: assume 4/4 meter; the downbeat phase is chosen as the
#     shift (0..3) maximizing the mean onset strength on every 4th beat.
#  3. Structural segmentation (Foote, 2000): beat-synchronous features
#     (CQT chroma - harmony, MFCC - timbre) -> self-similarity matrix ->
#     convolution with a "checkerboard" kernel along the diagonal ->
#     novelty curve -> peaks = section boundaries (intro/verse/chorus/bridge...).
#  4. Musical quantization: boundaries are snapped to the nearest downbeat,
#     minimum section length - 4 bars (a typical musical phrase).

from __future__ import annotations

import numpy as np
import scipy.signal
import scipy.ndimage
import librosa

SR = 22050
HOP = 512

# Boundary scoring weights (tuned on manual markups; the tuning script lives in the dev branch)
BOUNDARY_WEIGHTS = {
    "loop_q": 0.30,   # loop closure quality left/right of the boundary
    "phrase": 0.20,   # part lengths are multiples of 4/8 bars
    "audib": 0.20,    # transition audibility (onset + RMS jump)
    "novelty": 0.30,  # structural novelty (SSM)
    "dist_pen": 0.08, # penalty per bar of shift from the novelty peak
}


def _checkerboard_kernel(size: int) -> np.ndarray:
    """Gaussian-weighted Foote checkerboard kernel (size - half the side)."""
    n = 2 * size
    g = scipy.signal.windows.gaussian(n, std=size / 2.0)
    kernel = np.outer(g, g)
    sign = np.ones((n, n))
    sign[:size, size:] = -1
    sign[size:, :size] = -1
    return kernel * sign


def _novelty_from_ssm(ssm: np.ndarray, kernel_size: int) -> np.ndarray:
    """Novelty curve: convolve the SSM with a checkerboard kernel along
    the main diagonal."""
    n = ssm.shape[0]
    ks = min(kernel_size, max(4, n // 4))
    kernel = _checkerboard_kernel(ks)
    pad = ks
    padded = np.pad(ssm, pad, mode="edge")
    novelty = np.zeros(n)
    for i in range(n):
        window = padded[i : i + 2 * ks, i : i + 2 * ks]
        novelty[i] = np.sum(window * kernel)
    novelty = np.maximum(novelty, 0.0)
    if novelty.max() > 0:
        novelty /= novelty.max()
    return novelty


def _estimate_downbeat_phase(onset_env: np.ndarray, beat_frames: np.ndarray,
                             beats_per_bar: int = 4) -> int:
    """Downbeat phase: the shift where onsets on beats are strongest."""
    if len(beat_frames) < beats_per_bar:
        return 0
    strengths = onset_env[np.clip(beat_frames, 0, len(onset_env) - 1)]
    scores = [strengths[p::beats_per_bar].mean() for p in range(beats_per_bar)]
    return int(np.argmax(scores))


# --- Meter (time signature) ---------------------------------------------------
# Supported bar lengths in beats. Auto-detection decides between duple (4) and
# triple (3) grouping: 2/4 is folded into 4 (two bars of 2/4 loop the same way
# as one of 4/4) and 6/8 counted in eighths shows up as 3 (or as 2 when the
# beat tracker locks onto dotted quarters). 5 and 7 are manual-only.
METERS = (2, 3, 4, 5, 6, 7)
AUTO_METERS = (3, 4)


def _zs(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    s = float(x.std())
    return (x - float(x.mean())) / s if s > 1e-9 else np.zeros_like(x)


def beat_accents(y: np.ndarray, sr: int, beat_frames: np.ndarray,
                 onset_env: np.ndarray | None = None) -> np.ndarray:
    """Per-beat "downbeat-ness" (z-scored), the evidence for bar starts:
      * low-band onset (kick / bass notes usually land on the one),
      * full-band onset strength (general accent),
      * harmonic change (chords tend to change at bar lines).
    Also used by the UI to pick the phase when the user changes the meter."""
    n = len(beat_frames)
    if n < 2:
        return np.zeros(n)
    if onset_env is None:
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    mel = librosa.feature.melspectrogram(y=y, sr=sr, hop_length=HOP, n_mels=64, fmax=8000)
    low = librosa.onset.onset_strength(S=librosa.power_to_db(mel[:6]), sr=sr)  # < ~200 Hz
    L = min(len(onset_env), len(low))

    def at(env, fr):
        # max in a +-2 frame window: beat trackers are a few ms off
        fr = np.clip(fr, 0, L - 1)
        return np.array([env[max(0, f - 2):f + 3].max() for f in fr])

    acc = at(onset_env[:L], beat_frames)
    bass = at(low[:L], beat_frames)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP)
    fr = librosa.util.fix_frames(beat_frames, x_min=0, x_max=chroma.shape[1] - 1)
    cs = librosa.util.sync(chroma, fr, aggregate=np.median)
    cs = cs / np.maximum(np.linalg.norm(cs, axis=0, keepdims=True), 1e-8)
    # fix_frames prepends frame 0: column k+1 starts at beat k
    seg = cs[:, 1:n + 1] if cs.shape[1] >= n + 1 else cs[:, -n:]
    prev = np.concatenate([seg[:, :1], seg[:, :-1]], axis=1)
    hchange = 1.0 - np.sum(seg * prev, axis=0)
    hchange = np.pad(hchange, (0, max(0, n - len(hchange))))[:n]
    return _zs(1.0 * _zs(bass) + 0.6 * _zs(acc) + 0.8 * _zs(hchange))


def best_phase(accents: np.ndarray, beats_per_bar: int) -> int:
    """Phase (0..bpb-1) whose beats carry the strongest accents."""
    if len(accents) < beats_per_bar:
        return 0
    return int(np.argmax([accents[p::beats_per_bar].mean() for p in range(beats_per_bar)]))


def estimate_meter(accents: np.ndarray, feats: np.ndarray | None = None) -> dict:
    """Heuristic time-signature estimate (3 vs 4 beats per bar).

    Two independent cues, both in "beats":
      1. Accent pattern: how well the per-beat accents separate into one
         strong beat per bar for period M (contrast of the best phase,
         t-statistic so that periods with fewer "on" beats aren't favoured).
      2. Repetition: self-similarity of beat-synchronous chroma/timbre at
         lags that are multiples of 3 but not 4 (3, 9, 15) vs. multiples of
         4 but not 3 (4, 8, 16). Music repeats in whole bars.
    The prior favours 4/4 (by far the most common in the genres musloop is
    used for): 3 wins only with a clear margin."""
    n = len(accents)
    out = {"beats_per_bar": 4, "confidence": 0.0, "source": "heuristic",
           "scores": {}}
    if n < 24:
        return out

    def contrast(M):
        best = -np.inf
        for p in range(M):
            on = accents[p::M]
            off = np.delete(accents, np.arange(p, n, M))
            if len(on) < 4 or len(off) < 4:
                continue
            pooled = np.sqrt((on.var() / len(on)) + (off.var() / len(off))) + 1e-9
            best = max(best, (on.mean() - off.mean()) / pooled)
        return float(best)

    acc = {M: contrast(M) for M in AUTO_METERS}

    rep = {3: 0.0, 4: 0.0}
    if feats is not None and feats.shape[1] > 40:
        F = feats / np.maximum(np.linalg.norm(feats, axis=0, keepdims=True), 1e-8)
        def lag_sim(k):
            return float(np.mean(np.sum(F[:, :-k] * F[:, k:], axis=0))) if F.shape[1] > k + 8 else 0.0
        base = np.mean([lag_sim(k) for k in (1, 2, 5, 7, 10, 11, 13, 14)])
        rep[3] = float(np.mean([lag_sim(k) for k in (3, 9, 15)]) - base)
        rep[4] = float(np.mean([lag_sim(k) for k in (4, 8, 16)]) - base)

    # combine: accent t-stats are O(1..10), repetition deltas O(0.01..0.2)
    s3 = 0.5 * acc[3] + 25.0 * rep[3]
    s4 = 0.5 * acc[4] + 25.0 * rep[4]
    margin = s3 - s4
    PRIOR = 0.6  # 4/4 prior, in score units
    bpb = 3 if margin > PRIOR else 4
    conf = float(1.0 / (1.0 + np.exp(-abs(margin - PRIOR) / 0.8)))  # 0.5..1
    out.update(beats_per_bar=bpb, confidence=round(conf, 3),
               scores={"accent_3": round(acc[3], 3), "accent_4": round(acc[4], 3),
                       "repeat_3": round(rep[3], 4), "repeat_4": round(rep[4], 4)})
    return out


def meter_from_downbeats(beats: list, downbeats: list) -> dict | None:
    """Meter implied by a neural beat/downbeat tracker: the most common
    number of beats between consecutive downbeats, with the share of bars
    that agree as the confidence."""
    if len(beats) < 8 or len(downbeats) < 4:
        return None
    b = np.asarray(beats, dtype=float)
    d = np.asarray(downbeats, dtype=float)
    counts = [int(((b >= d[i] - 1e-3) & (b < d[i + 1] - 1e-3)).sum()) for i in range(len(d) - 1)]
    counts = [c for c in counts if c > 0]
    if not counts:
        return None
    vals, freq = np.unique(counts, return_counts=True)
    k = int(np.argmax(freq))
    bpb = int(vals[k])
    if bpb not in METERS:
        return None
    hist = {int(v): int(f) for v, f in zip(vals, freq)}
    return {"beats_per_bar": bpb, "confidence": round(float(freq[k]) / len(counts), 3),
            "source": "neural", "bar_histogram": hist}


def grid_downbeats(beat_times: np.ndarray, beats_per_bar: int, phase: int) -> np.ndarray:
    return np.asarray(beat_times)[phase % max(1, beats_per_bar)::beats_per_bar]


def _neural_phase(beats: np.ndarray, downbeats: np.ndarray, M: int) -> int | None:
    """Phase for meter M that hits the most neural downbeats."""
    if len(downbeats) == 0 or len(beats) < M:
        return None
    is_db = np.array([np.min(np.abs(downbeats - t)) < 0.07 for t in beats])
    return int(np.argmax([is_db[p::M].sum() for p in range(M)]))


def detect_meter_for_beats(path: str, beats: list, downbeats: list,
                           neural: bool) -> dict:
    """Meter for an existing beat grid (markups saved before meter
    detection existed): does not touch the segmentation.

    neural=True: trust the network's downbeats (bar histogram); otherwise
    run the heuristic on audio accents at the given beats."""
    b = np.asarray(beats, dtype=float)
    d = np.asarray(downbeats, dtype=float)
    if neural:
        det = meter_from_downbeats(beats, downbeats) or \
            {"beats_per_bar": 4, "confidence": 0.0, "source": "neural"}
        acc = [1.0 if len(d) and float(np.min(np.abs(d - t))) < 0.07 else 0.0 for t in b]
        auto = list(downbeats)
        ph = int(np.argmin(np.abs(b - float(d[0])))) % det["beats_per_bar"] if len(b) and len(d) else 0
    else:
        y, sr = librosa.load(path, sr=SR, mono=True)
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
        bf = librosa.time_to_frames(b, sr=sr, hop_length=HOP)
        acc_arr = beat_accents(y, sr, bf, onset_env) if len(bf) >= 2 else np.zeros(len(bf))
        feats = None
        if len(bf) >= 24:
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP)
            fr = librosa.util.fix_frames(bf, x_min=0, x_max=chroma.shape[1] - 1)
            feats = librosa.util.sync(chroma, fr, aggregate=np.median)
        det = estimate_meter(acc_arr, feats)
        M = det["beats_per_bar"]
        ph = _estimate_downbeat_phase(onset_env, bf, 4) if M == 4 else best_phase(acc_arr, M)
        acc = _round_list(acc_arr, 2)
        auto = grid_downbeats(b, M, ph).tolist()
    meter = _meter_info(det["beats_per_bar"], det, False)
    meter["phase"] = meter["detected_phase"] = int(ph)
    return {"beats_per_bar": det["beats_per_bar"], "meter": meter,
            "beat_accents": acc, "downbeats_auto": auto}


def _snap(value: float, grid: np.ndarray) -> float:
    """Nearest grid point."""
    if len(grid) == 0:
        return value
    return float(grid[np.argmin(np.abs(grid - value))])


def analyze_deep_merge(path: str, deep: dict, beats_per_bar: int | None = None) -> dict:
    """Merge the allin1 result (boundaries/labels/beats from the neural net)
    with our metrics: loopability, build-up detection, snapping of tiny
    sections.

    allin1 is trained on Harmonix (human structure annotations) - we take
    its boundaries. Our addition is the loop-specific properties.

    Meter: the neural downbeats imply it (beats per bar). With a user
    override the bar grid is rebuilt from the neural beats, phase chosen to
    hit the most neural downbeats.
    """
    y, sr = librosa.load(path, sr=SR, mono=True)
    duration = float(len(y) / sr)
    rms = librosa.feature.rms(y=y, hop_length=HOP)
    rms_env = rms[0]
    rms_times = librosa.times_like(rms_env, sr=sr, hop_length=HOP)

    downbeats = deep.get("downbeats") or []
    beats = deep.get("beats") or []
    tempo = _snap_tempo(float(deep.get("bpm") or 120.0))

    b_arr = np.asarray(beats, dtype=float)
    db_arr = np.asarray(downbeats, dtype=float)
    override = beats_per_bar is not None and int(beats_per_bar) in METERS
    assumed = bool(deep.get("downbeats_assumed")) and len(b_arr) >= 24
    if assumed:
        # beat model failed and the runner fell back to a fixed 4/4 grid:
        # estimate meter + phase from the audio like the fast engine does
        bf = librosa.time_to_frames(b_arr, sr=sr, hop_length=HOP)
        acc_arr = beat_accents(y, sr, bf)
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP)
        fr = librosa.util.fix_frames(bf, x_min=0, x_max=chroma.shape[1] - 1)
        detected = estimate_meter(acc_arr, librosa.util.sync(chroma, fr, aggregate=np.median))
        accents = _round_list(acc_arr, 2)
    else:
        detected = meter_from_downbeats(beats, downbeats) or \
            {"beats_per_bar": 4, "confidence": 0.0, "source": "neural"}
        # per-beat accents for the UI (phase shifts / meter changes
        # client-side): 1 on a neural downbeat, 0 elsewhere
        accents = [1.0 if len(db_arr) and float(np.min(np.abs(db_arr - t))) < 0.07 else 0.0
                   for t in b_arr] if len(b_arr) else []
    bpb = int(beats_per_bar) if override else int(detected["beats_per_bar"])
    phase = 0
    if assumed:
        d_bpb = int(detected["beats_per_bar"])
        downbeats_auto = grid_downbeats(b_arr, d_bpb, best_phase(np.asarray(accents), d_bpb)).tolist()
    else:
        downbeats_auto = list(downbeats)
    if assumed:
        phase = best_phase(np.asarray(accents), bpb)
        downbeats = grid_downbeats(b_arr, bpb, phase).tolist()
    elif override and bpb != detected["beats_per_bar"] and len(b_arr) >= bpb:
        phase = _neural_phase(b_arr, db_arr, bpb) or 0
        downbeats = grid_downbeats(b_arr, bpb, phase).tolist()
    elif len(b_arr) and len(downbeats):
        phase = int(np.argmin(np.abs(b_arr - float(downbeats[0])))) % max(1, bpb)
    meter = _meter_info(bpb, detected, override)
    meter["phase"] = int(phase)
    d_bpb = max(1, int(detected["beats_per_bar"]))
    meter["detected_phase"] = (
        int(np.argmin(np.abs(b_arr - float(downbeats_auto[0])))) % d_bpb
        if len(b_arr) and len(downbeats_auto) else 0)

    # sections: drop micro "start/end" (<2s), merge adjacent ones shorter than 4s
    raw = [s for s in deep.get("segments", [])
           if s["end"] - s["start"] > 0.5]
    segs: list[dict] = []
    for s in raw:
        if segs and (s["end"] - s["start"] < 4.0 or s["label"] in ("start", "end")):
            segs[-1]["end"] = s["end"]
            continue
        if not segs and (s["end"] - s["start"] < 4.0 or s["label"] == "start"):
            # merge the first mini-chunk into the next one
            segs.append({"start": s["start"], "end": s["end"],
                         "label": s["label"], "_merge_next": True})
            continue
        segs.append({"start": s["start"], "end": s["end"], "label": s["label"]})
    merged: list[dict] = []
    for s in segs:
        if merged and merged[-1].get("_merge_next"):
            merged[-1] = {"start": merged[-1]["start"], "end": s["end"],
                          "label": s["label"]}
        else:
            merged.append(s)
    segs = [{k: v for k, v in s.items() if k != "_merge_next"} for s in merged]
    if segs:
        segs[0]["start"] = 0.0
        segs[-1]["end"] = duration

    # snap boundaries to downbeats (SongFormer doesn't quantize them; allin1 - almost)
    if len(downbeats) > 2 and len(segs) > 1:
        db = np.asarray(downbeats, dtype=float)
        bar_dur_est = float(np.median(np.diff(db))) if len(db) > 3 else 2.0
        for i in range(1, len(segs)):
            t = segs[i]["start"]
            j = int(np.argmin(np.abs(db - t)))
            snapped = float(db[j])
            # snap only if close (within a bar)
            if abs(snapped - t) <= bar_dur_est * 0.6:
                segs[i]["start"] = snapped
                segs[i - 1]["end"] = snapped
        # remove sections degenerated after the snap
        segs = [s for s in segs if s["end"] - s["start"] > 1.0]

    # functional labels -> human-readable with repeat numbering
    counts: dict[str, int] = {}
    for s in segs:
        base = str(s["label"]).capitalize()
        counts[base] = counts.get(base, 0) + 1
        s["label"] = f"{base} {counts[base]}" if counts[base] > 1 else base

    for s in segs:
        s["loopability"] = _loopability(y, sr, s["start"], s["end"])
        s["transition"] = _is_transition(y, sr, rms_env, rms_times,
                                         s["start"], s["end"], s["loopability"])
        s["loop"] = not s["transition"]

    bar_dur = 60.0 / max(tempo, 1e-6) * bpb
    min_len = max(4.0, bar_dur * 2)
    return {
        "duration": duration,
        "tempo": tempo,
        "beats": beats,
        "downbeats": downbeats,
        "segments": segs,
        "n_suggested": len(segs),
        "n_max": min(24, max(1, int(duration // min_len))),
        "fallback": False,
        "engine": "deep",
        "beats_per_bar": bpb,
        "meter": meter,
        "beat_accents": accents,
        "downbeats_auto": downbeats_auto,
    }


def _snap_tempo(tempo: float) -> float:
    """Musicians write whole BPMs; frame-quantized estimates come out like
    130.4. Snap to the nearest integer when close enough (<0.4 BPM)."""
    r = round(tempo)
    if r > 0 and abs(tempo - r) < 0.4:
        return float(r)
    return float(tempo)


def _meter_info(chosen: int, detected: dict, override: bool) -> dict:
    return {
        "beats_per_bar": int(chosen),
        "detected": int(detected.get("beats_per_bar", 4)),
        "confidence": detected.get("confidence", 0.0),
        "source": "manual" if override else detected.get("source", "heuristic"),
        "detected_source": detected.get("source", "heuristic"),
        "scores": detected.get("scores") or detected.get("bar_histogram") or {},
    }


def _round_list(a, nd=3) -> list:
    return [round(float(x), nd) for x in a]


def analyze(path: str, n_segments: int | None = None,
            beats_per_bar: int | None = None) -> dict:
    """Full track analysis. Returns a dict for the JSON response.

    beats_per_bar: None = detect the meter automatically (3 vs 4), otherwise
    a user override (one of METERS) — the bar grid and the segmentation
    window are built for that meter."""
    y, sr = librosa.load(path, sr=SR, mono=True)
    duration = float(len(y) / sr)

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env, sr=sr, hop_length=HOP, trim=False
    )
    tempo = _snap_tempo(float(np.atleast_1d(tempo)[0]))
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)

    # --- Meter + downbeats ----------------------------------------------------
    accents = beat_accents(y, sr, beat_frames, onset_env) if len(beat_frames) >= 2 \
        else np.zeros(len(beat_frames))
    rep_feats = None
    if len(beat_frames) >= 24:
        _chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP)
        _fr = librosa.util.fix_frames(beat_frames, x_min=0, x_max=_chroma.shape[1] - 1)
        rep_feats = librosa.util.sync(_chroma, _fr, aggregate=np.median)
    detected = estimate_meter(accents, rep_feats)
    override = beats_per_bar is not None and int(beats_per_bar) in METERS
    beats_per_bar = int(beats_per_bar) if override else int(detected["beats_per_bar"])
    if beats_per_bar == 4:
        # 4/4: the original onset-based phase (the segmentation weights were
        # tuned with it)
        phase = _estimate_downbeat_phase(onset_env, beat_frames, 4)
    else:
        phase = best_phase(accents, beats_per_bar)
    downbeat_times = grid_downbeats(beat_times, beats_per_bar, phase)
    meter = _meter_info(beats_per_bar, detected, override)
    meter["phase"] = int(phase)
    # the auto grid, so the UI can return to "auto" without a re-analysis
    d_bpb = int(detected["beats_per_bar"])
    d_phase = _estimate_downbeat_phase(onset_env, beat_frames, 4) if d_bpb == 4 \
        else best_phase(accents, d_bpb)
    meter["detected_phase"] = int(d_phase)
    meter_out = {"beats_per_bar": beats_per_bar, "meter": meter,
                 "beat_accents": _round_list(accents, 2),
                 "downbeats_auto": grid_downbeats(beat_times, d_bpb, d_phase).tolist()}

    if len(beat_times) < 16:
        # fallback: uniform split if no rhythm was found
        n = n_segments or max(2, int(duration // 20))
        bounds = np.linspace(0, duration, n + 1)
        segments = [
            {"start": float(bounds[i]), "end": float(bounds[i + 1]),
             "label": f"Part {i + 1}", "loop": True, "transition": False}
            for i in range(n)
        ]
        return {
            "duration": duration,
            "tempo": tempo,
            "beats": beat_times.tolist(),
            "downbeats": downbeat_times.tolist(),
            "segments": segments,
            "n_suggested": n,
            "n_max": min(24, max(1, int(duration // 8))),
            "fallback": True,
            **meter_out,
        }

    # --- Beat-synchronous features: harmony + timbre + energy ---------------
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, hop_length=HOP, n_mfcc=13)
    mfcc = mfcc[1:]  # drop the 0th coefficient (loudness)
    rms = librosa.feature.rms(y=y, hop_length=HOP)  # loudness envelope

    sync_frames = librosa.util.fix_frames(beat_frames, x_min=0,
                                          x_max=chroma.shape[1] - 1)
    chroma_sync = librosa.util.sync(chroma, sync_frames, aggregate=np.median)
    mfcc_sync = librosa.util.sync(mfcc, sync_frames, aggregate=np.mean)
    rms_sync = librosa.util.sync(rms, sync_frames, aggregate=np.mean)

    def _norm(f):
        f = f - f.mean(axis=1, keepdims=True)
        s = f.std(axis=1, keepdims=True)
        return f / np.maximum(s, 1e-8)

    feats = np.vstack([
        _norm(chroma_sync) * 1.0,   # harmony
        _norm(mfcc_sync) * 0.7,     # timbre
        _norm(rms_sync) * 2.0,      # dynamics (instruments entering/leaving)
    ])
    feats = librosa.util.normalize(feats, axis=0)

    # --- SSM + novelty ------------------------------------------------------
    ssm = np.dot(feats.T, feats)
    ssm = scipy.ndimage.median_filter(ssm, size=(3, 3))

    n_beats = ssm.shape[0]
    kernel_beats = 4 * beats_per_bar  # context window: 4 bars each way
    novelty = _novelty_from_ssm(ssm, kernel_beats)
    novelty = scipy.ndimage.gaussian_filter1d(novelty, sigma=2)

    min_gap_beats = 4 * beats_per_bar  # section no shorter than 4 bars
    peaks, props = scipy.signal.find_peaks(
        novelty, distance=min_gap_beats, prominence=0.05
    )

    if n_segments is not None and len(peaks) > n_segments - 1:
        order = np.argsort(props["prominences"])[::-1][: n_segments - 1]
        peaks = np.sort(peaks[order])
    elif n_segments is None and len(peaks) > 11:
        order = np.argsort(props["prominences"])[::-1][:11]
        peaks = np.sort(peaks[order])

    sync_times = librosa.frames_to_time(sync_frames, sr=sr, hop_length=HOP)
    bound_times = [sync_times[p] for p in peaks if p < len(sync_times)]

    # --- Quantize boundaries to downbeats + refinement -----------------------
    # The novelty peak is blurred (4-bar kernel), so the nearest downbeat may
    # miss by a bar. Refine: among downbeats within a +-1 bar window around
    # the candidate, pick the one where the transition is most "audible":
    #   * a strong onset right at the boundary (a new part entering),
    #   * the largest RMS energy jump between the bar before and the bar after.
    grid = downbeat_times if len(downbeat_times) > 2 else beat_times
    onset_times = librosa.times_like(onset_env, sr=sr, hop_length=HOP)
    rms_env = rms[0]
    rms_times = librosa.times_like(rms_env, sr=sr, hop_length=HOP)
    bar_dur = (60.0 / max(tempo, 1e-6)) * beats_per_bar

    onset_n = onset_env / max(onset_env.max(), 1e-8)

    def _onset_at(t: float) -> float:
        i = int(np.argmin(np.abs(onset_times - t)))
        lo, hi = max(0, i - 2), min(len(onset_n), i + 3)
        return float(onset_n[lo:hi].max())

    def _rms_jump(t: float) -> float:
        """|mean RMS of the bar after - the bar before| (normalized)."""
        pre = rms_env[(rms_times >= t - bar_dur) & (rms_times < t)]
        post = rms_env[(rms_times >= t) & (rms_times < t + bar_dur)]
        if len(pre) == 0 or len(post) == 0:
            return 0.0
        denom = max(float(rms_env.mean()), 1e-8)
        return abs(float(post.mean()) - float(pre.mean())) / denom

    def _refine(t: float) -> float:
        cands = grid[np.abs(grid - t) <= bar_dur * 1.05]
        if len(cands) == 0:
            return _snap(t, grid)
        best, best_score = float(cands[0]), -1.0
        for c in cands:
            c = float(c)
            # penalty for drifting from the novelty peak, so we don't wander off
            dist_pen = abs(c - t) / bar_dur * 0.15
            score = 0.6 * _onset_at(c) + 0.4 * min(_rms_jump(c), 2.0) - dist_pen
            if score > best_score:
                best_score, best = score, c
        return best

    snapped = sorted({_refine(t) for t in bound_times})

    min_len = max(4.0, (60.0 / max(tempo, 1e-6)) * beats_per_bar * 2)  # >= 2 bars
    bounds = [0.0]
    for t in snapped:
        if t - bounds[-1] >= min_len and duration - t >= min_len:
            bounds.append(float(t))
    bounds.append(duration)

    # --- Loop-aware boundary refinement ---------------------------------------
    # For loops what matters is not "transition audibility" but that each part:
    #   1) closes cleanly (the end->start seam is similar in spectrum and level),
    #   2) has a phrase length (multiple of 4/8 bars - how music is built).
    # Coordinate descent: move each boundary along downbeats within +-2 bars,
    # maximizing closure quality of neighboring parts + phrase-ness of lengths.
    def _bars_between(a: float, b: float) -> int:
        """Number of bars between points on the downbeat grid (not from
        the tempo estimate)."""
        return int(np.searchsorted(grid, b - 1e-3) - np.searchsorted(grid, a - 1e-3))

    def _phrase_score(a: float, b: float) -> float:
        """1.0 - length is a multiple of 8 bars, 0.85 - 4, 0.6 - 2, else 0.35."""
        nb = _bars_between(a, b)
        if nb <= 0:
            return 0.0
        if nb % 8 == 0:
            return 1.0
        if nb % 4 == 0:
            return 0.85
        if nb % 2 == 0:
            return 0.6
        return 0.35

    def _novelty_at_time(t: float) -> float:
        i = int(np.argmin(np.abs(sync_times - t)))
        lo, hi = max(0, i - 1), min(len(novelty), i + 2)
        return float(novelty[lo:hi].max())

    def _boundary_score(lo: float, c: float, hi: float, t_orig: float) -> float:
        W = BOUNDARY_WEIGHTS
        loop_q = 0.5 * (_loopability(y, sr, lo, c) + _loopability(y, sr, c, hi))
        phrase = 0.5 * (_phrase_score(lo, c) + _phrase_score(c, hi))
        audib = 0.6 * _onset_at(c) + 0.4 * min(_rms_jump(c), 2.0)
        nov = _novelty_at_time(c)  # structural signal: where the section actually changes
        dist_pen = abs(c - t_orig) / bar_dur * W["dist_pen"]
        return (W["loop_q"] * loop_q + W["phrase"] * phrase
                + W["audib"] * audib + W["novelty"] * nov - dist_pen)

    orig = list(bounds)
    for _ in range(2):  # two passes of coordinate descent
        moved = False
        for j in range(1, len(bounds) - 1):
            lo, hi = bounds[j - 1], bounds[j + 1]
            cands = grid[(np.abs(grid - orig[j]) <= bar_dur * 2.05)
                         & (grid >= lo + min_len) & (grid <= hi - min_len)]
            if len(cands) == 0:
                continue
            cur = bounds[j]
            best, best_s = cur, _boundary_score(lo, cur, hi, orig[j])
            for c in cands:
                c = float(c)
                if abs(c - cur) < 1e-3:
                    continue
                s = _boundary_score(lo, c, hi, orig[j])
                if s > best_s + 1e-4:
                    best_s, best = s, c
            if best != cur:
                bounds[j] = best
                moved = True
        if not moved:
            break

    # --- Filling up to the requested number of parts -------------------------
    # If there weren't enough novelty peaks, split the longest parts at the
    # downbeat closest to the local novelty maximum inside the part.
    if n_segments is not None:
        def _novelty_at(t: float) -> float:
            i = int(np.argmin(np.abs(sync_times - t)))
            return float(novelty[i])

        while len(bounds) - 1 < n_segments:
            best = None  # (novelty, split_time, insert_pos)
            for i in range(len(bounds) - 1):
                lo, hi = bounds[i] + min_len, bounds[i + 1] - min_len
                if hi <= lo:
                    continue
                cands = grid[(grid >= lo) & (grid <= hi)]
                for c in cands:
                    score = _novelty_at(float(c))
                    if best is None or score > best[0]:
                        best = (score, float(c), i + 1)
            if best is None:
                break  # physically nowhere to split (min_len)
            bounds.insert(best[2], best[1])

    segments = [
        {"start": bounds[i], "end": bounds[i + 1], "label": f"Part {i + 1}"}
        for i in range(len(bounds) - 1)
    ]

    # --- Auto labels: clustering similar sections (A/B/A/C -> verse/chorus) --
    _label_segments(segments, feats, sync_times)

    # --- Loop quality: how seamlessly each part will loop --------------------
    for s in segments:
        s["loopability"] = _loopability(y, sr, s["start"], s["end"])

    # --- Build-ups (build-up/transition): parts with a directed crescendo ----
    # Looping such a part sounds unnatural: the tension rises and drops
    # abruptly at the seam. Detection: a monotonic trend of RMS + spectral
    # brightness (centroid) over the part + poor loop closure.
    for s in segments:
        s["transition"] = _is_transition(y, sr, rms_env, rms_times,
                                         s["start"], s["end"],
                                         s["loopability"])
        s["loop"] = not s["transition"]

    # --- Recommendations for the number of parts ------------------------------
    # suggested - how many the algorithm found; max - how many fit physically
    # (per min_len), capped at 24 (as in the API).
    n_suggested = len(segments)
    n_max = min(24, max(1, int(duration // min_len)))

    return {
        "duration": duration,
        "tempo": tempo,
        "beats": beat_times.tolist(),
        "downbeats": downbeat_times.tolist(),
        "segments": segments,
        "n_suggested": n_suggested,
        "n_max": n_max,
        "fallback": False,
        **meter_out,
    }


def _is_transition(y: np.ndarray, sr: int, rms_env: np.ndarray,
                   rms_times: np.ndarray, start: float, end: float,
                   loopability: float) -> bool:
    """Does the part look like a build-up/transition (should not be looped).

    Signs of a "cone-shaped" chunk:
      * a strong monotonic loudness trend (normalized RMS slope),
      * a consistent spectral brightness trend (centroid rises during
        a build-up),
      * poor loop closure (the end does not resemble the beginning).
    """
    mask = (rms_times >= start) & (rms_times < end)
    seg_rms = rms_env[mask]
    if len(seg_rms) < 16:
        return False

    n = len(seg_rms)
    x = np.arange(n, dtype=float)
    # slope of the RMS linear regression, normalized by mean level and length:
    # slope_norm ~ by how much the loudness changes from start to end
    denom = max(float(seg_rms.mean()), 1e-8)
    slope = float(np.polyfit(x, seg_rms, 1)[0]) * n / denom
    # monotonicity: fraction of variance explained by the trend (R^2)
    trend = np.polyval(np.polyfit(x, seg_rms, 1), x)
    ss_res = float(np.sum((seg_rms - trend) ** 2))
    ss_tot = float(np.sum((seg_rms - seg_rms.mean()) ** 2)) + 1e-12
    r2 = max(0.0, 1.0 - ss_res / ss_tot)

    # spectral brightness (centroid) - rises during a typical build-up
    i0, i1 = int(start * sr), int(end * sr)
    cent = librosa.feature.spectral_centroid(y=y[i0:i1], sr=sr, hop_length=HOP)[0]
    if len(cent) >= 8:
        xc = np.arange(len(cent), dtype=float)
        cden = max(float(cent.mean()), 1e-8)
        cslope = float(np.polyfit(xc, cent, 1)[0]) * len(cent) / cden
    else:
        cslope = 0.0

    # scoring: |loudness change| > ~60% with pronounced monotonicity,
    # or a moderate trend confirmed by brightness and poor closure
    strong_ramp = abs(slope) > 0.6 and r2 > 0.35
    agree = (slope * cslope) > 0 and abs(cslope) > 0.25
    weak_loop = loopability < 0.65
    moderate_ramp = abs(slope) > 0.35 and r2 > 0.25 and (agree or weak_loop)
    return bool(strong_ramp or moderate_ramp)


def _label_segments(segments: list, feats: np.ndarray,
                    sync_times: np.ndarray) -> None:
    """Marks similar sections with the same letter (A, B, C...) via
    agglomerative clustering of the segment's averaged beat features."""
    n = len(segments)
    if n == 0:
        return
    vecs = []
    for s in segments:
        mask = (sync_times >= s["start"]) & (sync_times < s["end"])
        v = feats[:, mask].mean(axis=1) if mask.any() else feats.mean(axis=1)
        nrm = np.linalg.norm(v)
        vecs.append(v / max(nrm, 1e-8))
    vecs = np.array(vecs)

    if n == 1:
        segments[0]["group"] = "A"
        segments[0]["label"] = "A1"
        return

    from scipy.cluster.hierarchy import linkage, fcluster
    from scipy.spatial.distance import pdist
    d = pdist(vecs, metric="cosine")
    z = linkage(d, method="average")
    # threshold: sections that sound close are considered the "same" part
    labels = fcluster(z, t=0.45, criterion="distance")

    # letters in order of first appearance
    order: dict[int, str] = {}
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    counts: dict[str, int] = {}
    for seg, lab in zip(segments, labels):
        if lab not in order:
            order[lab] = letters[len(order) % len(letters)]
        g = order[lab]
        counts[g] = counts.get(g, 0) + 1
        seg["group"] = g
        seg["label"] = f"{g}{counts[g]}"


def loop_quality(path: str, segments: list) -> list:
    """Loop quality for a ready list of boundaries (for manual editing)."""
    y, sr = librosa.load(path, sr=SR, mono=True)
    return [_loopability(y, sr, float(s["start"]), float(s["end"]))
            for s in segments]


def _loopability(y: np.ndarray, sr: int, start: float, end: float,
                 win: float = 0.5) -> float:
    """Loop seamlessness estimate 0..1: are the outer half-seconds similar
    in spectrum, and is there no sharp loudness gap at the end->start seam."""
    i0, i1 = int(start * sr), int(end * sr)
    w = int(win * sr)
    if i1 - i0 < 4 * w:
        return 0.5
    head = y[i0:i0 + w]
    tail = y[i1 - w:i1]

    # spectral similarity of tail and head (mel spectrum, cosine)
    def _spec(x):
        s = librosa.feature.melspectrogram(y=x, sr=sr, n_mels=48, hop_length=HOP)
        v = np.log1p(s).mean(axis=1)
        return v / max(np.linalg.norm(v), 1e-8)

    sim = float(np.dot(_spec(head), _spec(tail)))  # 0..1

    # loudness gap at the seam
    rh = float(np.sqrt(np.mean(head ** 2)) + 1e-8)
    rt = float(np.sqrt(np.mean(tail ** 2)) + 1e-8)
    level = min(rh, rt) / max(rh, rt)  # 1 = levels are equal

    out = 0.7 * sim + 0.3 * level
    if not np.isfinite(out):  # silence/NaN in the spectrum -> neutral score
        return 0.5
    return float(np.clip(out, 0.0, 1.0))
