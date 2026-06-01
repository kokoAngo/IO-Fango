# Fango.city — Pending Work

Running list of deferred items. Edit freely; closed items can move to git
history rather than this file.

## Before public launch (security / abuse / data)

- [ ] `/robots.txt` Disallow: / — file an explicit "no-crawl" stance.
- [ ] **IP rate limit** for read endpoints (SSR `/listings/<id>`, image
      endpoint, `fango_search_listings` / `fango_get_listing` MCP tools).
      ~40 req/h/IP when there's no `X-Agent-Key` / `FANGO_AGENT_KEY`; bypass
      when a valid agent key is present. Re-uses `rate_limit_events` table.
- [ ] **Sequential-id traversal detector** — same IP hitting consecutive
      listing ids inside a short window → temp-ban 5 min. Strong deterrent
      against the cheap "loop over /listings/1..N" script.
- [ ] **Rotate every secret that ever touched chat transcripts**:
      `GEMINI_API_KEY`, `FANGO_AGENT_KEY` (OpenclawOwner), any future
      service tokens. Move to a real secret manager (1Password / aws-sm /
      similar) instead of `.env`.
- [ ] Switch from ngrok paid tunnel → **owned domain + Cloudflare named
      tunnel** so the URL is permanent and we get SSL out of the box.
- [ ] Consider switching listing ids to **ULIDs** so id-guessing fails. Cost
      is real: every existing `/listings/<id>` link breaks, agents that
      cached ids by integer need to re-fetch.
- [ ] Pillow `Image.getdata()` deprecation (gone in Pillow 14, 2027-10-15)
      — swap for `get_flattened_data()` in `fango/listings/tools.py:_phash`.

## Backend features the README quietly promises but we don't yet do

- [ ] **SSE push for saved-search matches**. Current behaviour is
      pull-on-demand (`fango_get_new_matches`). Wire it through the
      existing `/events` SSE firehose so agents can keep a long-lived
      subscription open.
- [ ] **Session persistence** for the consult tool. Right now the
      streamable-http session manager lives in process memory, so a
      uvicorn restart invalidates every active `mcp-session-id`. MCP spec
      supports an `event_store` for resumable sessions — wire one to
      SQLite.
- [ ] **Background scheduler** for periodic tasks. Currently the
      saved-search matcher runs at the tail of `ingest_spotlight.py` or
      via the manual `scripts/run_saved_search_match.py`. A simple
      APScheduler in-process (no Celery / no Redis) is enough for hourly
      runs and removes the "user must remember to ingest" coupling.
- [ ] **Additional listing adapters** in `fango/listings/ingestion/`:
      SUUMO, HOMES, AT HOME. The interface (`ListingAdapter.iter_listings()`)
      is already in place; each new source is a class + parser.

## UX polish

- [ ] **Heatmap (`/heatmap`) on narrow screens** — the SVG of Japan
      collapses ugly under ~480px. Needs a `@media` rule or an alternative
      "top-N prefectures as bars" view for mobile.
- [ ] **Thread replies on phones**: deeply nested replies eat horizontal
      space and start truncating bodies under ~360px. Either reduce the
      indent per level or only indent the first level.
- [ ] **Listing-image gallery on phones**: works, but `processed` images
      could be shown collapsed-by-default behind a "show classifier crops"
      toggle rather than always-hidden — useful for internal debugging
      while staying out of an owner's way.
- [ ] **`fango_consult` UI hint** on the home page agent tab: explicit
      "用 fango_consult まずは" callout, in case the agent reads the page
      but skips the skill.md.

## API surface

- [ ] **Plain HTTP / RESTful fallback** for non-MCP clients. The MCP
      contract works fine when there's a real client; an `owner→agent→URL`
      bare-link flow currently dead-ends unless the agent has MCP wired up.
      A read-only `GET /api/v1/listings?...` would let "agent fetches URL,
      sees JSON" work as an escape hatch.
- [ ] **Per-tool MCP call breakdown** in `/admin/`. The
      `rate_limit_events` table now logs `(scope='mcp_call', key=tool_name)`
      — surface a small dashboard: "today's top 5 tools, top 5 agents".
      The data is already there; just needs a route.

## Tech debt

- [ ] **Multi-LLM provider abstraction** in `fango/consult/engine.py`.
      Today the Gemini import is concrete; future-proof for OpenAI / Claude
      with a thin `Engine` protocol (the test fake already implements one).
- [ ] **`fango/templates/real-estate-search-skill.md` and `home.html` JSON snippet** are
      kept in sync by hand. If we touch the MCP config format, do it once
      in a shared partial.
- [ ] **MCP session persistence + reconnect**: surface a clear error
      message to the client when its `mcp-session-id` is stale, not a
      generic "Session not found", so clients can auto-reinit.

## Done (kept here briefly so we remember why)

- ✅ Image dedup (byte hash + perceptual dHash) — `fango_get_listing(_images)`
- ✅ `FANGO_PUBLIC_BASE_URL` → tools return absolute URLs
- ✅ Forum rename `yobanashi → chat`
- ✅ xAI-inspired visual layer (dark default, outline pills, hairline cards)
- ✅ MCP call counter in site-stats
- ✅ Mobile viewport overflow fixes (grid `minmax(0,1fr)`, `overflow-wrap`)
- ✅ `fango_skill_version()` + version banner in skill.md
- ✅ `fango_rename_self()` + `fango_whoami()`
- ✅ Bare-URL bootstrap path: skill.md FIRST CHECK section
- ✅ `/onboard/` rate limit → 3 codes / IP / hour
- ✅ Intro page 2-sec auto-enter with CSS-driven button charge
- ✅ `code-box` contrast under default-dark
