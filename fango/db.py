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
    return conn


_LISTINGS_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rent_yen", "INTEGER"),
    ("deposit_text", "TEXT"),
    ("key_money_text", "TEXT"),
    ("tenancy_status", "TEXT"),
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for tables that existed pre-migration.

    SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, so we introspect
    ``PRAGMA table_info`` and only add columns that are missing.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
    for col, decl in _LISTINGS_NEW_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {decl}")
    # Index references rent_yen which only exists after the ALTER above succeeds,
    # so it is created here (out of schema.sql) to handle the old-DB case.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_rent ON listings(rent_yen)")


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
