"""Test the Android app's web part (mobile/www) in a phone-sized browser,
with the Capacitor bridge stubbed (window.Capacitor.Plugins), against a
running musloop.

Covers: connect screen, discovery list, manual address + key, QR scan result,
wrong key / unreachable PC messages, the remote UI driving the PC, haptics and
keep-awake going through the native plugins, "Another PC" back to connect.
The app page is served from its own origin (like the WebView), so CORS is
exercised too.

    python mobile/tests/app_web_test.py LAN_IP PORT
"""
import asyncio, functools, http.server, json, os, socketserver, sys, threading, urllib.request
from playwright.async_api import async_playwright

CH = os.environ.get("CHROME") or None
LAN, PORT = sys.argv[1], int(sys.argv[2])
WWW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "www")
R = []
def check(c, m): R.append(bool(c)); print(("  OK  " if c else "  FAIL") + " " + m, flush=True)

STUB = """
window.__calls = [];
const rec = (p, m, a) => { window.__calls.push([p, m, a]); };
const store = JSON.parse(sessionStorage.getItem('__prefs') || '{}');
const save = () => sessionStorage.setItem('__prefs', JSON.stringify(store));
window.Capacitor = { isNativePlatform: () => true, Plugins: {
  Preferences: {
    get: async ({key}) => ({ value: key in store ? store[key] : null }),
    set: async ({key, value}) => { store[key] = value; save(); },
    remove: async ({key}) => { delete store[key]; save(); },
  },
  Haptics: { impact: async a => rec('Haptics', 'impact', a), notification: async a => rec('Haptics', 'notification', a) },
  KeepAwake: { keepAwake: async () => rec('KeepAwake', 'keepAwake'), allowSleep: async () => rec('KeepAwake', 'allowSleep') },
  Discovery: {
    find: async () => (window.__discovery || { servers: [] }),
    // the real plugin does this request natively (Java HttpURLConnection); the
    // test runner performs it for us via an exposed binding
    ping: async ({server, key}) => window.__nativePing(server, key),
  },
  QrScan: { scan: async () => { if (window.__qr instanceof Error) throw window.__qr; return { text: window.__qr }; } },
}};
"""


def serve_www():
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=WWW)
    h.log_message = lambda *a: None
    s = socketserver.ThreadingTCPServer(("127.0.0.1", 0), h); s.daemon_threads = True
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


