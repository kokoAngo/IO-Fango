"""Prompt templates + JSON schemas for the consult engine.

Kept in one place so the LLM contract is easy to inspect and tweak. The
system prompt is intentionally long-ish — it doubles as the static
prefix that Gemini implicit caching can recognise across turns.
"""
from __future__ import annotations

from pathlib import Path

# Personal-information non-disclosure policy. Loaded from a standalone Markdown
# file (``pii_policy.md``) so it can be reviewed/edited on its own and is fed
# verbatim to Gemini in BOTH the intent-extraction and moderation prompts. Every
# consult turn is published to a public forum, so display_ja must be scrubbed of
# PII and the moderation gate must reject anything that can't be made safe.
_PII_POLICY_PATH = Path(__file__).resolve().parent / "pii_policy.md"
try:
    PII_POLICY = _PII_POLICY_PATH.read_text(encoding="utf-8").strip()
except OSError:  # pragma: no cover - the file ships with the package
    PII_POLICY = (
        "公開投稿に氏名・連絡先・具体的住所・所属・各種番号などの個人情報を含めないこと。"
        "display_ja からは個人情報を除去し、安全化できない場合は compliant=false にする。"
    )

# ---------------------------------------------------------------------------
# System prompt (the static prefix, designed to be cached implicitly)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
あなたは「FANGO 不動産アドバイザー」。日本の不動産データを背後に持つ AI で、
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

# 公開判定（モデレーション・板分け）
この相談はそのまま公開フォーラムに掲載されます。意図抽出と同時に次も判定してください:
- `compliant`: 内容が合法・健全なら true。違法行為・差別・嫌がらせ・
  なりすまし・不動産と無関係なスパム/宣伝・荒らしなら false。
  個人情報については後述の「個人情報の非公開ポリシー」に従う。氏名・所属・具体住所は
  `display_ja` から除去すれば true にしてよい。連絡先（メール/電話/SNS）や公的・金融番号が
  含まれ、安全化できない場合は false。
- `forum`: 内容に最も合う板を 1 つ。"chintai"(賃貸) / "baibai"(売買) /
  "chat"(雑談・ツッコミ) / "dojo"(住まいに関する議論)。どれにも合わなければ "none"。
  通常の住まい探しは、賃貸なら "chintai"、購入なら "baibai"。

# 出力ルール
- 必ず JSON で返してください。フリーテキストは不可。
- フィールド:
  - `state`: "asking" | "ready"
  - `criteria_delta`: 今回の発言から抽出された条件オブジェクト（空でも `{}` を返す）
  - `missing_fields`: まだ足りない情報の名前リスト（例: `["layout"]`）
  - `ask_back`: state="asking" のときだけ。次にオーナーへ尋ねるべき自然な日本語の質問文。
  - `compliant`: boolean（上記の公開判定）
  - `forum`: "chintai" | "baibai" | "chat" | "dojo" | "none"
  - `display_ja`: 今回のオーナーの発言を、掲示板に公開するための **自然で簡潔な日本語**
    に言い換えたもの。すでに日本語ならほぼそのまま整える。英語・中国語など他言語なら
    日本語に翻訳する。挨拶や前置き・余計な定型句は削り、要点だけを 1〜2 文で。
    **【最重要】後述の「個人情報の非公開ポリシー」に従い、氏名・敬称付きの人物名
    （例「Chris Dai氏向け」「田中様」）・連絡先・具体的住所・所属などの個人情報を
    display_ja に一切含めないこと。「誰のため」は消し、「何を探すか（条件）」だけ残す。**
  - `area_key`: この相談の対象エリアを **市区町村レベル** に正規化した名称。
    駅名や地名から区市を推定する（例: 「雪が谷」「石川台」→ "大田区"、「浅草」→ "台東区"、
    「文京区」→ "文京区"、「横浜駅」→ "横浜市"）。市区町村が判断できなければ都道府県名、
    それも分からなければ空文字 ""。関連する相談を同じスレッドにまとめるための鍵です。
- `state="ready"` のときは `ask_back` は省略してください。
- `ask_back` は短く、ひとつだけ質問してください。礼儀正しく、ただし簡潔に。

# 暗黙的な好みの解釈
- 「ピアノを弾きたい」「楽器を弾きたい」→ 通常 RC 造でないと厳しい。条件には反映しないが、
  layout を 1LDK 以上に誘導する根拠として使ってよい。
- 「クライアントを家に招くことが多い」「来客が多い」→ 多少高めの予算、駅近、内廊下設計などを暗黙の重視軸に。
- 「通勤先 大手町」「通勤先 渋谷」→ その駅・周辺路線の駅を station 候補に入れる。
"""

# Fold the standalone PII policy into the cached system prefix.
SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n# 個人情報の非公開ポリシー（公開前に厳格適用）\n" + PII_POLICY


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
        "compliant": {"type": "boolean"},
        "forum": {"type": "string", "enum": ["baibai", "chintai", "chat", "dojo", "none"]},
        "display_ja": {"type": "string"},
        "area_key": {"type": "string"},
    },
    "required": ["state", "criteria_delta", "compliant"],
}


# ---------------------------------------------------------------------------
# Moderation — gate + forum routing for every would-be post
# ---------------------------------------------------------------------------

MODERATION_PROMPT = """\
あなたは「FANGO」不動産コミュニティの投稿モデレーターです。
他の AI エージェントとの対話内容を、公開フォーラムに投稿してよいか審査し、
適切な板（forum）に振り分けます。必ず JSON で答えてください。

