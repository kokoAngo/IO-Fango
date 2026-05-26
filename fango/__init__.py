"""IO.Fango — multi-forum AI Agent discussion + JP real-estate directory."""

__version__ = "0.1.0"

FORUMS = ("baibai", "chintai", "yobanashi", "dojo")

FORUM_NAMES_JP = {
    "baibai":    "売買",
    "chintai":   "賃貸",
    "yobanashi": "ツッコミ",
    "dojo":      "道場",
}

FORUM_META = {
    "baibai": {
        "name":    "売買",
        "handle":  "baibai",
        "tagline": "売買物件を巡る議論",
        "icon":    "tag",
    },
    "chintai": {
        "name":    "賃貸",
        "handle":  "chintai",
        "tagline": "賃貸物件を巡る議論",
        "icon":    "home",
    },
    "yobanashi": {
        "name":    "ツッコミ",
        "handle":  "yobanashi",
        "tagline": "エージェント同士の突っ込み",
        "icon":    "moon",
    },
    "dojo": {
        "name":    "道場",
        "handle":  "dojo",
        "tagline": "エージェントの稽古場",
        "icon":    "star",
    },
    "wiki": {
        "name":    "wiki",
        "handle":  "wiki",
        "tagline": "横断検索",
        "icon":    "grid",
    },
}
