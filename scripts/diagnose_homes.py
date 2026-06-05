#!/usr/bin/env python3
"""Diagnose why HOMES link-preview cards show NO image on the server.

A card with no image + the note "同じ建物の別の住戸（参考…）" and a building-page
URL (…/chintai/b-<id>/) is a DuckDuckGo-fallback result. The fallback returns the
URL only — no og:image — and fires whenever the Playwright browser path can't
run. The browser path is what fetches the photo, so when it's unavailable EVERY
link degrades to image-less.

This checks the three things that force the fallback, in order, and prints which
one is the problem on THIS machine:

    .venv/bin/python scripts/diagnose_homes.py

Run it on the server, from the repo root.
"""
from __future__ import annotations

import sys
import time


def main() -> int:
    print("=== HOMES image / browser diagnostic ===")

    # 1) Is the playwright package importable? (the sync_api import is the real
    # test — don't touch playwright.__version__, which doesn't exist as an attr.)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"playwright package: MISSING → {exc!r}")
        print("\nDIAGNOSIS: the optional 'playwright' extra isn't installed.")
        print("FIX:\n  .venv/bin/pip install playwright")
        print("  .venv/bin/playwright install chromium")
        print("  sudo .venv/bin/playwright install-deps   # system libs for headless Chrome")
        print("  sudo systemctl restart fangoio")
        return 1
    try:
        from importlib.metadata import version
        ver = version("playwright")
    except Exception:
        ver = "?"
    print(f"playwright package: OK (v{ver})")

    # 2) Can we actually launch a browser? (system chrome → bundled chromium)
    from fango.listings import external_lookup as el
    launched = None
    with sync_playwright() as p:
        try:
            b = el._launch(p)
            launched = b.browser_type.name
            b.close()
            print(f"browser launch: OK (via {launched})")
        except Exception as exc:
            print(f"browser launch: FAILED → {exc!r}")
            print("\nDIAGNOSIS: playwright is installed but no browser binary is.")
            print("FIX:\n  .venv/bin/playwright install chromium")
            print("  sudo .venv/bin/playwright install-deps")
            print("  sudo systemctl restart fangoio")
            return 1

    # 3) Browser works — does HOMES actually serve us results, or WAF-block us?
    print("\nrunning a live HOMES lookup for a known building (プラウド) …")
    t = time.time()
    try:
        res = el.find_listings([{"name": "プラウド", "floor": None}], kind="rent")
    except Exception as exc:
        print(f"live lookup raised: {exc!r}")
        return 1
    r = res.get("プラウド")
    dt = time.time() - t
    if not r:
        print(f"  no result in {dt:.1f}s (neither browser nor DDG found anything)")
        return 1
    has_img = bool(r.get("image"))
    is_room = "/room/" in (r.get("url") or "")
    print(f"  url:   {r.get('url')}")
    print(f"  image: {r.get('image')!r}")
    print(f"  note:  {r.get('note')!r}   ({dt:.1f}s)")
    print()
    if has_img and is_room:
        print("DIAGNOSIS: browser path HEALTHY here — got a room page WITH an image.")
        print("If live cards are still image-less, they're CACHED from before the")
        print("browser was set up. Re-resolve them (clear listing_external_links")
        print("rows with source='homes' AND image IS NULL, or where note is set).")
        return 0
    print("DIAGNOSIS: browser launches but the result is a no-image DDG fallback →")
    print("HOMES's AWS WAF is blocking this server's IP (datacenter IPs get")
    print("challenged far more than residential). The link still works; we just")
    print("can't fetch the photo while blocked. Options: a residential/proxy")
    print("egress for the lookup, or accept text-only cards from this host.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
