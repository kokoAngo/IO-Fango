"""Upstream inventory Postgres → Listings adapter.

The upstream inventory DB is a single Postgres holding two schemas that are
maintained by two different pipelines:

* ``main.shinchaku_bukken`` — 賃貸 (rental). One row per property, ~350k rows.
* ``baibai.properties``     — 売買 (sale), with photos in ``baibai.images``.

Two things about that database shape everything below.

1. **Almost every upstream column is ``text``**, including 賃料 / 面積 / 階 /
   築年月. Values are dirty in the ways free text always is (``built_ym`` of
   ``"202681"`` exists today). So parsing is best-effort and must never raise:
   an unparseable field becomes ``None`` and the row still ingests.
2. **The two schemas disagree about column names that look identical.**
   ``baibai.properties.transaction_type`` is 取引態様 (売主/仲介) — it is *not*
   the sale-vs-rent axis, which is what ``listings.transaction_type`` means
   here. Mapping it straight across is what caused the buy-vs-rent
   misrouting fixed in b91b3fa, so the upstream value is parked in
   ``raw_json`` and the canonical column gets ``'sale'``/``'rent'``.

The mapping layer (:func:`map_rental_row` / :func:`map_sale_row` and the
parsing helpers) is pure and driver-free — importable and testable without
psycopg or a live database. Only :class:`PgListingAdapter` needs the driver.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

from ...config import load_pg_settings
from .base import ListingAdapter

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical values
# ---------------------------------------------------------------------------

# fango's sale-vs-rent axis (listings.transaction_type). Sale is the only value
# matched positively in the search gate; everything else counts as rental.
TT_SALE = "sale"
TT_RENT = "rent"

# listings.source value stamped on everything this adapter produces.
SOURCE_PG = "pg"

# 広告可 verdicts cleared for the public surface.
#   Rental: main.shinchaku_bukken.ad_ok — only 可.
#   Sale:   baibai.properties.ad_repost — 広告可 plus 「但し要連絡」, which means
#           the listing broker wants a call before the ad runs, not that the ad
#           is refused. 一部可（…） is media-scoped (チラシ/新聞 etc.) and is NOT
#           a blanket web clearance, so it stays out.
#
# These come in two spellings and both are needed. The *_UPSTREAM tuples are
# the literal Postgres values (full-width parens) and belong in SQL WHERE
# clauses; the plain tuples are what survives the NFKC fold in _normalize()
# and so are what actually lands in listings.ad_status — which is the form the
# fango-side gate must compare against. Deriving one from the other keeps them
# from drifting apart.
RENTAL_ADVERTISABLE_UPSTREAM = ("可",)
SALE_ADVERTISABLE_UPSTREAM = ("広告可", "広告可（但し要連絡）")

# ---------------------------------------------------------------------------
# Parsing helpers — every one of these returns None rather than raising.
# ---------------------------------------------------------------------------

# "104.03㎡" / "79.71m2" / bare "37.67" (the rental table stores it unitless).
_AREA_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(?:㎡|m2|平米)?")
# "8,500円" / "8500" — the rental 管理費/共益費 columns mix both.
_YEN_RE = re.compile(r"([0-9][0-9,]*)")
# "2008年（平成20年） 1月" → (2008, 1). Non-greedy so we skip the era-year digits.
_BUILT_AT_RE = re.compile(r"(\d{4})年.*?(\d{1,2})月")
# "56階" / "2" / "B1階" → 56 / 2 / None.
_FLOOR_RE = re.compile(r"(-?\d+)\s*階?")
# "徒歩7分" / "7分".
_WALK_RE = re.compile(r"(\d+)\s*分")
# 都道府県 + 市区町村. 郡 is followed by a 町/村, so it is consumed together.
_PREF_RE = re.compile(r"^(北海道|東京都|京都府|大阪府|.{2,3}?[県])")
_CITY_RE = re.compile(r"^(.+?[市区町村])")
_WARD_RE = re.compile(r"^(.+?区)")
# One 交通 leg of baibai.properties.access: "大江戸線 勝どき駅 徒歩7分".
_ACCESS_LEG_RE = re.compile(r"^\s*(\S+線)?\s*(\S+?駅)?\s*(?:徒歩\s*(\d+)\s*分)?\s*$")


def _normalize(text: Any) -> str | None:
    """NFKC-fold to half-width and strip. Empty / whitespace-only → None.

    Upstream mixes full-width and half-width freely (``"中央線　三鷹"`` uses a
    full-width space), so folding before any further parsing is mandatory.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    return unicodedata.normalize("NFKC", text).strip() or None


