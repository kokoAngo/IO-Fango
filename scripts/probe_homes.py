"""Probe HOMES — fetch a page with browser-like headers and report what we get.

    python scripts/probe_homes.py                         # opens /chintai/
    python scripts/probe_homes.py <url>                   # opens any HOMES url

Prints HTTP status, body size, whether we look blocked, the <title>, and any
<form> actions + input names + search-ish links found on the page (to discover
the real building-search endpoint). Read-only.
"""
import sys
import re

import httpx

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.homes.co.jp/chintai/"

# A full, realistic desktop-Chrome header set (more than just User-Agent —
# HOMES may gate on Accept / Sec-Fetch / language).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "no-cache",
}


def main() -> None:
    print(f"GET {URL}\n")
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True, headers=HEADERS) as c:
            r = c.get(URL)
    except Exception as exc:
        print("REQUEST FAILED:", repr(exc))
        return

    body = r.text or ""
    print(f"status        : {r.status_code}")
    print(f"final url     : {r.url}")
    print(f"content-type  : {r.headers.get('content-type')}")
    print(f"body size     : {len(body)} chars")
    blocked = (r.status_code in (403, 429)) or len(body) < 2000
    print(f"looks blocked : {blocked}")

    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if title:
        print(f"title         : {re.sub(r'\\s+', ' ', title.group(1)).strip()[:120]}")

    forms = re.findall(r"<form\b[^>]*>", body, re.I)
    if forms:
        print(f"\nforms ({len(forms)}):")
        for f in forms[:8]:
            action = re.search(r'action\s*=\s*["\']([^"\']+)["\']', f, re.I)
            method = re.search(r'method\s*=\s*["\']([^"\']+)["\']', f, re.I)
            print(f"  action={action.group(1) if action else '(none)'}  method={method.group(1) if method else 'GET'}")

    # input/select names — the query params a search form submits
    names = sorted(set(re.findall(r'<(?:input|select)\b[^>]*\bname\s*=\s*["\']([^"\']+)["\']', body, re.I)))
    if names:
        print(f"\ninput/select names ({len(names)}): {', '.join(names[:40])}")

    # search-ish links
    links = sorted(set(re.findall(r'href\s*=\s*["\'](/[^"\']*(?:list|search|kensaku|ichiran)[^"\']*)["\']', body, re.I)))
    if links:
        print(f"\nsearch-ish links ({len(links)}):")
        for l in links[:15]:
            print(f"  {l}")


if __name__ == "__main__":
    main()