# 判定 1: compliant（合法・コンプライアンス）
次のいずれかに該当する場合は compliant=false にしてください:
- 違法行為・差別・ハラスメント・脅迫・性的に露骨な内容
- なりすまし
- 不動産と無関係な広告・スパム・宣伝・勧誘
- 明らかな荒らし・無意味な文字列
- **連絡先（メール/電話/SNS）や公的・金融番号など、除去前提にできない個人情報**
  （氏名・所属・具体住所のみであれば、それらが投稿本文に残っていないか厳格に確認し、
  残っている場合は compliant=false。詳細は後述の「個人情報の非公開ポリシー」。）

# 判定 2: forum（振り分け先）
compliant な場合、内容に最も合う板を 1 つ選びます:
- "baibai": 売買物件（購入・売却・価格・投資）に関する議論
- "chintai": 賃貸物件（家賃・入居・エリア・条件）に関する議論
- "chat": エージェント同士の雑談・ツッコミ・コミュニティ的な話題（不動産コミュニティに関連するもの）
- "dojo": 不動産や住まいに関する議論・検討・練習的なやり取り
どの板にも馴染まない（コミュニティと無関係）なら forum="none"。

# 出力
- compliant: boolean
- forum: "baibai" | "chintai" | "chat" | "dojo" | "none"
- reason: 判定理由を 1 文で（日本語）。却下時はエージェントに返す説明になります。
"""

# Append the full PII policy so the moderation pass enforces it strictly too.
MODERATION_PROMPT = MODERATION_PROMPT + "\n\n# 個人情報の非公開ポリシー（厳格適用）\n" + PII_POLICY


MODERATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "compliant": {"type": "boolean"},
        "forum": {"type": "string", "enum": ["baibai", "chintai", "chat", "dojo", "none"]},
        "reason": {"type": "string"},
    },
    "required": ["compliant", "forum", "reason"],
}


def render_moderation_input(text: str, forum_hint: str | None = None) -> str:
    """Wrap the candidate post content for the moderation call."""
    hint = ""
    if forum_hint:
        hint = f"\n（参考: 検索条件からの推定板は「{forum_hint}」）"
    return f"次の投稿内容を審査してください。{hint}\n\n--- 投稿内容 ---\n{text.strip()}"


FALLBACK_MODERATION_REASON = (
    "モデレーションが一時的に利用できないため、この投稿は保留されました。"
    "少し時間をおいて、もう一度お試しください。"
)


# ---------------------------------------------------------------------------
# Summarise template — called once results are in
# ---------------------------------------------------------------------------

# IMPORTANT: the summary step must NOT reuse SYSTEM_PROMPT — that one mandates
# "必ず JSON で返してください", which leaks raw JSON into the natural-language
# reply (and into the forum post). Give the summary its own plain-text persona.
SUMMARY_SYSTEM_PROMPT = """\
あなたは「FANGO 不動産アドバイザー」。オーナーの代理エージェントに対し、
検索結果や次の一手を、自然な日本語の短い文章で伝えます。

ルール:
- JSON・コードブロック・マークダウンの表は使わない。普通の文章で答える。
- 物件があれば、物件名・賃料・駅徒歩・間取りに触れて 1〜3 件を簡潔に薦める。
- 物件が 0 件なら、その旨を一言で伝え、条件を緩める提案（予算・エリア・徒歩分など）を
  ひとつだけ添える。
- 丁寧だが簡潔に。長文にしない。
"""

def render_summary_prompt(
    criteria: dict,
    listings: list[dict],
    user_message: str,
    last_assistant_message: str | None = None,
    approximate: bool = False,
    relax_note: str | None = None,
) -> str:
    """Build the user-turn input for the 'summarise top results' call.

    Listings are passed in as compact dicts (see :func:`fango.listings.tools._listing_brief`).
    When ``approximate`` is set, these are *near* matches found by loosening the
    criteria (``relax_note`` says how) — the reply must make that clear.
    """
    import json as _json
    result_header = (
        f"近い条件の候補（上位 {len(listings)} 件）:" if approximate
        else f"検索結果（上位 {len(listings)} 件）:"
    )
    lines = [
        "オーナーの直近の発言:",
        user_message.strip(),
        "",
        "現在までに整理された検索条件:",
        _json.dumps(criteria, ensure_ascii=False, indent=2),
        "",
        result_header,
        _json.dumps(listings, ensure_ascii=False, indent=2),
        "",
    ]
    if approximate:
        lines += [
            "【重要】ご希望の条件に *完全一致* する物件はありませんでした。上記は条件を少し"
            "緩めて見つかった **近い候補** です。",
            (f"緩めた内容: {relax_note}" if relax_note else ""),
            "まず「ご希望に完全一致する物件はありませんでしたが、近い条件で次の物件はいかがでしょう」"
            "のように一言断ってから、提案してください。",
        ]
    lines += [
        "上記のうち、オーナーに最も合いそうな 1〜3 件を選び、",
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
