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
