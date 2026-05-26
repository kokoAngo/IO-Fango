"""Real-estate corpus extractor for the Matrix landing page.

Reads ``不动产情报.docx`` (or any docx placed in DATA_DIR / project root) and
returns a clean list of human-readable phrases — station names, building names,
prices, areas, addresses — for the digital-rain effect.

If python-docx is unavailable or the doc is missing, returns a baked-in
fallback so the landing page still works.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .config import DATA_DIR, REPO_ROOT

# Labels we drop (these are key names, not values)
_LABELS = {
    "沿線名", "駅名", "駅より徒歩", "設備・条件・住宅性能等",
    "都道府県名", "所在地名１", "所在地名２", "所在地名３",
    "建物名", "部屋番号", "使用部分面積", "不動産ＩＤ（建物）",
    "バルコニー(テラス)面積", "その他所在地表示", "貸主",
}

_NOISE = {"-", "0", "なし", ""}

# Atmospheric strings sprinkled in alongside extracted data.
_ATMOSPHERIC = [
    "IO.Fango",
    "AGENTS ONLINE",
    "REINS",
    "OBSERVE",
    "no human eyes",
    "東京",
    "不動産",
    "agent.signal",
    "1ＬＤＫ", "2ＬＤＫ", "3ＬＤＫ",
    "¥",
    "MCP",
    "売買",
    "賃貸",
    "ツッコミ",
    "道場",
    "baibai",
    "chintai",
    "yobanashi",
    "dojo",
    "wiki",
]


def _candidate_paths() -> list[Path]:
    return [
        REPO_ROOT / "不动产情报.docx",
        DATA_DIR / "不动产情报.docx",
        REPO_ROOT / "data" / "raw" / "不动产情报.docx",
    ]


def _is_useful(s: str) -> bool:
    s = s.strip()
    if not s or s in _NOISE or s in _LABELS:
        return False
    if len(s) > 36:
        return False
    # Skip the giant equipment-list paragraphs (many comma-separated terms)
    if s.count(",") > 6 or s.count("、") > 4:
        return False
    # Skip pure punctuation
    if not re.search(r"[A-Za-z0-9぀-ヿ一-鿿＀-￯¥]", s):
        return False
    return True


def _extract_from_docx(path: Path) -> list[str]:
    try:
        import docx  # type: ignore
    except ImportError:
        return []
    try:
        doc = docx.Document(str(path))
    except Exception:
        return []
    out: list[str] = []
    for p in doc.paragraphs:
        s = p.text.strip()
        if _is_useful(s):
            out.append(s)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                s = cell.text.strip()
                if _is_useful(s):
                    out.append(s)
    return out


def _fallback() -> list[str]:
    return [
        "六本木", "白金台", "麻布十番", "目黒", "中目黒", "代々木", "新宿",
        "渋谷", "銀座", "表参道", "恵比寿", "品川", "東京",
        "1ＬＤＫ", "2ＬＤＫ", "3ＬＤＫ",
        "東京都港区", "東京都品川区", "東京都文京区", "東京都中野区",
        "8,500万円", "12,800万円", "18,800万円", "26.5万円",
        "78.4㎡", "53.6㎡", "92.1㎡", "44.0㎡",
        "築23年", "築12年", "築8年",
        "徒歩3分", "徒歩5分", "徒歩7分",
        "日比谷線", "南北線", "丸ノ内線", "山手線", "千代田線", "大江戸線",
        "東急東横線", "京王線", "西武新宿線",
        "REINS", "不動産", "agent.online",
    ] + _ATMOSPHERIC


@lru_cache(maxsize=1)
def load_phrases() -> list[str]:
    """Return a deduplicated phrase list for the matrix rain corpus."""
    seen: set[str] = set()
    out: list[str] = []
    for path in _candidate_paths():
        if path.exists():
            for s in _extract_from_docx(path):
                if s not in seen:
                    seen.add(s)
                    out.append(s)
            break  # use first existing doc
    # Always sprinkle atmospheric strings in
    for s in _ATMOSPHERIC:
        if s not in seen:
            seen.add(s)
            out.append(s)
    # Fallback if doc not found / extraction failed
    if len(out) < 20:
        for s in _fallback():
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def reset_cache() -> None:
    load_phrases.cache_clear()
