"""Find a public HOMES listing URL for a building, by driving a real browser.

HOMES sits behind an AWS WAF JS challenge, so a plain HTTP GET only ever gets a
202 "challenge" page. We instead drive headless Chrome (Playwright): it solves
the challenge, we type the building name into HOMES's free-word box
(``cond[freeword]``), submit (a POST to ``/chintai/list/``), match the result
card whose building name equals ours, and read that room page's ``og:image`` /
``og:title`` in the same session. Returns the HOMES room URL + image + title so
we can hand the customer a "rent it here" link with a photo.

Heavy + ToS-sensitive + brittle by nature — gated by ``FANGO_EXTERNAL_LOOKUP_ENABLED``,
rate-limited, and cached per listing by the caller (:mod:`fango.listings.enrich`),
so the browser runs at most once per building. SUUMO is intentionally dropped.

The browser runs in an isolated thread so the sync Playwright API never clashes
with a running asyncio loop (FastAPI / MCP).
"""
from __future__ import annotations

import logging
import re
import threading
import unicodedata
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_TOP = "https://www.homes.co.jp/chintai/"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_STEALTH = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "Object.defineProperty(navigator,'languages',{get:()=>['ja-JP','ja','en']});"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
    "window.chrome=window.chrome||{runtime:{}};"
)

# In the result page: return the first /chintai/room/ link whose surrounding
# card text contains the building name (NFKC-folded).
_MATCH_JS = """(name) => {
  const norm = s => (s||'').normalize('NFKC').replace(/\\s+/g,'').toLowerCase();
  const key = norm(name);
  if (!key) return null;
  const links = document.querySelectorAll('a[href*="/chintai/room/"]');
  for (const a of links) {
    let el = a, txt = '';
    for (let i=0; i<6 && el; i++) { el = el.parentElement; if (el) txt = el.innerText || ''; if (norm(txt).includes(key)) return a.href; }
  }
  return null;
}"""
_OG_JS = """() => {
  const g = p => (document.querySelector(`meta[property="${p}"]`)
                 || document.querySelector(`meta[name="${p}"]`) || {}).content || null;
  return { image: g('og:image') || g('twitter:image'), title: g('og:title') || document.title };
}"""

_TIMEOUT = 40000


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", "", s).replace("　", "").lower()


def source_of_url(url: str) -> str | None:
    host = (urlparse(url or "").hostname or "").lower()
    if "homes.co.jp" in host:
        return "homes"
    if "suumo.jp" in host:
        return "suumo"
    return None


def _launch(p):
    """Prefer system Chrome; fall back to a Playwright-managed chromium."""
    args = ["--disable-blink-features=AutomationControlled"]
    try:
        return p.chromium.launch(channel="chrome", headless=True, args=args)
    except Exception:
        return p.chromium.launch(headless=True, args=args)


def _settle(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(1500)


def _search_one(page, building_name: str) -> dict | None:
    """One freeword search on an already-open HOMES page: type the building
    name, submit, match the result card to our building, read its room page's
    og:image/title. Returns {url, source, image, title} or None."""
    page.goto(_TOP, wait_until="domcontentloaded", timeout=_TIMEOUT)
    _settle(page)
    box = page.locator('input[name="cond[freeword]"]').first
    box.click()
    box.fill(building_name)
    box.press("Enter")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    href = page.evaluate(_MATCH_JS, building_name)
    if not href:
        return None
    page.goto(href, wait_until="domcontentloaded", timeout=_TIMEOUT)
    _settle(page)
    og = page.evaluate(_OG_JS) or {}
    return {
        "url": href,
        "source": "homes",
        "image": og.get("image"),
        "title": (og.get("title") or "").strip() or None,
    }


def _browser_find_many(names: list[str]) -> dict:
    """Resolve several building names in ONE browser session (open HOMES once,
    clear the WAF once, then search each). Returns {name: result|None}."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        log.warning("playwright unavailable for HOMES lookup: %s", exc)
        return {}
    out: dict = {n: None for n in names}
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            ctx = browser.new_context(
                user_agent=_UA, locale="ja-JP", timezone_id="Asia/Tokyo",
                viewport={"width": 1280, "height": 900},
            )
            ctx.add_init_script(_STEALTH)
            page = ctx.new_page()
            for name in names:
                try:
                    out[name] = _search_one(page, name)
                except Exception as exc:  # pragma: no cover - browser variance
                    log.debug("HOMES search failed for %r: %s", name, exc)
            return out
        finally:
            browser.close()


def _run_isolated(fn, *args, timeout: float = 70.0):
    """Run a sync function in a dedicated thread (so sync Playwright doesn't
    collide with an asyncio loop on the calling thread)."""
    box: dict = {}

    def target():
        try:
            box["v"] = fn(*args)
        except Exception as exc:  # pragma: no cover - browser variance
            box["e"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.warning("HOMES browser lookup timed out")
        return None
    if "e" in box:
        log.warning("HOMES browser lookup failed: %s", box["e"])
        return None
    return box.get("v")


def find_listing(building_name: str, *, source_url: str | None = None,
                 **_ignored) -> dict | None:
    """Find a HOMES listing for ``building_name``. Returns
    ``{url, source, image, title}`` or None. Best-effort, never raises.

    If ``source_url`` is already a HOMES/SUUMO detail page, it's returned as-is
    (no browser needed; image/title left None)."""
    if source_url and source_of_url(source_url):
        return {"url": source_url, "source": source_of_url(source_url),
                "image": None, "title": None}
    name = (building_name or "").strip()
    if not name:
        return None
    res = _run_isolated(_browser_find_many, [name], timeout=75.0) or {}
    return res.get(name)


def find_listings(names: list[str]) -> dict:
    """Resolve several building names in a single browser session (open HOMES +
    clear the WAF once, then search each). Returns ``{name: result|None}``.
    Best-effort, never raises."""
    clean = list(dict.fromkeys(n.strip() for n in (names or []) if n and n.strip()))
    if not clean:
        return {}
    # ~roughly per-building budget + one WAF warm-up.
    timeout = 40.0 + 30.0 * len(clean)
    return _run_isolated(_browser_find_many, clean, timeout=timeout) or {}
