"""Spotlight (.Spotlight-V100/) → Listings adapter.

The local crawler dumps two cooperating folders per REINS property:

* ``<reinsId>/`` (numeric-only) — raw photos, processed photos, and 周辺 (POI)
  photos. ``reins_N.jpg`` are the originals, ``processed/cat_XX_YY*.jpg`` are
  classifier outputs, and ``shuhen/shuhen_N_<POI類別>.jpg`` are surroundings.
* ``<YYYYMMDD-HHMMSS>_<reinsId>/`` — ``reins-data.json`` (the REINS field
  dump in Japanese) and ``run.json`` (crawl bookkeeping).

This adapter walks the timestamped folders, parses the JSON, locates the
companion image directory by reins_id, and produces a payload that
``service.upsert_listing()`` can consume plus side-channel image / transport
records that ``replace_images()`` / ``replace_transports()`` apply.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ...config import REPO_ROOT
from .base import ListingAdapter

log = logging.getLogger(__name__)

SPOTLIGHT_DIR_NAME = ".Spotlight-V100"

# Folder name like 20260427-093209_100138924518.
_TIMESTAMPED_RE = re.compile(r"^(\d{8}-\d{6})_(\d+)$")
# 7.85万円 / 10万円 / 13.5万円 → 78500 / 100000 / 135000 yen.
_RENT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*万円")
# "26.08㎡" / "57.39㎡" / "70m2".
_AREA_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(?:㎡|m2|平米)")
# "2014年（平成26年） 8月" / "2019年（令和 1年）11月" → (2014, 8).
# Non-greedy ``.*?`` so we skip past the era-year digits between 年 and the
# month — ``[^0-9]*`` would refuse to cross "26" and the regex would fail.
_BUILT_RE = re.compile(r"(\d{4})年.*?(\d{1,2})月")
# Drop commas: "8,500円" → 8500.
_YEN_RE = re.compile(r"([0-9][0-9,]*)\s*円")
# "5分" / "12分" / "18分".
_WALK_RE = re.compile(r"(\d+)\s*分")
# shuhen_3_ドラッグストア.jpg → ('3', 'ドラッグストア').
_SHUHEN_NAME_RE = re.compile(r"shuhen_(\d+)(?:_(.+))?\.jpe?g$", re.IGNORECASE)
# processed/cat_07_2_var1.jpg → category prefix "cat_07".
_PROCESSED_LABEL_RE = re.compile(r"^(cat_\d+)", re.IGNORECASE)
# reins_1.jpg / reins_10.jpg.
_RAW_NAME_RE = re.compile(r"reins_(\d+)\.jpe?g$", re.IGNORECASE)


@dataclass
class SpotlightRecord:
    """One listing extracted from the Spotlight tree."""

    payload: dict[str, Any]
    transports: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)


class SpotlightListingAdapter(ListingAdapter):
    """Walk REPO_ROOT/.Spotlight-V100/ and yield SpotlightRecord-style dicts.

    Yielded dicts have shape::

        {
            "payload":    <listings table row>,
            "transports": [{line, station, walk_minutes, sort_order}, ...],
            "images":     [{kind, rel_path, label, sort_order}, ...],
        }

    The standard ``iter_listings()`` interface returns just the ``payload`` so
    that callers like ``bulk_import()`` keep working. To get the relations,
    use ``iter_records()``.
    """

    name = "spotlight"

    def __init__(self, root: Path | None = None):
        self.repo_root = REPO_ROOT
        self.root = Path(root) if root else (REPO_ROOT / SPOTLIGHT_DIR_NAME)

    def is_configured(self) -> bool:
        return self.root.exists() and self.root.is_dir()

    def iter_listings(self) -> Iterator[dict[str, Any]]:
        """Standard ListingAdapter contract: yield upsert payloads only."""
        for rec in self.iter_records():
            yield rec.payload

    def iter_records(self) -> Iterator[SpotlightRecord]:
        """Yield (payload + transports + images) for each property."""
        if not self.is_configured():
            log.warning("Spotlight root %s missing; skipping.", self.root)
            return
        seen_reins: set[str] = set()
        # Pick the newest folder when multiple timestamped runs exist per reins_id.
        candidates: dict[str, Path] = {}
        for entry in sorted(self.root.iterdir()):
            if entry.name.startswith("._") or not entry.is_dir():
                continue
            m = _TIMESTAMPED_RE.match(entry.name)
            if not m:
                continue
            reins_id = m.group(2)
            # Newer suffix wins (sorted ascending → keep last).
            candidates[reins_id] = entry

        for reins_id, folder in candidates.items():
            if reins_id in seen_reins:
                continue
            seen_reins.add(reins_id)
            record = self._build_record(folder, reins_id)
            if record is not None:
                yield record

    def _build_record(self, folder: Path, reins_id: str) -> SpotlightRecord | None:
        data_path = folder / "reins-data.json"
        if not data_path.exists():
            return None
        try:
            raw = json.loads(data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("skip %s: %s", data_path, exc)
            return None

        payload = _parse_reins_payload(raw, reins_id)
        transports = _parse_transports(raw.get("交通"))
        if transports:
            first = transports[0]
            payload.setdefault("station_line", first.get("line"))
            payload.setdefault("station", first.get("station"))
            payload.setdefault("walk_minutes", first.get("walk_minutes"))

        images_dir = self.root / reins_id
        images = _scan_images(images_dir, self.repo_root) if images_dir.exists() else []

        return SpotlightRecord(payload=payload, transports=transports, images=images)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _normalize(text: str | None) -> str | None:
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    # NFKC folds 全角→半角 alphanumerics and standardises whitespace.
    return unicodedata.normalize("NFKC", text).strip() or None


def _parse_rent_yen(text: str | None) -> tuple[int | None, float | None]:
    """``7.85万円`` → (78500, 7.85). Returns (rent_yen, rent_man)."""
    if not text:
        return None, None
    m = _RENT_RE.search(_normalize(text) or "")
    if not m:
        return None, None
    man = float(m.group(1))
    yen = int(round(man * 10000))
    return yen, man


def _parse_area(text: str | None) -> float | None:
    if not text:
        return None
    m = _AREA_RE.search(_normalize(text) or "")
    return float(m.group(1)) if m else None


def _parse_yen(text: str | None) -> int | None:
    if not text:
        return None
    norm = _normalize(text)
    if not norm or "なし" in norm or norm.lower() == "none":
        return None
    m = _YEN_RE.search(norm)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def _parse_built(text: str | None) -> tuple[int | None, int | None]:
    if not text:
        return None, None
    m = _BUILT_RE.search(text)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _parse_int(text: str | None) -> int | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        try:
            return int(text)
        except (TypeError, ValueError):
            return None
    norm = _normalize(text)
    if not norm:
        return None
    try:
        return int(norm)
    except ValueError:
        # Pull leading digit run.
        m = re.search(r"-?\d+", norm)
        return int(m.group(0)) if m else None


def _compose_layout(tipo: str | None, rooms: str | None) -> str | None:
    """``Ｋ`` + ``1室`` → ``1K``;  ``ＬＤＫ`` + ``2室`` → ``2LDK``.

    Special case: "ワンルーム" is itself a complete layout ("studio") — no
    leading digit; "1ワンルーム" would be nonsense.
    """
    t = _normalize(tipo)
    r = _normalize(rooms)
    if not t:
        return None
    if "ワンルーム" in t:
        return "ワンルーム"
    # Extract leading integer from rooms ("1室" → 1, fallback 1).
    n: int | None = None
    if r:
        rm = re.search(r"(\d+)", r)
        if rm:
            n = int(rm.group(1))
    if n is None:
        n = 1
    # If tipo already starts with a digit (rare), trust it.
    if re.match(r"^\d", t):
        return t
    return f"{n}{t}"


def _parse_reins_payload(raw: dict[str, Any], reins_id: str) -> dict[str, Any]:
    """Map a raw REINS JSON dict to a ``listings`` table payload."""
    rent_yen, rent_man = _parse_rent_yen(raw.get("賃料"))
    area_sqm = _parse_area(raw.get("使用部分面積"))
    built_year, built_month = _parse_built(raw.get("築年月"))

    # Address: 所在地名１ + ２ + ３, NFKC-normalised for searchability.
    addr_parts = [raw.get("所在地名１") or "", raw.get("所在地名２") or "", raw.get("所在地名３") or ""]
    address_raw = "".join(addr_parts).strip() or None
    address = _normalize(address_raw)

    payload: dict[str, Any] = {
        "reins_id": reins_id,
        "title": _normalize(raw.get("建物名")),
        "building_name": _normalize(raw.get("建物名")),
        "address": address,
        "prefecture": _normalize(raw.get("都道府県名")),
        "city": _normalize(raw.get("所在地名１")),
        "ward": _normalize(raw.get("所在地名１")),
        "layout": _compose_layout(raw.get("間取タイプ"), raw.get("間取部屋数")),
        "area_sqm": area_sqm,
        "rent_yen": rent_yen,
        "price_man": int(round(rent_man)) if rent_man is not None else None,
        "deposit_text": _normalize(raw.get("敷金")),
        "key_money_text": _normalize(raw.get("礼金")),
        "tenancy_status": _normalize(raw.get("現況")),
        "maintenance_fee_yen": _parse_yen(raw.get("管理費")),
        "repair_fee_yen": _parse_yen(raw.get("共益費")),
        "built_year": built_year,
        "built_month": built_month,
        "structure": _normalize(raw.get("建物構造")),
        "floor": _parse_int(raw.get("所在階")),
        "total_floors": _parse_int(raw.get("地上階層")),
        "direction": _normalize(raw.get("バルコニー方向")),
        "parking": _normalize(raw.get("駐車場在否")),
        "listing_type": _normalize(raw.get("物件種目")),
        "transaction_type": _normalize(raw.get("取引態様")),
        "agent_company": _normalize(raw.get("商号")),
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }
    # Strip Nones so upsert preserves existing values on partial updates.
    return {k: v for k, v in payload.items() if v is not None or k in ("raw_json",)}


def _parse_transports(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            continue
        line = _normalize(t.get("沿線"))
        station = _normalize(t.get("駅"))
        walk_text = _normalize(t.get("徒歩"))
        walk_min: int | None = None
        if walk_text:
            m = _WALK_RE.search(walk_text)
            if m:
                walk_min = int(m.group(1))
        if not (line or station or walk_min):
            continue
        out.append({
            "line": line,
            "station": station,
            "walk_minutes": walk_min,
            "sort_order": i,
        })
    return out


def _scan_images(images_dir: Path, repo_root: Path) -> list[dict[str, Any]]:
    """Walk <reinsId>/, classify each .jpg into raw / processed / shuhen."""
    out: list[dict[str, Any]] = []

    raw_files = []
    for f in images_dir.iterdir():
        if f.name.startswith("._") or not f.is_file():
            continue
        m = _RAW_NAME_RE.search(f.name)
        if m:
            raw_files.append((int(m.group(1)), f))
    raw_files.sort()
    for order, (n, path) in enumerate(raw_files):
        out.append({
            "kind": "raw",
            "rel_path": _rel(repo_root, path),
            "label": f"raw_{n}",
            "sort_order": order,
        })

    processed_dir = images_dir / "processed"
    if processed_dir.is_dir():
        proc_files = []
        for f in processed_dir.iterdir():
            if f.name.startswith("._") or not f.is_file():
                continue
            if f.suffix.lower() not in (".jpg", ".jpeg"):
                continue
            proc_files.append(f)
        proc_files.sort(key=lambda p: p.name)
        for order, path in enumerate(proc_files):
            label_m = _PROCESSED_LABEL_RE.match(path.name)
            out.append({
                "kind": "processed",
                "rel_path": _rel(repo_root, path),
                "label": label_m.group(1).lower() if label_m else path.stem,
                "sort_order": order,
            })

    shuhen_dir = images_dir / "shuhen"
    if shuhen_dir.is_dir():
        shuhen_files = []
        for f in shuhen_dir.iterdir():
            if f.name.startswith("._") or not f.is_file():
                continue
            if f.suffix.lower() not in (".jpg", ".jpeg"):
                continue
            shuhen_files.append(f)
        shuhen_files.sort(key=lambda p: p.name)
        for order, path in enumerate(shuhen_files):
            m = _SHUHEN_NAME_RE.search(path.name)
            label = m.group(2) if m and m.group(2) else None
            out.append({
                "kind": "shuhen",
                "rel_path": _rel(repo_root, path),
                "label": label,
                "sort_order": order,
            })

    return out


def _rel(repo_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path.resolve())
