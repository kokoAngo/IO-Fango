"""Find a public HOMES listing URL for a building, by driving a real browser.

HOMES sits behind an AWS WAF JS challenge, so a plain HTTP GET only ever gets a
202 "challenge" page. We instead drive headless Chrome (Playwright): it solves
the challenge, we type the building name into HOMES's free-word box
(``cond[freeword]``), submit (a POST to ``/chintai/list/``), match the result
card whose building name equals ours, and read that room page's ``og:image`` /
``og:title`` in the same session. Returns the HOMES room URL + image + title so
we can hand the customer a "rent it here" link with a photo.

When HOMES escalates to a WAF / "Human Verification" challenge, the browser path
raises :class:`HomesBlocked` and we **fall back to a DuckDuckGo search** for the
building's HOMES page (URL only — we can't open HOMES to read its og:image while
blocked). Controlled by ``FANGO_EXTERNAL_LOOKUP_FALLBACK`` (default on).

Heavy + ToS-sensitive + brittle by nature — gated by ``FANGO_EXTERNAL_LOOKUP_ENABLED``,
rate-limited, and cached per listing by the caller (:mod:`fango.listings.enrich`),
so the browser runs at most once per building. SUUMO is intentionally dropped.

The browser runs in an isolated thread so the sync Playwright API never clashes
with a running asyncio loop (FastAPI / MCP).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import unicodedata
from urllib.parse import parse_qs, unquote, urlparse

from ..config import load_settings

log = logging.getLogger(__name__)


class HomesBlocked(Exception):
    """Raised when HOMES serves an AWS WAF / CAPTCHA challenge instead of a real
    page — the signal to fall back to a web-search lookup."""

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_STEALTH = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "Object.defineProperty(navigator,'languages',{get:()=>['ja-JP','ja','en']});"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
    "window.chrome=window.chrome||{runtime:{}};"
)

# Per kind: the HOMES landing whose freeword box we drive, and the CSS selector
# for that section's listing-detail links (rental rooms vs sale buildings).
_ENTRY = {
    "rent": "https://www.homes.co.jp/chintai/",
    "sale": "https://www.homes.co.jp/mansion/",
}
_LINK_SEL = {
    "rent": 'a[href*="/chintai/room/"]',
    "sale": 'a[href*="/mansion/b-"], a[href*="/kodate/b-"]',
}

# In the result page: among links whose surrounding card text contains the
# building name (NFKC-folded), prefer the one on the SAME floor (args.floor →
# "<n>階"); fall back to the first building match. Returns {href, exact} where
# exact=true means we matched the same floor. ``sel`` picks rent-room vs
# sale-building links.
# Precise unit match: a result card must contain the building name AND our floor
# (◯階) AND our rent/price (any token in args.rents, e.g. "13.5万" or "135000").
# All three are required — we only link THIS exact unit, never a same-building
# reference. Floor/rent use a digit-boundary check so "4階" doesn't hit "14階"
# and "135000" doesn't hit "1135000".
_MATCH_JS = """(args) => {
  const norm = s => (s||'').normalize('NFKC').replace(/\\s+/g,'').toLowerCase();
  const money = s => norm(s).replace(/,/g,'');
  const isDigit = c => c >= '0' && c <= '9';
  const hasTok = (hay, tok) => {
    if (!tok) return false;
    let i = 0;
    while ((i = hay.indexOf(tok, i)) !== -1) {
      const b = hay[i-1], a = hay[i+tok.length];
      const bOk = (i === 0) || (!isDigit(b) && b !== '.');
      const aOk = (i+tok.length >= hay.length) || !isDigit(a);
      if (bOk && aOk) return true;
      i += 1;
    }
    return false;
  };
  const key = norm(args.name);
  const floorTok = args.floor ? (String(args.floor) + '階') : null;
  const rents = (args.rents || []).map(money);
  if (!key || !floorTok || !rents.length) return null;   // need all three to confirm a unit
  const links = document.querySelectorAll(args.sel);
  for (const a of links) {
    // Climb to the smallest ancestor whose text includes the building name —
    // that's this card. Stop there, or the text bleeds into neighbouring cards.
    let el = a, txt = '';
    for (let i=0; i<6 && el; i++) { el = el.parentElement; if (el) txt = el.innerText || ''; if (norm(txt).includes(key)) break; }
    const n = norm(txt), mm = money(txt);
    if (n.includes(key) && hasTok(n, floorTok) && rents.some(r => hasTok(mm, r))) {
      return { href: a.href };
    }
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


# Markers of the AWS WAF challenge / CAPTCHA HOMES serves to suspected bots.
_BLOCK_MARKERS = ("awswaf", "challenge.js", "gokuprops", "human verification")


def _is_block_markup(title: str, html: str) -> bool:
    """True when a page is an AWS WAF challenge rather than real content. Pure
    (no Playwright) so it's unit-testable."""
    t = (title or "").lower()
    if "human verification" in t:
        return True
    h = (html or "").lower()
    if any(m in h for m in _BLOCK_MARKERS):
        # A real results page can legitimately be huge; the challenge page is
        # tiny. Treat marker-bearing pages as blocked regardless of size — the
        # markers are challenge-specific (awswaf/gokuProps), not on real pages.
        return True
    return False


def _check_blocked(page) -> None:
    try:
        if _is_block_markup(page.title(), page.content()):
            raise HomesBlocked()
    except HomesBlocked:
        raise
    except Exception:
        pass


def _search_one(page, building_name: str, kind: str = "rent",
                floor: int | None = None, rents: list[str] | None = None) -> dict | None:
    """One freeword search on an already-open HOMES page for the given ``kind``
    (rent → 賃貸 rooms, sale → 売買 buildings): type the building name, submit,
    and return ONLY the card that matches our exact unit — building name + floor
    (``◯階``) + rent/price (one of ``rents``). If no card matches all three,
    return None (no same-building reference). Returns {url, source, image, title,
    note=None} or None. Raises HomesBlocked if HOMES serves a WAF challenge."""
    page.goto(_ENTRY.get(kind, _ENTRY["rent"]), wait_until="domcontentloaded", timeout=_TIMEOUT)
    _settle(page)
    _check_blocked(page)
    box = page.locator('input[name="cond[freeword]"]').first
    box.click()
    box.fill(building_name)
    box.press("Enter")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    _check_blocked(page)
    m = page.evaluate(_MATCH_JS, {"name": building_name,
                                  "sel": _LINK_SEL.get(kind, _LINK_SEL["rent"]),
                                  "floor": floor, "rents": rents or []})
    if not m:
        return None
    href = m["href"]
    page.goto(href, wait_until="domcontentloaded", timeout=_TIMEOUT)
    _settle(page)
    og = page.evaluate(_OG_JS) or {}
    return {
        "url": href,
        "source": "homes",
        "image": og.get("image"),
        "title": (og.get("title") or "").strip() or None,
        # Only exact-unit matches reach here (name + floor + rent), so never a
        # reference note.
        "note": None,
    }


def _browser_find_many(targets: list[dict], kind: str = "rent") -> dict:
    """Resolve several ``targets`` ({name, floor}) of one ``kind`` in ONE browser
    session (open HOMES once, clear the WAF once, then search each, preferring
    the same floor). Returns ``{"results": {name: result|None}, "blocked":
    [names]}``. On a WAF challenge the current + remaining names go into
    ``blocked`` for the search fallback. If Playwright is unavailable, every name
    is reported blocked."""
    names = [t["name"] for t in targets]
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        log.warning("playwright unavailable for HOMES lookup: %s", exc)
        return {"results": {}, "blocked": list(names)}
    results: dict = {}
    blocked: list[str] = []
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            ctx = browser.new_context(
                user_agent=_UA, locale="ja-JP", timezone_id="Asia/Tokyo",
                viewport={"width": 1280, "height": 900},
            )
            ctx.add_init_script(_STEALTH)
            page = ctx.new_page()
            for i, t in enumerate(targets):
                try:
                    results[t["name"]] = _search_one(page, t["name"], kind, t.get("floor"), t.get("rents"))
                except HomesBlocked:
                    # WAF kicked in — abandon the browser path for this name and
                    # everything after it; the caller will web-search these.
                    log.info("HOMES blocked; %d name(s) deferred to web fallback", len(targets) - i)
                    blocked = [x["name"] for x in targets[i:]]
                    break
                except Exception as exc:  # pragma: no cover - browser variance
                    log.debug("HOMES search failed for %r: %s", t["name"], exc)
                    results[t["name"]] = None
            return {"results": results, "blocked": blocked}
        finally:
            browser.close()


def _lookup_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("FANGO_LOOKUP_CONCURRENCY", "1")))
    except ValueError:
        return 1


