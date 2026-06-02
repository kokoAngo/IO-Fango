"""Discover HOMES's building-name search by driving its real search box.

Opens HOMES in Chrome (passes the WAF), types a building name into the search
input like a human, and captures:
  * the autocomplete/suggest network calls it fires (the endpoint we want),
  * any suggestion items shown (text + link),
  * where pressing Enter / clicking the first suggestion navigates.

    python scripts/homes_discover.py "GENOVIA南麻布"
    python scripts/homes_discover.py --headed "GENOVIA南麻布"
"""
import sys
import json
from playwright.sync_api import sync_playwright

args = [a for a in sys.argv[1:] if a != "--headed"]
HEADED = "--headed" in sys.argv
QUERY = args[0] if args else "GENOVIA南麻布"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
STEALTH = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['ja-JP','ja','en']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
window.chrome=window.chrome||{runtime:{}};
"""
# network we care about: API-ish calls (json/suggest/autocomplete), not assets
INTEREST = ("suggest", "autocomplet", "api", "keyword", "search", "incremental", "candidate", "bukken")


def main():
    print(f"query={QUERY!r} headed={HEADED}\n")
    calls = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=not HEADED,
                              args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(user_agent=UA, locale="ja-JP", timezone_id="Asia/Tokyo",
                            viewport={"width": 1280, "height": 900})
        ctx.add_init_script(STEALTH)
        page = ctx.new_page()

        def on_req(req):
            u = req.url
            if "homes.co.jp" in u and any(k in u.lower() for k in INTEREST) \
               and not u.endswith((".js", ".css", ".png", ".jpg", ".svg", ".woff2")):
                calls.append((req.method, u))
        page.on("request", on_req)

        page.goto("https://www.homes.co.jp/chintai/", wait_until="domcontentloaded", timeout=40000)
        try: page.wait_for_load_state("networkidle", timeout=12000)
        except Exception: pass
        page.wait_for_timeout(1500)

        # find a plausible search input
        sel = None
        for cand in ['input[type="search"]', 'input[name="keyword"]', 'input[name="fw"]',
                     'input[placeholder*="エリア"]', 'input[placeholder*="駅"]',
                     'input[placeholder*="検索"]', 'input[placeholder*="物件名"]',
                     'header input[type="text"]', 'input[type="text"]']:
            if page.locator(cand).count() > 0:
                sel = cand
                break
        print("search input selector:", sel)
        if not sel:
            # dump candidate inputs to help
            print("inputs on page:", page.eval_on_selector_all(
                "input", "els=>els.slice(0,15).map(e=>({name:e.name,ph:e.placeholder,type:e.type}))"))
            b.close(); return

        box = page.locator(sel).first
        box.click()
        calls.clear()  # only keep calls fired by typing
        box.type(QUERY, delay=120)        # human-like per-key
        page.wait_for_timeout(2500)       # let autocomplete fire

        print("\n=== autocomplete/API calls fired while typing ===")
        for m, u in dict.fromkeys(calls):
            print(f"  {m} {u}")

        # any suggestion dropdown items?
        sugg = page.eval_on_selector_all(
            "li a, [class*=suggest] a, [class*=Suggest] a, [role=option]",
            "els=>els.slice(0,12).map(e=>({t:(e.textContent||'').trim().slice(0,40),href:e.href||''}))")
        sugg = [s for s in sugg if s["t"]]
        print("\n=== visible suggestions ===")
        for s in sugg[:12]:
            print(" ", s)

        # try pressing Enter and see where we land
        try:
            box.press("Enter")
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)
            print("\nafter Enter -> url:", page.url)
            print("title:", page.title())
        except Exception as e:
            print("Enter nav failed:", e)
        b.close()


if __name__ == "__main__":
    main()