# Normalized spellings — see the *_UPSTREAM note above. Defined here because
# they are computed with _normalize().
RENTAL_ADVERTISABLE = tuple(_normalize(v) for v in RENTAL_ADVERTISABLE_UPSTREAM)
SALE_ADVERTISABLE = tuple(_normalize(v) for v in SALE_ADVERTISABLE_UPSTREAM)


def parse_man_to_yen(text: Any) -> int | None:
    """``"10.3"`` (万円) → ``103000``. The rental table stores 賃料 unitless."""
    norm = _normalize(text)
    if not norm:
        return None
    m = re.search(r"[0-9]+(?:\.[0-9]+)?", norm)
    if not m:
        return None
    try:
        return int(round(float(m.group(0)) * 10_000))
    except (ValueError, OverflowError):
        return None


def parse_area(text: Any) -> float | None:
    norm = _normalize(text)
    if not norm:
        return None
    m = _AREA_RE.search(norm)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    # A 0 or absurd area is upstream noise, not a real measurement.
    return val if 0 < val < 100_000 else None


def parse_yen(text: Any) -> int | None:
    """``"8,500円"`` / ``"8500"`` → 8500. ``なし`` and friends → None."""
    norm = _normalize(text)
    if not norm or "なし" in norm or norm in ("-", "--"):
        return None
    m = _YEN_RE.search(norm)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_built_ym(text: Any) -> int | None:
    """``"198762"`` → 1987. Returns the year only — there is no month here.

    Despite the column name, ``built_ym`` is NOT YYYYMM: it is 西暦年 followed
    by the *和暦* year. ``"198762"`` is 1987年（昭和62年）, ``"200719"`` is
    2007年（平成19年）. Reading digits 5-6 as a month yields nonsense months
    (62, 19, 81) and drops ~96% of the column on the floor. The real 築年月 —
    the only place the month exists — is the ``built_at_text`` expression
    (``raw->>'築年月'``), which is populated on about a third of rows.
    """
    norm = _normalize(text)
    if not norm:
        return None
    digits = re.sub(r"\D", "", norm)
    if len(digits) < 4:
        return None
    year = int(digits[:4])
    return year if 1850 <= year <= 2100 else None


def parse_built_at(text: Any) -> tuple[int | None, int | None]:
    """``"2008年（平成20年） 1月"`` → (2008, 1) — the sale table's 築年月 form."""
    norm = _normalize(text)
    if not norm:
        return None, None
    m = _BUILT_AT_RE.search(norm)
    if not m:
        # 年 with no 月 still pins the year, which is what built_year_min filters on.
        y = re.search(r"(\d{4})年", norm)
        if y and 1850 <= int(y.group(1)) <= 2100:
            return int(y.group(1)), None
        return None, None
    year, month = int(m.group(1)), int(m.group(2))
    if not (1850 <= year <= 2100) or not (1 <= month <= 12):
        return None, None
    return year, month


def parse_floor(text: Any) -> int | None:
    """``"56階"`` / ``"2"`` → 56 / 2. Basements (``B1階``) → None (no schema for them)."""
    norm = _normalize(text)
    if not norm or norm.upper().startswith("B"):
        return None
    m = _FLOOR_RE.search(norm)
    if not m:
        return None
    try:
        val = int(m.group(1))
    except ValueError:
        return None
    return val if -10 < val < 200 else None


