"""Best-effort discovery of a public SUUMO / HOMES listing URL by building name.

We search each site's results page for the building name and grab the first
link that matches a *detail-page* URL pattern, verifying the building name is
actually present so we don't attach an unrelated property.

⚠️ This scrapes third-party search HTML. It is inherently brittle (markup
changes, anti-bot pages) and ToS-sensitive. Everything is best-effort and
returns ``None`` on any doubt. The two things most likely to need maintenance —
the **search-URL templates** and the **detail-link patterns** — are isolated as
the module constants below. Outbound calls are gated by a kill-switch
(``FANGO_EXTERNAL_LOOKUP_ENABLED``), rate-limited, and cached per listing by the
caller (see :mod:`fango.listings.enrich`).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote, urljoin, urlparse

from .. import unfurl

log = logging.getLogger(__name__)

# Search-results endpoints, by source. ``{q}`` is the URL-encoded building name.
# These are deliberately simple keyword entry points; adjust here if a site
# changes its search routing.
_SEARCH_ENDPOINTS: dict[str, str] = {
    "homes": "https://www.homes.co.jp/list/?keyword={q}",
    "suumo": "https://suumo.jp/edit/kessaku/?keyword={q}",
}

# Per-source regexes matching a *detail page* href (absolute or site-relative).
_DETAIL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "suumo": (
        re.compile(r'https?://(?:www\.)?suumo\.jp/(?:chintai|ms|ikkodate|tochi)/[^\s"\'<>]+', re.I),
        re.compile(r'/(?:chintai|ms)/(?:jnc_|bc_|mansion/|nc_)[^\s"\'<>]+', re.I),
    ),
    "homes": (
        re.compile(r'https?://(?:www\.)?homes\.co\.jp/(?:chintai|mansion|kodate|tochi)/[^\s"\'<>]+', re.I),
        re.compile(r'/(?:chintai/room|mansion/b-|kodate/b-)[^\s"\'<>]+', re.I),
    ),
}

# Try sources in this order; first verified hit wins.
_SOURCE_ORDER = ("homes", "suumo")

_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)


def _normalize(s: str) -> str:
    """Loose comparison key: drop whitespace + common building suffixes/marks."""
    s = re.sub(r"\s+", "", s or "")
    return s.replace("　", "").lower()


def source_of_url(url: str) -> str | None:
    """Return 'suumo' / 'homes' if the URL is a detail page on that site."""
    host = (urlparse(url).hostname or "").lower()
    if "suumo.jp" in host:
        return "suumo"
    if "homes.co.jp" in host:
        return "homes"
    return None


def _extract_first_listing(html: str, source: str, base_url: str) -> str | None:
    """First href in ``html`` matching ``source``'s detail-page patterns,
    returned as an absolute URL. Pure (no network) → unit-testable."""
    patterns = _DETAIL_PATTERNS.get(source, ())
    for href in _HREF_RE.findall(html):
        for pat in patterns:
            if pat.search(href):
                return urljoin(base_url, href.strip())
    return None


def find_external_url(
    building_name: str,
    *,
    ward: str | None = None,
    address: str | None = None,
    source_url: str | None = None,
) -> dict | None:
    """Find a SUUMO/HOMES detail URL for ``building_name``. Returns
    ``{"url": ..., "source": ...}`` or None. Best-effort, never raises.

    If ``source_url`` is already a SUUMO/HOMES detail page, it is returned
    directly (no scraping needed)."""
    if source_url:
        src = source_of_url(source_url)
        if src:
            return {"url": source_url, "source": src}

    name = (building_name or "").strip()
    if not name:
        return None
    key = _normalize(name)
    q = quote(f"{name} {ward}".strip() if ward else name)

    for source in _SOURCE_ORDER:
        endpoint = _SEARCH_ENDPOINTS.get(source)
        if not endpoint:
            continue
        search_url = endpoint.format(q=q)
        html = unfurl.fetch_html(search_url)
        if not html:
            continue
        # Verify the building actually appears in the results before trusting
        # the first detail link (keyword search can drift to neighbours).
        if key and key not in _normalize(html):
            continue
        hit = _extract_first_listing(html, source, search_url)
        if hit:
            return {"url": hit, "source": source}
    return None
