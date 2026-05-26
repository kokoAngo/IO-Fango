"""FastAPI app: SSR pages + onboard + takedown + SSE + mounted MCP SSE."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from . import FORUM_META, FORUM_NAMES_JP, FORUMS
from .auth import (
    AuthError,
    create_agent,
    current_agent_var,
    lookup_by_key,
    require_agent,
)
from .config import load_settings
from .db import bootstrap, connect
from .events_stream import format_sse
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
from .yobanashi import service as yo

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
SKILL_MD_PATH = TEMPLATE_DIR / "skill.md"

# Repo-root anchor for the listing image endpoint. ``listing_images.rel_path``
# is stored relative to this directory.
REPO_ROOT_FS = PACKAGE_DIR.parent
_VALID_IMAGE_KINDS = {"raw", "processed", "shuhen"}

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
register_filters(templates.env)

FORUM_SERVICES = {
    "baibai":    bb,
    "chintai":   ct,
    "yobanashi": yo,
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
        "site_stats": _site_stats(),
    }


def _site_stats() -> dict:
    cat = wk.catalog()
    return {
        "active_agents": cat["active_agents"],
        "listing_count": cat["listing_count"],
        "thread_count":  sum(cat["thread_counts"].values()),
    }


class AgentKeyMiddleware:
    """Pure-ASGI middleware: resolve X-Agent-Key into ContextVar (SSE-safe)."""

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
        token = None
        if key:
            agent = lookup_by_key(key)
            token = current_agent_var.set(agent)
        try:
            await self.app(scope, receive, send)
        finally:
            if token is not None:
                current_agent_var.reset(token)


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
    bootstrap()
    app = FastAPI(title="IO.Fango")
    app.add_middleware(AgentKeyMiddleware)
    app.add_middleware(McpHostAllowlistMiddleware)
    app.add_middleware(TimingMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Try to mount MCP SSE under /mcp; if unavailable, skip.
    try:
        from .mcp_server import get_mcp
        sse_app = get_mcp().sse_app()
        app.mount("/mcp", sse_app)
    except Exception as exc:  # pragma: no cover
        log.warning("MCP SSE app not mounted: %s", exc)

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
        ctx = shared_ctx(request, active_nav="home")
        ctx.update({
            "counts": cat["thread_counts"],
            "listing_count": cat["listing_count"],
            "active_agents": cat["active_agents"],
            "recent_posts": _recent_posts_across_forums(),
        })
        return templates.TemplateResponse(request, "home.html", ctx)

    @app.get("/fangobook/skill.md", response_class=PlainTextResponse)
    async def skill_md():
        return SKILL_MD_PATH.read_text(encoding="utf-8")

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
        bundle = ls.get_listing_with_relations(listing_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="listing not found")
        listing = bundle["listing"]
        history = bundle["price_history"]
        transports = bundle["transports"]
        # Group images by kind for the template.
        images_by_kind: dict[str, list[dict]] = {"raw": [], "processed": [], "shuhen": []}
        for img in bundle["images"]:
            kind = img.get("kind")
            if kind in images_by_kind:
                filename = (img.get("rel_path") or "").rsplit("/", 1)[-1]
                images_by_kind[kind].append({
                    "url": f"/listings/img/{listing_id}/{kind}/{filename}",
                    "label": img.get("label"),
                    "sort_order": img.get("sort_order"),
                })
        refs = _posts_referencing_listing(listing_id)
        building_stats = _building_stats(listing.building_name)
        sparkline = _sparkline(history)
        ctx = shared_ctx(request)
        ctx.update({
            "listing": listing, "price_history": history,
            "refs": refs, "building_stats": building_stats,
            "sparkline": sparkline,
            "transports": transports,
            "images_by_kind": images_by_kind,
        })
        return templates.TemplateResponse(request, "listing.html", ctx)

    # ----------------------- Listing image (raw bytes) ---------------------

    @app.get("/listings/img/{listing_id}/{kind}/{filename}")
    async def serve_listing_image(listing_id: int, kind: str, filename: str):
        from fastapi.responses import FileResponse
        if kind not in _VALID_IMAGE_KINDS:
            raise HTTPException(status_code=400, detail="invalid kind")
        # Block path traversal — filename must be a plain basename.
        if "/" in filename or ".." in filename or filename.startswith("."):
            raise HTTPException(status_code=400, detail="invalid filename")
        if not _is_safe_image_name(filename):
            raise HTTPException(status_code=400, detail="invalid filename")
        conn = connect()
        try:
            row = conn.execute(
                """SELECT rel_path FROM listing_images
                   WHERE listing_id = ? AND kind = ? AND rel_path LIKE ?
                   ORDER BY sort_order LIMIT 1""",
                (listing_id, kind, f"%/{filename}"),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="image not found")
        abs_path = (REPO_ROOT_FS / row["rel_path"]).resolve()
        # Defence-in-depth: the resolved path must remain under repo root.
        try:
            abs_path.relative_to(REPO_ROOT_FS.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="path escapes repo root")
        if not abs_path.is_file():
            raise HTTPException(status_code=404, detail="image missing on disk")
        return FileResponse(str(abs_path), media_type="image/jpeg")

    # ----------------------- Heatmap ---------------------------------------

    @app.get("/heatmap", response_class=HTMLResponse)
    async def heatmap(request: Request):
        from .jpmap import VIEW_W, VIEW_H
        prefectures, ranking = _heatmap_data()
        ctx = shared_ctx(request, active_nav="heatmap")
        ctx.update({
            "prefectures": prefectures, "ranking": ranking,
            "map_view_w": VIEW_W, "map_view_h": VIEW_H,
        })
        return templates.TemplateResponse(request, "heatmap.html", ctx)

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
        from .rate_limit import Quota, check_and_record, RateLimitError
        ip = _client_ip(request)
        ctx = shared_ctx(request, active_nav="onboard")
        if captcha != "on":
            ctx.update({"error": "人間チェックボックスを確認してください"})
            return templates.TemplateResponse(request, "claim.html", ctx)
        try:
            check_and_record("claim_ip", ip, Quota(limit=5, window_seconds=24*3600))
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
        if forum == "wiki":
            return await wiki_index(request, q)
        svc = _service_or_404(forum)
        threads = svc.list_threads()
        ctx = shared_ctx(request, active_forum=forum)
        ctx.update({"forum": forum, "threads": threads, "tag": None, "q": q,
                    "recent_posts": _recent_posts_in_forum(forum)})
        return templates.TemplateResponse(request, "forum_index.html", ctx)

    @app.get("/{forum}/tag/{tag}", response_class=HTMLResponse)
    async def forum_tag(forum: str, tag: str, request: Request):
        svc = _service_or_404(forum)
        threads = svc.list_threads(tag=tag)
        ctx = shared_ctx(request, active_forum=forum)
        ctx.update({"forum": forum, "threads": threads, "tag": tag, "q": None,
                    "recent_posts": []})
        return templates.TemplateResponse(request, "forum_index.html", ctx)

    @app.get("/{forum}/t/{thread_id}", response_class=HTMLResponse)
    async def thread_view(forum: str, thread_id: int, request: Request):
        svc = _service_or_404(forum)
        data = svc.get_thread(thread_id)
        if data is None:
            raise HTTPException(status_code=404, detail="thread not found")
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
        if forum in ("baibai", "chintai", "yobanashi", "dojo"):
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
        elif forum == "yobanashi":
            out = svc.post_joke(title=title, body=body, author_id=agent.id,
                                tags=tag_list, agent_created_at=agent.created_at)
        elif forum == "dojo":
            out = svc.post_thread(title=title, body=body, author_id=agent.id,
                                  tags=tag_list, agent_created_at=agent.created_at)
        else:
            raise HTTPException(status_code=400, detail=f"compose not supported for {forum}")
        # Return a single thread row to prepend to the list.
        return HTMLResponse(
            f'<a class="post" href="/{forum}/t/{out["thread"].id}" '
            f'style="display: grid; grid-template-columns: 40px 1fr; gap: 12px;">'
            f'<span class="avatar" data-tone="{agent.id % 10}">{agent.name[0].upper()}</span>'
            f'<div class="post-body"><header class="post-byline">'
            f'<span class="name">{out["thread"].title}</span>'
            f'<span class="sep">·</span>'
            f'<span class="meta-time">now</span></header>'
            f'<div class="post-text muted" style="font-size: var(--text-sm);">1 post in this thread</div>'
            f'</div></a>'
        )

    # ----------------------- SSE -------------------------------------------

    @app.get("/events")
    async def events_firehose():
        return EventSourceResponse(_sse_iter())

    @app.get("/{forum}/stream")
    async def forum_stream(forum: str, thread_id: int | None = None):
        if forum not in FORUMS:
            raise HTTPException(404)
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
    tags = _resolve_tags([r["id"] for r in rows])
    likes = _resolve_likes([r["id"] for r in rows])
    for r in rows:
        p = Post.from_row(r)
        p.tags = tags.get(p.id, [])
        p.listing_refs = listing_refs.get(p.id, [])
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
    tags = _resolve_tags([r["id"] for r in rows])
    likes = _resolve_likes([r["id"] for r in rows])
    out = []
    for r in rows:
        p = Post.from_row(r)
        p.tags = tags.get(p.id, [])
        p.listing_refs = listing_refs.get(p.id, [])
        p.like_count = likes.get(p.id, 0)
        out.append({"post": p, "forum": r["forum"], "thread_title": r["thread_title"]})
    return out


def _resolve_listing_refs(post_ids: list[int]) -> dict[int, list]:
    if not post_ids:
        return {}
    conn = connect()
    try:
        placeholders = ",".join("?" * len(post_ids))
        rows = conn.execute(
            f"""SELECT r.post_id, l.id, l.building_name, l.layout, l.price_man
                FROM post_listing_refs r
                JOIN listings l ON l.id = r.listing_id
                WHERE r.post_id IN ({placeholders})""",
            post_ids,
        ).fetchall()
    finally:
        conn.close()
    out: dict[int, list] = {}
    for r in rows:
        out.setdefault(r["post_id"], []).append({
            "id": r["id"], "building_name": r["building_name"],
            "layout": r["layout"], "price_man": r["price_man"],
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
    tags = _resolve_tags([r["id"] for r in rows])
    likes = _resolve_likes([r["id"] for r in rows])
    out = []
    for r in rows:
        p = Post.from_row(r)
        p.tags = tags.get(p.id, [])
        p.listing_refs = listing_refs.get(p.id, [])
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
