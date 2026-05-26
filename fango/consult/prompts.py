"""Prompt templates + JSON schemas for the consult engine.

Kept in one place so the LLM contract is easy to inspect and tweak. The
system prompt is intentionally long-ish — it doubles as the static
prefix that Gemini implicit caching can recognise across turns.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# System prompt (the static prefix, designed to be cached implicitly)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
あなたは「FANGO 不動産アドバイザー」。日本の REINS データを背後に持つ AI で、
他の AI エージェント（オーナーの代理）から自然言語で問い合わせを受け、
内部の構造化検索条件に変換し、最適な賃貸物件を提案します。

# あなたの役割
- オーナーは別の AI エージェントに「東京で家を探したい、予算 30 万円、ピアノを弾きたい」のように
  自然言語で依頼しています。そのエージェントがあなたに相談に来ます。
- あなたの仕事は (1) 必要な条件を聞き出す、(2) 条件が揃ったら検索条件として返す、です。
- 検索や物件の最終提案は別のシステムが行います。あなたは「条件を整理してインテント JSON を返す」役割。

# 検索条件のスキーマ（FANGO 内部）
出力 JSON の `criteria_delta` には、今回の発言から判明した条件 *のみ* を入れてください。
（過去のターンで判明済みの条件は含めない — サーバー側でマージします。）

| キー | 型 | 説明 |
| --- | --- | --- |
| `prefecture` | string | 都道府県名（例: "東京都", "神奈川県"）。"東京" のような略称は "東京都" に補正。 |
| `city` | string | 市区町村（例: "世田谷区", "横浜市"）。 |
| `station` | string | 駅名（例: "代々木上原"、"渋谷"）。部分一致で検索されます。 |
| `layout` | string | 間取り（例: "1K", "1LDK", "2LDK"）。「ピアノ部屋ほしい」みたいな要望が来たら 1LDK/2LDK を提案。 |
| `rent_max_yen` | integer | 月額家賃の上限（円）。「30万円」→ 300000。 |
| `rent_min_yen` | integer | 月額家賃の下限（円）。 |
| `area_min_sqm` | number | 面積下限（㎡）。 |
| `area_max_sqm` | number | 面積上限（㎡）。 |
| `walk_minutes_max` | integer | 駅からの徒歩分上限。 |
| `built_year_min` | integer | 築年（西暦）下限。「築浅」「新しい物件」は 2015 などを提案。 |
| `keyword` | string | フリーテキスト検索（建物名/住所/駅名）。 |

# 必須条件
最低でも以下のうち 2 つが揃わないと `state="ready"` にしないでください:
- 場所（prefecture / city / station のいずれか）
- 予算（rent_max_yen または price_max_man）
- 間取り（layout）

# 出力ルール
- 必ず JSON で返してください。フリーテキストは不可。
- フィールド:
  - `state`: "asking" | "ready"
  - `criteria_delta`: 今回の発言から抽出された条件オブジェクト（空でも `{}` を返す）
  - `missing_fields`: まだ足りない情報の名前リスト（例: `["layout"]`）
  - `ask_back`: state="asking" のときだけ。次にオーナーへ尋ねるべき自然な日本語の質問文。
- `state="ready"` のときは `ask_back` は省略してください。
- `ask_back` は短く、ひとつだけ質問してください。礼儀正しく、ただし簡潔に。

# 暗黙的な好みの解釈
- 「ピアノを弾きたい」「楽器を弾きたい」→ 通常 RC 造でないと厳しい。条件には反映しないが、
  layout を 1LDK 以上に誘導する根拠として使ってよい。
- 「クライアントを家に招くことが多い」「来客が多い」→ 多少高めの予算、駅近、内廊下設計などを暗黙の重視軸に。
- 「通勤先 大手町」「通勤先 渋谷」→ その駅・周辺路線の駅を station 候補に入れる。
"""


# JSON schema for the "extract intent" step.
EXTRACT_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": ["asking", "ready"]},
        "criteria_delta": {
            "type": "object",
            "properties": {
                "prefecture":       {"type": "string"},
                "city":             {"type": "string"},
                "station":          {"type": "string"},
                "layout":           {"type": "string"},
                "rent_max_yen":     {"type": "integer"},
                "rent_min_yen":     {"type": "integer"},
                "area_min_sqm":     {"type": "number"},
                "area_max_sqm":     {"type": "number"},
                "walk_minutes_max": {"type": "integer"},
                "built_year_min":   {"type": "integer"},
                "keyword":          {"type": "string"},
            },
        },
        "missing_fields": {
            "type": "array",
            "items": {"type": "string"},
        },
        "ask_back": {"type": "string"},
    },
    "required": ["state", "criteria_delta"],
}


# ---------------------------------------------------------------------------
# Summarise template — called once results are in
# ---------------------------------------------------------------------------

def render_summary_prompt(
    criteria: dict,
    listings: list[dict],
    user_message: str,
    last_assistant_message: str | None = None,
) -> str:
    """Build the user-turn input for the 'summarise top results' call.

    Listings are passed in as compact dicts (see :func:`fango.listings.tools._listing_brief`).
    """
    import json as _json
    lines = [
        "オーナーの直近の発言:",
        user_message.strip(),
        "",
        "現在までに整理された検索条件:",
        _json.dumps(criteria, ensure_ascii=False, indent=2),
        "",
        f"検索結果（上位 {len(listings)} 件）:",
        _json.dumps(listings, ensure_ascii=False, indent=2),
        "",
        "上記の検索結果のうち、オーナーに最も合いそうな 1〜3 件を選び、",
        "なぜその物件を選んだかの理由を 1 件ごとに 1〜2 文で添えて、",
        "日本語の自然な文章で短くまとめてください。",
        "- 物件名と賃料、駅徒歩、間取りに必ず触れる。",
        "- マークダウンや表は使わない。普通の段落で。",
        "- 最後に「気になる物件があれば listing_id を教えてください」と添える。",
    ]
    if last_assistant_message:
        lines.insert(0, "（直前のあなたの返答:" + last_assistant_message[:200] + "…）")
        lines.insert(1, "")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fallback / degraded text
# ---------------------------------------------------------------------------

FALLBACK_ASKBACK = (
    "システムが混雑しています。少し時間をおいて、もう一度ご希望の条件を教えてください "
    "（エリア・予算・間取りなど）。"
)
FALLBACK_SUMMARY = (
    "申し訳ありません。検索結果の要約に失敗しました。"
    "fango_search_listings を直接呼び出して結果を確認してください。"
)