def parse_walk_minutes(text: Any) -> int | None:
    norm = _normalize(text)
    if not norm:
        return None
    m = _WALK_RE.search(norm)
    if m:
        val = int(m.group(1))
    else:
        d = re.search(r"\d+", norm)
        if not d:
            return None
        val = int(d.group(0))
    return val if 0 <= val <= 180 else None


def split_address(address: Any) -> tuple[str | None, str | None, str | None]:
    """``"東京都武蔵野市西久保3丁目"`` → ("東京都", "武蔵野市", None).

    Returns (prefecture, city, ward). ``city`` is the 市区町村 token and
    ``ward`` is set only when that token is a 区 — so 東京都港区 yields
    city=港区 *and* ward=港区. Both search paths (``city LIKE`` and
    ``ward LIKE``) then hit, which is the convention the majority of rows
    already follow.
    """
    norm = _normalize(address)
    if not norm:
        return None, None, None
    pm = _PREF_RE.match(norm)
    if not pm:
        return None, None, None
    pref = pm.group(1)
    rest = norm[len(pref):]
    cm = _CITY_RE.match(rest)
    if not cm:
        return pref, None, None
    city = cm.group(1)
    # 政令指定都市: "横浜市西区…" — the 市 token wins as city, the 区 as ward.
    if city.endswith("市"):
        wm = _WARD_RE.match(rest[len(city):])
        return pref, city, wm.group(1) if wm else None
    ward = city if city.endswith("区") else None
    return pref, city, ward


def split_line_station(text: Any) -> tuple[str | None, str | None]:
    """``"中央線　三鷹"`` → ("中央線", "三鷹駅").

    The rental table packs 沿線 and 駅 into one column separated by a
    (full-width, hence the NFKC fold first) space.
    """
    norm = _normalize(text)
    if not norm:
        return None, None
    parts = [p for p in norm.split(" ") if p]
    if not parts:
        return None, None
    if len(parts) == 1:
        # Only one token: a bare 駅 name unless it self-identifies as a 線.
        return (parts[0], None) if parts[0].endswith("線") else (None, _as_station(parts[0]))
    return parts[0], _as_station(" ".join(parts[1:]))


def _as_station(name: str | None) -> str | None:
    """Normalise to a bare station name without the 駅 suffix.

    ``search_listings`` matches ``station LIKE %入力%``, and users type 「三鷹」
    far more often than 「三鷹駅」 — storing the suffix would still match, but
    stripping it keeps the two upstream conventions (rental has no 駅, sale
    has it) from producing two different values for the same station.
    """
    n = _normalize(name)
    if not n:
        return None
    return n[:-1] if n.endswith("駅") and len(n) > 1 else n


def parse_access(text: Any) -> list[dict[str, Any]]:
    """``"A線 甲駅 徒歩7分 / B線 乙駅 徒歩14分"`` → two transport rows."""
    norm = _normalize(text)
    if not norm:
        return []
    out: list[dict[str, Any]] = []
    for i, leg in enumerate(p.strip() for p in norm.split("/")):
        if not leg:
            continue
        m = _ACCESS_LEG_RE.match(leg)
        if m:
            line, station, walk = m.group(1), _as_station(m.group(2)), m.group(3)
            walk_min = int(walk) if walk else None
        else:
            # Unrecognised shape: keep the raw leg as the station rather than
            # dropping a transport the user might search on.
            line, station, walk_min = None, leg, parse_walk_minutes(leg)
        if not (line or station or walk_min):
            continue
        out.append({
            "line": line,
            "station": station,
            "walk_minutes": walk_min,
            "sort_order": i,
        })
    return out


# ---------------------------------------------------------------------------
# Row → payload mapping
# ---------------------------------------------------------------------------


