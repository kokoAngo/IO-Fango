"""Deterministic PII safety net for public forum posts.

The LLM is instructed (see ``pii_policy.md`` + the consult prompts) to keep
personal information out of ``display_ja`` and to reject anything that can't be
made safe. This module is the *belt-and-suspenders* second line: a small,
deterministic scrubber run right before any consult content is published, so an
obvious leak (a name with an honorific, an email, a phone number) can never
reach the public forum even if the model slips.

Design:
- **Names** (latin or Japanese) attached to an honorific, and western
  titles (Mr./Dr. …) → replaced with「オーナー」. Non-blocking: we publish the
  scrubbed text.
- **Contact info** (email / phone) → also removed, but treated as *blocking*:
  the caller should hold the whole post rather than publish a redacted version,
  because contact info has no business being in a housing query at all.

Conservative by design — better to occasionally generalise a building/role word
than to leak a real name. ``BLOCKING`` lists the hit categories that mean
"do not publish this turn at all".
"""
from __future__ import annotations

import re

# Hit categories that mean "hold the post entirely" (don't publish a redacted
# version). Names are scrubbed in place; contact info blocks.
BLOCKING = frozenset({"email", "phone"})

_REPLACEMENT = "オーナー"
_REDACT = "[削除]"

# Personal honorifics that, when suffixed to a token, mark it as a person's name.
_HONORIFICS = (
    "氏|様|さま|さん|サン|ちゃん|くん|君|"
    "社長|会長|専務|常務|部長|課長|係長|室長|主任|"
    "先生|教授|博士"
)

# Common non-name nouns that legitimately take an honorific — never treat the
# token before the honorific as a personal name in these cases.
_HONORIFIC_STOP = {
    "皆", "皆さま", "みな", "みなさ", "お客", "神", "仏", "王", "女王",
    "ご家族", "家族", "ご近所", "両親", "ご両親", "お子", "お互い", "おじい", "おばあ",
}

# Western title + name, e.g. "Mr. John Smith", "Dr Tanaka".
_TITLE_NAME = re.compile(
    r"\b(?:Mr|Mrs|Ms|Mx|Dr|Prof)\.?\s+[A-Z][A-Za-z'’.\-]+(?:\s+[A-Z][A-Za-z'’.\-]+){0,2}"
)

# Latin name immediately followed by a Japanese honorific, e.g. "Chris Dai氏".
_LATIN_NAME_HON = re.compile(
    r"[A-Z][A-Za-z'’.\-]*(?:\s+[A-Z][A-Za-z'’.\-]*){0,3}\s*(?:" + _HONORIFICS + r")"
)

# A bare Latin personal name with NO title and NO honorific — the exact hole the
# "Chris Dai向け" incident slipped through. Two-to-four consecutive Capitalised
# Latin tokens (each Upper+lower so all-caps acronyms like LDK/JR/NYC are
# ignored), not bounded by other Latin letters so it still fires when glued to
# Japanese ("Chris Dai向け"). To avoid generalising legitimate place/building
# names, a match is only treated as a name when *not every* token is in the
# geography/real-estate stoplist below.
_LATIN_NAME = re.compile(
    r"(?<![A-Za-z])[A-Z][a-z][A-Za-z'’\-]*(?:\s+[A-Z][a-z][A-Za-z'’\-]*){1,3}(?![A-Za-z])"
)

