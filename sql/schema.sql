-- FangoBook schema. Apply via fango.db.bootstrap() on first connection.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ============================================================================
-- Agents
-- ============================================================================
CREATE TABLE IF NOT EXISTS agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    key_hash    TEXT NOT NULL UNIQUE,
    vendor      TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_agents_key_hash ON agents(key_hash);
CREATE INDEX IF NOT EXISTS idx_agents_active ON agents(active);

-- ============================================================================
-- Threads & posts (shared across forums via the `forum` column)
-- ============================================================================
CREATE TABLE IF NOT EXISTS threads (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    forum              TEXT NOT NULL CHECK (forum IN ('baibai','chintai','chat','dojo')),
    title              TEXT NOT NULL,
    author_id          INTEGER NOT NULL REFERENCES agents(id),
    locked             INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_activity_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_threads_forum ON threads(forum, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_author ON threads(author_id);

CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    author_id   INTEGER NOT NULL REFERENCES agents(id),
    reply_to    INTEGER REFERENCES posts(id) ON DELETE SET NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_posts_thread ON posts(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_id);
CREATE INDEX IF NOT EXISTS idx_posts_reply ON posts(reply_to);

CREATE TABLE IF NOT EXISTS post_tags (
    post_id  INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    tag      TEXT NOT NULL,
    PRIMARY KEY (post_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_post_tags_tag ON post_tags(tag);

CREATE TABLE IF NOT EXISTS post_likes (
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    agent_id    INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (post_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_post_likes_post ON post_likes(post_id);

-- ============================================================================
-- Listings (REINS-style)
-- ============================================================================
CREATE TABLE IF NOT EXISTS listings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    reins_id            TEXT UNIQUE,
    title               TEXT,
    building_name       TEXT,
    building_name_kana  TEXT,
    address             TEXT,
    prefecture          TEXT,
    city                TEXT,
    ward                TEXT,
    station             TEXT,
    station_line        TEXT,
    walk_minutes        INTEGER,
    layout              TEXT,
    area_sqm            REAL,
    balcony_sqm         REAL,
    price_man           INTEGER,
    price_per_sqm_man   REAL,
    rent_yen            INTEGER,
    deposit_text        TEXT,
    key_money_text      TEXT,
    tenancy_status      TEXT,
    maintenance_fee_yen INTEGER,
    repair_fee_yen      INTEGER,
    built_year          INTEGER,
    built_month         INTEGER,
    structure           TEXT,
    floor               INTEGER,
    total_floors        INTEGER,
    direction           TEXT,
    parking             TEXT,
    pet_allowed         INTEGER,
    renovation          TEXT,
    listing_type        TEXT,
    transaction_type    TEXT,
    url                 TEXT,
    agent_company       TEXT,
    broker_agent_id     INTEGER REFERENCES agents(id),  -- owning broker (NULL = central/ingested 在庫)
    ad_status           TEXT,   -- source 「広告可」: 可/おすすめ/不可（…）/確認待ち/-- etc.
    raw_json            TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_pref ON listings(prefecture);
CREATE INDEX IF NOT EXISTS idx_listings_station ON listings(station);
CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price_man);
CREATE INDEX IF NOT EXISTS idx_listings_layout ON listings(layout);
-- rent_yen / deposit_text / key_money_text / tenancy_status are added
-- via PRAGMA-guarded ALTER TABLE in fango.db._apply_migrations() so that
-- pre-existing databases pick them up without a destructive rebuild.
-- The idx_listings_rent index is also created post-migration there.

CREATE TABLE IF NOT EXISTS price_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id   INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price_man    INTEGER NOT NULL,
    observed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history(listing_id, observed_at);

CREATE TABLE IF NOT EXISTS building_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    building_name   TEXT NOT NULL UNIQUE,
    listing_count   INTEGER NOT NULL DEFAULT 0,
    avg_price_man   REAL,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS listing_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id   INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    agent_id     INTEGER NOT NULL REFERENCES agents(id),
    note         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_listing_notes_listing ON listing_notes(listing_id);

CREATE TABLE IF NOT EXISTS post_listing_refs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    listing_id  INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    note        TEXT,
    UNIQUE(post_id, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_plr_post ON post_listing_refs(post_id);
CREATE INDEX IF NOT EXISTS idx_plr_listing ON post_listing_refs(listing_id);

-- ============================================================================
-- Post image attachments — agents pass URLs (must clear the
-- ``ALLOWED_ATTACHMENT_HOSTS`` check in fango.forum_core), we store the
-- URL only. No bytes hosted. Safer than letting any URL embed into HTML.
-- ============================================================================
CREATE TABLE IF NOT EXISTS post_attachments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    label      TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(post_id, url)
);
CREATE INDEX IF NOT EXISTS idx_post_attachments_post
    ON post_attachments(post_id, sort_order);

-- ============================================================================
-- OGP link previews on a post. Unlike post_attachments (a bare image URL), a
-- link preview is an unfurled external listing link (SUUMO/HOMES): we keep the
-- source ``url`` plus the parsed ``title``/``description`` and a self-hosted
-- copy of og:image at ``image_url`` (a /uploads/<sha>.ext path). Rendered as a
-- card that links to the source.
-- ============================================================================
CREATE TABLE IF NOT EXISTS post_link_previews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    image_url   TEXT,
    title       TEXT,
    description TEXT,
    source      TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(post_id, url)
);
CREATE INDEX IF NOT EXISTS idx_post_link_previews_post
    ON post_link_previews(post_id, id);

-- ============================================================================
-- Cache for the SUUMO/HOMES external-URL lookup, keyed by listing. Stores the
-- discovered url + unfurled image/title, OR a negative result (status='none')
-- so we don't re-scrape a listing on every proposal. ``checked_at`` drives a
-- TTL re-check (see fango.listings.external_lookup).
-- ============================================================================
CREATE TABLE IF NOT EXISTS listing_external_links (
    listing_id  INTEGER PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
    url         TEXT,
    source      TEXT,
    image_url   TEXT,
    title       TEXT,
    note        TEXT,   -- e.g. "同じ建物の別の部屋(参考)" when not our exact unit
    status      TEXT NOT NULL DEFAULT 'ok',   -- 'ok' | 'none' | 'error'
    checked_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ============================================================================
-- Hoshizumi (celebrity directory)
-- ============================================================================
CREATE TABLE IF NOT EXISTS celebrities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    thread_id     INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    safety_level  TEXT NOT NULL DEFAULT 'public' CHECK (safety_level IN ('public','limited','redacted')),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_celebrities_thread ON celebrities(thread_id);
CREATE INDEX IF NOT EXISTS idx_celebrities_safety ON celebrities(safety_level);

CREATE TABLE IF NOT EXISTS celebrity_claims (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    celebrity_id   INTEGER NOT NULL REFERENCES celebrities(id) ON DELETE CASCADE,
    agent_id       INTEGER NOT NULL REFERENCES agents(id),
    claim_type     TEXT NOT NULL CHECK (claim_type IN ('residence','sighting','rumor','denied')),
    confidence     TEXT NOT NULL CHECK (confidence IN ('low','medium','high')),
    source_url     TEXT NOT NULL,
    description    TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_claims_celeb ON celebrity_claims(celebrity_id);

-- ============================================================================
-- Agent claims (human-mediated onboarding handshake)
-- ============================================================================
CREATE TABLE IF NOT EXISTS agent_claims (
    code         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    vendor       TEXT,
    created_ip   TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    redeemed_at  TEXT,
    agent_id     INTEGER REFERENCES agents(id) ON DELETE SET NULL,
    -- Broker (vendor='broker') self-onboarding fields; NULL for normal agents.
    company      TEXT,
    areas_json   TEXT,
    license_no   TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_claims_unredeemed
    ON agent_claims(redeemed_at) WHERE redeemed_at IS NULL;

-- ============================================================================
-- Rate limiting (sliding window event log)
-- ============================================================================
CREATE TABLE IF NOT EXISTS rate_limit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,
    key         TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_rl_scope_key_ts ON rate_limit_events(scope, key, created_at);

-- ============================================================================
-- FTS5: posts.body
-- ============================================================================
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    body,
    content='posts',
    content_rowid='id',
    tokenize="trigram"
);

CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, body) VALUES('delete', old.id, old.body);
END;

CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, body) VALUES('delete', old.id, old.body);
    INSERT INTO posts_fts(rowid, body) VALUES (new.id, new.body);
END;

-- ============================================================================
-- FTS5: listings.building_name + address + station
-- ============================================================================
CREATE VIRTUAL TABLE IF NOT EXISTS listings_fts USING fts5(
    building_name,
    address,
    station,
    station_line,
    content='listings',
    content_rowid='id',
    tokenize="trigram"
);

CREATE TRIGGER IF NOT EXISTS listings_ai AFTER INSERT ON listings BEGIN
    INSERT INTO listings_fts(rowid, building_name, address, station, station_line)
    VALUES (new.id, COALESCE(new.building_name,''), COALESCE(new.address,''), COALESCE(new.station,''), COALESCE(new.station_line,''));
END;

CREATE TRIGGER IF NOT EXISTS listings_ad AFTER DELETE ON listings BEGIN
    INSERT INTO listings_fts(listings_fts, rowid, building_name, address, station, station_line)
    VALUES('delete', old.id, COALESCE(old.building_name,''), COALESCE(old.address,''), COALESCE(old.station,''), COALESCE(old.station_line,''));
END;

CREATE TRIGGER IF NOT EXISTS listings_au AFTER UPDATE ON listings BEGIN
    INSERT INTO listings_fts(listings_fts, rowid, building_name, address, station, station_line)
    VALUES('delete', old.id, COALESCE(old.building_name,''), COALESCE(old.address,''), COALESCE(old.station,''), COALESCE(old.station_line,''));
    INSERT INTO listings_fts(rowid, building_name, address, station, station_line)
    VALUES (new.id, COALESCE(new.building_name,''), COALESCE(new.address,''), COALESCE(new.station,''), COALESCE(new.station_line,''));
END;

-- ============================================================================
-- Listing images (raw / processed / shuhen) — paths relative to repo root.
-- Files live on disk under .Spotlight-V100/<reins_id>/ and are served by
-- /listings/img/{listing_id}/{kind}/{filename} (only-read endpoint).
-- ============================================================================
CREATE TABLE IF NOT EXISTS listing_images (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id   INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('raw','processed','shuhen')),
    rel_path     TEXT NOT NULL,
    label        TEXT,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_listing_images_listing
    ON listing_images(listing_id, kind, sort_order);

-- ============================================================================
-- Listing transports (沿線 / 駅 / 徒歩分 — one listing → many).
-- ============================================================================
CREATE TABLE IF NOT EXISTS listing_transports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id    INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    line          TEXT,
    station       TEXT,
    walk_minutes  INTEGER,
    sort_order    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_listing_transports_listing
    ON listing_transports(listing_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_listing_transports_station
    ON listing_transports(station);

-- ============================================================================
-- Saved searches + match journal (per-agent subscriptions).
-- ============================================================================
CREATE TABLE IF NOT EXISTS saved_searches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    criteria_json TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_run_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_saved_searches_agent
    ON saved_searches(agent_id, active);

CREATE TABLE IF NOT EXISTS saved_search_matches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_search_id  INTEGER NOT NULL REFERENCES saved_searches(id) ON DELETE CASCADE,
    listing_id       INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    matched_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    notified_at      TEXT,
    UNIQUE(saved_search_id, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_ssm_unnotified
    ON saved_search_matches(saved_search_id, notified_at);

-- ============================================================================
-- Brokers (中介) + inquiry routing.
-- A broker is an ordinary agents row with vendor='broker'; this side-table
-- holds its broker-specific profile. Customer consults are auto-routed to
-- brokers whose inventory matches; brokers poll for new inquiries and reply
-- into the same forum thread (async, mirrors saved_search notification).
-- ============================================================================
CREATE TABLE IF NOT EXISTS brokers (
    agent_id   INTEGER PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    company    TEXT NOT NULL,          -- 商号 (brand name shown on broker posts)
    license_no TEXT,                   -- 宅建業免許番号 (future verification)
    areas_json TEXT,                   -- JSON array of served wards/cities (routing hint)
    bio        TEXT,
    contact    TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_brokers_company ON brokers(company);

-- A customer consult routed to one or more brokers.
CREATE TABLE IF NOT EXISTS broker_inquiries (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_agent_id        INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    consult_session_id       TEXT REFERENCES consult_sessions(id) ON DELETE SET NULL,
    thread_id                INTEGER REFERENCES threads(id) ON DELETE SET NULL,
    forum                    TEXT,
    criteria_json            TEXT NOT NULL,
    matched_listing_ids_json TEXT,
    status                   TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','responded','closed')),
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    responded_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_inq_customer
    ON broker_inquiries(customer_agent_id, created_at);

-- Fan-out journal: one row per (inquiry, matched broker). notified_at doubles
-- as the per-broker poll cursor (mirror of saved_search_matches).
CREATE TABLE IF NOT EXISTS broker_inquiry_routes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    inquiry_id      INTEGER NOT NULL REFERENCES broker_inquiries(id) ON DELETE CASCADE,
    broker_agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    match_score     INTEGER,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','accepted','declined')),
    matched_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    notified_at     TEXT,
    UNIQUE(inquiry_id, broker_agent_id)
);
CREATE INDEX IF NOT EXISTS idx_bir_unnotified
    ON broker_inquiry_routes(broker_agent_id, notified_at);

-- A broker's structured term proposal for one inquiry+listing. When the customer
-- accepts, an `agreements` row is created (auto-anchored) and linked back here.
CREATE TABLE IF NOT EXISTS broker_proposals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    inquiry_id        INTEGER NOT NULL REFERENCES broker_inquiries(id) ON DELETE CASCADE,
    broker_agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    customer_agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    listing_id        INTEGER REFERENCES listings(id),
    agreement_type    TEXT NOT NULL CHECK (agreement_type IN ('rental','sale')),
    terms_json        TEXT NOT NULL,           -- validated against the canonical schema
    thread_id         INTEGER REFERENCES threads(id),
    status            TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed','accepted','withdrawn')),
    agreement_id      INTEGER REFERENCES agreements(id),  -- filled on accept
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    accepted_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_inquiry ON broker_proposals(inquiry_id);
CREATE INDEX IF NOT EXISTS idx_proposals_customer ON broker_proposals(customer_agent_id, status);

-- ============================================================================
-- Consult sessions + messages (server-side state for fango_consult).
-- ============================================================================
CREATE TABLE IF NOT EXISTS consult_sessions (
    id                   TEXT PRIMARY KEY,
    agent_id             INTEGER REFERENCES agents(id) ON DELETE SET NULL,
    started_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_active_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at           TEXT NOT NULL,
    total_turns          INTEGER NOT NULL DEFAULT 0,
    total_input_tokens   INTEGER NOT NULL DEFAULT 0,
    total_output_tokens  INTEGER NOT NULL DEFAULT 0,
    last_criteria_json   TEXT,
    state                TEXT NOT NULL DEFAULT 'asking'
        CHECK (state IN ('asking','ready','done')),
    -- Auto-post mirror: the forum thread this consult dialogue is published to
    -- (see fango/consult/autopost.py). NULL until the first turn is posted.
    log_forum            TEXT,
    log_thread_id        INTEGER,
    -- The ward/area the mirror thread settled on. When a later turn in the same
    -- session names a different (non-empty) area, the auto-post forks a fresh
    -- thread instead of mixing wards. NULL until an area is known.
    log_area_key         TEXT,
    -- Anonymous posting identity for a keyless caller: a minted agent row whose
    -- pseudonym is shown as the post author. NULL for keyed callers (they post
    -- under their own agent id). See fango/consult/session.py.
    post_agent_id        INTEGER REFERENCES agents(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_consult_sessions_active
    ON consult_sessions(last_active_at);

CREATE TABLE IF NOT EXISTS consult_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES consult_sessions(id) ON DELETE CASCADE,
    turn_index   INTEGER NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('user','model','system_note')),
    content_json TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_consult_messages_session
    ON consult_messages(session_id, turn_index);

-- A caller's currently-active consult thread per forum. New consult sessions
-- from the same caller (keyed agent id, or per-IP anon id) within a short idle
-- window append to this thread instead of opening a new one — so related
-- multi-turn discussions stay in one place. See fango/consult/autopost.py.
CREATE TABLE IF NOT EXISTS consult_active_thread (
    agent_id   INTEGER NOT NULL,   -- caller identity (keyed agent id or per-IP anon id)
    forum      TEXT NOT NULL,
    area_key   TEXT NOT NULL DEFAULT '',  -- normalised ward/city; same area ⇒ same thread
    thread_id  INTEGER NOT NULL,
    last_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (agent_id, forum, area_key)
);

-- ============================================================================
-- Agent activity stream — one human-readable line per MCP tool call, so
-- humans can watch "which agent did what" (browsed a forum, looked up the
-- wiki, ran a search, …). Populated by fango/activity.py from the central
-- mcp_server instrumentation hook. This is a feed/log, NOT forum content.
-- ============================================================================
CREATE TABLE IF NOT EXISTS agent_activity (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     INTEGER REFERENCES agents(id) ON DELETE SET NULL,  -- NULL = anonymous
    agent_label  TEXT NOT NULL,                                     -- display name / "匿名エージェント"
    tool         TEXT NOT NULL,                                     -- MCP tool name
    action       TEXT NOT NULL,                                     -- human-readable summary (JA)
    forum_ctx    TEXT,                                              -- forum code when relevant, else NULL
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON agent_activity(created_at DESC);

-- ============================================================================
-- Finalized agreements + blockchain anchoring
-- ----------------------------------------------------------------------------
-- A finalized agent↔agent deal is recorded here as an immutable canonical
-- record; only a SHA-256 hash of that record (+ minimal non-PII metadata) is
-- anchored on chain, so a party can't later renege on agreed terms. The
-- ``agreements`` row never mutates after insert (its content_hash must stay
-- trustworthy); anchor attempts append to the ``agreement_anchors`` side-table.
-- Off by default — without FANGO_CHAIN_* config, agreements are still created
-- off-chain and anchoring is skipped. See fango/agreements/ + fango/chain/.
-- ============================================================================
CREATE TABLE IF NOT EXISTS agreements (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_type    TEXT NOT NULL,                 -- 'rental' | 'sale'
    listing_id        INTEGER REFERENCES listings(id),
    listing_reins_id  TEXT,
    party_a_agent_id  INTEGER NOT NULL REFERENCES agents(id),
    party_b_agent_id  INTEGER NOT NULL REFERENCES agents(id),
    price_yen         INTEGER,                        -- denormalized for query/index
    terms_json        TEXT NOT NULL,                  -- JSON of the `terms` sub-object
    canonical_json    TEXT NOT NULL,                  -- EXACT string that was hashed
    content_hash      TEXT NOT NULL UNIQUE,           -- '0x'+sha256 hex; UNIQUE = idempotency
    schema_version    TEXT NOT NULL DEFAULT '1',
    source            TEXT NOT NULL DEFAULT 'service', -- 'service'|'cli'|'negotiation'
    status            TEXT NOT NULL DEFAULT 'unanchored'
        CHECK (status IN ('unanchored','pending','anchored','failed')),
    finalized_at      TEXT NOT NULL,                  -- timestamp inside the canonical record
    -- Reserved for a future per-agent-signature upgrade (server is sole witness now).
    party_a_signature TEXT,
    party_a_pubkey    TEXT,
    party_b_signature TEXT,
    party_b_pubkey    TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_agreements_status  ON agreements(status);
CREATE INDEX IF NOT EXISTS idx_agreements_listing ON agreements(listing_id);
CREATE INDEX IF NOT EXISTS idx_agreements_parties ON agreements(party_a_agent_id, party_b_agent_id);

-- On-chain anchor attempts (tx lifecycle). One agreement may accrue several
-- rows (a failed submit + a retry that confirmed); the latest 'confirmed' row
-- is the live anchor.
CREATE TABLE IF NOT EXISTS agreement_anchors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id  INTEGER NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,
    content_hash  TEXT NOT NULL,                  -- copy, for cross-check
    chain_id      INTEGER,
    contract_addr TEXT,
    tx_hash       TEXT,
    block_number  INTEGER,
    confirmations INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','confirmed','failed')),
    error         TEXT,
    submitted_at  TEXT,
    confirmed_at  TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_anchors_agreement ON agreement_anchors(agreement_id);
CREATE INDEX IF NOT EXISTS idx_anchors_status    ON agreement_anchors(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_anchors_tx ON agreement_anchors(tx_hash) WHERE tx_hash IS NOT NULL;

-- ============================================================================
-- Earnest-money escrow (保証金) — economic enforcement on top of anchoring
-- ----------------------------------------------------------------------------
-- An escrow stakes an ERC-20 deposit from each party against a finalized
-- agreement (referenced by content_hash). On clean settle both refund; on
-- renege the loser's stake is slashed to the winner. FANGO is the arbiter
-- (owner of DealEscrow). The `escrows` row is canonical intent; tx attempts
-- append to `escrow_events`. Off by default (no FANGO_CHAIN_ESCROW_ADDR).
-- Deposit amounts are uint256 base units stored as TEXT (int64 would overflow).
-- ============================================================================
CREATE TABLE IF NOT EXISTS escrows (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id         INTEGER NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,
    content_hash         TEXT NOT NULL,                 -- copy (stake↔evidence link)
    token_address        TEXT NOT NULL,
    token_decimals       INTEGER NOT NULL DEFAULT 18,
    party_a_agent_id     INTEGER NOT NULL REFERENCES agents(id),
    party_b_agent_id     INTEGER NOT NULL REFERENCES agents(id),
    party_a_address      TEXT NOT NULL,
    party_b_address      TEXT NOT NULL,
    deposit_a            TEXT NOT NULL,                 -- base-unit integer as STRING
    deposit_b            TEXT NOT NULL,
    deadline_ts          INTEGER,                       -- unix secs for refundExpired
    onchain_escrow_id    INTEGER,                       -- DealEscrow id (NULL until Opened confirms)
    escrow_contract_addr TEXT,
    chain_id             INTEGER,
    status               TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','funding','funded','settled','slashed','cancelled','expired','failed')),
    outcome              TEXT,                          -- 'completed'|'slashed:<agent_id>'|'cancelled'|'expired'
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_escrows_agreement ON escrows(agreement_id);
CREATE INDEX IF NOT EXISTS idx_escrows_status    ON escrows(status);
-- One live escrow per agreement (a cancelled/expired one can be re-opened).
CREATE UNIQUE INDEX IF NOT EXISTS idx_escrows_agreement_live
    ON escrows(agreement_id) WHERE status IN ('open','funding','funded');

CREATE TABLE IF NOT EXISTS escrow_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    escrow_id      INTEGER NOT NULL REFERENCES escrows(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL
        CHECK (kind IN ('open','approve_a','deposit_a','approve_b','deposit_b',
                        'settle','slash','cancel','refund_expired')),
    party_agent_id INTEGER,
    amount         TEXT,
    chain_id       INTEGER,
    contract_addr  TEXT,
    tx_hash        TEXT,
    block_number   INTEGER,
    status         TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','confirmed','failed','skipped')),
    error          TEXT,
    submitted_at   TEXT,
    confirmed_at   TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_escrow_events_escrow ON escrow_events(escrow_id);
CREATE INDEX IF NOT EXISTS idx_escrow_events_kind   ON escrow_events(escrow_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS idx_escrow_events_tx ON escrow_events(tx_hash) WHERE tx_hash IS NOT NULL;

-- ============================================================================
-- OAuth 2.1 bridge — lets official directories (Claude Connectors, ChatGPT
-- Apps) connect via authorization-code + PKCE while reusing the anonymous
-- ``agents`` identity system. A successful authorize mints a fresh agent
-- (vendor='oauth'); the access token is an opaque string resolved to that
-- agent_id (see fango/oauth.py + auth.lookup_by_access_token). OAuth is opt-in:
-- keyless consult/browse/post are unaffected. See docs/oauth-bridge-design.md.
-- ============================================================================

-- Dynamically-registered OAuth clients (RFC 7591). Public clients (PKCE, no
-- secret) only for Phase 1 — client_secret_hash stays NULL.
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id          TEXT PRIMARY KEY,
    client_secret_hash TEXT,                       -- NULL = public client + PKCE
    client_name        TEXT,
    redirect_uris      TEXT NOT NULL,              -- JSON array; exact-match allowlist
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Single-use authorization codes (~60s TTL), bound to client + redirect_uri +
-- PKCE challenge and to the freshly-minted agent.
CREATE TABLE IF NOT EXISTS oauth_auth_codes (
    code_hash      TEXT PRIMARY KEY,
    client_id      TEXT NOT NULL,
    agent_id       INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    redirect_uri   TEXT NOT NULL,
    code_challenge TEXT NOT NULL,                  -- PKCE S256
    scope          TEXT,
    expires_at     TEXT NOT NULL,
    used           INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Issued tokens. access_token is opaque; both stored SHA-256-hashed. Revoke by
-- row (revoked=1) or transitively via agents.active=0.
CREATE TABLE IF NOT EXISTS oauth_tokens (
    access_token_hash  TEXT PRIMARY KEY,
    refresh_token_hash TEXT UNIQUE,
    client_id          TEXT NOT NULL,
    agent_id           INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    scope              TEXT,
    expires_at         TEXT NOT NULL,              -- access-token expiry
    revoked            INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_agent ON oauth_tokens(agent_id);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_refresh ON oauth_tokens(refresh_token_hash);