@dataclass
class PgRecord:
    """One upstream property, in the shape the ingest CLI writes.

    ``payload`` goes to ``service.upsert_listing()`` and ``transports`` to
    ``replace_transports()``. ``images`` is a photo *manifest* (storage keys),
    not ``listing_images`` rows — see :func:`map_images`.
    """

    payload: dict[str, Any]
    transports: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _json_safe(row: dict[str, Any]) -> dict[str, Any]:
    """Make a psycopg row JSON-serialisable (datetimes, Decimals → str)."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if v is None or isinstance(v, (str, int, float, bool, list, dict)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _prune(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop None values so an upsert never blanks a column it has no news about.

    ``raw_json`` is kept even when empty — it is always freshly computed.
    """
    return {k: v for k, v in payload.items() if v is not None or k == "raw_json"}


# The fields below the line of "is this a listing at all". Roughly 8% of the
# rental table is skeleton rows — an id was seen upstream but the detail fetch
# has not run, so every descriptive column is NULL. Ingesting those would put
# blank cards in search results, so the write path (P2) filters on this.
_SUBSTANCE_FIELDS = ("address", "building_name", "rent_yen", "price_man", "layout")


def is_ingestable(payload: dict[str, Any]) -> bool:
    """False for an upstream row that carries an id and nothing else."""
    if not payload.get("reins_id"):
        return False
    return any(payload.get(f) is not None for f in _SUBSTANCE_FIELDS)


def map_rental_row(row: dict[str, Any]) -> PgRecord:
    """``main.shinchaku_bukken`` row → a rental listing record."""
    address = _normalize(row.get("address"))
    pref, city, ward = split_address(address)
    line, station = split_line_station(row.get("line_station"))
    walk = parse_walk_minutes(row.get("walk_min"))
    # Prefer the full 築年月 text (it carries the month); fall back to the
    # built_ym column, which only pins the year. See parse_built_ym.
    built_year, built_month = parse_built_at(row.get("built_at_text"))
    if built_year is None:
        built_year = parse_built_ym(row.get("built_ym"))
    building = _normalize(row.get("building_name"))

    # `seiyaku` is a Notion relation — a JSON array of 成約 record ids, not a
    # status string. Its mere presence means the property is off-market, so it
    # is folded into a status word rather than surfaced raw.
    seiyaku = row.get("seiyaku")
    contracted = bool(seiyaku) and str(seiyaku).strip() not in ("", "[]", "null")

    payload = {
        "reins_id": _normalize(row.get("reins_id")),
        "title": building,
        "building_name": building,
        "address": address,
        "prefecture": pref,
        "city": city,
        "ward": ward,
        "station": station,
        "station_line": line,
        "walk_minutes": walk,
        "layout": _normalize(row.get("layout")),
        "area_sqm": parse_area(row.get("area_sqm")),
        # rent_yen is the rental budget filter; price_man is the *sale* price
        # column and is deliberately left NULL so the two never blur.
        "rent_yen": parse_man_to_yen(row.get("rent_man")),
        "deposit_text": _normalize(row.get("shikikin")),
        "key_money_text": _normalize(row.get("reikin")),
        "maintenance_fee_yen": parse_yen(row.get("kanrihi")),
        "repair_fee_yen": parse_yen(row.get("kyoekihi")),
        "built_year": built_year,
        "built_month": built_month,
        "floor": parse_floor(row.get("floor")),
        "listing_type": _normalize(row.get("property_type")),
        "transaction_type": TT_RENT,
        "tenancy_status": "成約済" if contracted else None,
        "ad_status": _normalize(row.get("ad_ok")),
        "agent_company": _normalize(row.get("mgmt_company")),
        "raw_json": json.dumps(_json_safe(row), ensure_ascii=False),
        "last_seen_at": _utc_now_iso(),
        # Provenance: only rows we synced from upstream may be reconciled and
        # retired against it. A broker's own listing has no upstream row and
        # must not be withdrawn for failing to match one.
        "source": SOURCE_PG,
    }

    transports: list[dict[str, Any]] = []
    if line or station or walk is not None:
        transports.append(
            {"line": line, "station": station, "walk_minutes": walk, "sort_order": 0}
        )
    return PgRecord(payload=_prune(payload), transports=transports)