# Lower-cased tokens that, when they make up the WHOLE matched phrase, mark it as
# a place / building / real-estate term rather than a person. Generous on
# purpose — a missed place word just over-generalises a noun (acceptable per this
# module's design); a missed name would leak PII.
_LATIN_NAME_STOP = frozenset({
    # Tokyo 23 wards + major cities / prefectures (romaji)
    "chiyoda", "chuo", "minato", "shinjuku", "bunkyo", "taito", "sumida", "koto",
    "shinagawa", "meguro", "ota", "setagaya", "shibuya", "nakano", "suginami",
    "toshima", "kita", "arakawa", "itabashi", "nerima", "adachi", "katsushika",
    "edogawa", "tokyo", "osaka", "kyoto", "yokohama", "kawasaki", "saitama",
    "chiba", "nagoya", "kobe", "fukuoka", "sapporo", "sendai", "hiroshima",
    "nara", "yokosuka", "musashino", "machida", "hachioji",
    # popular areas / stations
    "ebisu", "daikanyama", "roppongi", "ginza", "ikebukuro", "ueno", "asakusa",
    "akihabara", "harajuku", "omotesando", "jiyugaoka", "kichijoji", "koenji",
    "nakameguro", "gotanda", "osaki", "tamachi", "shimbashi", "yurakucho",
    "kanda", "ochanomizu", "iidabashi", "ichigaya", "yotsuya", "takadanobaba",
    "otsuka", "sugamo", "komagome", "nippori", "akabane", "oji", "azabu",
    # transit / geography
    "station", "line", "eki", "dori", "street", "avenue", "road", "bridge",
    "river", "gawa", "park", "koen", "garden", "gardens", "hills", "heights",
    "tower", "towers", "central", "north", "south", "east", "west", "side",
    "town", "city", "ward", "area", "district", "front", "gate", "square",
    # building / real-estate vocabulary
    "residence", "residences", "mansion", "apartment", "apartments", "house",
    "home", "homes", "court", "terrace", "place", "plaza", "building", "bldg",
    "flat", "room", "rooms", "floor", "villa", "palace", "annex", "wing",
    "grand", "maison", "royal", "crystal", "sun", "green", "blue", "white",
    "view", "condo", "studio", "loft", "suite", "estate", "real", "property",
    "properties", "new", "used", "rent", "rental", "sale", "buy",
    # common English query filler
    "the", "and", "for", "with", "near", "close", "please", "looking", "want",
    "need", "find", "search", "budget", "yen", "around", "under", "over",
    "within", "between", "family", "single", "pet", "parking",
})


def _latin_name_sub(m: "re.Match") -> str:
    tokens = [t for t in re.split(r"\s+", m.group(0)) if t]
    # Whole phrase is geography/RE vocabulary → leave it (e.g. "Tokyo Tower").
    if all(t.lower().strip("'’.-") in _LATIN_NAME_STOP for t in tokens):
        return m.group(0)
    return _REPLACEMENT

# Japanese (kanji/kana) name + honorific, e.g. "田中さん", "佐藤様". Stoplist-aware.
_JP_NAME_HON = re.compile(
    r"([一-龥々〆ヶぁ-んァ-ヶー]{1,6})(" + _HONORIFICS + r")"
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Phone numbers: international (+81…), JP landline/mobile with separators, or a
# bare 0X0-prefixed 11-digit mobile. Requires a separator or a mobile prefix so
# plain figures (prices, years, areas) don't false-positive.
_PHONE = re.compile(
    r"(?<!\d)(?:"
    r"\+\d{1,3}[\s\-]?\d{1,4}[\s\-]?\d{2,4}[\s\-]?\d{3,4}"   # +81 90-1234-5678
    r"|0[789]0[\s\-]?\d{4}[\s\-]?\d{4}"                        # 090-1234-5678 / 09012345678
    r"|0\d{1,3}[\s\-]\d{2,4}[\s\-]\d{3,4}"                     # 03-1234-5678
    r")(?!\d)"
)


def scrub_for_publish(text: str) -> tuple[str, list[str]]:
    """Return ``(scrubbed_text, hits)``. ``hits`` is the list of PII categories
    found ("email", "phone", "name"). If ``hits`` intersects :data:`BLOCKING`,
    the caller should hold the post rather than publish the scrubbed text."""
    if not text:
        return text, []
    out = text
    hits: list[str] = []

    if _EMAIL.search(out):
        hits.append("email")
        out = _EMAIL.sub(_REDACT, out)
    if _PHONE.search(out):
        hits.append("phone")
        out = _PHONE.sub(_REDACT, out)

    name_found = False
    new = _TITLE_NAME.sub(_REPLACEMENT, out)
    if new != out:
        name_found = True
        out = new
    new = _LATIN_NAME_HON.sub(_REPLACEMENT, out)
    if new != out:
        name_found = True
        out = new

    # Bare Latin name (no title/honorific) — the "Chris Dai向け" hole.
    new = _LATIN_NAME.sub(_latin_name_sub, out)
    if new != out:
        name_found = True
        out = new

    def _jp_sub(m: re.Match) -> str:
        token = m.group(1)
        if token in _HONORIFIC_STOP or (token + m.group(2)) in _HONORIFIC_STOP:
            return m.group(0)
        return _REPLACEMENT

    new = _JP_NAME_HON.sub(_jp_sub, out)
    if new != out:
        name_found = True
        out = new

    if name_found:
        hits.append("name")
    return out, hits


def has_blocking_pii(text: str) -> bool:
    """True if ``text`` contains PII that should block publication outright."""
    _, hits = scrub_for_publish(text or "")
    return bool(BLOCKING & set(hits))