# Global cap on concurrent browser sessions — THE memory guard. Each headless
# Chrome is ~300-400MB, so without this a burst of consults/tool-calls could open
# dozens at once and OOM the box. Default 1 → at most one browser at a time.
_BROWSER_SEM = threading.BoundedSemaphore(_lookup_concurrency())
# How long a caller waits for a free slot before giving up and letting the web
# (DuckDuckGo) fallback handle it instead of piling on more browsers.
_SLOT_WAIT_SECONDS = 8.0


def _run_isolated(fn, *args, timeout: float = 70.0):
    """Run a sync function (a browser session) in a dedicated thread so sync
    Playwright doesn't collide with the caller's asyncio loop — under a global
    concurrency cap so only N browsers ever run at once.

    The slot is acquired/released on THIS thread (not the worker), so a worker
    that wedges past ``timeout`` can't permanently strand the slot. A timed-out
    browser is left to Playwright's own per-op timeouts to close; tightening that
    to a hard process-kill is a follow-up."""
    if not _BROWSER_SEM.acquire(timeout=_SLOT_WAIT_SECONDS):
        log.info("HOMES lookup at capacity; skipping browser (web fallback)")
        return None
    try:
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
    finally:
        _BROWSER_SEM.release()


# ---------------------------------------------------------------------------
# Fallback: when HOMES blocks our browser, recover the URL via a web search.
# ---------------------------------------------------------------------------