async def main():
    info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/remote/info").read())
    key = info["key"]
    server = f"http://{LAN}:{PORT}"
    app_url = "http://localhost/index.html"   # exactly the Capacitor WebView origin (androidScheme http)
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path=CH, args=["--disable-gpu", "--autoplay-policy=no-user-gesture-required"])
        # PC tab (the host) on the musloop server
        pc = await (await br.new_context()).new_page()
        await pc.goto(f"http://127.0.0.1:{PORT}/"); await pc.wait_for_selector("header")
        await pc.click(".drawer-btn"); await pc.wait_for_timeout(300)
        if not await pc.locator(".drawer .seg-chip", has_text="Village Consort").count():
            await pc.locator(".drawer .collapse-head").last.click(); await pc.wait_for_timeout(300)
        await pc.locator(".drawer .seg-chip", has_text="Village Consort").first.click()
        await pc.wait_for_selector(".meter-badge", timeout=120000)
        await pc.click(".drawer-backdrop", position={"x": 1250, "y": 450})

        ctx = await br.new_context(**p.devices["Pixel 7"], locale="ru-RU", ignore_https_errors=True)
        await ctx.add_init_script(STUB)
        # native HTTP stand-in: same request the Java plugin makes, outside the browser
        def native_ping(source, server, key):
            try:
                with urllib.request.urlopen(urllib.request.Request(server + "/api/remote/ping",
                                            headers={"X-Musloop-Key": key}), timeout=3.5) as r:
                    return {"status": r.status, "body": r.read().decode()}
            except urllib.error.HTTPError as e:
                return {"status": e.code, "body": ""}
            except Exception:
                return {"status": 0, "body": ""}
        await ctx.expose_binding("__nativePing", native_ping)
        # The app holds Android's network permission (INTERNET; on Android 17+
        # ACCESS_LOCAL_NETWORK), so the WebView may open sockets to the LAN.
        # Desktop Chromium models that as the "local-network-access" permission.
        await ctx.grant_permissions(["local-network-access"], origin="http://localhost")
        # serve mobile/www on http://localhost/ like the WebView does (from the APK assets)
        async def www_route(route):
            path = route.request.url.split("http://localhost/", 1)[1].split("?")[0] or "index.html"
            f = os.path.normpath(os.path.join(WWW, path))
            if not f.startswith(os.path.normpath(WWW)) or not os.path.isfile(f):
                return await route.fulfill(status=404, body="")
            ctype = "text/html" if f.endswith(".html") else "application/javascript" if f.endswith(".js") else "application/octet-stream"
            await route.fulfill(status=200, body=open(f, "rb").read(), headers={"Content-Type": ctype})
        await ctx.route("http://localhost/**", www_route)
        ph = await ctx.new_page(); errs = []
        ph.on("pageerror", lambda e: errs.append(str(e)))
        await ph.add_init_script(f"window.__discovery = {{servers: [{{host: '{LAN}', port: {PORT}, name: 'Test PC', url: '{server}'}}]}};")
        await ph.goto(app_url)
        await ph.wait_for_selector("#connect h1")
        check("Подключение к musloop" in await ph.text_content("#connect h1"), "connect screen in the phone's language (ru)")
        await ph.wait_for_selector("[data-found]")
        check("Test PC" in await ph.text_content("[data-found]"), "discovery lists the PC found on the Wi-Fi")
        # tap the found PC without a key yet -> asks to scan once
        await ph.click("[data-found]"); await ph.wait_for_timeout(200)
        check(await ph.input_value("#addr") == f"{LAN}:{PORT}" and "QR" in await ph.text_content("#msg"),
              "tapping a found PC fills the address and asks to scan the QR once")
        # QR scan with a foreign QR
        await ph.evaluate("window.__qr = 'https://example.com/hello'"); await ph.click("#scan"); await ph.wait_for_timeout(300)
        check("не QR-код musloop" in await ph.text_content("#msg"), "foreign QR code rejected")
        # QR with a wrong key -> server says 403 -> asks to rescan
        await ph.evaluate(f"window.__qr = '{server}/remote#k=WRONG'"); await ph.click("#scan"); await ph.wait_for_timeout(1500)
        check("отсканируйте QR-код" in (await ph.text_content("#msg")).lower() or "QR" in await ph.text_content("#msg"),
              f"wrong key -> rescan message ({(await ph.text_content('#msg'))[:50]})")
        # unreachable PC
        await ph.fill("#addr", "10.255.255.1:8801"); await ph.fill("#key", "x"); await ph.click("#go")
        await ph.wait_for_timeout(5000)
        check("Не удаётся связаться" in await ph.text_content("#msg"), "unreachable PC -> clear message")
        # the real QR
        await ph.evaluate(f"window.__qr = '{server}/remote#k={key}'"); await ph.click("#scan")
        try:
            await ph.wait_for_selector(".transport", timeout=15000)
        except Exception:
            print("   DEBUG msg:", await ph.text_content("#msg") if await ph.locator("#msg").count() else "-",
                  "| root:", (await ph.text_content("#root"))[:200], "| errs:", errs[:3])
            raise
        check(await ph.locator("#connect").is_hidden(), "valid QR -> remote opens")
        try:
            await ph.wait_for_function("(() => { const r = document.getElementById('root').textContent; return r.includes('Village Consort') && r.includes('ПК на связи'); })()", timeout=8000)
            ok = True
        except Exception:
            ok = False
        check(ok, f"remote shows the PC's track, online ({(await ph.text_content('#root'))[:80]!r})")
        # control the PC
        await ph.click(".transport .primary"); await pc.wait_for_timeout(1000)
        playing = await pc.evaluate("!!document.querySelector('.player-bar, header') && [...document.querySelectorAll('button')].some(b => /Stop|Стоп/.test(b.textContent))")
        check(playing, "app ▶ -> PC plays")
        calls = await ph.evaluate("window.__calls")
        check(any(c[0] == "Haptics" for c in calls), f"button press -> native haptics ({[c for c in calls if c[0]=='Haptics'][:1]})")
        check(any(c[:2] == ["KeepAwake", "keepAwake"] for c in calls), "connected -> native keep-awake on")
        # tap a part other than the playing one (works with 2+ parts)
        await ph.locator(".pad:not(.scene):not(.on)").last.click(); await pc.wait_for_timeout(600)
        nxt = await ph.locator(".pad.queued").count()
        check(nxt >= 1, "app taps a part -> queued on the PC")
        # restart the app: it reconnects to the saved PC without asking
        await ph.reload(); await ph.wait_for_selector(".transport", timeout=15000)
        check(True, "app restart -> straight back to the saved PC")
        # "Another PC" -> connect screen, recent list has the PC
        await ph.click(".chg"); await ph.wait_for_selector("#connect h1", timeout=10000)
        check("Test PC" in (await ph.text_content("#connect")) or LAN in (await ph.text_content("#connect")), "'Another PC' -> connect screen, PC in recents")
        # PC changes the key -> the app offers to rescan
        await ph.click("[data-recent]"); await ph.wait_for_selector(".transport", timeout=15000)
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/api/remote/reset", data=b"{}", method="POST",
                                                      headers={"Content-Type": "application/json"}))
        try:
            await ph.wait_for_selector(".center button", timeout=8000)
        except Exception:
            print("   DEBUG root:", (await ph.text_content("#root"))[:160])
        btn = ph.locator(".center button")
        check(await btn.count() == 1 and "Сканировать снова" in await btn.text_content(), "new code on the PC -> app offers 'Scan again'")
        await btn.click(); await ph.wait_for_selector("#connect h1", timeout=10000)
        check(True, "'Scan again' -> back to the connect screen")
        check(not errs, f"no JS errors ({errs[:2]})")
        await br.close()
    print("PASS" if all(R) else "FAIL", f"{sum(R)}/{len(R)}")

asyncio.run(main())
