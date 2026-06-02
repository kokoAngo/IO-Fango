"""Open HOMES in a real Chrome (Playwright) so the AWS WAF JS challenge solves,
then report whether we reached real content. Optionally run a building search.

    python scripts/homes_browser.py                      # open /chintai/
    python scripts/homes_browser.py "GENOVIA南麻布"       # free-word search that name
    python scripts/homes_browser.py --headed "三田"       # show the browser window

Unlike a raw GET (which gets a 202 awswaf challenge page), a real browser runs
challenge.js, gets the WAF cookie, and loads the page. Read-only.
"""
import sys
import re
from urllib.parse import quote

from playwright.sync_api import sync_playwright

args = [a for a in sys.argv[1:] if a != "--headed"]
HEADED = "--headed" in sys.argv
QUERY = args[0] if args else None

if QUERY:
    URL = f"https://www.homes.co.jp/chintai/tokyo/list/?keyword={quote(QUERY)}"
else:
    URL = "https://www.homes.co.jp/chintai/"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


_STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP','ja','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || { runtime: {} };
"""


def main() -> None:
    print(f"opening (headed={HEADED}): {URL}\n")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome", headless=not HEADED,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=UA, locale="ja-JP", timezone_id="Asia/Tokyo",
            viewport={"width": 1280, "height": 900},
        )
        ctx.add_init_script(_STEALTH)
        page = ctx.new_page()

        # Human-like: visit the top page first to earn the WAF cookie, pause,
        # then go to the search — instead of hitting the search URL cold.
        if QUERY:
            page.goto("https://www.homes.co.jp/chintai/", wait_until="domcontentloaded", timeout=40000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
        page.goto(URL, wait_until="domcontentloaded", timeout=40000)

        # Let the AWS WAF challenge run + the SPA settle.
        for _ in range(3):
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
                break
            except Exception:
                pass
        page.wait_for_timeout(2500)

        html = page.content()
        title = page.title()
        rooms = sorted(set(re.findall(r"/chintai/room/[0-9a-f]{8,}/?", html)))
        blocked = "awswaf" in html.lower() or "challenge.js" in html.lower()

        print(f"final url   : {page.url}")
        print(f"title       : {title}")
        print(f"body size   : {len(html)} chars")
        print(f"still WAF?   : {blocked}")
        print(f"room links  : {len(rooms)}")
        if rooms[:3]:
            print(f"sample      : {rooms[:3]}")
        if QUERY:
            present = QUERY in html
            print(f"'{QUERY}' in page : {present}")
            # building names on HOMES list cards
            names = re.findall(r'class="[^"]*bukkenName[^"]*"[^>]*>([^<]+)<', html)
            if names:
                print("card building names:", [n.strip() for n in names[:8]])
        browser.close()


if __name__ == "__main__":
    main()
