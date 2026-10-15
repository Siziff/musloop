#!/usr/bin/env python3
"""Evaluation and tuning of auto-segmentation against manual markups.

Usage:
  1. Annotate tracks in the UI and save the markup ("Markup" button).
  2. Put the pairs into the examples/ directory:
       examples/track1.mp3
       examples/track1.musloop.json   (name = track name + .musloop.json)
  3. Evaluate the current algorithm:
       python3 tools/tune.py examples/
     Search for _boundary_score weights (grid search):
       python3 tools/tune.py examples/ --search

Metric: F-measure of boundary hits with a +-1 bar tolerance (and +-0.5 s
for reference).
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np  # noqa: E402


def load_pairs(directory: str) -> list[tuple[str, list[float]]]:
    """[(audio_path, [boundaries in sec, without 0 and the end])]."""
    pairs = []
    for fn in sorted(os.listdir(directory)):
        ext = next((e for e in (".musloop.json", ".musslop.json") if fn.endswith(e)), None)
        if not ext:
            continue
        meta = json.load(open(os.path.join(directory, fn)))
        base = fn[: -len(ext)]
        audio = None
        for ext in (".mp3", ".wav", ".ogg", ".flac", ".m4a"):
            p = os.path.join(directory, base + ext)
            if os.path.exists(p):
                audio = p
                break
        if audio is None:
            print(f"!! no audio for {fn}")
            continue
        segs = sorted(meta["segments"], key=lambda s: s["start"])
        bounds = [float(s["start"]) for s in segs[1:]]  # inner boundaries
        pairs.append((audio, bounds))
    return pairs


def f_measure(pred: list[float], true: list[float], tol: float) -> tuple[float, float, float]:
    if not pred and not true:
        return 1.0, 1.0, 1.0
    if not pred or not true:
        return 0.0, 0.0, 0.0
    matched_t = set()
    tp = 0
    for p in pred:
        best, bd = None, tol
        for i, t in enumerate(true):
            if i in matched_t:
                continue
            d = abs(p - t)
            if d <= bd:
                bd, best = d, i
        if best is not None:
            matched_t.add(best)
            tp += 1
    prec = tp / len(pred)
    rec = tp / len(true)
    f = 2 * prec * rec / max(prec + rec, 1e-9)
    return f, prec, rec


def evaluate(pairs, weights=None) -> dict:
    from backend import analysis as A

    if weights is not None:
        A.BOUNDARY_WEIGHTS.update(weights)

    rows, fs = [], []
    for audio, true_bounds in pairs:
        r = A.analyze(audio)
        pred = [s["start"] for s in r["segments"][1:]]
        bar = 60.0 / max(r["tempo"], 1e-6) * r.get("beats_per_bar", 4)
        f_bar, p, rc = f_measure(pred, true_bounds, tol=bar)
        f_05, _, _ = f_measure(pred, true_bounds, tol=0.5)
        rows.append((os.path.basename(audio), f_bar, f_05, p, rc,
                     len(pred), len(true_bounds)))
        fs.append(f_bar)
    return {"rows": rows, "mean_f": float(np.mean(fs)) if fs else 0.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", help="directory with track+markup pairs")
    ap.add_argument("--search", action="store_true", help="grid search of weights")
    args = ap.parse_args()

    pairs = load_pairs(args.directory)
    if not pairs:
        print("No pairs found. Need files like track.mp3 + track.musloop.json")
        return
    print(f"Pairs found: {len(pairs)}")

    res = evaluate(pairs)
    print("\n=== Current algorithm ===")
    for name, fb, f05, p, r, np_, nt in res["rows"]:
        print(f"  {name:40s} F(+-1 bar)={fb:.2f} F(+-0.5s)={f05:.2f} "
              f"P={p:.2f} R={r:.2f} pred={np_} true={nt}")
    print(f"  mean F = {res['mean_f']:.3f}")

    if not args.search:
        return

    print("\n=== Grid search of boundary weights ===")
    grid = {
        "loop_q": [0.2, 0.3, 0.4],
        "phrase": [0.1, 0.2, 0.3],
        "audib": [0.1, 0.2, 0.3],
        "novelty": [0.2, 0.3, 0.4],
    }
    best = (res["mean_f"], None)
    keys = list(grid)
    for combo in itertools.product(*(grid[k] for k in keys)):
        w = dict(zip(keys, combo))
        r = evaluate(pairs, weights=w)
        if r["mean_f"] > best[0]:
            best = (r["mean_f"], dict(w))
            print(f"  new best F={best[0]:.3f}  {best[1]}")
    print(f"\nBest: F={best[0]:.3f}, weights: {best[1] or 'defaults'}")
    if best[1]:
        print("To apply, update BOUNDARY_WEIGHTS in backend/analysis.py")


if __name__ == "__main__":
    main()
