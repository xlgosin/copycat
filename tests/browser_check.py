"""Optional visual smoke check against a paused local service; never starts trading."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("http://127.0.0.1:8010")
    page.locator("#token").fill((root / "data/admin-token").read_text().strip())
    page.locator("#loginForm button").click()
    page.locator("#dashboard").wait_for(state="visible")
    page.wait_for_function("document.querySelector('#running').textContent === '已暂停'")
    data = page.evaluate("state")
    print(json.dumps({k:data.get(k) for k in ("mode","running","source","error")}, ensure_ascii=False))
    # Synthetic content for layout testing, only in this test browser.
    data.update(source={"equity":301278.90,"aum":6322638.85,"updated":"2026-09-05T10:00:00+08:00"},
                positions={"TRUMPUSDT:LONG":{"quantity":"11.342","entry":"2.7"}},
                records=[{"time":"2026-09-05T10:00:00+08:00","symbol":"TRUMPUSDT","side":"LONG",
                          "operation":"OPEN","status":"filled","note":"模拟成交 · 测试展示数据","quantity":"11.342","price":"2.7"}])
    page.route("**/api/status", lambda route: route.fulfill(json=data))
    page.evaluate("s=>render(s)", data)
    for width, height in ((1440,1000),(375,1000)):
        page.set_viewport_size({"width":width,"height":height})
        for tab in ("positions","orders","strategy"):
            page.locator(f"[data-tab={tab}]").click()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width,tab)
        page.locator("[data-tab=positions]").click()
        page.screenshot(path=f"/tmp/copycat-{width}.png",full_page=True)
    assert not errors, errors
    browser.close()
    print("Desktop/mobile tabs, auth, JS, horizontal overflow: OK")
