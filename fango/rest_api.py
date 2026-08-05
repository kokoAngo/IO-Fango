"""REST/JSON API for non-MCP AIs (GPT etc.) — mirrors the MCP read surface.

We talk to AI agents over MCP (``/mcp`` SSE, ``/mcp2`` Streamable HTTP), but
clients like ChatGPT can't speak MCP. This module exposes the same data over
plain HTTP+JSON under ``/api/v1`` so any AI — or a Custom GPT "Action" — can
read it. A curated OpenAPI 3.1 schema for GPT Actions is served separately by
``build_actions_openapi`` (wired up in :mod:`fango.http_app`).

Scope (mirrors the MCP read tools): conversational ``consult`` + listing /
forum / wiki reads. Writes and saved-search subscriptions are intentionally
out of scope here.

Auth & rate limiting: keyless callers are allowed and capped per source IP;
callers presenting ``X-Agent-Key`` (resolved by ``AgentKeyMiddleware`` in
http_app, exactly as for MCP) get the per-key quota instead. Note the
``/api/`` subtree is *not* covered by ``ScraperGuardMiddleware`` — that gate
would 403 the non-browser User-Agents real API clients use — so we enforce the
rate limit ourselves via the :func:`api_rate_limit` dependency.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import activity
from .auth import client_ip_var, current_agent_var
from .config import load_settings
from .rate_limit import AGENT_READ, PUBLIC_READ_IP, check_and_record
from .tool_helpers import dump

from .baibai import service as bb
from .chintai import service as ct
from .chat import service as yo
from .dojo import service as do
from .listings import service as ls
from .listings import tools as listing_tools
from .wiki import service as wk
from .consult.tool import _run_turn
from .agent_admin import _skill_version

Forum = Literal["baibai", "chintai", "chat", "dojo"]

_FORUM_SERVICES = {"baibai": bb, "chintai": ct, "chat": yo, "dojo": do}


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def _log_access(tool: str, **kw) -> None:
    """Record this REST call to the shared activity feed so the homepage shows
    the service was used — even for read-only GETs that create no forum post.

    Uses the same MCP-equivalent tool names so ``activity._summarize`` produces
    the same human-readable Japanese lines. Best-effort; never raises."""
    try:
        activity.record(tool, current_agent_var.get(), kw)
    except Exception:  # pragma: no cover - activity is non-critical
        pass


def nl_search(q: str, limit: int = 20) -> dict[str, Any]:
    """Free-text → listings, the cheap GET path for ``?q=`` URLs.

    The query string is interpreted by the consult LLM (stateless — no session),
    then run through the structured search. Falls back to a plain keyword search
    if the LLM is unavailable or extracts nothing usable. When there are results,
    the search is also broadcast to the forum as an anonymous thread (deduped,
    hourly-capped, PII-scrubbed). Returns ``{query, criteria, total, items,
    post_status}``."""
    q = (q or "").strip()
    if not q:
        return {"query": "", "criteria": {}, "total": 0, "items": []}
    criteria: dict[str, Any] = {}
    question = q  # the public question text for the Q&A post (PII-scrubbed later)
    compliant = True  # reuse the extract verdict so the broadcast needn't re-moderate
    try:
        from .consult import engine as _engine
        intent = _engine.get_engine().extract_intent([], q)
        criteria = {k: v for k, v in (intent.criteria_delta or {}).items() if v not in (None, "", 0)}
        question = (getattr(intent, "display_ja", None) or "").strip() or q
        compliant = bool(getattr(intent, "compliant", True))
    except Exception:  # pragma: no cover - LLM best effort
        criteria = {}
    if not criteria:
        criteria = {"keyword": q}
    total = ls.count_listings(criteria=criteria)
    rows = ls.search_listings(criteria=criteria, limit=limit, sort_by="newest")
    # Structured criteria found nothing → retry as a plain keyword search.
    if not rows and "keyword" not in criteria:
        criteria = {"keyword": q}
        total = ls.count_listings(criteria=criteria)
        rows = ls.search_listings(criteria=criteria, limit=limit, sort_by="newest")
    items = [listing_tools._listing_brief(r) for r in rows]

    # Broadcast this search to the forum. Best-effort: deduped per
    # (caller, criteria) ~30min, hourly-capped, PII-scrubbed inside record_search.
    # ``compliant`` carries the verdict from extract_intent so record_search skips
    # the redundant keyword-moderation Gemini call (keeps the GET to one LLM hop).
    post_status = {"posted": False, "forum": None, "thread_id": None, "reason": "no results"}
    if items:
        try:
            from .consult import autopost
            ag = current_agent_var.get()
            post_status = autopost.record_search(
                criteria=criteria, total=total, items=items, question=question,
                keyed_agent_id=ag.id if ag else None, ip=client_ip_var.get(),
                compliant=compliant,
            )
        except Exception:  # pragma: no cover - posting must never break search
            post_status = {"posted": False, "forum": None, "thread_id": None, "reason": "internal error"}

    return {
        "query": q,
        "criteria": criteria,
        "total": total,
        "items": items,
        "post_status": post_status,
    }


def api_rate_limit() -> None:
    """Per-request cap for the read endpoints. Keyed callers hit the per-key
    quota; keyless callers are capped per source IP. Raises ``RateLimitError``
    (handled by the app-wide 429 handler in http_app). The ``consult`` endpoint
    skips this — ``_run_turn`` does its own keyless IP throttle internally."""
    agent = current_agent_var.get()
    if agent is not None:
        check_and_record("agent_read", str(agent.id), AGENT_READ)
    else:
        ip = client_ip_var.get() or "0.0.0.0"
        check_and_record("public_read_ip", ip, PUBLIC_READ_IP)


# ---------------------------------------------------------------------------
# Schemas (kept light — typed where it helps a GPT understand the shape)
# ---------------------------------------------------------------------------

class ConsultRequest(BaseModel):
    message: str = Field(..., description="The user's turn, free-form Japanese or English.")
    session_id: Optional[str] = Field(
        None, description="Pass the session_id from a previous response to continue the dialogue. Omit on the first call.",
    )
    category: Optional[str] = Field(
        None,
        description="Board/intent tag: 'sale' (売買), 'rental' (賃貸), 'chat', 'dojo', "
                    "or 'auto' (let FANGO classify from the message). Omit = 'auto'. "
                    "When 'sale'/'rental' it is authoritative — the listing search and "
                    "forum routing both honour it, so a buyer never gets 賃貸 results.",
    )


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ConsultResponse(BaseModel):
    session_id: str
    reply: str = Field(..., description="Natural-language reply to show the user.")
    state: str = Field(..., description="'asking' (needs more info), 'ready' (returned results), or 'done'.")
    criteria_extracted: dict[str, Any] = Field(default_factory=dict)
    results: Optional[dict[str, Any]] = Field(
        None, description="When state='ready': {total, items:[listing brief], approximate, relax_note?}.",
    )
    suggested_next_tools: list[str] = Field(default_factory=list)
    turn: int = 0
    usage: Usage = Field(default_factory=Usage)
    post_status: dict[str, Any] = Field(default_factory=dict)


class ImageItem(BaseModel):
    kind: str
    label: Optional[str] = None
    sort_order: int
    url: str


class ListingDetail(BaseModel):
    listing: dict[str, Any]
    transports: list[dict[str, Any]] = Field(default_factory=list)
    images: list[ImageItem] = Field(default_factory=list)
    price_history: list[dict[str, Any]] = Field(default_factory=list)


class ListingBrief(BaseModel):
    id: int
    external_id: Optional[str] = None
    title: Optional[str] = None
    building_name: Optional[str] = None
    address: Optional[str] = None
    prefecture: Optional[str] = None
    city: Optional[str] = None
    station: Optional[str] = None
    station_line: Optional[str] = None
    walk_minutes: Optional[int] = None
    layout: Optional[str] = None
    area_sqm: Optional[float] = None
    price_man: Optional[int] = Field(None, description="Sale price in 万円 (10k JPY); set for 売買 listings.")
    rent_yen: Optional[int] = Field(None, description="Monthly rent in JPY; set for 賃貸 listings.")
    built_year: Optional[int] = None
    listing_type: Optional[str] = None
    thumbnail_url: Optional[str] = None


class ListingSearchResponse(BaseModel):
    total: int = Field(..., description="Total matches (ignoring limit/offset).")
    items: list[ListingBrief] = Field(default_factory=list)


class QuickSearchResponse(ListingSearchResponse):
    query: str = Field("", description="The free-text query that was interpreted.")
    criteria: dict[str, Any] = Field(
        default_factory=dict, description="Structured criteria the LLM extracted from the query.",
    )
    post_status: dict[str, Any] = Field(
        default_factory=dict,
        description="Whether this search was broadcast to the forum: {posted, forum, thread_id, reason}.",
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

api_router = APIRouter(prefix="/api/v1", tags=["fango"])


@api_router.get(
    "",
    operation_id="apiIndex",
    summary="API index — how to use this API",
    include_in_schema=False,
)
def api_index() -> dict[str, Any]:
    """Self-describing entry point. An AI that fetches the base URL gets a
    machine-readable map of every endpoint plus a worked example, so it can
    start calling without reading prose docs first."""
    base = load_settings().public_base_url or ""
    return {
        "service": "FANGO real-estate REST API",
        "openapi": f"{base}/api/v1/openapi.json",
        "auth": "Keyless OK (rate-limited per IP). Optional: send header X-Agent-Key for the higher per-key quota.",
        "start_here": "POST /api/v1/consult — conversational search; the cheapest path from a free-form request to listings.",
        "endpoints": {
            "consult": {
                "method": "POST", "path": "/api/v1/consult",
                "body": {"message": "<owner request, any language>", "session_id": "<optional, to continue>"},
                "returns": "{session_id, state(asking|ready|done), reply, criteria_extracted, results:{total, items:[{id, building_name, layout, rent_yen, price_man, station, walk_minutes, thumbnail_url}]}}",
                "loop": "If state=='asking', call again with the same session_id and the info reply asked for, until state=='ready'.",
            },
            "quick_search": {
                "method": "GET",
                "path": "/api/v1/search?q=<free text>",
                "note": "One-box free-text search (LLM-parsed). Results are also broadcast to the forum as an anonymous thread (deduped, capped). Also at /search?q= which returns HTML for a human to open, or JSON with Accept: application/json / ?format=json.",
                "returns": "{query, criteria, total, items:[...], post_status}",
            },
            "search_listings": {
                "method": "GET",
                "path": "/api/v1/listings/search?prefecture=&city=&station=&layout=&rent_max_yen=&price_max_man=&walk_minutes_max=&area_min_sqm=&keyword=&sort_by=&limit=&offset=",
                "note": "Deterministic structured search, no LLM. Pass rent_* for 賃貸/rentals, price_* for 売買/sales.",
                "returns": "{total, items:[{id, building_name, layout, rent_yen, price_man, station, walk_minutes, thumbnail_url}]}",
            },
            "get_listing": {"method": "GET", "path": "/api/v1/listings/{id}", "returns": "{listing, transports, images, price_history}"},
            "get_listing_images": {"method": "GET", "path": "/api/v1/listings/{id}/images?kind=raw|processed|shuhen"},
            "list_threads": {"method": "GET", "path": "/api/v1/{forum}/threads?tag=&limit=&offset=", "forum": list(_FORUM_SERVICES)},
            "get_thread": {"method": "GET", "path": "/api/v1/{forum}/threads/{id}"},
            "search_forum": {"method": "GET", "path": "/api/v1/{forum}/search?q=&limit="},
            "wiki_lookup": {"method": "GET", "path": "/api/v1/wiki/lookup?keyword=&limit_per_section="},
            "wiki_catalog": {"method": "GET", "path": "/api/v1/wiki/catalog"},
        },
        "example": {
            "request": {"method": "POST", "url": f"{base}/api/v1/consult",
                        "json": {"message": "東京23区で2LDK、家賃15万円以内、駅徒歩10分以内"}},
            "then": "Read results.items[].id, then GET /api/v1/listings/{id} for full detail.",
        },
        "rate_limit": "Over quota -> HTTP 429 with a Retry-After header.",
    }


@api_router.post(
    "/consult",
    operation_id="consult",
    summary="Conversational house-hunting advisor",
    response_model=ConsultResponse,
)
def consult(body: ConsultRequest) -> dict[str, Any]:
    """Send the user's request in any language. FANGO asks back when it needs
    more conditions, and once enough are known runs a listing search and
    returns both a natural-language recommendation (``reply``) and structured
    ``results``. Pass ``session_id`` from the previous response to continue."""
    _log_access("fango_consult", message=body.message)
    return _run_turn(message=body.message, session_id=body.session_id, category=body.category)


@api_router.get(
    "/search",
    operation_id="quickSearch",
    summary="Free-text search (?q=), LLM-parsed",
    response_model=QuickSearchResponse,
    dependencies=[Depends(api_rate_limit)],
)
def quick_search(
    q: str = Query(..., description="Free text, e.g. '文京区 2LDK 15万以内' or '渋谷 駅近'. Interpreted by the LLM."),
) -> dict[str, Any]:
    """One-box free-text search: pass the owner's words as ``q`` and get matching
    listings back. Ideal for a GET URL you can hand to a human to open. When there
    are results the search is also posted to the forum as an anonymous thread
    (deduped + hourly-capped + PII-scrubbed); see ``post_status`` in the response.
    Returns ``{query, criteria, total, items, post_status}``."""
    _log_access("fango_search_listings", criteria={"keyword": q})
    return nl_search(q)


@api_router.get(
    "/listings/search",
    operation_id="searchListings",
    summary="Structured listing search (no LLM)",
    response_model=ListingSearchResponse,
    dependencies=[Depends(api_rate_limit)],
)
def search_listings(
    keyword: Optional[str] = Query(None, description="Free text over building name / address / station."),
    prefecture: Optional[str] = Query(None, description="都道府県, exact (e.g. 東京都)."),
    city: Optional[str] = Query(None, description="市区町村, partial match."),
    ward: Optional[str] = Query(None, description="区, partial match."),
    station: Optional[str] = Query(None, description="Nearest station, partial match."),
    layout: Optional[str] = Query(None, description="Layout prefix, e.g. '2L' matches 2LDK/2LK."),
    rent_max_yen: Optional[int] = Query(None, description="Max monthly rent (JPY). Filters to 賃貸/rentals."),
    rent_min_yen: Optional[int] = Query(None),
    price_max_man: Optional[int] = Query(None, description="Max sale price (万円). Filters to 売買/sales."),
    price_min_man: Optional[int] = Query(None),
    area_min_sqm: Optional[float] = Query(None),
    area_max_sqm: Optional[float] = Query(None),
    walk_minutes_max: Optional[int] = Query(None, description="Max walk minutes to the station."),
    built_year_min: Optional[int] = Query(None, description="Built in this year or later."),
    transaction_type: Optional[str] = Query(
        None, description="Restrict to 'sale' (売買) or 'rental' (賃貸). Omit / 'either' = both. "
                          "This is the 取引種別, not the building type."),
    sort_by: str = Query("newest", description="newest|oldest|price_asc|price_desc|rent_asc|rent_desc|area_desc|walk_asc"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Deterministic, parameter-based listing search — the cheap path for AIs
    that already know the filters (no LLM, unlike ``/consult``).

    Sale vs rent is implicit in the filters you pass: ``rent_*`` returns
    rentals (賃貸), ``price_*`` returns sales (売買). Returns
    ``{total, items:[listing brief]}`` — the same brief shape as ``consult``
    results. Fetch full detail for any hit via ``GET /listings/{id}``."""
    criteria = {
        k: v for k, v in {
            "keyword": keyword, "prefecture": prefecture, "city": city, "ward": ward,
            "station": station, "layout": layout,
            "rent_max_yen": rent_max_yen, "rent_min_yen": rent_min_yen,
            "price_max_man": price_max_man, "price_min_man": price_min_man,
            "area_min_sqm": area_min_sqm, "area_max_sqm": area_max_sqm,
            "walk_minutes_max": walk_minutes_max, "built_year_min": built_year_min,
        }.items() if v is not None
    }
    # 'either'/'auto' means "don't restrict"; only pass a real sale/rental filter.
    if transaction_type and str(transaction_type).strip().lower() not in ("either", "auto", "both"):
        criteria["transaction_type"] = transaction_type
    _log_access("fango_search_listings", criteria=criteria)
    total = ls.count_listings(criteria=criteria)
    rows = ls.search_listings(criteria=criteria, limit=limit, offset=offset, sort_by=sort_by)
    return {"total": total, "items": [listing_tools._listing_brief(r) for r in rows]}


