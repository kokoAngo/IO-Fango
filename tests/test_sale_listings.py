"""Sale (売買) listing surfacing.

Two independent axes, in two columns: ad_status is 広告転載可否 (may we
advertise it) and tenancy_status is 取引状況 (is it still on the market).
Gating on 取引状況 = 公開中 alone — the pre-P3 behaviour — both showed listings
the broker refused advertising on and hid ~88% of ad-cleared inventory, since
the bulk of it sits at "-" meaning "not posted to a portal", not "sold".
"""
from __future__ import annotations

from fango.db import connect
from fango.listings import service as ls
from fango.listings.service import insert_listing


def _ids(rows):
    return {r.id for r in rows}


def _mk(conn, name, *, status, tenancy=None, kind="sale"):
    payload = {
        "building_name": name, "prefecture": "東京都", "city": "江戸川区",
        "transaction_type": kind, "listing_type": kind, "ad_status": status,
        "tenancy_status": tenancy,
    }
    if kind == "sale":
        payload["price_man"] = 6000
    else:
        payload["rent_yen"] = 150000
    return insert_listing(payload, conn=conn)


def test_display_name_falls_back_to_address_and_type():
    from fango.listings.service import display_name
    # Detached house: no 建物名 → address (sans 都道府県) + 物件種目.
    assert display_name(None, "東京都江戸川区大杉５丁目", "中古戸建") == "江戸川区大杉５丁目の中古戸建"
    # Real building name is kept as-is.
    assert display_name("プラウド小岩", "東京都江戸川区", "新築マンション") == "プラウド小岩"
    # No address → nothing to show (template will use 無題物件).
    assert display_name(None, None, "中古戸建") is None


def _page(props):
    return {"id": "pageid", "properties": props}


def _rt(s):
    return {"type": "rich_text", "rich_text": [{"plain_text": s}]}


def _title(s):
    return {"type": "title", "title": [{"plain_text": s}]}


def _sel(s):
    return {"type": "select", "select": {"name": s}}


def _numf(n):
    return {"type": "number", "number": n}


def test_sale_mapper_drops_numeric_name():
    """A row with no 建物名 whose 名称(title) is just the 物件番号 must NOT become
    a numeric 'building name'."""
    from fango.listings.ingestion.notion import _page_to_sale_record
    rec = _page_to_sale_record(_page({
        "建物名": _rt(""),
        "名称": _title("100138130569"),
        "物件番号": _rt("100138130569"),
        "所在地": _rt("東京都豊島区上池袋２丁目"),
        "取引状況": _sel("公開中"),
        "価格万円": _numf(6480),
    }))
    assert rec["building_name"] is None         # not the number
    assert rec["address"] == "東京都豊島区上池袋２丁目"
    assert rec["tenancy_status"] == "公開中"   # 取引状況 is on-market state...
    assert rec.get("ad_status") is None        # ...not ad clearance; Notion has no 広告可 here
    assert rec["price_man"] == 6480
    assert rec["transaction_type"] == "sale"


def test_sale_mapper_keeps_real_name():
    from fango.listings.ingestion.notion import _page_to_sale_record
    rec = _page_to_sale_record(_page({
        "建物名": _rt("プラウドタワー小岩フロント"),
        "所在地": _rt("東京都江戸川区"),
        "取引状況": _sel("公開中"),
    }))
    assert rec["building_name"] == "プラウドタワー小岩フロント"


def test_only_advertisable_on_market_sale_listings_surface(tmp_db):
    conn = connect(tmp_db)
    try:
        ok = _mk(conn, "売・広告可", status="広告可", tenancy="-")
        ok_pub = _mk(conn, "売・広告可・公開中", status="広告可", tenancy="公開中")
        ok_call = _mk(conn, "売・要連絡", status="広告可(但し要連絡)", tenancy="-")
        refused = _mk(conn, "売・不可", status="不可", tenancy="公開中")
        partial = _mk(conn, "売・一部可", status="一部可(インターネット)", tenancy="公開中")
        unknown = _mk(conn, "売・不明", status=None, tenancy="公開中")
        sold = _mk(conn, "売・成約", status="広告可", tenancy="成約")
        applied = _mk(conn, "売・申込あり", status="広告可", tenancy="申込あり")
        rent = _mk(conn, "賃貸物件", status="可", kind="rent")
        conn.commit()

        got = _ids(ls.search_listings(criteria={"prefecture": "東京都"}, limit=50, conn=conn))
        # Cleared for advertising and not off-market.
        assert ok.id in got
        assert ok_pub.id in got
        assert ok_call.id in got                   # 「但し要連絡」 still clears
        # Not cleared for advertising.
        assert refused.id not in got
        assert partial.id not in got               # media-scoped, not a web clearance
        assert unknown.id not in got               # unknown fails closed
        # Cleared, but off the market.
        assert sold.id not in got
        assert applied.id not in got
        # Rental is unaffected by either sale axis.
        assert rent.id in got

        # count_listings uses the same WHERE builder, so it agrees.
        assert ls.count_listings(criteria={"prefecture": "東京都"}, conn=conn) == 4
    finally:
        conn.close()


def test_include_non_advertisable_still_bounded_by_the_on_market_gate(tmp_db):
    """The escape hatch opens the ad gate for internal callers, but a 成約 row
    must not come back through it — that one is not a compliance preference."""
    conn = connect(tmp_db)
    try:
        refused = _mk(conn, "売・不可", status="不可", tenancy="公開中")
        sold = _mk(conn, "売・成約", status="広告可", tenancy="成約")
        conn.commit()
        got = _ids(ls.search_listings(
            criteria={"prefecture": "東京都", "include_non_advertisable": True},
            limit=50, conn=conn))
        assert refused.id in got
        assert sold.id not in got
    finally:
        conn.close()
