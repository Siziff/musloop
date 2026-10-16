"""Open musloop's PC page headlessly with a track loaded (the "host" tab that
plays audio and executes the phone's commands), and keep it open.
    python mobile/tests/pc_host.py http://127.0.0.1:8801 "Village Consort" &
"""
import asyncio, os, sys
from playwright.async_api import async_playwright

URL, TRACK = sys.argv[1], sys.argv[2]

async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path=os.environ.get("CHROME") or None,
                                     args=["--autoplay-policy=no-user-gesture-required"])
        pg = await (await br.new_context()).new_page()
        await pg.goto(URL + "/"); await pg.wait_for_selector("header")
        await pg.click(".drawer-btn"); await pg.wait_for_timeout(300)
        if not await pg.locator(".drawer .seg-chip", has_text=TRACK).count():
            await pg.locator(".drawer .collapse-head").last.click(); await pg.wait_for_timeout(300)
        await pg.locator(".drawer .seg-chip", has_text=TRACK).first.click()
        await pg.wait_for_selector(".meter-badge", timeout=180000)
        print("PC host ready:", TRACK, flush=True)
        await asyncio.sleep(1800)

asyncio.run(main())