_DDG_HTML = "https://html.duckduckgo.com/html/"
# DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<encoded-target>.
_DDG_REDIRECT_RE = re.compile(r'/l/\?(?:[^"\']*?&)?uddg=([^"&\']+)', re.I)
# Acceptable HOMES detail-link shapes per kind (rental rooms vs sale buildings).
_HOMES_LINK_RE = {
    "rent": re.compile(r'https?://(?:www\.)?homes\.co\.jp/chintai/[^\s"\'<>]+', re.I),
    "sale": re.compile(r'https?://(?:www\.)?homes\.co\.jp/(?:mansion|kodate)/[^\s"\'<>]+', re.I),
}
_DDG_QUERY = {"rent": "{name} 賃貸 homes.co.jp", "sale": "{name} 中古マンション homes.co.jp"}


def _ddg_parse(html: str, kind: str = "rent") -> list[str]:
    """Extract HOMES listing URLs (for ``kind``) from DuckDuckGo HTML results, in
    order. Pure (no network) → unit-testable. Decodes DDG's ``uddg`` redirect
    wrapper and also catches any direct homes.co.jp links."""
    rx = _HOMES_LINK_RE.get(kind, _HOMES_LINK_RE["rent"])
    urls: list[str] = []
    seen: set[str] = set()
    for enc in _DDG_REDIRECT_RE.findall(html):
        target = unquote(enc)
        if rx.match(target) and target not in seen:
            seen.add(target)
            urls.append(target)
    for m in rx.findall(html):
        if m not in seen:
            seen.add(m)
            urls.append(m)
    return urls


def rent_tokens(*, rent_yen: int | None = None, price_man: int | None = None) -> list[str]:
    """Strings a HOMES card must contain to confirm THIS unit's price — used by
    the exact-unit match. For rent: the 万 form ("13.5万") and the plain-yen form
    ("135000"); for sale: the 万 form ("5980万"). Empty when there's no price to
    match on, which makes the lookup return no link (we won't link an unverified
    unit). Pure → unit-testable."""
    toks: list[str] = []
    if rent_yen:
        try:
            man = int(rent_yen) / 10000
            toks.append(f"{man:.2f}".rstrip("0").rstrip(".") + "万")
            toks.append(str(int(rent_yen)))
        except (TypeError, ValueError):
            pass
    if price_man:
        try:
            toks.append(f"{int(price_man)}万")
        except (TypeError, ValueError):
            pass
    return toks


def find_listing(building_name: str, *, kind: str = "rent", floor: int | None = None,
                 rents: list[str] | None = None,
                 source_url: str | None = None, **_ignored) -> dict | None:
    """Find the HOMES page for our EXACT unit of ``building_name`` (``kind`` =
    'rent' | 'sale') — the card must match building name + ``floor`` + one of
    ``rents`` (rent/price tokens). No match → None (we never link a same-building
    reference). Returns ``{url, source, image, title, note}`` or None.

    If ``source_url`` is already a HOMES/SUUMO detail page (our own listing's
    URL), it's returned as-is — that's our exact unit."""
    if source_url and source_of_url(source_url):
        return {"url": source_url, "source": source_of_url(source_url),
                "image": None, "title": None, "note": None}
    name = (building_name or "").strip()
    if not name:
        return None
    return find_listings([{"name": name, "floor": floor, "rents": rents or []}], kind=kind).get(name)


def find_listings(targets, kind: str = "rent") -> dict:
    """Resolve several ``targets`` of one ``kind`` ('rent' | 'sale'). Each target
    is a ``{"name", "floor"}`` dict (a bare string is accepted as name-only).
    Tries HOMES in one browser session (clear the WAF once, search each,
    preferring the same floor); for any name HOMES *blocked* us on, falls back to
    a DuckDuckGo search. Returns ``{name: result|None}``. Best-effort."""
    norm: list[dict] = []
    seen: set[str] = set()
    for t in (targets or []):
        if isinstance(t, str):
            t = {"name": t, "floor": None}
        name = (t.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        norm.append({"name": name, "floor": t.get("floor"), "rents": t.get("rents") or []})
    if not norm:
        return {}
    # ~per-building budget + one WAF warm-up.
    timeout = 40.0 + 30.0 * len(norm)
    out = _run_isolated(_browser_find_many, norm, kind, timeout=timeout)
    if out is None:  # timeout/crash → no result (we never link an unverified unit)
        out = {"results": {}}
    results = dict(out.get("results") or {})
    # No web-search fallback: a same-building page can't confirm THIS unit's floor
    # + rent, so a name HOMES blocked or missed simply yields no link.
    for t in norm:
        results.setdefault(t["name"], None)
    return results