def map_sale_row(row: dict[str, Any], images: Sequence[dict[str, Any]] = ()) -> PgRecord:
    """``baibai.properties`` row (+ its ``baibai.images``) → a sale listing record."""
    address = _normalize(row.get("address"))
    pref, city, parsed_ward = split_address(address)
    # The sale table carries a curated `ward`; trust it over the parse.
    ward = _normalize(row.get("ward")) or parsed_ward
    # 交通 arrives in two shapes and both are in play. ~40% of rows put the
    # full legs in `access` ("有楽町線 豊洲駅 徒歩6分 / ..."); the rest put only
    # the walk time there ("徒歩 9分") and keep 沿線+駅 in the `station`
    # column. Parsing `access` alone loses the station on 10k+ rows, so the
    # `station` column backfills whatever the first leg is missing.
    transports = parse_access(row.get("access"))
    col_line, col_station = split_line_station(row.get("station"))
    if transports:
        transports[0].setdefault("line", None)
        transports[0]["line"] = transports[0]["line"] or col_line
        transports[0]["station"] = transports[0]["station"] or col_station
        line, station, walk = (
            transports[0]["line"],
            transports[0]["station"],
            transports[0]["walk_minutes"],
        )
    else:
        line, station, walk = col_line, col_station, None
        if line or station:
            transports = [
                {"line": line, "station": station, "walk_minutes": None, "sort_order": 0}
            ]
    built_year, built_month = parse_built_at(row.get("built_at"))
    building = _normalize(row.get("building_name"))

    price_man = row.get("price_man")
    if price_man is not None:
        try:
            price_man = int(price_man)
        except (TypeError, ValueError):
            price_man = None

    payload = {
        "reins_id": _normalize(row.get("bukken_no")),
        "title": building,
        "building_name": building,
        "address": address,
        "prefecture": pref,
        "city": city,
        "ward": ward,
        "station": station,
        "station_line": line,
        "walk_minutes": walk,
        "layout": _normalize(row.get("layout")),
        "area_sqm": parse_area(row.get("exclusive_area")),
        "balcony_sqm": parse_area(row.get("balcony_area")),
        "price_man": price_man,
        "built_year": built_year,
        "built_month": built_month,
        "floor": parse_floor(row.get("floor")),
        "direction": _normalize(row.get("direction")),
        "listing_type": _normalize(row.get("property_category")),
        # Canonical sale-vs-rent axis. Upstream's own `transaction_type` is
        # 取引態様 (売主/仲介) and stays in raw_json — see the module docstring.
        "transaction_type": TT_SALE,
        # 取引状況 (公開中 / 申込あり / 成約) is on-market state, NOT ad clearance.
        "tenancy_status": _normalize(row.get("transaction_status")),
        # 広告転載可否 is the actual advertising clearance, mirroring the
        # rental 広告可 column.
        "ad_status": _normalize(row.get("ad_repost")),
        "agent_company": _normalize(row.get("broker")),
        "raw_json": json.dumps(_json_safe(row), ensure_ascii=False),
        "last_seen_at": _utc_now_iso(),
        # Provenance: only rows we synced from upstream may be reconciled and
        # retired against it. A broker's own listing has no upstream row and
        # must not be withdrawn for failing to match one.
        "source": SOURCE_PG,
    }
    return PgRecord(
        payload=_prune(payload),
        transports=transports,
        images=map_images(images),
    )


# Upstream image categories. 販売図面 (the sales floor-plan sheet) and 概要書
# (the property summary sheet) are internal broker documents — they carry the
# listing broker's name and phone number and must never reach the public
# surface. Only 物件画像 is a photo of the property itself.
PUBLIC_IMAGE_CATEGORIES = ("物件画像",)


