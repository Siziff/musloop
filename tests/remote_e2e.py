"""End-to-end phone remote test.

PC: desktop Chromium on http://127.0.0.1:PORT (the host tab, plays audio).
Phones: iPhone 14 and Pixel 7 emulation on http://<LAN IP>:PORT/remote#k=...
(non-loopback address, so the server treats them as network clients).
"""
import asyncio, json, os, sys, urllib.request
from playwright.async_api import async_playwright

CH = os.environ.get("CHROME") or None   # None = Playwright's bundled Chromium
LAN, PORT = sys.argv[1], int(sys.argv[2])
RPORT = int(sys.argv[3]) if len(sys.argv) > 3 else PORT   # Docker mode: remote port
DOCKER = RPORT != PORT
R = []
def check(c, m): R.append(bool(c)); print(("  OK  " if c else "  FAIL") + " " + m, flush=True)

S = "(() => { const p = window.__player; return p ? {playing: p.playing, i: p.segIndex, pending: p.pendingTarget()} : null; })()"


async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path=CH, args=["--disable-gpu", "--autoplay-policy=no-user-gesture-required"])
        pc_ctx = await br.new_context(viewport={"width": 1280, "height": 900})
        pc = await pc_ctx.new_page(); pc_err = []
        pc.on("pageerror", lambda e: pc_err.append(str(e)))
        await pc.goto(f"http://127.0.0.1:{PORT}/")
        await pc.evaluate("localStorage.setItem('musloop_lang','ru')"); await pc.reload(); await pc.wait_for_selector("header")
        # open Village Consort, enter Live, open the phone panel
        await pc.click(".drawer-btn"); await pc.wait_for_timeout(300)
        if not await pc.locator(".drawer .seg-chip", has_text="Village Consort").count():
            await pc.locator(".drawer .collapse-head").last.click(); await pc.wait_for_timeout(300)
        await pc.locator(".drawer .seg-chip", has_text="Village Consort").first.click()
        await pc.wait_for_selector(".meter-badge", timeout=120000)
        await pc.click(".drawer-backdrop", position={"x": 1250, "y": 450}); await pc.wait_for_timeout(300)
        await pc.keyboard.press("KeyL"); await pc.wait_for_selector(".live")
        await pc.click(".remote-btn"); await pc.wait_for_selector(".remote-panel")
        if DOCKER:   # the container can't know the PC's address: the GM types it once
            await pc.wait_for_selector(".remote-ip input", timeout=10000)
            await pc.fill(".remote-ip input", LAN)
        await pc.wait_for_selector(".remote-qr img", timeout=10000)
        try:
            await pc.wait_for_function("(() => { const i = document.querySelector('.remote-qr img'); return i && i.complete && i.naturalWidth > 0; })()", timeout=10000)
            qr_ok = True
        except Exception:
            qr_ok = False
        check(qr_ok, "PC: QR code rendered in Live mode")
        info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/remote/info").read())
        if DOCKER:
            shown = (await pc.text_content(".remote-url code")).strip()
            url = shown + "#k=" + info["key"]
        else:
            url = info["urls"][0]
        check(url.startswith(f"http://{LAN}:{RPORT}/remote#k="), f"QR url: {url.split('#')[0]}#k=…")

        phones = []
        for dev in ("iPhone 14", "Pixel 7"):
            ctx = await br.new_context(**p.devices[dev])
            ph = await ctx.new_page(); errs = []
            ph.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
            await ph.goto(url)
            await ph.wait_for_selector(".transport", timeout=15000)
            phones.append((dev, ph, errs))
        await pc.wait_for_timeout(500)
        cnt = await pc.text_content(".remote-count")
        check(cnt and cnt.strip() == "2", f"PC shows 2 phones connected ({cnt})")
        dev, ph, _ = phones[0]
        txt = await ph.text_content("body")
        check("Village Consort" in txt and "ПК на связи" in txt, f"{dev}: sees track + 'PC online' in the PC's language (ru)")
        parts = await ph.locator(".pad:not(.scene)").count()
        scenes = await ph.locator(".pad.scene").count()
        check(parts >= 4 and scenes >= 2, f"{dev}: {parts} part pads, {scenes} scene pads")

        async def pc_state():
            return await pc.evaluate("""() => {
              const now = document.querySelector('.live-now .live-name'); const nx = document.querySelector('.live-next .live-name');
              const playing = !!document.querySelector('.live-bar button.big:not(.primary)') && !document.querySelector('.live-bar button.primary');
              return {now: now && now.textContent, next: nx && nx.textContent, playing}; }""")

        # Play from the phone
        await ph.click(".transport .primary"); await pc.wait_for_timeout(900)
        s1 = await pc_state()
        check(s1["playing"], f"phone ▶ -> PC playing ({s1})")
        # cue a part
        await ph.locator(".pad:not(.scene)").nth(2).click(); await pc.wait_for_timeout(500)
        s2 = await pc_state()
        check(s2["next"] and s2["next"] != "—", f"phone taps part 3 -> PC queued: next={s2['next']}")
        # the second phone sees the queued part
        dev2, ph2, _ = phones[1]
        await ph2.wait_for_timeout(1300)
        q2 = await ph2.locator(".pad.queued").count()
        check(q2 >= 1, f"{dev2}: sees the queued part too ({q2} queued pad)")
        # cancel
        await ph2.click(".transport button:nth-child(3)"); await pc.wait_for_timeout(500)
        s3 = await pc_state()
        check(s3["next"] == "—", f"{dev2}: ✕ Cancel -> PC next cleared ({s3['next']})")
        # mode switch
        await ph.locator(".modes button").nth(2).click(); await pc.wait_for_timeout(500)
        mode_on = await pc.evaluate("[...document.querySelectorAll('.live-top .lang-switch button')].findIndex(b => b.classList.contains('on'))")
        check(mode_on == 2, f"phone mode 'right away' -> PC mode button {mode_on}")
        # scene 'Ambush' (now) -> switches right away
        before = (await pc_state())["now"]
        await ph.locator(".pad.scene", has_text="Ambush").click(); await pc.wait_for_timeout(1200)
        s4 = await pc_state()
        check(s4["now"] != before, f"phone scene 'Ambush' -> PC now: {before} -> {s4['now']}")
        # volume
        await ph.evaluate("""() => { const r = document.querySelector('.vol input'); const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                                  set.call(r, '0.35'); r.dispatchEvent(new Event('input', {bubbles: true})); }""")
        await pc.wait_for_timeout(900)
        v = await pc.evaluate("localStorage.getItem('musloop_volume')")
        check(v and abs(float(v) - 0.35) < 0.02, f"phone volume 35% -> PC volume {v}")
        # track switch via the sheet
        await ph.click(".track .name"); await ph.wait_for_selector(".sheet")
        await ph.locator(".sheet .item", has_text="Crowd Hammer").click()
        await pc.wait_for_function("(() => { const e = document.querySelector('.live-track-name .nm'); return e && e.textContent.includes('Crowd Hammer') && !e.textContent.startsWith('Загрузка'); })()", timeout=60000)
        await ph.wait_for_timeout(1500)
        ptxt = await ph.text_content(".track .name")
        check("Crowd Hammer" in ptxt, f"phone picks 'Crowd Hammer' -> PC switched, phone shows: {ptxt.strip()[:40]}")
        s5 = await pc_state()
        check(s5["playing"], "music keeps playing after the switch")
        # fade
        await ph.click(".transport button:nth-child(1)"); await pc.wait_for_timeout(3200)
        s6 = await pc_state()
        check(not s6["playing"], "phone ◢ Fade -> PC stopped")
        # PC tab closes -> phones see 'PC not responding'
        await pc.close(); await ph.wait_for_timeout(1200)
        btxt = await ph.text_content("body")
        check("ПК не отвечает" in btxt or "закрыта" in btxt, "PC tab closed -> phone shows 'PC not responding'")
        # PC tab back -> phones recover by themselves
        pc = await pc_ctx.new_page(); await pc.goto(f"http://127.0.0.1:{PORT}/"); await pc.wait_for_selector("header")
        await ph.wait_for_timeout(2500)
        btxt = await ph.text_content(".conn")
        check("ПК на связи" in btxt, f"PC tab reopened -> phone back online ({btxt.strip()})")
        # new code -> old phones are cut off
        await pc.evaluate("fetch('/api/remote/reset', {method: 'POST'})"); await ph.wait_for_timeout(1500)
        btxt = await ph.text_content("body")
        check("больше не действует" in btxt, "'New code' -> old phone told to rescan")
        # keep-awake media reachable for the phone
        st = await ph.evaluate("fetch('/keepawake.mp4').then(r => r.status)")
        check(st == 200, f"phone can load keep-awake video ({st})")
        all_err = pc_err + sum((e for _, _, e in phones), [])
        check(not all_err, f"no JS errors ({all_err[:2]})")
        await br.close()
    print("PASS" if all(R) else "FAIL", f"{sum(R)}/{len(R)}")

asyncio.run(main())
