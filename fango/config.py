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
    notion_sale_db: str | None
    public_base_url: str | None
    # Browser-driven HOMES lookup (find a listing's public URL + photo). Off by
    # default (incl. tests/dev); set FANGO_EXTERNAL_LOOKUP_ENABLED=1 to enable.
    external_lookup_enabled: bool
    external_lookup_ua: str
    # When HOMES blocks the browser (WAF/CAPTCHA), recover the URL via a
    # DuckDuckGo search. On by default; only fires on a detected block.
    external_lookup_fallback: bool
    # Master secret for encrypting custodial agent private keys at rest
    # (fango/identity.py). Absent ⇒ server-custody identity generation is skipped.
    identity_secret: str | None


# A realistic desktop UA so SUUMO/HOMES return normal markup (not a bot page).
_DEFAULT_LOOKUP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def load_settings() -> Settings:
    db = Path(os.environ.get("FANGO_DB_PATH") or (DATA_DIR / "fango.db"))
    pub = os.environ.get("FANGO_PUBLIC_BASE_URL") or None
    if pub:
        pub = pub.rstrip("/")     # so we can naive-join with "/foo" everywhere
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
        notion_sale_db=os.environ.get("NOTION_SALE_DATABASE_ID") or None,
        public_base_url=pub,
        external_lookup_enabled=_env_bool("FANGO_EXTERNAL_LOOKUP_ENABLED", False),
        external_lookup_ua=os.environ.get("FANGO_EXTERNAL_LOOKUP_UA") or _DEFAULT_LOOKUP_UA,
        external_lookup_fallback=_env_bool("FANGO_EXTERNAL_LOOKUP_FALLBACK", True),
        identity_secret=os.environ.get("FANGO_IDENTITY_SECRET") or None,
    )


@dataclass(frozen=True)
class PgSettings:
    """Upstream inventory Postgres (賃貸 main.* / 売買 baibai.*).

    Read-only and LAN-only: the DSN must name a role with SELECT and nothing
    else. Unconfigured ⇒ the Postgres ingest adapter yields nothing and the
    rest of the app is unaffected (SQLite stays the runtime store)."""

    dsn: str | None
    statement_timeout_ms: int

    def is_configured(self) -> bool:
        return bool(self.dsn)


def load_pg_settings() -> PgSettings:
    return PgSettings(
        dsn=os.environ.get("FANGO_PG_DSN") or None,
        # A full rental sweep is ~350k rows; the per-statement ceiling is
        # generous but still bounded so a stuck sync can't pin an upstream
        # backend indefinitely.
        statement_timeout_ms=int(os.environ.get("FANGO_PG_STATEMENT_TIMEOUT_MS", "600000")),
    )


@dataclass(frozen=True)
class S3Settings:
    """Upstream photo bucket (Garage, S3-compatible, on the office LAN).

    Read-only: the key must have READ and nothing else. Unconfigured ⇒ the
    photo fetcher reports and does nothing; listings still ingest without
    their photos."""

    endpoint: str | None
    bucket: str | None
    region: str
    access_key_id: str | None
    secret_access_key: str | None

    def is_configured(self) -> bool:
        return bool(self.endpoint and self.bucket
                    and self.access_key_id and self.secret_access_key)


def load_s3_settings() -> S3Settings:
    return S3Settings(
        endpoint=os.environ.get("FANGO_S3_ENDPOINT") or None,
        bucket=os.environ.get("FANGO_S3_BUCKET") or None,
        # Garage's default region name; it is part of the signature scope, so
        # it has to match what the server expects even though it means nothing.
        region=os.environ.get("FANGO_S3_REGION", "garage"),
        access_key_id=os.environ.get("FANGO_S3_ACCESS_KEY_ID") or None,
        secret_access_key=os.environ.get("FANGO_S3_SECRET_ACCESS_KEY") or None,
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


@dataclass(frozen=True)
class ChainSettings:
    """EVM chain config (anchoring + escrow). Unconfigured ⇒ those features are
    skipped and stay off-chain. ``private_key`` controls real funds — never log it."""
    rpc_url: str | None
    private_key: str | None
    contract_addr: str | None       # AgreementRegistry (anchoring)
    escrow_addr: str | None         # DealEscrow (earnest-money escrow)
    token_addr: str | None          # default ERC-20 for stakes
    token_decimals: int
    chain_id: int
    confirmations: int
    confirm_timeout_sec: int
    legacy_gas: bool                 # force legacy gasPrice (chains without EIP-1559)

    def is_configured(self) -> bool:
        return bool(self.rpc_url and self.private_key and self.contract_addr)

    def escrow_is_configured(self) -> bool:
        return bool(self.rpc_url and self.private_key and self.escrow_addr and self.token_addr)


def load_chain_settings() -> ChainSettings:
    return ChainSettings(
        rpc_url=os.environ.get("FANGO_CHAIN_RPC_URL") or None,
        private_key=os.environ.get("FANGO_CHAIN_PRIVATE_KEY") or None,
        contract_addr=os.environ.get("FANGO_CHAIN_CONTRACT_ADDR") or None,
        escrow_addr=os.environ.get("FANGO_CHAIN_ESCROW_ADDR") or None,
        token_addr=os.environ.get("FANGO_CHAIN_TOKEN_ADDR") or None,
        token_decimals=int(os.environ.get("FANGO_CHAIN_TOKEN_DECIMALS", "18")),
        chain_id=int(os.environ.get("FANGO_CHAIN_ID", "11155111")),   # Sepolia default
        confirmations=int(os.environ.get("FANGO_CHAIN_CONFIRMATIONS", "1")),
        confirm_timeout_sec=int(os.environ.get("FANGO_CHAIN_CONFIRM_TIMEOUT_SEC", "180")),
        legacy_gas=os.environ.get("FANGO_CHAIN_LEGACY_GAS", "").lower() in ("1", "true", "yes"),
    )