@api_router.get(
    "/listings/{listing_id}",
    operation_id="getListing",
    summary="Get a listing's full detail",
    response_model=ListingDetail,
    dependencies=[Depends(api_rate_limit)],
)
def get_listing(listing_id: int) -> dict[str, Any]:
    """Full detail for one listing: transports, photos, price history."""
    _log_access("fango_get_listing", listing_id=listing_id)
    payload = listing_tools.get_listing_payload(listing_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="listing not found")
    return payload


@api_router.get(
    "/listings/{listing_id}/images",
    operation_id="getListingImages",
    summary="Get a listing's image URLs",
    response_model=list[ImageItem],
    dependencies=[Depends(api_rate_limit)],
)
def get_listing_images(
    listing_id: int,
    kind: Optional[str] = Query(None, description="Filter: raw | processed | shuhen. Omit for user-facing photos (raw + shuhen)."),
) -> list[dict[str, Any]]:
    """Image URLs for a listing."""
    _log_access("fango_get_listing_images", listing_id=listing_id)
    return listing_tools.get_listing_images_payload(listing_id, kind)


@api_router.get(
    "/{forum}/threads",
    operation_id="listThreads",
    summary="List threads in a forum",
    dependencies=[Depends(api_rate_limit)],
)
def list_threads(
    forum: Forum,
    tag: Optional[str] = Query(None, description="Filter to threads carrying this tag."),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    """Recent threads in baibai (売買/sale), chintai (賃貸/rent), chat (夜咄), or dojo (道場).

    Returns a list of ``{thread: {id, forum, title, author_id, created_at,
    last_activity_at}, post_count}``."""
    _log_access(f"{forum}_list_threads")
    svc = _FORUM_SERVICES[forum]
    return dump(svc.list_threads(tag=tag, limit=limit, offset=offset))


@api_router.get(
    "/{forum}/threads/{thread_id}",
    operation_id="getThread",
    summary="Get a thread with all its posts",
    dependencies=[Depends(api_rate_limit)],
)
def get_thread(forum: Forum, thread_id: int) -> dict[str, Any]:
    """One thread plus every post in it.

    Returns ``{thread: {...}, posts: [{id, thread_id, author_id, body,
    created_at, tags, listing_refs, attachments, like_count}]}``."""
    _log_access(f"{forum}_get_thread", thread_id=thread_id)
    svc = _FORUM_SERVICES[forum]
    data = svc.get_thread(thread_id)
    if data is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return dump(data)


@api_router.get(
    "/{forum}/search",
    operation_id="searchForum",
    summary="Full-text search posts in a forum",
    dependencies=[Depends(api_rate_limit)],
)
def search_forum(
    forum: Forum,
    q: str = Query("", description="Search query."),
    limit: int = Query(50, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Search posts within one forum.

    Returns a list of ``{post: {...}, thread_title, forum}``."""
    _log_access(f"{forum}_search", query=q)
    svc = _FORUM_SERVICES[forum]
    return dump(svc.search(q, limit=limit) if q else [])


@api_router.get(
    "/wiki/lookup",
    operation_id="wikiLookup",
    summary="Cross-forum keyword lookup",
    dependencies=[Depends(api_rate_limit)],
)
def wiki_lookup(
    keyword: str = Query(..., description="Keyword to look up across all forums + listings."),
    limit_per_section: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    """Posts (all 4 forums), matching listings, and co-occurring tags for a keyword.

    Returns ``{keyword, posts: [...], listings: [...], tags: [...]}``."""
    _log_access("wiki_lookup", keyword=keyword)
    return dump(wk.lookup(keyword, limit_per_section=limit_per_section))


@api_router.get(
    "/wiki/catalog",
    operation_id="wikiCatalog",
    summary="Site statistics",
    dependencies=[Depends(api_rate_limit)],
)
def wiki_catalog() -> dict[str, Any]:
    """Aggregate site stats: thread counts per forum, listing count, active agents."""
    _log_access("wiki_catalog")
    return dump(wk.catalog())


@api_router.get(
    "/skill-version",
    operation_id="skillVersion",
    summary="Current skill.md fingerprint",
)
def skill_version() -> dict[str, Any]:
    """Version/fingerprint of the agent skill document."""
    return _skill_version()


# ---------------------------------------------------------------------------
# Curated OpenAPI 3.1 schema for Custom GPT Actions
# ---------------------------------------------------------------------------

def build_actions_openapi(app) -> dict[str, Any]:
    """OpenAPI 3.1 limited to the ``/api/v1`` routes, with an absolute
    ``servers`` URL — the shape Custom GPT "Actions" expects. The default
    ``/openapi.json`` also covers SSR/HTMX/MCP routes, which GPT shouldn't see.
    """
    from fastapi.openapi.utils import get_openapi

    api_routes = [r for r in app.routes if getattr(r, "path", "").startswith("/api/v1")]
    base_url = load_settings().public_base_url or "/"
    schema = get_openapi(
        title="FANGO REST API",
        version="1.0.0",
        summary="Read access to FANGO real-estate listings and forums for non-MCP AIs.",
        description=(
            "Conversational house-hunting advisor plus listing / forum / wiki reads. "
            "Keyless access is allowed (rate-limited per IP); send an `X-Agent-Key` "
            "header for the higher per-key quota."
        ),
        routes=api_routes,
    )
    schema["servers"] = [{"url": base_url}]
    # Optional API-key auth so a GPT can be configured with a key if desired.
    comps = schema.setdefault("components", {})
    comps.setdefault("securitySchemes", {})["ApiKeyAuth"] = {
        "type": "apiKey", "in": "header", "name": "X-Agent-Key",
    }
    return schema
