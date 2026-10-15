"""End-to-end smoke test of a fresh install: upload -> analyze (+meter) ->
markup save/load -> loopability -> export zip (manifest) -> Foundry export.
Usage: python smoke.py PORT AUDIO"""
import io, json, sys, time, urllib.request, uuid, zipfile

port, audio = sys.argv[1], sys.argv[2]
B = f"http://localhost:{port}"


def req(path, data=None, ctype="application/json", timeout=300):
    if data is not None and ctype == "application/json":
        data = json.dumps(data).encode()
    r = urllib.request.Request(B + path, data=data, headers={"Content-Type": ctype} if data is not None else {})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        body = resp.read()
        return resp.headers, body


def upload(path):
    bnd = uuid.uuid4().hex
    name = path.split("/")[-1]
    payload = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
               f"Content-Type: audio/mpeg\r\n\r\n").encode() + open(path, "rb").read() + f"\r\n--{bnd}--\r\n".encode()
    _, b = req("/api/upload", payload, f"multipart/form-data; boundary={bnd}")
    return json.loads(b)


ok = True
def check(cond, msg):
    global ok
    print(("  OK  " if cond else "  FAIL") + " " + msg)
    ok &= bool(cond)

t = time.time()
up = upload(audio)
tid = up["track_id"]
check(tid, f"upload -> {tid}")
_, b = req(f"/api/analyze/{tid}")
a = json.loads(b)
check(len(a["segments"]) >= 2, f"analyze: {len(a['segments'])} parts, {a['tempo']} BPM")
check(a.get("beats_per_bar") in (3, 4) and a.get("meter", {}).get("source") == "heuristic",
      f"meter: {a.get('beats_per_bar')} ({a['meter'].get('confidence')})")
_, b = req(f"/api/analyze/{tid}?beats_per_bar=3")
check(json.loads(b)["meter"]["source"] == "manual", "manual meter override")
mk = {k: a[k] for k in ("duration", "tempo", "beats", "downbeats", "beats_per_bar", "meter", "segments")}
mk["scenes"] = [{"id": "s1", "name": "Test scene", "segIndex": 1, "cueMode": "soon", "hotkey": "1"}]
req(f"/api/markup/{tid}", mk)
_, b = req(f"/api/markup/{tid}")
check(json.loads(b)["markup"]["scenes"][0]["name"] == "Test scene", "markup save/load")
_, b = req(f"/api/loopability/{tid}", [{"start": s["start"], "end": s["end"]} for s in a["segments"]])
check(len(json.loads(b)["loopability"]) == len(a["segments"]), "loopability")
_, b = req(f"/api/meter/{tid}", {"beats": a["beats"], "downbeats": a["downbeats"]})
check(json.loads(b)["meter"]["detected"] in (3, 4), "meter backfill endpoint")
body = {"segments": a["segments"], "scenes": mk["scenes"], "tempo": a["tempo"], "downbeats": a["downbeats"],
        "beats_per_bar": a["beats_per_bar"], "phrase_bars": 4, "tails": True}
h, zb = req(f"/api/export/{tid}", body)
z = zipfile.ZipFile(io.BytesIO(zb))
man = json.loads(z.read("manifest.json"))
wavs = [n for n in z.namelist() if n.endswith(".wav") and "_tail" not in n]
check(len(wavs) == len(a["segments"]) and man["beats_per_bar"] == a["beats_per_bar"],
      f"export: {len(wavs)} loops, {len(z.namelist())} files, {len(zb)//1024} KB, cd={h.get('Content-Disposition')}")
check(all(L.get("tail_file") in z.namelist() for L in man["loops"] if L.get("tail_file")), "tails present")
h, zb = req(f"/api/export/{tid}", {**body, "target": "foundry", "crossfade": 0.4})
z = zipfile.ZipFile(io.BytesIO(zb))
pl = json.loads(z.read("foundry-playlist.json"))
check("foundry-playlist-scenes.json" in z.namelist() and len(pl["sounds"]) == len(a["segments"]),
      f"foundry export: playlist with {len(pl['sounds'])} sounds, slug={man.get('slug')}")

print(f"{'PASS' if ok else 'FAIL'} in {time.time()-t:.0f}s")
sys.exit(0 if ok else 1)
