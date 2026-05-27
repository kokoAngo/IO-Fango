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
    agent_id     INTEGER REFERENCES agents(id) ON DELETE SET NULL
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
    content='listings',
    content_rowid='id',
    tokenize="trigram"
);

CREATE TRIGGER IF NOT EXISTS listings_ai AFTER INSERT ON listings BEGIN
    INSERT INTO listings_fts(rowid, building_name, address, station)
    VALUES (new.id, COALESCE(new.building_name,''), COALESCE(new.address,''), COALESCE(new.station,''));
END;

CREATE TRIGGER IF NOT EXISTS listings_ad AFTER DELETE ON listings BEGIN
    INSERT INTO listings_fts(listings_fts, rowid, building_name, address, station)
    VALUES('delete', old.id, COALESCE(old.building_name,''), COALESCE(old.address,''), COALESCE(old.station,''));
END;

CREATE TRIGGER IF NOT EXISTS listings_au AFTER UPDATE ON listings BEGIN
    INSERT INTO listings_fts(listings_fts, rowid, building_name, address, station)
    VALUES('delete', old.id, COALESCE(old.building_name,''), COALESCE(old.address,''), COALESCE(old.station,''));
    INSERT INTO listings_fts(rowid, building_name, address, station)
    VALUES (new.id, COALESCE(new.building_name,''), COALESCE(new.address,''), COALESCE(new.station,''));
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
        CHECK (state IN ('asking','ready','done'))
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
