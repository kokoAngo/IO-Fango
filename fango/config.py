"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "sql"
DATA_DIR = REPO_ROOT / "data"
SEED_DIR = DATA_DIR / "seed"
TAKEDOWNS_PATH = DATA_DIR / "takedowns.jsonl"


def _load_dotenv_once() -> None:
    """Load REPO_ROOT/.env into os.environ on first import.

    Tiny hand-rolled parser so we don't pull python-dotenv as a dep. Existing
    os.environ values win (so shell exports always override the file).
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv_once()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return tuple(default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    db_path: Path
    http_enabled: bool
    agent_key: str | None
    mcp_allowed_hosts: tuple[str, ...]
    http_host: str
    http_port: int
    notion_token: str | None
    notion_listings_db: str | None


def load_settings() -> Settings:
    db = Path(os.environ.get("FANGO_DB_PATH") or (DATA_DIR / "fango.db"))
    return Settings(
        db_path=db,
        http_enabled=_env_bool("FANGO_HTTP", False),
        agent_key=os.environ.get("FANGO_AGENT_KEY") or None,
        mcp_allowed_hosts=_env_list(
            "FANGO_MCP_ALLOWED_HOSTS", ("localhost", "127.0.0.1")
        ),
        http_host=os.environ.get("FANGO_HOST", "127.0.0.1"),
        http_port=int(os.environ.get("FANGO_PORT", "8000")),
        notion_token=os.environ.get("NOTION_TOKEN") or None,
        notion_listings_db=os.environ.get("NOTION_LISTINGS_DATABASE_ID") or None,
    )


@dataclass(frozen=True)
class ConsultSettings:
    api_key: str | None
    model: str
    max_turns: int
    session_ttl_seconds: int
    history_compress_after: int


def load_consult_settings() -> ConsultSettings:
    return ConsultSettings(
        api_key=os.environ.get("GEMINI_API_KEY") or None,
        model=os.environ.get("FANGO_CONSULT_MODEL", "gemini-2.5-flash"),
        max_turns=int(os.environ.get("FANGO_CONSULT_MAX_TURNS", "10")),
        session_ttl_seconds=int(os.environ.get("FANGO_CONSULT_SESSION_TTL_SEC", "86400")),
        history_compress_after=int(os.environ.get("FANGO_CONSULT_HISTORY_COMPRESS_AFTER", "3")),
    )
