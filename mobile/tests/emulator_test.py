"""Instrumented check on a real Android emulator (CI): the installed APK
connects to a musloop server and drives it.

Steps (adb + the app's WebView over Chrome DevTools Protocol):
  1. start the app, wait for the WebView
  2. feed the "scanned" QR (the real server URL with the key) into the app —
     the Google scanner UI itself can't be driven on a headless emulator
  3. the app pings the PC natively, opens the remote, receives the PC's state
  4. tapping Play in the app makes the PC (a headless Chromium "host" tab) play

    python mobile/tests/emulator_test.py http://10.0.2.2:8801 KEY APK
(10.0.2.2 = the host machine as seen from the Android emulator)
"""
import json, os, subprocess, sys, time, urllib.request

SERVER, KEY, APK = sys.argv[1], sys.argv[2], sys.argv[3]
PKG = "io.github.siziff.musloop"
R = []
def check(c, m): R.append(bool(c)); print(("  OK  " if c else "  FAIL") + " " + m, flush=True)
def adb(*a, check_=True):
    return subprocess.run(["adb", *a], capture_output=True, text=True, check=check_).stdout


def app_pid():
    return adb("shell", "pidof", PKG, check_=False).strip().split(" ")[0]


def webview_ws():
    """DevTools websocket of the app's WebView (debug builds are inspectable).
    The socket is named webview_devtools_remote_<pid>: pick the current app's."""
    adb("forward", "--remove-all", check_=False)
    for _ in range(90):
        pid = app_pid()
        socks = [l.split()[-1].lstrip("@") for l in adb("shell", "cat", "/proc/net/unix").splitlines()
                 if "webview_devtools_remote" in l]
        socks = [x for x in socks if pid and x.endswith("_" + pid)] or []
        if socks:
            adb("forward", "tcp:9229", f"localabstract:{socks[0]}")
            try:
                pages = json.loads(urllib.request.urlopen("http://127.0.0.1:9229/json", timeout=3).read())
                page = next((p for p in pages if p.get("type") == "page"), None)
                if page:
                    return page["webSocketDebuggerUrl"]
            except Exception:
                pass
        time.sleep(1)
    raise SystemExit("WebView DevTools socket not found")


def main():
    import asyncio, websockets

    adb("install", "-r", APK)
    adb("shell", "pm", "grant", PKG, "android.permission.NEARBY_WIFI_DEVICES", check_=False)
    adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity")
    ws_url = webview_ws()
    check(True, "app started, WebView inspectable")

    async def run():
        async with websockets.connect(ws_url, max_size=None) as cdp:
            mid = 0
            async def ev(expr, wait=True):
                nonlocal mid
                mid += 1
                await cdp.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                                           "params": {"expression": expr, "awaitPromise": wait, "returnByValue": True}}))
                while True:
                    m = json.loads(await cdp.recv())
                    if m.get("id") == mid:
                        return m.get("result", {}).get("result", {}).get("value")
            async def wait_for(expr, timeout=30):
                t0 = time.time()
                while time.time() - t0 < timeout:
                    if await ev(expr):
                        return True
                    await asyncio.sleep(0.5)
                return False

            check(await wait_for("!!document.querySelector('#connect h1')"), "connect screen shown")
            plugins = await ev("Object.keys((window.Capacitor && window.Capacitor.Plugins) || {}).join(',')")
            check(all(p in plugins for p in ("Discovery", "QrScan", "Haptics", "KeepAwake", "Preferences")),
                  f"native plugins registered: {plugins}")
            # native ping through the Java plugin, straight to the server
            res = await ev(f"Capacitor.Plugins.Discovery.ping({{server: '{SERVER}', key: '{KEY}'}}).then(r => JSON.stringify(r))")
            r = json.loads(res or "{}")
            check(r.get("status") == 200 and '"musloop"' in r.get("body", ""), f"native ping to the PC: {r.get('status')}")
            bad = json.loads(await ev(f"Capacitor.Plugins.Discovery.ping({{server: '{SERVER}', key: 'wrong'}}).then(r => JSON.stringify(r))") or "{}")
            check(bad.get("status") == 403, f"wrong key refused by the PC: {bad.get('status')}")
            # simulate a successful QR scan with the real URL
            await ev(f"Capacitor.Plugins.QrScan.scan = async () => ({{ text: '{SERVER}/remote#k={KEY}' }}); document.getElementById('scan').click(); true", wait=False)
            check(await wait_for("!!document.querySelector('.transport')", 30), "QR -> remote opened")
            check(await wait_for("document.getElementById('root').textContent.includes('Village Consort')", 20),
                  "the PC's track shown in the app (WebSocket over the LAN works)")
            await ev("document.querySelector('.transport .primary').click(); true", wait=False)
            playing = False
            for _ in range(20):
                playing = await ev("!!document.querySelector('.card.now .sub') && document.querySelector('.card.now .sub').textContent.trim().length > 0")
                if playing:
                    break
                await asyncio.sleep(0.5)
            check(playing, "app ▶ -> the PC plays (pass counter appears in the app)")
    asyncio.run(run())
    # app restart -> reconnects to the saved PC by itself
    adb("shell", "am", "force-stop", PKG)
    time.sleep(2)
    adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity")
    time.sleep(3)
    ws_url = webview_ws()
    async def again():
        async with websockets.connect(ws_url, max_size=None) as cdp:
            for i in range(60):
                await cdp.send(json.dumps({"id": i + 1, "method": "Runtime.evaluate",
                                           "params": {"expression": "!!document.querySelector('.transport')", "returnByValue": True}}))
                while True:
                    m = json.loads(await cdp.recv())
                    if m.get("id") == i + 1:
                        break
                if m.get("result", {}).get("result", {}).get("value"):
                    return True
                await asyncio.sleep(0.5)
            return False
    check(asyncio.run(again()), "app restart -> back on the saved PC without scanning")
    print("PASS" if all(R) else "FAIL", f"{sum(R)}/{len(R)}")
    sys.exit(0 if all(R) else 1)


main()
