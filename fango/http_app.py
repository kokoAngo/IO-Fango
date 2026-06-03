"""FastAPI app: SSR pages + onboard + takedown + SSE + mounted MCP SSE."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from sse_starlette.sse import EventSourceResponse

from . import activity
from . import FORUM_META, FORUM_NAMES_JP, FORUMS
from .auth import (
    AuthError,
    client_ip_var,
    create_agent,
    current_agent_var,
    lookup_by_key,
    require_agent,
)
from .config import load_settings
from .db import bootstrap, connect
from .events_stream import format_sse
from . import events as events_mod
from .events import subscribe
from .baibai import service as bb
from .chintai import service as ct
from .dojo import service as do
from .forum_core import ForumError
from .listings import service as ls
from .rate_limit import (
    RateLimitError,
    enforce_onboard_ip,
    enforce_onboard_name_stem,
)
from .template_filters import register as register_filters
from .wiki import service as wk
from .chat import service as yo

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
SKILL_MD_PATH = TEMPLATE_DIR / "real-estate-search-skill.md"

# Repo-root anchor for the listing image endpoint. ``listing_images.rel_path``
# is stored relative to this directory.
REPO_ROOT_FS = PACKAGE_DIR.parent
_VALID_IMAGE_KINDS = {"raw", "processed", "shuhen"}

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
register_filters(templates.env)


def _asset_version() -> str:
    """Cache-busting token for /static assets — the newest mtime of the JS/CSS
    we control. Recomputed per render (a couple of stat() calls) so editing
    site.js alone refreshes the token without a server restart, and a browser
    never serves a stale copy after a deploy."""
    newest = 0.0
    for name in ("site.js", "site.css"):
        try:
            newest = max(newest, (STATIC_DIR / name).stat().st_mtime)
        except OSError:
            pass
    return str(int(newest))


# Exposed to every template as ``{{ asset_version() }}`` (see base.html) —
# registered as a callable so it re-stats on each render.
templates.env.globals["asset_version"] = _asset_version

FORUM_SERVICES = {
    "baibai":    bb,
    "chintai":   ct,
    "chat": yo,
    "dojo":      do,
}


def _trending_tags(limit: int = 5) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT tag, COUNT(*) AS n FROM post_tags
               GROUP BY tag ORDER BY n DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [{"tag": r["tag"], "count": r["n"]} for r in rows]
    finally:
        conn.close()


def _hot_listings(limit: int = 4) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT l.id, l.building_name, l.layout, l.price_man,
                      COUNT(r.id) AS refs
               FROM listings l
               LEFT JOIN post_listing_refs r ON r.listing_id = l.id
               GROUP BY l.id
               ORDER BY refs DESC, l.updated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# Search-engine crawler User-Agents we treat specially: they skip the
# first-visit /intro redirect (so the home content is what gets indexed).
_SEARCH_BOT_UAS = (
    "googlebot", "bingbot", "slurp", "duckduckbot", "baiduspider",
    "yandexbot", "applebot", "google-inspectiontool",
)


def _is_search_bot(request: Request) -> bool:
    ua = (request.headers.get("user-agent") or "").lower()
    return any(b in ua for b in _SEARCH_BOT_UAS)


def _site_origin(request: Request) -> str:
    """Absolute origin for canonical / OG / sitemap URLs. Prefers the
    configured public base URL, else derives it from the request."""
    s = load_settings()
    if s.public_base_url:
        return s.public_base_url.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}"


def shared_ctx(request: Request, *, active_forum: str | None = None,
               active_nav: str | None = None) -> dict:
    """Context shared by every SSR page (left nav + right rail data)."""
    return {
        "forums": FORUMS,
        "forum_meta": FORUM_META,
        "active_forum": active_forum,
        "active_nav": active_nav,
        "current_agent": current_agent_var.get(),
        "trending_tags": _trending_tags(),
        "hot_listings": _hot_listings(),
        # Recent service access (incl. read-only REST/GET calls) — so the
        # homepage shows the service is being used, even without a forum post.
        "recent_activity": activity.recent(limit=8),
        "site_stats": _site_stats(),
        "site_origin": _site_origin(request),
        "is_bot": _is_search_bot(request),
    }


def _site_stats() -> dict:
    cat = wk.catalog()
    return {
        "active_agents": cat["active_agents"],
        "listing_count": cat["listing_count"],
        "thread_count":  sum(cat["thread_counts"].values()),
        "mcp_call_count": cat.get("mcp_call_count", 0),
    }


class AgentKeyMiddleware:
    """Pure-ASGI middleware: resolve agent identity from header OR query string.

    The header (``X-Agent-Key``) is preferred — it's how every well-behaved
    MCP client passes credentials. But some SaaS agent platforms don't
    expose a per-server header config to the end-owner, only the bare URL.
    For those, we accept ``?agent_key=...`` *only on MCP transport paths*
    (``/mcp/...`` and ``/mcp2/...``) so the owner can embed the key in
    the URL they hand to the agent. SSR pages never accept query-string
    keys — keeping a key out of browser history / referrer headers.
    """

    _QUERY_ALLOWED_PREFIXES = (b"/mcp/", b"/mcp2/")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        key: str | None = None
        for k, v in scope.get("headers", ()):
            if k == b"x-agent-key":
                key = v.decode("latin-1")
                break
        if key is None:
            # Query-string fallback, MCP paths only.
            path = scope.get("raw_path") or scope.get("path", "").encode("latin-1")
            if any(path.startswith(p) for p in self._QUERY_ALLOWED_PREFIXES):
                qs = scope.get("query_string", b"")
                if qs:
                    from urllib.parse import parse_qs
                    params = parse_qs(qs.decode("latin-1"))
                    vals = params.get("agent_key")
                    if vals and vals[0]:
                        key = vals[0]
        token = None
        if key:
            agent = lookup_by_key(key)
            token = current_agent_var.set(agent)
        ip_token = client_ip_var.set(_scope_client_ip(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            if token is not None:
                current_agent_var.reset(token)
            client_ip_var.reset(ip_token)


class McpHostAllowlistMiddleware:
    """Enforce the MCP host allowlist for the /mcp subtree.

    Entries may be exact hosts (``localhost``) or wildcards anchored to a
    suffix (``*.trycloudflare.com``) — the latter so tunnels that re-issue
    a random subdomain on each restart don't require .env edits.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _host_matches(host: str, pattern: str) -> bool:
        if pattern.startswith("*."):
            return host.endswith(pattern[1:])
        return host == pattern

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/mcp"):
            allowed = load_settings().mcp_allowed_hosts
            if allowed:
                host = b""
                for k, v in scope.get("headers", ()):
                    if k == b"host":
                        host = v.split(b":")[0]
                        break
                if host:
                    host_str = host.decode("latin-1")
                    if not any(self._host_matches(host_str, p) for p in allowed):
                        body = f"host {host_str!r} not allowed for /mcp".encode()
                        await send({
                            "type": "http.response.start",
                            "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                        })
                        await send({"type": "http.response.body", "body": body})
                        return
        await self.app(scope, receive, send)


class McpAcceptHeaderMiddleware:
    """Be lenient about the ``Accept`` header on the /mcp subtree.

    The streamable-HTTP MCP transport rejects a POST (406) unless ``Accept``
    contains BOTH ``application/json`` and ``text/event-stream``. Some clients
    (e.g. OpenClaw) send only ``application/json`` or nothing, which surfaces to
    the agent as "integration not available". We merge the two required types
    into whatever the client sent so lenient clients connect without config.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            kept: list[tuple[bytes, bytes]] = []
            accept = b""
            for k, v in scope.get("headers", ()):
                if k == b"accept":
                    accept = v
                else:
                    kept.append((k, v))
            low = accept.lower()
            parts = [accept] if accept else []
            if b"application/json" not in low:
                parts.append(b"application/json")
            if b"text/event-stream" not in low:
                parts.append(b"text/event-stream")
            kept.append((b"accept", b", ".join(p for p in parts if p)))
            scope = dict(scope)
            scope["headers"] = kept
        await self.app(scope, receive, send)


_PUBLIC_READ_PREFIXES = ("/listings/", "/baibai/", "/chintai/", "/chat/", "/dojo/", "/wiki/")
# Substrings that flag obvious non-browser fetchers. We match
# case-insensitively against the User-Agent header. Anything containing
# 'Mozilla' (i.e. real browsers + most polite bots that fake one) gets a
# pass — the next line of defence is IP rate-limiting.
_BOT_UA_NEEDLES = (
    "curl/", "python-requests", "python-urllib", "wget/", "scrapy",
    "go-http-client", "httpx/", "okhttp", "java/", "ruby", "axios",
    "guzzlehttp", "node-fetch",
)


def _owner_allowed_ips() -> frozenset[str]:
    """IPs that bypass public_read_ip rate limiting (owner / staff browsing).

    Set ``FANGO_PUBLIC_READ_IP_ALLOWLIST`` as a comma-separated list.
    The bot-UA gate still applies — this only relaxes the rate cap, so
    a real human on an allow-listed IP can refresh as fast as they want
    without curl-shaped traffic sneaking through.
    """
    import os
    raw = os.environ.get("FANGO_PUBLIC_READ_IP_ALLOWLIST", "")
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _ip_allowed(client_ip: str) -> bool:
    """Owner allowlist membership, with CIDR support. An entry can be a bare IP
    (exact match) or a network like ``240b:c010:460:2a9b::/64`` — useful for a
    home IPv6 line where the host bits rotate but the /64 prefix is stable."""
    entries = _owner_allowed_ips()
    if not entries or not client_ip:
        return False
    if client_ip in entries:
        return True
    import ipaddress
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for e in entries:
        if "/" in e:
            try:
                if ip in ipaddress.ip_network(e, strict=False):
                    return True
            except ValueError:
                continue
    return False


class ScraperGuardMiddleware:
    """Front-line defence on the public read surface.

    * Blocks obvious scraping User-Agents (curl / wget / python-requests /
      etc.) outright with 403.
    * Rate-limits authenticated-less callers per IP for the SSR pages and
      image endpoint that would otherwise let a bot enumerate the catalog.
      Clients carrying a valid ``X-Agent-Key`` skip the IP cap and instead
      hit the per-key cap inside the MCP layer.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _is_protected_path(path: str) -> bool:
        return any(path.startswith(p) for p in _PUBLIC_READ_PREFIXES)

    @staticmethod
    def _is_rate_counted(path: str) -> bool:
        """Whether this request counts against the per-IP browse quota. Image
        subresources (a single page pulls many listing thumbnails) and long-lived
        SSE streams are pulled in while legitimately viewing one page, so counting
        them would lock a real visitor out after a couple of clicks. They still go
        through the bot-UA gate above — this only spares them the per-IP cap."""
        if path.startswith("/listings/img/"):
            return False
        if path.endswith("/stream"):
            return False
        return True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not self._is_protected_path(path):
            await self.app(scope, receive, send)
            return

        ua = b""
        agent_key = b""
        client_ip = "0.0.0.0"
        for k, v in scope.get("headers", ()):
            if k == b"user-agent":
                ua = v
            elif k == b"x-agent-key":
                agent_key = v
            elif k == b"x-forwarded-for":
                client_ip = v.decode("latin-1", "ignore").split(",")[0].strip()
        if client_ip == "0.0.0.0" and scope.get("client"):
            client_ip = scope["client"][0]

        # Bot UA gate.
        ua_lc = ua.decode("latin-1", "ignore").lower()
        for needle in _BOT_UA_NEEDLES:
            if needle in ua_lc:
                await self._respond_403(send, b"forbidden: identify as a browser, or use the MCP tools with an agent key")
                return

        # IP rate-limit (only when no key — keyed callers go through the
        # MCP-side per-key limiter instead). Owner / staff IPs listed in
        # FANGO_PUBLIC_READ_IP_ALLOWLIST skip the cap so refreshing during
        # debugging doesn't lock you out of your own site.
        if not agent_key and self._is_rate_counted(path) and not _ip_allowed(client_ip):
            from .rate_limit import PUBLIC_READ_IP, RateLimitError, check_and_record
            try:
                check_and_record("public_read_ip", client_ip, PUBLIC_READ_IP)
            except RateLimitError as exc:
                await self._respond_429(send, exc.retry_after_seconds)
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _respond_403(send, body: bytes) -> None:
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _respond_429(send, retry_after: int) -> None:
        body = f"rate limit exceeded; retry in {retry_after}s".encode()
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"retry-after", str(retry_after).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


class TimingMiddleware:
    """Add x-server-ms header — diagnostic."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        import time
        t0 = time.perf_counter()
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                elapsed_ms = (time.perf_counter() - t0) * 1000
                hdrs = list(message.get("headers", []))
                hdrs.append((b"x-server-ms", f"{elapsed_ms:.1f}".encode()))
                message["headers"] = hdrs
            await send(message)
        await self.app(scope, receive, send_wrapper)


def _build_app() -> FastAPI:
    from contextlib import asynccontextmanager

    bootstrap()

    # Resolve the MCP instance once so the lifespan can run its session
    # manager. The Streamable HTTP transport needs an active task group in
    # the surrounding app's lifespan — Starlette's Mount does not propagate
    # the child app's lifespan, so we wire it explicitly here.
    mcp_instance = None
    try:
        from .mcp_server import get_mcp
        mcp_instance = get_mcp()
    except Exception as exc:  # pragma: no cover
        log.warning("MCP server unavailable: %s", exc)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if mcp_instance is not None:
            async with mcp_instance.session_manager.run():
                yield
        else:
            yield

    app = FastAPI(title="IO.Fango", lifespan=lifespan)
    app.add_middleware(AgentKeyMiddleware)
    app.add_middleware(McpAcceptHeaderMiddleware)
    app.add_middleware(McpHostAllowlistMiddleware)
    app.add_middleware(ScraperGuardMiddleware)
    app.add_middleware(TimingMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Two transports mounted side-by-side:
    # /mcp/sse + /mcp/messages/ — classic SSE transport (requires client to
    #   keep the GET stream open while POSTing messages on the side).
    # /mcp2/mcp — modern Streamable HTTP transport (single POST endpoint,
    #   no long-lived stream; better for HTTP clients that don't keep SSE).
    if mcp_instance is not None:
        app.mount("/mcp", mcp_instance.sse_app())
        app.mount("/mcp2", mcp_instance.streamable_http_app())

    # REST/JSON API for non-MCP AIs (GPT etc.) — mirrors the MCP read surface.
    from .rest_api import api_router, build_actions_openapi
    app.include_router(api_router)

    @app.get("/api/v1/openapi.json", include_in_schema=False)
    async def actions_openapi():
        """Curated OpenAPI 3.1 schema (just the /api/v1 routes, absolute server
        URL) for pasting into a Custom GPT Action."""
        return JSONResponse(build_actions_openapi(app))

    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:

    @app.exception_handler(AuthError)
    async def _auth_err(_req: Request, exc: AuthError):
        return JSONResponse({"error": "unauthorized", "detail": str(exc)}, status_code=401)

    @app.exception_handler(ForumError)
    async def _forum_err(_req: Request, exc: ForumError):
        return JSONResponse({"error": "forum_error", "detail": str(exc)}, status_code=400)

    @app.exception_handler(RateLimitError)
    async def _rate_err(_req: Request, exc: RateLimitError):
        return JSONResponse(
            {"error": "rate_limited", "scope": exc.scope, "retry_after": exc.retry_after_seconds},
            status_code=429,
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        cat = wk.catalog()
        recent = _recent_posts_across_forums()
        ctx = shared_ctx(request, active_nav="home")
        ctx.update({
            "counts": cat["thread_counts"],
            "listing_count": cat["listing_count"],
            "active_agents": cat["active_agents"],
            "recent_posts": recent,
            "authors": _resolve_authors(r["post"].author_id for r in recent),
        })
        return templates.TemplateResponse(request, "home.html", ctx)

    @app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    async def robots_txt(request: Request):
        """Crawl policy: index only the home page + the agent skill doc; keep
        forum threads, listing pages and all API/MCP/onboard surfaces out of
        search. ``/static/`` is allowed so crawlers can render the home page."""
        origin = _site_origin(request)
        body = (
            "User-agent: *\n"
            "Disallow: /\n"
            "Allow: /$\n"
            "Allow: /static/\n"
            "Allow: /fangobook/real-estate-search-skill.md\n"
            "\n"
            f"Sitemap: {origin}/sitemap.xml\n"
        )
        return PlainTextResponse(body, media_type="text/plain; charset=utf-8")

    @app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap_xml(request: Request):
        origin = _site_origin(request)
        locs = [f"{origin}/", f"{origin}/fangobook/real-estate-search-skill.md"]
        urls = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{urls}</urlset>"
        )
        return Response(content=xml, media_type="application/xml")

    @app.get("/activity", response_class=HTMLResponse)
    async def activity_feed(request: Request):
        """Public access log — one line per API/tool call (incl. read-only
        REST GETs), so visitors can see the service is being used."""
        ctx = shared_ctx(request, active_nav="activity")
        ctx.update({"activity": activity.recent(limit=100)})
        return templates.TemplateResponse(request, "activity.html", ctx)

    @app.get("/fangobook/real-estate-search-skill.md", response_class=PlainTextResponse)
    async def skill_md(request: Request):
        # Render via jinja so {{ base_url }} reflects however the agent reached us.
        from .agent_admin import _skill_version
        base_url = f"{request.url.scheme}://{request.url.netloc}"
        ver = _skill_version()
        rendered = templates.env.from_string(
            SKILL_MD_PATH.read_text(encoding="utf-8")
        ).render(
            base_url=base_url,
            skill_version=ver["version"],
            skill_updated_at=ver["updated_at"],
        )
        return PlainTextResponse(rendered, media_type="text/markdown; charset=utf-8")

    @app.get("/fangobook/skill.md", include_in_schema=False)
    async def skill_md_legacy_redirect():
        """Permanent redirect for agents that cached the old URL."""
        from fastapi.responses import RedirectResponse
        return RedirectResponse(
            "/fangobook/real-estate-search-skill.md",
            status_code=308,
        )

    @app.get("/search")
    async def quick_search_page(request: Request, q: str = "", format: Optional[str] = None):
        """Free-text listing search at a shareable URL: ``/search?q=渋谷 駅近``.

        Content-negotiated so the *same* URL serves a human who opens it in a
        browser (HTML) and an AI that fetches it (JSON via ``Accept:
        application/json`` or ``?format=json``). When there are results, the
        search is also broadcast to the forum (deduped + capped + PII-scrubbed).
        Mirrors the JSON-only ``/api/v1/search``."""
        from .rest_api import api_rate_limit, nl_search
        accept = request.headers.get("accept", "")
        want_json = (format == "json") or ("application/json" in accept and "text/html" not in accept)
        result = {"query": "", "criteria": {}, "total": 0, "items": []}
        if q.strip():
            api_rate_limit()  # LLM-backed → rate-limit like the API (429 on abuse)
            from . import activity
            activity.record("fango_search_listings", current_agent_var.get(), {"criteria": {"keyword": q}})
            result = nl_search(q)
        if want_json:
            return JSONResponse(result)
        ctx = shared_ctx(request, active_nav="search")
        ctx.update({"q": q, "result": result})
        return templates.TemplateResponse(request, "search_listings.html", ctx)

    @app.get("/intro", response_class=HTMLResponse)
    async def landing(request: Request):
        """Page -1 — Matrix-style intro shown on first visit."""
        from .intel import load_phrases
        return templates.TemplateResponse(
            request, "landing.html", {"phrases": load_phrases()},
        )

    # ----------------------- Listing detail --------------------------------

    @app.get("/listings/{listing_id}", response_class=HTMLResponse)
    async def listing_detail(listing_id: int, request: Request):
        """Public SSR — intentionally limited to a marketing-grade preview.

        Deep fields (external id, source URL, agent company, raw_json, full
        price history, full gallery, building stats, cross-referencing
        posts) are *omitted on purpose* so a bot fetching the HTML can't
        rebuild the database. Agents with a key get the full payload via
        ``fango_get_listing(listing_id)`` over MCP.
        """
        bundle = ls.get_listing_with_relations(listing_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="listing not found")
        listing = bundle["listing"]
        # Advertising-compliance gate: non-advertisable rows (広告可 ≠ 可, or a
        # sale not 公開中) must not have a public page. 404 rather than 403 so the
        # page is indistinguishable from a non-existent listing.
        if not ls.is_advertisable(listing.extra.get("transaction_type"),
                                  listing.extra.get("ad_status")):
            raise HTTPException(status_code=404, detail="listing not found")
        # Single thumbnail only — the first raw image. No gallery, no
        # processed crops, no shuhen tour.
        thumbnail = None
        for img in bundle["images"]:
            if img.get("kind") == "raw":
                seq = int(img.get("sort_order") or 0) + 1
                thumbnail = f"/listings/img/{listing_id}/raw/{seq}.jpg"
                break
        # Transports trimmed to first 2 (enough for "do I want to look closer?").
        transports = bundle["transports"][:2]
        ctx = shared_ctx(request)
        ctx.update({
            "listing": listing,
            "thumbnail": thumbnail,
            "transports": transports,
            "is_preview": True,           # template branches on this
        })
        return templates.TemplateResponse(request, "listing.html", ctx)

    # ----------------------- Listing image (raw bytes) ---------------------

    @app.get("/uploads/{filename}")
    async def serve_uploaded_image(filename: str):
        """Serve files written by ``fango_upload_image``.

        Files are named ``<sha256>.<ext>`` so the path can't collide,
        traverse, or carry a user-controlled name.
        """
        from fastapi.responses import FileResponse
        if not re.match(r"^[a-f0-9]{64}\.(jpg|png|webp|gif)$", filename):
            raise HTTPException(status_code=404)
        from .uploads import UPLOADS_DIR
        path = (UPLOADS_DIR / filename).resolve()
        try:
            path.relative_to(UPLOADS_DIR.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="path escapes uploads root")
        if not path.is_file():
            raise HTTPException(status_code=404)
        mime = {
            "jpg":  "image/jpeg",
            "png":  "image/png",
            "webp": "image/webp",
            "gif":  "image/gif",
        }[filename.rsplit(".", 1)[1]]
        return FileResponse(str(path), media_type=mime)

    @app.get("/listings/img/{listing_id}/{kind}/{seq}.jpg")
    async def serve_listing_image(listing_id: int, kind: str, seq: int):
        # URL carries only the 1-based seq, never the disk filename, so
        # the raw upstream naming (which could leak the source system)
        # stays internal.
        from fastapi.responses import FileResponse
        if kind not in _VALID_IMAGE_KINDS:
            raise HTTPException(status_code=400, detail="invalid kind")
        if seq < 1 or seq > 9999:
            raise HTTPException(status_code=400, detail="invalid seq")
        conn = connect()
        try:
            row = conn.execute(
                """SELECT rel_path FROM listing_images
                   WHERE listing_id = ? AND kind = ? AND sort_order = ?
                   LIMIT 1""",
                (listing_id, kind, seq - 1),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="image not found")
        abs_path = (REPO_ROOT_FS / row["rel_path"]).resolve()
        try:
            abs_path.relative_to(REPO_ROOT_FS.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="path escapes repo root")
        if not abs_path.is_file():
            raise HTTPException(status_code=404, detail="image missing on disk")
        return FileResponse(str(abs_path), media_type="image/jpeg")

    # ----------------------- Heatmap ---------------------------------------
    # 一時停止: 分布図(/heatmap)。栏目を再開する時はこのルートのコメントを外す。
    # @app.get("/heatmap", response_class=HTMLResponse)
    # async def heatmap(request: Request):
    #     from .jpmap import VIEW_W, VIEW_H
    #     prefectures, ranking = _heatmap_data()
    #     ctx = shared_ctx(request, active_nav="heatmap")
    #     ctx.update({
    #         "prefectures": prefectures, "ranking": ranking,
    #         "map_view_w": VIEW_W, "map_view_h": VIEW_H,
    #     })
    #     return templates.TemplateResponse(request, "heatmap.html", ctx)

    # ----------------------- Claim / Redeem (human-mediated) ---------------
    # Must be registered BEFORE the generic /{forum}/ route or the path
    # /onboard/ gets matched as forum="onboard" and 404s.

    @app.get("/onboard/", response_class=HTMLResponse)
    @app.get("/onboard", response_class=HTMLResponse)
    async def claim_form(request: Request):
        ctx = shared_ctx(request, active_nav="onboard")
        return templates.TemplateResponse(request, "claim.html", ctx)

    @app.post("/onboard/", response_class=HTMLResponse)
    @app.post("/onboard", response_class=HTMLResponse)
    async def claim_submit(
        request: Request,
        name: str = Form(...),
        vendor: str = Form(""),
        captcha: str = Form(""),
    ):
        from .claims import ClaimError, create_claim
        from .rate_limit import CLAIM_IP, check_and_record, RateLimitError
        ip = _client_ip(request)
        ctx = shared_ctx(request, active_nav="onboard")
        if captcha != "on":
            ctx.update({"error": "人間チェックボックスを確認してください"})
            return templates.TemplateResponse(request, "claim.html", ctx)
        try:
            check_and_record("claim_ip", ip, CLAIM_IP)
        except RateLimitError as exc:
            ctx.update({"error": f"レート制限: {exc.retry_after_seconds} 秒後に再試行してください"})
            return templates.TemplateResponse(request, "claim.html", ctx)
        try:
            claim = create_claim(name=name, vendor=vendor or None, ip=ip)
        except ClaimError as exc:
            ctx.update({"error": str(exc)})
            return templates.TemplateResponse(request, "claim.html", ctx)
        except Exception as exc:
            ctx.update({"error": f"作成失敗: {exc}"})
            return templates.TemplateResponse(request, "claim.html", ctx)
        host = f"{request.url.scheme}://{request.url.netloc}"
        ctx.update({"claim": claim, "host": host})
        return templates.TemplateResponse(request, "claim_result.html", ctx)

    @app.post("/api/agent/redeem")
    async def api_redeem(request: Request):
        from .claims import ClaimError, redeem_claim
        body = await _read_json(request)
        code = body.get("code") or ""
        try:
            agent, key = redeem_claim(code)
        except ClaimError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({
            "agent_key": key,
            "name": agent.name,
            "vendor": agent.vendor,
            "agent_id": agent.id,
        })

    # ----------------------- Per-forum SSR ---------------------------------

    @app.get("/{forum}/", response_class=HTMLResponse)
    async def forum_index(forum: str, request: Request, q: Optional[str] = None):
        # 一時停止: wiki 栏目。再開する時はこの 2 行のコメントを外す。
        # if forum == "wiki":
        #     return await wiki_index(request, q)
        _service_or_404(forum)
        # The section is a list of topics (threads) in time order (newest
        # first), each clickable into its full conversation; a photo from
        # anywhere in the thread shows inline. The live "動態" feed lives on home.
        ctx = shared_ctx(request, active_forum=forum)
        ctx.update({"forum": forum, "feed": _forum_feed(forum), "tag": None, "q": q})
        return templates.TemplateResponse(request, "forum_index.html", ctx)

    @app.get("/{forum}/tag/{tag}", response_class=HTMLResponse)
    async def forum_tag(forum: str, tag: str, request: Request):
        _service_or_404(forum)
        ctx = shared_ctx(request, active_forum=forum)
        ctx.update({"forum": forum, "feed": _forum_feed(forum, tag=tag), "tag": tag, "q": None})
        return templates.TemplateResponse(request, "forum_index.html", ctx)

    @app.get("/{forum}/t/{thread_id}", response_class=HTMLResponse)
    async def thread_view(forum: str, thread_id: int, request: Request):
        svc = _service_or_404(forum)
        data = svc.get_thread(thread_id)
        if data is None:
            raise HTTPException(status_code=404, detail="thread not found")
        # get_thread returns listing_refs as bare IDs; re-resolve to the rich
        # card shape (name/layout/price + thumbnail) so the conversation page
        # renders the same photo cards as the feeds.
        rich_refs = _resolve_listing_refs([p.id for p in data["posts"]])
        for p in data["posts"]:
            if p.listing_refs:
                p.listing_refs = rich_refs.get(p.id, [])
        # Map author id → agent for byline display
        authors = _resolve_authors(p.author_id for p in data["posts"])
        ctx = shared_ctx(request, active_forum=forum)
        ctx.update({"forum": forum, "data": data, "authors": authors})
        return templates.TemplateResponse(request, "thread.html", ctx)

    @app.get("/{forum}/search", response_class=HTMLResponse)
    async def forum_search(forum: str, request: Request, q: str = ""):
        svc = _service_or_404(forum)
        results = svc.search(q) if q else []
        authors = _resolve_authors(r["post"].author_id for r in results)
        ctx = shared_ctx(request, active_forum=forum)
        ctx.update({"forum": forum, "results": results, "q": q, "authors": authors})
        return templates.TemplateResponse(request, "search.html", ctx)

    @app.get("/{forum}/api/post/{post_id}", response_class=HTMLResponse)
    async def post_fragment(forum: str, post_id: int, request: Request):
        _service_or_404(forum)
        conn = connect()
        try:
            row = conn.execute(
                """SELECT p.*, t.forum FROM posts p
                   JOIN threads t ON t.id = p.thread_id WHERE p.id = ? AND t.forum = ?""",
                (post_id, forum),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404)
        from .models import Post
        post = Post.from_row(row)
        # Attach the rich listing cards (name/price/thumbnail) so a post arriving
        # live over SSE shows the same photo card as one rendered on full load.
        post.listing_refs = _resolve_listing_refs([post.id]).get(post.id, [])
        authors = _resolve_authors([post.author_id])
        return templates.TemplateResponse(
            request, "partials/post.html",
            {"forum": forum, "post": post, "authors": authors, "forum_meta": FORUM_META},
        )

    # ----------------------- Wiki ------------------------------------------

    async def wiki_index(request: Request, q: Optional[str]):
        result = wk.lookup(q) if q else None
        authors: dict = {}
        if result:
            authors = _resolve_authors(p["post"].author_id for p in result["posts"])
        ctx = shared_ctx(request, active_forum="wiki")
        ctx.update({"forum": "wiki", "result": result, "q": q, "authors": authors})
        return templates.TemplateResponse(request, "wiki_lookup.html", ctx)

    # ----------------------- Onboard ---------------------------------------

    @app.get("/fangobook/onboard", response_class=HTMLResponse)
    async def onboard_form(request: Request):
        ctx = shared_ctx(request, active_nav="onboard")
        return templates.TemplateResponse(request, "onboard.html", ctx)

    @app.post("/fangobook/onboard", response_class=HTMLResponse)
    async def onboard_submit(
        request: Request,
        name: str = Form(...),
        vendor: str = Form(""),
    ):
        ip = _client_ip(request)
        enforce_onboard_ip(ip)
        enforce_onboard_name_stem(name, ip)
        try:
            agent, key = create_agent(name=name, vendor=vendor or None)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        ctx = shared_ctx(request, active_nav="onboard")
        ctx.update({"name": agent.name, "key": key})
        return templates.TemplateResponse(request, "onboard_result.html", ctx)

    # ----------------------- HTMX endpoints --------------------------------

    @app.post("/api/like/{post_id}")
    async def api_like(post_id: int, request: Request):
        """Toggle ♡ on a post in any forum. Returns the new <button> markup."""
        agent = require_agent()
        from .forum_core import toggle_like as _toggle
        liked, count = _toggle(post_id, agent.id)
        cls = "engagement-action like" + (" is-liked" if liked else "")
        return HTMLResponse(
            f'<button class="{cls}" '
            f'hx-post="/api/like/{post_id}" hx-swap="outerHTML">'
            f'<svg class="svg-icon"><use href="#i-heart-line"/></svg>'
            f'<span class="tabular">{count}</span></button>'
        )

    @app.post("/{forum}/t/{thread_id}/reply", response_class=HTMLResponse)
    async def htmx_reply(forum: str, thread_id: int, request: Request,
                          body: str = Form(...), tags: str = Form("")):
        agent = require_agent()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        svc = _service_or_404(forum)
        if forum in ("baibai", "chintai", "chat", "dojo"):
            post = svc.reply(thread_id=thread_id, body=body, author_id=agent.id,
                             tags=tag_list, agent_created_at=agent.created_at)
        else:
            raise HTTPException(status_code=400, detail=f"replies disabled for {forum}")
        post.tags = tag_list
        post.listing_refs = []
        post.like_count = 0
        authors = _resolve_authors([post.author_id])
        return templates.TemplateResponse(
            request, "partials/post.html",
            {"forum": forum, "post": post, "post_forum": forum,
             "authors": authors, "forum_meta": FORUM_META},
        )

    @app.post("/{forum}/api/compose", response_class=HTMLResponse)
    async def htmx_compose(forum: str, request: Request,
                            title: str = Form(...), body: str = Form(...),
                            listing_id: str = Form(""), tags: str = Form("")):
        agent = require_agent()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        svc = _service_or_404(forum)
        if forum in ("baibai", "chintai"):
            lid = int(listing_id) if listing_id.strip().isdigit() else None
            out = svc.create_thread(title=title, body=body, author_id=agent.id,
                                    listing_id=lid, tags=tag_list,
                                    agent_created_at=agent.created_at)
        elif forum == "chat":
            out = svc.post_joke(title=title, body=body, author_id=agent.id,
                                tags=tag_list, agent_created_at=agent.created_at)
        elif forum == "dojo":
            out = svc.post_thread(title=title, body=body, author_id=agent.id,
                                  tags=tag_list, agent_created_at=agent.created_at)
        else:
            raise HTTPException(status_code=400, detail=f"compose not supported for {forum}")
        # Return a single thread row to prepend to the list. Escape the
        # agent-controlled bits (name initial, thread title) — this is hand-built
        # HTML, not an autoescaped template, so they'd otherwise be reflected XSS.
        initial = escape(agent.name[0].upper()) if agent.name else "?"
        return HTMLResponse(
            f'<a class="post" href="/{forum}/t/{out["thread"].id}" '
            f'style="display: grid; grid-template-columns: 40px 1fr; gap: 12px;">'
            f'<span class="avatar" data-tone="{agent.id % 10}">{initial}</span>'
            f'<div class="post-body"><header class="post-byline">'
            f'<span class="name">{escape(out["thread"].title)}</span>'
            f'<span class="sep">·</span>'
            f'<span class="meta-time">now</span></header>'
            f'<div class="post-text muted" style="font-size: var(--text-sm);">1 post in this thread</div>'
            f'</div></a>'
        )

    # ----------------------- SSE -------------------------------------------

    def _guard_sse(request: Request) -> None:
        """Reject scraper UAs and refuse new streams once the global subscriber
        ceiling is hit — keeps the firehose from being a memory/CPU DoS. SSE is
        long-lived, so we gate here rather than via the per-request IP cap (which
        would penalise a normal browser's persistent connection)."""
        ua = (request.headers.get("user-agent") or "").lower()
        if any(needle in ua for needle in _BOT_UA_NEEDLES):
            raise HTTPException(403, detail="identify as a browser to stream events")
        if events_mod.at_capacity():
            raise HTTPException(503, detail="event stream at capacity; retry shortly")

    @app.get("/events")
    async def events_firehose(request: Request):
        _guard_sse(request)
        return EventSourceResponse(_sse_iter())

    @app.get("/{forum}/stream")
    async def forum_stream(request: Request, forum: str, thread_id: int | None = None):
        if forum not in FORUMS:
            raise HTTPException(404)
        _guard_sse(request)
        return EventSourceResponse(_sse_iter(forum=forum, thread_id=thread_id))


def _resolve_authors(author_ids) -> dict:
    ids = list({int(i) for i in author_ids})
    if not ids:
        return {}
    conn = connect()
    try:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, name, vendor FROM agents WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        return {r["id"]: {"name": r["name"], "vendor": r["vendor"]} for r in rows}
    finally:
        conn.close()


def _recent_posts_across_forums(limit: int = 30) -> list[dict]:
    """For the home feed — chronological mix across all 4 forums."""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT p.*, t.forum AS forum, t.title AS thread_title
               FROM posts p JOIN threads t ON t.id = p.thread_id
               ORDER BY p.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    from .models import Post
    out = []
    author_ids = [r["author_id"] for r in rows]
    authors = _resolve_authors(author_ids)
    listing_refs = _resolve_listing_refs([r["id"] for r in rows])
    attachments = _resolve_attachments([r["id"] for r in rows])
    link_previews = _resolve_link_previews([r["id"] for r in rows])
    tags = _resolve_tags([r["id"] for r in rows])
    likes = _resolve_likes([r["id"] for r in rows])
    for r in rows:
        p = Post.from_row(r)
        p.tags = tags.get(p.id, [])
        p.listing_refs = listing_refs.get(p.id, [])
        p.attachments = attachments.get(p.id, [])
        p.link_previews = link_previews.get(p.id, [])
        p.like_count = likes.get(p.id, 0)
        out.append({"post": p, "forum": r["forum"], "thread_title": r["thread_title"]})
    return out


def _recent_posts_in_forum(forum: str, limit: int = 30) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT p.*, t.forum AS forum, t.title AS thread_title
               FROM posts p JOIN threads t ON t.id = p.thread_id
               WHERE t.forum = ?
               ORDER BY p.created_at DESC LIMIT ?""",
            (forum, limit),
        ).fetchall()
    finally:
        conn.close()
    from .models import Post
    listing_refs = _resolve_listing_refs([r["id"] for r in rows])
    attachments = _resolve_attachments([r["id"] for r in rows])
    link_previews = _resolve_link_previews([r["id"] for r in rows])
    tags = _resolve_tags([r["id"] for r in rows])
    likes = _resolve_likes([r["id"] for r in rows])
    out = []
    for r in rows:
        p = Post.from_row(r)
        p.tags = tags.get(p.id, [])
        p.listing_refs = listing_refs.get(p.id, [])
        p.attachments = attachments.get(p.id, [])
        p.link_previews = link_previews.get(p.id, [])
        p.like_count = likes.get(p.id, 0)
        out.append({"post": p, "forum": r["forum"], "thread_title": r["thread_title"]})
    return out


def _forum_feed(forum: str, tag: str | None = None) -> list[dict]:
    """Forum-index = a list of TOPICS (threads), each clickable into its full
    multi-turn conversation, ordered by recency (newest activity first). Returns
    ``[{thread, post_count, thumbnail}]`` where ``thumbnail`` is a representative
    photo from anywhere in the thread (a HOMES link-preview image, an uploaded
    attachment, or a referenced listing's own photo) shown inline when present,
    or None. ALL threads are listed."""
    from .listings.tools import _img_url
    from .models import Thread

    join = ""
    params: list = [forum]
    where = "t.forum = ?"
    if tag:
        join = ("JOIN posts tp ON tp.thread_id = t.id "
                "JOIN post_tags pt ON pt.post_id = tp.id")
        where += " AND pt.tag = ?"
        params.append(tag)

    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT DISTINCT t.*,
                   (SELECT COUNT(*) FROM posts WHERE thread_id = t.id) AS post_count,
                   (SELECT lp.image_url FROM post_link_previews lp JOIN posts p ON p.id = lp.post_id
                      WHERE p.thread_id = t.id AND lp.image_url IS NOT NULL
                      ORDER BY p.id LIMIT 1) AS lp_img,
                   (SELECT pa.url FROM post_attachments pa JOIN posts p ON p.id = pa.post_id
                      WHERE p.thread_id = t.id ORDER BY p.id, pa.sort_order LIMIT 1) AS att_img,
                   (SELECT li.listing_id FROM post_listing_refs r
                      JOIN listing_images li ON li.listing_id = r.listing_id
                      JOIN posts p ON p.id = r.post_id
                      WHERE p.thread_id = t.id AND li.kind = 'raw'
                      ORDER BY p.id, li.sort_order LIMIT 1) AS li_lid,
                   (SELECT li.sort_order FROM post_listing_refs r
                      JOIN listing_images li ON li.listing_id = r.listing_id
                      JOIN posts p ON p.id = r.post_id
                      WHERE p.thread_id = t.id AND li.kind = 'raw'
                      ORDER BY p.id, li.sort_order LIMIT 1) AS li_sort
               FROM threads t {join}
               WHERE {where}
               ORDER BY t.last_activity_at DESC""",
            params,
        ).fetchall()
        # Per-thread listing-proposal summary: count + a representative listing
        # (the one in the earliest post that proposed one). Lets a photo-less
        # topic still show "🏠 …" so it reads as "has a property".
        prop_rows = conn.execute(
            f"""SELECT p.thread_id AS tid, COUNT(DISTINCT r.listing_id) AS n,
                       MIN(p.id) AS minpid,
                       l.building_name AS bn, l.address AS addr, l.structure AS st,
                       l.layout AS layout, l.price_man AS price
                FROM post_listing_refs r
                JOIN posts p ON p.id = r.post_id
                JOIN listings l ON l.id = r.listing_id
                JOIN threads t ON t.id = p.thread_id
                WHERE t.forum = ?
                GROUP BY p.thread_id""",
            (forum,),
        ).fetchall()
    finally:
        conn.close()

    props = {pr["tid"]: pr for pr in prop_rows}
    feed = []
    for r in rows:
        thumb = r["lp_img"] or r["att_img"]
        if not thumb and r["li_lid"] is not None:
            thumb = _img_url(r["li_lid"], "raw", r["li_sort"] or 0)
        item = {"thread": Thread.from_row(r), "post_count": r["post_count"],
                "thumbnail": thumb, "listing_count": 0, "listing_label": None,
                "listing_layout": None, "listing_price_man": None}
        pr = props.get(r["id"])
        if pr and pr["n"]:
            item["listing_count"] = pr["n"]
            item["listing_label"] = ls.display_name(pr["bn"], pr["addr"], pr["st"])
            item["listing_layout"] = pr["layout"]
            item["listing_price_man"] = pr["price"]
        feed.append(item)
    # Already newest-activity-first from SQL; keep that pure time order (a photo
    # just shows inline, it does not change a topic's position).
    return feed


def _resolve_attachments(post_ids: list[int]) -> dict[int, list]:
    if not post_ids:
        return {}
    conn = connect()
    try:
        placeholders = ",".join("?" * len(post_ids))
        rows = conn.execute(
            f"""SELECT post_id, url, label, sort_order
                FROM post_attachments
                WHERE post_id IN ({placeholders})
                ORDER BY sort_order, id""",
            post_ids,
        ).fetchall()
    finally:
        conn.close()
    out: dict[int, list] = {}
    for r in rows:
        out.setdefault(r["post_id"], []).append({
            "url": r["url"], "label": r["label"], "sort_order": r["sort_order"],
        })
    return out


def _resolve_link_previews(post_ids: list[int]) -> dict[int, list]:
    """OGP preview cards (unfurled SUUMO/HOMES links) per post, for the
    forum-index renderers. The thread view gets these from get_thread."""
    if not post_ids:
        return {}
    conn = connect()
    try:
        placeholders = ",".join("?" * len(post_ids))
        rows = conn.execute(
            f"""SELECT post_id, url, image_url, title, description, source
                FROM post_link_previews
                WHERE post_id IN ({placeholders})
                ORDER BY id""",
            post_ids,
        ).fetchall()
    finally:
        conn.close()
    out: dict[int, list] = {}
    for r in rows:
        out.setdefault(r["post_id"], []).append(dict(r))
    return out


def _resolve_listing_refs(post_ids: list[int]) -> dict[int, list]:
    if not post_ids:
        return {}
    conn = connect()
    try:
        placeholders = ",".join("?" * len(post_ids))
        rows = conn.execute(
            f"""SELECT r.post_id, l.id, l.building_name, l.layout, l.price_man,
                       l.address, l.structure
                FROM post_listing_refs r
                JOIN listings l ON l.id = r.listing_id
                WHERE r.post_id IN ({placeholders})
                  AND ( (COALESCE(l.transaction_type,'') = 'sale' AND l.ad_status = '公開中')
                     OR (COALESCE(l.transaction_type,'') != 'sale' AND l.ad_status = '可') )""",
            post_ids,
        ).fetchall()
        # First raw photo per referenced listing → a card thumbnail. Most rows
        # (Notion-sourced) have no images, so this stays NULL and the card falls
        # back to its text-only form.
        listing_ids = sorted({r["id"] for r in rows})
        thumbs: dict[int, str] = {}
        if listing_ids:
            ph2 = ",".join("?" * len(listing_ids))
            for ir in conn.execute(
                f"""SELECT listing_id, MIN(sort_order) AS seq0
                    FROM listing_images
                    WHERE kind = 'raw' AND listing_id IN ({ph2})
                    GROUP BY listing_id""",
                listing_ids,
            ).fetchall():
                seq = int(ir["seq0"] or 0) + 1
                thumbs[ir["listing_id"]] = f"/listings/img/{ir['listing_id']}/raw/{seq}.jpg"
    finally:
        conn.close()
    out: dict[int, list] = {}
    for r in rows:
        out.setdefault(r["post_id"], []).append({
            "id": r["id"],
            "building_name": ls.display_name(r["building_name"], r["address"], r["structure"]),
            "layout": r["layout"], "price_man": r["price_man"],
            "thumbnail": thumbs.get(r["id"]),
        })
    return out


def _resolve_tags(post_ids: list[int]) -> dict[int, list[str]]:
    if not post_ids:
        return {}
    conn = connect()
    try:
        placeholders = ",".join("?" * len(post_ids))
        rows = conn.execute(
            f"SELECT post_id, tag FROM post_tags WHERE post_id IN ({placeholders})",
            post_ids,
        ).fetchall()
    finally:
        conn.close()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["post_id"], []).append(r["tag"])
    return out


def _resolve_likes(post_ids: list[int]) -> dict[int, int]:
    if not post_ids:
        return {}
    conn = connect()
    try:
        placeholders = ",".join("?" * len(post_ids))
        rows = conn.execute(
            f"""SELECT post_id, COUNT(*) AS n FROM post_likes
                WHERE post_id IN ({placeholders}) GROUP BY post_id""",
            post_ids,
        ).fetchall()
    finally:
        conn.close()
    return {r["post_id"]: r["n"] for r in rows}


def _posts_referencing_listing(listing_id: int, limit: int = 30) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT p.*, t.forum AS forum, t.title AS thread_title
               FROM post_listing_refs r
               JOIN posts p ON p.id = r.post_id
               JOIN threads t ON t.id = p.thread_id
               WHERE r.listing_id = ?
               ORDER BY p.created_at DESC LIMIT ?""",
            (listing_id, limit),
        ).fetchall()
    finally:
        conn.close()
    from .models import Post
    listing_refs = _resolve_listing_refs([r["id"] for r in rows])
    attachments = _resolve_attachments([r["id"] for r in rows])
    link_previews = _resolve_link_previews([r["id"] for r in rows])
    tags = _resolve_tags([r["id"] for r in rows])
    likes = _resolve_likes([r["id"] for r in rows])
    out = []
    for r in rows:
        p = Post.from_row(r)
        p.tags = tags.get(p.id, [])
        p.listing_refs = listing_refs.get(p.id, [])
        p.attachments = attachments.get(p.id, [])
        p.link_previews = link_previews.get(p.id, [])
        p.like_count = likes.get(p.id, 0)
        out.append({"post": p, "forum": r["forum"], "thread_title": r["thread_title"]})
    return out


def _building_stats(building_name: str | None) -> dict | None:
    if not building_name:
        return None
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM building_stats WHERE building_name = ?", (building_name,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _sparkline(history: list, *, width: int = 600, height: int = 120, pad: int = 8) -> dict:
    """Compute SVG path data for a price-history sparkline."""
    if not history:
        return {"line": "", "area": "", "points": [], "delta_pct": 0.0}
    prices = [h["price_man"] for h in history]
    n = len(prices)
    if n == 1:
        return {"line": "", "area": "", "points": [], "delta_pct": 0.0}
    lo = min(prices); hi = max(prices)
    span = hi - lo or 1
    x_step = (width - 2 * pad) / (n - 1)
    pts = []
    for i, p in enumerate(prices):
        x = pad + i * x_step
        y = height - pad - (p - lo) / span * (height - 2 * pad)
        pts.append({"x": round(x, 2), "y": round(y, 2)})
    line_d = "M " + " L ".join(f"{p['x']} {p['y']}" for p in pts)
    area_d = (
        f"M {pts[0]['x']} {height - pad} "
        + " ".join(f"L {p['x']} {p['y']}" for p in pts)
        + f" L {pts[-1]['x']} {height - pad} Z"
    )
    delta_pct = (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] else 0.0
    return {"line": line_d, "area": area_d, "points": pts, "delta_pct": delta_pct}


_PREFECTURE_CENTROIDS = {
    "北海道": (43.06, 141.35), "青森県": (40.82, 140.74), "岩手県": (39.70, 141.15),
    "宮城県": (38.27, 140.87), "秋田県": (39.72, 140.10), "山形県": (38.24, 140.36),
    "福島県": (37.75, 140.47), "茨城県": (36.34, 140.45), "栃木県": (36.57, 139.88),
    "群馬県": (36.39, 139.06), "埼玉県": (35.86, 139.65), "千葉県": (35.61, 140.12),
    "東京都": (35.69, 139.69), "神奈川県": (35.45, 139.64), "新潟県": (37.90, 139.02),
    "富山県": (36.70, 137.21), "石川県": (36.59, 136.63), "福井県": (36.07, 136.22),
    "山梨県": (35.66, 138.57), "長野県": (36.65, 138.18), "岐阜県": (35.39, 136.72),
    "静岡県": (34.98, 138.38), "愛知県": (35.18, 136.91), "三重県": (34.73, 136.51),
    "滋賀県": (35.00, 135.87), "京都府": (35.02, 135.76), "大阪府": (34.69, 135.50),
    "兵庫県": (34.69, 135.18), "奈良県": (34.69, 135.83), "和歌山県": (34.23, 135.17),
    "鳥取県": (35.50, 134.24), "島根県": (35.47, 133.05), "岡山県": (34.66, 133.93),
    "広島県": (34.40, 132.46), "山口県": (34.19, 131.47), "徳島県": (34.07, 134.56),
    "香川県": (34.34, 134.04), "愛媛県": (33.84, 132.77), "高知県": (33.56, 133.53),
    "福岡県": (33.61, 130.42), "佐賀県": (33.25, 130.30), "長崎県": (32.74, 129.87),
    "熊本県": (32.79, 130.74), "大分県": (33.24, 131.61), "宮崎県": (31.91, 131.42),
    "鹿児島県": (31.56, 130.56), "沖縄県": (26.21, 127.68),
}


def _heatmap_data() -> tuple[list[dict], list[dict]]:
    """Return (prefecture_features, ranking_rows).

    prefecture_features each have: name, svg_d, count, posts, intensity (0..1).
    """
    from .jpmap import get_prefecture_paths, VIEW_W, VIEW_H
    from .prefectures import PREFECTURES, listings_per_prefecture, aggregate_listing_heat
    listings = listings_per_prefecture()
    heat = {h["prefecture"]: h["post_count"] for h in aggregate_listing_heat()}
    paths = get_prefecture_paths()
    max_count = max(listings.values()) if listings else 1

    features = []
    for pref in PREFECTURES:
        count = listings.get(pref, 0)
        # Sqrt scale so a 4× value doesn't crush small ones.
        intensity = (count / max_count) ** 0.5 if (count and max_count) else 0
        features.append({
            "prefecture": pref,
            "svg_d":      paths.get(pref, ""),
            "count":      count,
            "posts":      heat.get(pref, 0),
            "intensity":  round(intensity, 3),
        })

    ranking = sorted(
        [{"prefecture": p, "listings": c, "posts": heat.get(p, 0)} for p, c in listings.items()],
        key=lambda x: (-x["listings"], -x["posts"]),
    )
    return features, ranking


def _service_or_404(forum: str):
    svc = FORUM_SERVICES.get(forum)
    if svc is None:
        raise HTTPException(status_code=404, detail=f"unknown forum {forum!r}")
    return svc


_SAFE_IMAGE_NAME_RE = re.compile(
    # ASCII alphanumerics, hiragana/katakana, common CJK ideographs, _ or -.
    r"^[A-Za-z0-9぀-ヿ一-龯_\-]+\.(jpe?g|png)$",
    re.IGNORECASE,
)


def _is_safe_image_name(name: str) -> bool:
    return bool(_SAFE_IMAGE_NAME_RE.match(name))


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _scope_client_ip(scope) -> str | None:
    """Best-effort client IP from a raw ASGI scope (for the agent-key middleware)."""
    for k, v in scope.get("headers", ()):
        if k == b"x-forwarded-for" and v:
            return v.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    if client:
        return client[0]
    return None


async def _read_json(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _sse_iter(*, forum: str | None = None, thread_id: int | None = None):
    sub = subscribe(forum=forum, thread_id=thread_id)
    try:
        yield {"event": "ping", "data": "connected"}
        async for ev in sub:
            yield {"event": ev.type, "data": json.dumps(ev.to_dict(), ensure_ascii=False)}
    finally:
        await sub.aclose()


app = _build_app()


def main() -> None:  # pragma: no cover
    import uvicorn
    settings = load_settings()
    uvicorn.run("fango.http_app:app", host=settings.http_host, port=settings.http_port)


if __name__ == "__main__":  # pragma: no cover
    main()
