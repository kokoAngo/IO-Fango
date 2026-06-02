"""Fetch a URL and extract Open Graph (OGP) preview metadata.

Used to unfurl external listing links (SUUMO/HOMES) into a preview card: parse
``og:image`` / ``og:title`` / ``og:description``, and (separately) download the
og:image bytes so we can self-host them via :mod:`fango.uploads`.

All network access is best-effort and guarded:
* only http(s);
* an SSRF guard rejects hosts that resolve to private / loopback / reserved IPs
  (so this can't be aimed at internal services);
* short timeouts and a hard body-size cap.
"""
from __future__ import annotations

import html as _html
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

from .config import load_settings

log = logging.getLogger(__name__)

_HTML_READ_CAP = 1_000_000          # bytes of HTML we read for meta tags
_IMAGE_READ_CAP = 5 * 1024 * 1024   # matches uploads.MAX_SIZE_BYTES
_TIMEOUT = 6.0

_META_RE = re.compile(r"<meta\b[^>]*?>", re.I | re.S)
_ATTR_RE = re.compile(
    r'([a-zA-Z_:][\w:-]*)\s*=\s*"([^"]*)"'
    r"|([a-zA-Z_:][\w:-]*)\s*=\s*'([^']*)'"
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _is_safe_url(url: str) -> bool:
    """True only for http(s) URLs whose host resolves exclusively to public
    IPs — an SSRF guard for fetching arbitrary remote URLs."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = p.hostname
    if not host:
        return False
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def _client():
    import httpx
    ua = load_settings().external_lookup_ua
    return httpx.Client(
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": ua, "Accept-Language": "ja,en;q=0.8"},
    )


def fetch_html(url: str) -> str | None:
    """GET ``url`` and return up to ``_HTML_READ_CAP`` bytes of decoded text,
    or None on any failure / unsafe URL / non-HTML response."""
    if not _is_safe_url(url):
        return None
    try:
        with _client() as client, client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                return None
            ctype = resp.headers.get("content-type", "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype and not ctype.startswith("text/"):
                return None
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) >= _HTML_READ_CAP:
                    break
            enc = resp.encoding or "utf-8"
            try:
                return bytes(buf).decode(enc, errors="replace")
            except (LookupError, UnicodeDecodeError):
                return bytes(buf).decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - network variance
        log.debug("fetch_html failed for %s: %s", url, exc)
        return None


def _parse_meta(html_text: str) -> dict[str, str]:
    """Map of ``property``/``name`` → ``content`` for every <meta> tag. First
    occurrence wins."""
    out: dict[str, str] = {}
    for tag in _META_RE.findall(html_text):
        attrs: dict[str, str] = {}
        for m in _ATTR_RE.finditer(tag):
            k = (m.group(1) or m.group(3) or "").lower()
            v = m.group(2) if m.group(2) is not None else m.group(4)
            if k:
                attrs[k] = v
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs:
            out.setdefault(key, attrs["content"])
    return out


def parse_ogp(html_text: str, base_url: str) -> dict:
    """Parse OGP/meta out of already-fetched HTML. Pure (no network) so it's
    easy to unit-test. Returns ``{url, image, title, description, site}``."""
    meta = _parse_meta(html_text)
    image = (meta.get("og:image") or meta.get("og:image:url")
             or meta.get("twitter:image") or meta.get("twitter:image:src"))
    title = meta.get("og:title") or meta.get("twitter:title")
    if not title:
        m = _TITLE_RE.search(html_text)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
    desc = (meta.get("og:description") or meta.get("twitter:description")
            or meta.get("description"))
    site = meta.get("og:site_name")
    if image:
        image = urljoin(base_url, _html.unescape(image.strip()))
    return {
        "url": base_url,
        "image": image or None,
        "title": _html.unescape(title).strip() if title else None,
        "description": _html.unescape(desc).strip() if desc else None,
        "site": _html.unescape(site).strip() if site else None,
    }


def fetch_ogp(url: str) -> dict | None:
    """Fetch ``url`` and parse OGP/meta. Returns the :func:`parse_ogp` dict, or
    None if the page couldn't be fetched."""
    html_text = fetch_html(url)
    if html_text is None:
        return None
    return parse_ogp(html_text, url)


def fetch_image_bytes(url: str, *, max_bytes: int = _IMAGE_READ_CAP) -> bytes | None:
    """Download an image URL (size-capped). Returns raw bytes, or None on
    failure / unsafe URL / oversize."""
    if not _is_safe_url(url):
        return None
    try:
        with _client() as client, client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                return None
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    return None
            return bytes(buf)
    except Exception as exc:  # pragma: no cover - network variance
        log.debug("fetch_image_bytes failed for %s: %s", url, exc)
        return None