def map_images(images: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter + order ``baibai.images`` rows into a photo *manifest*.

    These are NOT ``listing_images`` rows. That table's ``rel_path`` is a
    repo-relative path on local disk (the image endpoint opens it as a file),
    and the bytes live in a LAN-only bucket — so what comes out of here is a
    list of ``storage_key`` entries for ``scripts/fetch_listing_images.py`` to
    download and re-host. See :mod:`fango.listings.objectstore`.

    Keyed on ``storage_key``, not ``storage_url``: the URL column still points
    at ``http://localhost:9000/fango/...``, a MinIO deployment that no longer
    exists — wrong host *and* wrong bucket. The key is the durable part.

    Held back: non-photo categories (internal broker documents), rows the
    upstream classifier disqualified, and anything with a person in frame
    (portrait rights).
    """
    out: list[dict[str, Any]] = []
    for img in images:
        if _normalize(img.get("category")) not in PUBLIC_IMAGE_CATEGORIES:
            continue
        if img.get("disqualified") or img.get("has_person"):
            continue
        key = _normalize(img.get("storage_key"))
        if not key:
            continue
        out.append({
            "kind": "raw",
            "storage_key": key,
            "label": _normalize(img.get("subject")),
            "sort_order": len(out),
        })
    return out


# ---------------------------------------------------------------------------
# The adapter — the only part that needs the driver
# ---------------------------------------------------------------------------

# Explicit column lists rather than SELECT *: the upstream tables gain columns
# without notice (44 on the sale side already), and an unpinned SELECT would
# silently widen raw_json and the network transfer on every upstream migration.
RENTAL_COLUMNS = (
    # `id` is the keyset pagination key: PK, NOT NULL, unique. Not mapped into
    # the payload, but it must be selected for the cursor to advance.
    "id", "reins_id", "created_time", "building_name", "address", "rent_man",
    "area_sqm", "layout", "built_ym", "floor", "walk_min", "line_station",
    "property_type", "mgmt_company", "kanrihi", "kyoekihi", "shikikin",
    "reikin", "ad_ok", "seiyaku", "market_rank", "recommend_score",
    # Not a column: the only place the 築年 *month* survives. `raw` holds a
    # flat Japanese dict on ~30% of rows and a Notion page object (with the
    # Japanese fields empty) on the rest, so this is NULL for the majority —
    # which is why built_ym still carries the year. Pulled as an expression
    # rather than selecting `raw` itself, which is a large jsonb.
    "raw->>'築年月' AS built_at_text",
)
SALE_COLUMNS = (
    "bukken_no", "ward", "property_type", "property_category", "price_man",
    "address", "station", "access", "layout", "exclusive_area",
    "building_name", "floor", "built_at", "transaction_type",
    "transaction_status", "ad_repost", "broker", "direction", "balcony_area",
    "room_number", "is_corner", "management_company", "first_seen_at",
    "updated_at",
)
IMAGE_COLUMNS = (
    # storage_key, NOT storage_url — the URL column points at a MinIO that no
    # longer exists (see map_images). Anything that selects images must carry
    # every field map_images reads, which test_image_columns_cover_the_mapper
    # pins down.
    "bukken_no", "storage_key", "category", "subject", "disqualified",
    "has_person", "source",
)

# Rows per round trip. The rental table is ~350k rows, so it is read in keyset
# pages rather than one query.
PAGE_SIZE = 1000


class PgListingAdapter(ListingAdapter):
    """Read the upstream inventory Postgres. Read-only, always.

    Unconfigured (no ``FANGO_PG_DSN``) or missing psycopg ⇒ ``iter_records()``
    yields nothing and logs, matching how the other optional integrations in
    this repo degrade.
    """

    name = "postgres"

    def __init__(
        self,
        dsn: str | None = None,
        *,
        advertisable_only: bool = False,
        include_contracted: bool = False,
        since: datetime | None = None,
    ):
        settings = load_pg_settings()
        self.dsn = dsn or settings.dsn
        self.statement_timeout_ms = settings.statement_timeout_ms
        self.advertisable_only = advertisable_only
        self.include_contracted = include_contracted
        self.since = since

    def is_configured(self) -> bool:
        return bool(self.dsn)

    # -- connection ---------------------------------------------------------

    def _connect(self):
        import psycopg  # imported lazily: an optional extra (`pip install -e '.[pg]'`)

        conn = psycopg.connect(self.dsn, autocommit=True)
        # Belt and braces on top of the role's own read-only grants: even a
        # mistaken write in this process cannot reach the upstream DB.
        conn.execute("SET default_transaction_read_only = on")
        conn.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")
        return conn

    def _driver_missing(self) -> bool:
        try:
            import psycopg  # noqa: F401
        except ImportError:
            log.error(
                "psycopg not installed; cannot reach the upstream inventory DB. "
                "Install with: pip install -e '.[pg]'"
            )
            return True
        return False

    # -- ListingAdapter contract -------------------------------------------

    def iter_listings(self) -> Iterator[dict[str, Any]]:
        for rec in self.iter_records():
            yield rec.payload

    def iter_records(
        self, kinds: Sequence[str] = ("rent", "sale")
    ) -> Iterator[PgRecord]:
        if not self.is_configured():
            log.warning("PgListingAdapter not configured (FANGO_PG_DSN empty); skipping.")
            return
        if self._driver_missing():
            return
        from psycopg.rows import dict_row

        conn = self._connect()
        try:
            if "rent" in kinds:
                yield from self._iter_rental(conn, dict_row)
            if "sale" in kinds:
                yield from self._iter_sale(conn, dict_row)
        finally:
            conn.close()

    # -- paging -------------------------------------------------------------

    def _iter_pages(
        self, conn, dict_row, *, table: str, columns: Sequence[str],
        key: str, filters: Sequence[str], params: dict[str, Any],
    ) -> Iterator[list[dict[str, Any]]]:
        """Walk a table in keyset pages, yielding one page of rows at a time.

        Keyset (``WHERE key > last ORDER BY key LIMIT n``) rather than a
        server-side cursor or OFFSET. A server-side cursor would need an open
        transaction for the whole sweep, and the consumer writes ~65k rows to
        SQLite between fetches — that is minutes of idle-in-transaction on a
        database that also serves the upstream crawlers, holding back
        autovacuum the entire time. OFFSET would re-scan from the top on every
        page. Each page here is one short, independent statement.

        ``key`` must be unique and NOT NULL or pages will skip or repeat rows.
        """
        after: Any = None
        while True:
            clauses = list(filters)
            page_params = dict(params)
            if after is not None:
                clauses.append(f"{key} > %(after)s")
                page_params["after"] = after
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            sql = (
                f"SELECT {', '.join(columns)} FROM {table}{where} "
                f"ORDER BY {key} LIMIT {PAGE_SIZE}"
            )
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, page_params)
                rows = cur.fetchall()
            if not rows:
                return
            yield rows
            if len(rows) < PAGE_SIZE:
                return
            after = rows[-1][key]

    # -- reconcile ----------------------------------------------------------

    # Only the gate columns. A reconcile sweep re-reads these for every
    # listing already held locally, so it must stay narrow: this is the
    # difference between a few MB and re-transferring the whole inventory.
    RENTAL_GATE_COLUMNS = ("reins_id", "ad_ok", "seiyaku")
    SALE_GATE_COLUMNS = ("bukken_no", "ad_repost", "transaction_status")

    def gate_status(self, kind: str, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Current ad/on-market status upstream for the given listing keys.

        Returns ``{key: {"ad_status": ..., "tenancy_status": ...}}``. A key
        absent from the result is absent upstream — the caller retires it.

        This is what keeps an incremental sync honest. ``--since`` filters on
        ``created_time`` / ``updated_at``, so a row that was ingested while
        cleared and has since been withdrawn (広告可 → 不可) or contracted is
        never revisited by the incremental pass. Leaving it advertisable is
        おとり広告, so the gate columns get re-read on their own schedule.
        """
        if not keys or not self.is_configured() or self._driver_missing():
            return {}
        from psycopg.rows import dict_row

        conn = self._connect()
        try:
            return self._gate_status(conn, dict_row, kind, list(keys))
        finally:
            conn.close()

    def _gate_status(self, conn, dict_row, kind: str, keys: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(keys), PAGE_SIZE):
            chunk = keys[i:i + PAGE_SIZE]
            if kind == "sale":
                sql = (f"SELECT {', '.join(self.SALE_GATE_COLUMNS)} "
                       "FROM baibai.properties WHERE bukken_no = ANY(%s)")
            else:
                # reins_id is NOT unique upstream (~7% of the rental table is
                # duplicate rows for the same property), so collapse to the
                # newest row per key the same way the ingest does — otherwise
                # a stale duplicate could retire a live listing.
                sql = (f"SELECT DISTINCT ON (reins_id) {', '.join(self.RENTAL_GATE_COLUMNS)} "
                       "FROM main.shinchaku_bukken WHERE reins_id = ANY(%s) "
                       "ORDER BY reins_id, id DESC")
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (chunk,))
                for row in cur:
                    if kind == "sale":
                        out[row["bukken_no"]] = {
                            "ad_status": _normalize(row.get("ad_repost")),
                            "tenancy_status": _normalize(row.get("transaction_status")),
                        }
                    else:
                        seiyaku = row.get("seiyaku")
                        contracted = (bool(seiyaku)
                                      and str(seiyaku).strip() not in ("", "[]", "null"))
                        out[row["reins_id"]] = {
                            "ad_status": _normalize(row.get("ad_ok")),
                            "tenancy_status": "成約済" if contracted else None,
                        }
        return out

    # -- rental -------------------------------------------------------------

    def _rental_filters(self) -> tuple[list[str], dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if not self.include_contracted:
            # `seiyaku` non-empty ⇒ 成約済み. '[]' is how an empty Notion
            # relation serialises, so it is not "contracted".
            clauses.append("(seiyaku IS NULL OR seiyaku IN ('', '[]'))")
        if self.advertisable_only:
            clauses.append("ad_ok = ANY(%(ad_ok)s)")
            params["ad_ok"] = list(RENTAL_ADVERTISABLE_UPSTREAM)
        if self.since is not None:
            clauses.append("created_time >= %(since)s")
            params["since"] = self.since
        return clauses, params

    def _iter_rental(self, conn, dict_row) -> Iterator[PgRecord]:
        filters, params = self._rental_filters()
        for page in self._iter_pages(
            conn, dict_row, table="main.shinchaku_bukken",
            columns=RENTAL_COLUMNS, key="id", filters=filters, params=params,
        ):
            for row in page:
                yield map_rental_row(row)

    # -- sale ---------------------------------------------------------------

    def _sale_filters(self) -> tuple[list[str], dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if self.advertisable_only:
            clauses.append("ad_repost = ANY(%(ad_repost)s)")
            params["ad_repost"] = list(SALE_ADVERTISABLE_UPSTREAM)
        if self.since is not None:
            clauses.append("updated_at >= %(since)s")
            params["since"] = self.since
        return clauses, params

    def _iter_sale(self, conn, dict_row) -> Iterator[PgRecord]:
        filters, params = self._sale_filters()
        for page in self._iter_pages(
            conn, dict_row, table="baibai.properties",
            columns=SALE_COLUMNS, key="bukken_no", filters=filters, params=params,
        ):
            # One images query per page instead of ~17k per-property lookups.
            keys = [r["bukken_no"] for r in page if r.get("bukken_no")]
            by_bukken: dict[str, list[dict[str, Any]]] = {}
            if keys:
                with conn.cursor(row_factory=dict_row) as icur:
                    icur.execute(
                        f"SELECT {', '.join(IMAGE_COLUMNS)} FROM baibai.images "
                        "WHERE bukken_no = ANY(%s) ORDER BY bukken_no, id",
                        (keys,),
                    )
                    for img in icur:
                        by_bukken.setdefault(img["bukken_no"], []).append(img)
            for row in page:
                yield map_sale_row(row, by_bukken.get(row.get("bukken_no"), ()))
