"""Full check of a running musloop: upload, every available engine for real
(fast / SongFormer / All-In-One), Demucs layers, Foundry export.
Usage: python tests/fullcheck.py PORT AUDIO [--keep] [--strict]"""
import io, json, sys, time, urllib.request, uuid, zipfile

port, audio = sys.argv[1], sys.argv[2]
B = f"http://localhost:{port}"
R = []


def check(c, m):
    R.append(bool(c)); print(("  OK  " if c else "  FAIL") + " " + m, flush=True)


def req(path, data=None, method=None, timeout=1800, ctype="application/json"):
    if data is not None and ctype == "application/json":
        data = json.dumps(data).encode()
    r = urllib.request.Request(B + path, data=data, method=method,
                               headers={"Content-Type": ctype} if data is not None else {})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.headers, resp.read()


bnd = uuid.uuid4().hex
name = "fullcheck " + audio.split("/")[-1]
payload = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
           f"Content-Type: audio/mpeg\r\n\r\n").encode() + open(audio, "rb").read() + f"\r\n--{bnd}--\r\n".encode()
_, b = req("/api/upload", payload, ctype=f"multipart/form-data; boundary={bnd}")
tid = json.loads(b)["track_id"]
check(tid, f"upload -> {tid}")

_, hb = req("/api/health")
health = json.loads(hb)
engines = ["fast"] + (["songformer"] if health.get("songformer_available") else []) + (["deep"] if health.get("deep_available") else [])
print("  engines available:", engines, flush=True)
for eng in engines:
    t = time.time()
    try:
        _, b = req(f"/api/analyze/{tid}?engine={eng}")
        a = json.loads(b)
        # neural engines may (rightly) find one section in synthetic CI audio:
        # require a valid result, and >= 2 parts only for the fast engine
        min_parts = 2 if eng == "fast" or "--strict" in sys.argv else 1
        check(len(a["segments"]) >= min_parts and a.get("meter") and a["tempo"] > 0,
              f"analyze {eng:10s}: {len(a['segments'])} parts, {a['tempo']:.0f} BPM, meter {a['beats_per_bar']}/{a['meter'].get('source')} ({time.time()-t:.0f}s)")
    except Exception as e:
        check(False, f"analyze {eng}: {e}")

t = time.time()
req(f"/api/stems/{tid}", {}, method="POST")
while True:
    _, b = req(f"/api/stems/{tid}/status")
    st = json.loads(b)
    if st.get("status") in ("done", "error") or time.time() - t > 900:
        break
    time.sleep(3)
ok = st.get("status") == "done"
if ok:
    sizes = {}
    for s in ("drums", "bass", "other", "vocals"):
        h, data = req(f"/api/stems/{tid}/{s}")
        sizes[s] = len(data) // 1024
    ok = all(v > 100 for v in sizes.values())
check(ok, f"stems (Demucs): {st.get('status')} {st.get('error','')} in {time.time()-t:.0f}s")

_, b = req(f"/api/analyze/{tid}?engine=fast")
a = json.loads(b)
h, zb = req(f"/api/export/{tid}", {"segments": a["segments"], "tempo": a["tempo"], "downbeats": a["downbeats"],
                                     "beats_per_bar": a["beats_per_bar"], "tails": True, "target": "foundry"})
z = zipfile.ZipFile(io.BytesIO(zb))
man = json.loads(z.read("manifest.json"))
check(man["format"] == "musloop-loops" and "foundry-playlist.json" in z.namelist(),
      f"export foundry zip: {len(z.namelist())} files, format {man['format']}")

if "--keep" not in sys.argv:
    req(f"/api/tracks/{tid}", method="DELETE")
    _, b = req("/api/tracks")
    check(tid not in [t["track_id"] for t in json.loads(b)["tracks"]], "test track removed from the library")
print("PASS" if all(R) else "FAIL", f"{sum(R)}/{len(R)}")
