"""Sale (売買) listings are only recommended while 取引状況 = 公開中."""
from __future__ import annotations

from fango.db import connect
from fango.listings import service as ls
from fango.listings.service import insert_listing


def _ids(rows):
    return {r.id for r in rows}


def _mk(conn, name, *, status, kind="sale"):
    payload = {
        "building_name": name, "prefecture": "東京都", "city": "江戸川区",
        "transaction_type": kind, "listing_type": kind, "ad_status": status,
    }
    if kind == "sale":
        payload["price_man"] = 6000
    else:
        payload["rent_yen"] = 150000
    return insert_listing(payload, conn=conn)


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
    assert rec["ad_status"] == "公開中"
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


def test_only_public_sale_listings_surface(tmp_db):
    conn = connect(tmp_db)
    try:
        pub = _mk(conn, "売・公開中", status="公開中")
        applied = _mk(conn, "売・申込あり", status="申込あり")
        paused = _mk(conn, "売・一時停止", status="一時停止")
        dash = _mk(conn, "売・ハイフン", status="-")
        rent = _mk(conn, "賃貸物件", status="可", kind="rent")
        conn.commit()

        got = _ids(ls.search_listings(criteria={"prefecture": "東京都"}, limit=50, conn=conn))
        assert pub.id in got                       # 公開中 sale → shown
        assert applied.id not in got               # 申込あり → hidden
        assert paused.id not in got                # 一時停止 → hidden
        assert dash.id not in got                  # "-" → hidden
        assert rent.id in got                      # rental unaffected by the sale gate

        # count_listings uses the same WHERE builder, so it agrees.
        assert ls.count_listings(criteria={"prefecture": "東京都"}, conn=conn) == 2
    finally:
        conn.close()
