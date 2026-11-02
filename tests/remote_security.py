"""Security + relay test for the phone remote. The server is reached through the
machine's LAN address, so it sees a non-loopback client (= a phone).

    native:  python tests/remote_security.py LAN_IP 8801
    Docker:  python tests/remote_security.py LAN_IP 8801 8802   (app port, remote port)
"""
import asyncio, json, sys, urllib.request
import websockets

LAN, PORT = sys.argv[1], int(sys.argv[2])
RPORT = int(sys.argv[3]) if len(sys.argv) > 3 else PORT   # Docker: separate remote port
DOCKER = RPORT != PORT
NET = f"http://{LAN}:{RPORT}"
LOC = f"http://127.0.0.1:{PORT}"
R = []
def check(c, m): R.append(bool(c)); print(("  OK  " if c else "  FAIL") + " " + m, flush=True)

def http(base, path, method="GET", key=None, data=None):
    h = {"X-Musloop-Key": key} if key else {}
    if data is not None: h["Content-Type"] = "application/json"; data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, data=data, method=method, headers=h), timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

async def main():
    info = json.loads(http(LOC, "/api/remote/info")[1]); key = info["key"]
    print("  QR urls:", info["urls"], "| remote port:", info.get("remote_port"))
    if DOCKER:
        check(info["urls"] == [] and info["remote_port"] == RPORT and info["docker_hint"],
              "Docker: no guessed LAN url, remote port reported, UI asks for the PC's IP")
        st, _ = http(f"http://127.0.0.1:{RPORT}", "/api/tracks")
        check(st == 403, f"Docker: even loopback on the remote port gets only the remote ({st})")
    else:
        check(info["urls"] and all("#k=" in u for u in info["urls"]), "local: /api/remote/info gives QR urls with key")
    # --- from the network, no key
    for path, method, data in [("/", "GET", None), ("/api/tracks", "GET", None), ("/api/folders", "GET", None),
                               ("/api/folders/stream?path=/etc/passwd", "GET", None), ("/api/folders", "POST", {"path": "/"}),
                               ("/api/remote/info", "GET", None), ("/api/tracks/x", "DELETE", None), ("/app.js", "GET", None)]:
        st, _ = http(NET, path, method, data=data)
        check(st == 403, f"network, no key: {method} {path} -> {st}")
    # --- from the network WITH key: still no dangerous endpoints
    for path, method, data in [("/api/folders", "GET", None), ("/api/folders", "POST", {"path": "/"}),
                               ("/api/folders/stream?path=/etc/passwd", "GET", None), ("/api/remote/info", "GET", None),
                               ("/api/remote/reset", "POST", {}), ("/api/tracks", "GET", None), ("/", "GET", None)]:
        st, _ = http(NET, path, method, key=key, data=data)
        check(st == 403, f"network + key: {method} {path} -> {st} (refused)")
    # --- what a phone may load
    st, body = http(NET, "/remote"); check(st == 200 and b"<html" in body.lower(), f"network: GET /remote -> {st}")
    st, _ = http(NET, "/vendor/react.min.js"); check(st == 200, f"network: GET /vendor/react.min.js -> {st}")
    st, _ = http(NET, "/api/remote/ping", key=key); check(st == 200, f"network + key: /api/remote/ping -> {st}")
    st, _ = http(NET, "/api/remote/ping", key="wrong"); check(st == 403, f"network + wrong key: /api/remote/ping -> {st}")
    # --- websockets
    try:
        async with websockets.connect(f"ws://{LAN}:{RPORT}/ws/host") as w:
            await w.recv(); check(False, "network must not open /ws/host")
    except Exception as e:
        check(True, f"network: /ws/host refused ({type(e).__name__})")
    async with websockets.connect(f"ws://{LAN}:{RPORT}/ws/remote") as w:
        await w.send(json.dumps({"type": "hello", "key": "wrong"}))
        m = json.loads(await w.recv()); check(m.get("error") == "bad_key", f"network: remote with wrong key -> {m}")
    # host (PC tab, local) + remote (phone, network) relay
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/host") as host:
        await host.recv()                                   # presence
        await host.send(json.dumps({"type": "state", "state": {"track": "Village Consort", "playing": True, "now": "A1"}}))
        async with websockets.connect(f"ws://{LAN}:{RPORT}/ws/remote") as phone:
            await phone.send(json.dumps({"type": "hello", "key": key, "ua": "test-phone"}))
            got = [json.loads(await phone.recv()) for _ in range(3)]
            types = [g["type"] for g in got]
            check("welcome" in types and "state" in types, f"phone gets welcome + current state: {types}")
            st = next(g for g in got if g["type"] == "state")["state"]
            check(st.get("now") == "A1", "state relayed to phone")
            # host learns a phone joined
            pres = json.loads(await asyncio.wait_for(host.recv(), 5))
            check(pres.get("type") == "presence" and pres.get("remotes") == 1, f"host sees phone: {pres}")
            # command phone -> host, ack host -> phone
            await phone.send(json.dumps({"type": "cmd", "seq": 7, "cmd": "cue", "args": {"index": 2}}))
            cmd = json.loads(await asyncio.wait_for(host.recv(), 5))
            check(cmd.get("type") == "cmd" and cmd["cmd"] == "cue" and cmd["args"] == {"index": 2}, f"host receives cmd: {cmd}")
            await host.send(json.dumps({"type": "ack", "to": cmd["from"], "seq": cmd["seq"], "ok": True}))
            ack = None
            for _ in range(5):
                m = json.loads(await asyncio.wait_for(phone.recv(), 5))
                if m["type"] == "ack": ack = m; break
            check(ack and ack["ok"] and ack["seq"] == 7, f"phone receives ack: {ack}")
            # reset key -> phone is disconnected
            http(LOC, "/api/remote/reset", "POST", data={})
            try:
                for _ in range(5): await asyncio.wait_for(phone.recv(), 5)
                check(False, "phone should be disconnected after reset")
            except websockets.ConnectionClosed as e:
                check(e.rcvd and e.rcvd.code == 4401, f"key reset disconnects phones (code {e.rcvd and e.rcvd.code})")
    # --- browser attacks from this computer (a website open in the user's browser)
    def lhttp(path, method="GET", headers=None, data=None):
        h = dict(headers or {})
        if data is not None: h["Content-Type"] = "application/json"; data = json.dumps(data).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(LOC + path, data=data, method=method, headers=h), timeout=20) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    # DNS rebinding: foreign host name pointed at 127.0.0.1
    for path in ("/", "/api/tracks", "/api/folders", "/api/remote/info"):
        st = lhttp(path, headers={"Host": f"evil.example:{PORT}"})
        check(st == 403, f"rebinding: Host evil.example -> GET {path} -> {st}")
    for h in (f"localhost:{PORT}", f"127.0.0.1:{PORT}", f"{LAN}:{PORT}", f"[::1]:{PORT}"):
        st = lhttp("/api/tracks", headers={"Host": h})
        check(st == 200, f"own host name {h} -> {st}")
    # cross-site requests from another site's page
    st = lhttp("/api/folders", "POST", headers={"Origin": "http://evil.example"}, data={"path": "/"})
    check(st == 403, f"cross-site: POST /api/folders from evil.example -> {st}")
    st = lhttp("/api/remote/reset", "POST", headers={"Origin": "http://evil.example"}, data={})
    check(st == 403, f"cross-site: POST /api/remote/reset -> {st}")
    st = lhttp("/api/tracks", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"})
    check(st == 403, f"cross-site: GET /api/tracks via <img>/<script> -> {st}")
    st = lhttp("/api/tracks", headers={"Origin": "null"})
    check(st == 403, f"sandboxed / file page (Origin null) -> {st}")
    st = lhttp("/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
    check(st == 200, f"opening musloop from a link elsewhere still works -> {st}")
    # own page: same-origin requests keep working
    st = lhttp("/api/remote/reset", "POST", headers={"Origin": f"http://localhost:{PORT}", "Host": f"localhost:{PORT}"}, data={})
    check(st == 200, f"own page: POST with Origin localhost -> {st}")
    st = lhttp("/api/tracks", headers={"Sec-Fetch-Site": "same-origin", "Origin": f"http://127.0.0.1:{PORT}"})
    check(st == 200, f"own page: GET same-origin -> {st}")
    # cross-site WebSockets
    try:
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/host", origin="http://evil.example") as w:
            await asyncio.wait_for(w.recv(), 5); check(False, "foreign site must not open /ws/host")
    except Exception as e:
        check(True, f"cross-site: /ws/host refused ({type(e).__name__})")
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/remote", origin="http://evil.example") as w:
        await w.send(json.dumps({"type": "hello"}))
        m = json.loads(await asyncio.wait_for(w.recv(), 5))
        check(m.get("error") == "bad_key", f"cross-site: /ws/remote without key -> {m.get('type')} {m.get('error')}")
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/host", origin=f"http://127.0.0.1:{PORT}") as w:
        m = json.loads(await asyncio.wait_for(w.recv(), 5))
        check(m.get("type") == "presence", f"own page: /ws/host works -> {m.get('type')}")
    # host offline -> phone gets host_offline
    key2 = json.loads(http(LOC, "/api/remote/info")[1])["key"]
    check(key2 != key, "key rotated")
    async with websockets.connect(f"ws://{LAN}:{RPORT}/ws/remote") as phone:
        await phone.send(json.dumps({"type": "hello", "key": key2}))
        await phone.recv()
        await phone.send(json.dumps({"type": "cmd", "seq": 1, "cmd": "next"}))
        m = None
        for _ in range(4):
            m = json.loads(await asyncio.wait_for(phone.recv(), 5))
            if m["type"] == "ack": break
        check(m and m.get("error") == "host_offline", f"no PC tab -> {m}")
    print("PASS" if all(R) else "FAIL", f"{sum(R)}/{len(R)}")

asyncio.run(main())
