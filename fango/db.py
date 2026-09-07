"""SQLite connection + schema bootstrap."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import SQL_DIR, load_settings

_SCHEMA_PATH = SQL_DIR / "schema.sql"
_bootstrapped: set[str] = set()
_lock = threading.Lock()


def _connect_raw(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
        isolation_level=None,  # autocommit; we manage txns explicitly
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Now that consult runs in worker threads (and background enrich in daemon
    # threads), two writers can collide. Without this, the loser gets an instant
    # `SQLITE_BUSY`; wait up to 5s for the write lock instead.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


_LISTINGS_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rent_yen", "INTEGER"),
    ("deposit_text", "TEXT"),
    ("key_money_text", "TEXT"),
    ("tenancy_status", "TEXT"),
    ("ad_status", "TEXT"),
    # Owning broker (中介) — NULL means central/ingested 在庫 visible to all.
    ("broker_agent_id", "INTEGER"),
    # Provenance. 'pg' = synced from the upstream inventory database, which is
    # what makes a row eligible for the reconcile pass (and, when it vanishes
    # upstream, for retirement). A broker's own hand-created listing must NOT
    # be retired just because no upstream row matches it, so it stays NULL.
    ("source", "TEXT"),
    # Upstream posting date (賃貸 created_time / 売買 first_seen_at), UTC
    # '...Z'. Drives the visibility window and the "newest" sort; our own
    # updated_at cannot, because a bulk sync stamps it identically across
    # tens of thousands of rows.
    ("posted_at", "TEXT"),
)

# Columns added to consult_sessions after the table first shipped (auto-post
# mirror — see fango/consult/autopost.py).
_CONSULT_SESSIONS_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("log_forum", "TEXT"),
    ("log_thread_id", "INTEGER"),
    ("post_agent_id", "INTEGER"),
    # Area the mirror thread settled on; lets a mid-session area switch fork a
    # new thread instead of appending the new ward to the old one.
    ("log_area_key", "TEXT"),
)

# Durable secp256k1 identity for keyed agents (see fango/identity.py). Added by
# migration so existing DBs gain them; keyless/system agents leave them NULL.
_AGENTS_IDENTITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("eth_address", "TEXT"),            # 0x… checksum address — the durable id
    ("pubkey", "TEXT"),                 # secp256k1 public key (hex)
    ("privkey_enc", "TEXT"),            # Fernet-encrypted private key; NULL if self-custody
    ("key_custody", "TEXT"),            # 'server' | 'self' | NULL (no identity)
    ("identity_created_at", "TEXT"),
)


def _add_missing_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, decl in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for tables that existed pre-migration.

    SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, so we introspect
    ``PRAGMA table_info`` and only add columns that are missing.
    """
    _add_missing_columns(conn, "listings", _LISTINGS_NEW_COLUMNS)
    # listing_external_links gained a `note` (reference caveat) after first ship.
    _add_missing_columns(conn, "listing_external_links", (("note", "TEXT"),))
    # Index references rent_yen which only exists after the ALTER above succeeds,
    # so it is created here (out of schema.sql) to handle the old-DB case.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_rent ON listings(rent_yen)")
    # broker_agent_id added just above; index it (out of schema.sql for old DBs).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_broker ON listings(broker_agent_id)")
    _add_missing_columns(conn, "consult_sessions", _CONSULT_SESSIONS_NEW_COLUMNS)
    # Broker self-onboarding fields on agent_claims (see fango/claims.py).
    _add_missing_columns(conn, "agent_claims",
                         (("company", "TEXT"), ("areas_json", "TEXT"), ("license_no", "TEXT")))
    _add_missing_columns(conn, "agents", _AGENTS_IDENTITY_COLUMNS)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_eth_address "
                 "ON agents(eth_address) WHERE eth_address IS NOT NULL")
    # listings_fts gained a `station_line` column (so keyword search matches line
    # names like 中央線). FTS5 can't be ALTERed — drop + recreate + rebuild.
    try:
        fts_cols = {row["name"] for row in conn.execute("PRAGMA table_info(listings_fts)").fetchall()}
    except sqlite3.OperationalError:
        fts_cols = set()
    if fts_cols and "station_line" not in fts_cols:
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS listings_ai;
            DROP TRIGGER IF EXISTS listings_ad;
            DROP TRIGGER IF EXISTS listings_au;
            DROP TABLE IF EXISTS listings_fts;
            CREATE VIRTUAL TABLE listings_fts USING fts5(
                building_name, address, station, station_line,
                content='listings', content_rowid='id', tokenize="trigram");
            CREATE TRIGGER listings_ai AFTER INSERT ON listings BEGIN
                INSERT INTO listings_fts(rowid, building_name, address, station, station_line)
                VALUES (new.id, COALESCE(new.building_name,''), COALESCE(new.address,''), COALESCE(new.station,''), COALESCE(new.station_line,''));
            END;
            CREATE TRIGGER listings_ad AFTER DELETE ON listings BEGIN
                INSERT INTO listings_fts(listings_fts, rowid, building_name, address, station, station_line)
                VALUES('delete', old.id, COALESCE(old.building_name,''), COALESCE(old.address,''), COALESCE(old.station,''), COALESCE(old.station_line,''));
            END;
            CREATE TRIGGER listings_au AFTER UPDATE ON listings BEGIN
                INSERT INTO listings_fts(listings_fts, rowid, building_name, address, station, station_line)
                VALUES('delete', old.id, COALESCE(old.building_name,''), COALESCE(old.address,''), COALESCE(old.station,''), COALESCE(old.station_line,''));
                INSERT INTO listings_fts(rowid, building_name, address, station, station_line)
                VALUES (new.id, COALESCE(new.building_name,''), COALESCE(new.address,''), COALESCE(new.station,''), COALESCE(new.station_line,''));
            END;
            INSERT INTO listings_fts(listings_fts) VALUES('rebuild');
            """
        )
    # consult_active_thread gained `area_key` in its PRIMARY KEY — ALTER can't
    # change a PK, so recreate the (ephemeral) grouping table when it's missing.
    cat_cols = {row["name"] for row in conn.execute("PRAGMA table_info(consult_active_thread)").fetchall()}
    if cat_cols and "area_key" not in cat_cols:
        conn.execute("DROP TABLE consult_active_thread")
        conn.execute(
            """CREATE TABLE consult_active_thread (
                   agent_id   INTEGER NOT NULL,
                   forum      TEXT NOT NULL,
                   area_key   TEXT NOT NULL DEFAULT '',
                   thread_id  INTEGER NOT NULL,
                   last_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                   PRIMARY KEY (agent_id, forum, area_key)
               )"""
        )


def bootstrap(path: Path | None = None) -> Path:
    """Apply schema.sql idempotently. Returns the resolved DB path."""
    settings = load_settings()
    target = Path(path) if path else settings.db_path
    key = str(target.resolve()) if target.exists() else str(target)
    with _lock:
        if key in _bootstrapped:
            return target
        conn = _connect_raw(target)
        try:
            conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            _apply_migrations(conn)
        finally:
            conn.close()
        _bootstrapped.add(str(target.resolve()))
    return target


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else load_settings().db_path
    if str(target.resolve() if target.exists() else target) not in _bootstrapped:
        bootstrap(target)
    return _connect_raw(target)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Re-entrant transaction. If we're already inside one, piggyback on it."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def reset_bootstrap_cache() -> None:
    """Test-only: forget that any DB has been bootstrapped (so a fresh tmp path bootstraps again)."""
    with _lock:
        _bootstrapped.clear()
