#!/usr/bin/env python3
"""Poll Fango for new broker inquiries and auto-reply to each.

Turns a broker agent into an always-on responder: every cycle it drains the
broker's new-inquiry mailbox (``broker_get_new_inquiries``, mark-on-read) and
posts a reply into each inquiry's thread (``broker_respond``) that references the
matched listings and offers a viewing. The broker's own identity/company is read
from ``broker_whoami`` so any broker can run this with their own key.

Auth: the broker key comes from the FANGO_BROKER_KEY env var (never hard-coded).
The MCP endpoint defaults to https://fango.city/mcp2/mcp (override with
FANGO_MCP_URL).

    export FANGO_BROKER_KEY=<your broker key>
    python -m scripts.broker_autoresponder --once            # drain once (cron)
    python -m scripts.broker_autoresponder --interval 120    # daemon loop
    python -m scripts.broker_autoresponder --once --dry-run  # preview, post nothing

Cron example (every 3 min):
    */3 * * * * FANGO_BROKER_KEY=... /path/.venv/bin/python -m scripts.broker_autoresponder --once >> /var/log/fango-broker.log 2>&1

Daemon:
    nohup python -m scripts.broker_autoresponder --interval 120 >> autoresponder.log 2>&1 &

Notes / caveats:
- ``broker_get_new_inquiries`` is fetch-and-ack: a returned inquiry won't reappear.
  So each new inquiry is auto-replied exactly once. If the script dies AFTER
  fetching but BEFORE posting, that inquiry is marked read and won't be retried —
  acceptable for an auto-responder; reply happens immediately after fetch.
- ``--dry-run`` uses the non-destructive ``broker_list_inquiries`` so it never
  drains the mailbox while you preview.
- This only sends a first-touch reply (availability + viewing offer). Proposing
  concrete terms / prices (``broker_propose_terms``) is left to a human/broker
  decision, not automated here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    sys.exit("mcp client not installed — run inside the project venv "
             "(pip install mcp) or use the repo's .venv.")

DEFAULT_URL = os.environ.get("FANGO_MCP_URL", "https://fango.city/mcp2/mcp")


def _log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


async def _one(s, name, args=None):
    """Call a tool that returns a single JSON object."""
    res = await s.call_tool(name, args or {})
    return json.loads(res.content[0].text) if res.content else None


async def _many(s, name, args=None):
    """Call a tool that returns a list (one content block per item)."""
    res = await s.call_tool(name, args or {})
    return [json.loads(c.text) for c in res.content if getattr(c, "text", None)]


def _money(m: dict) -> str:
    rent = m.get("rent_yen")
    price = m.get("price_man")
    if rent:
        return f"月額{int(rent):,}円"
    if price:
        return f"{int(price):,}万円"
    return "価格応談"


def _access(m: dict) -> str:
    st, walk = m.get("station"), m.get("walk_minutes")
    if st and walk:
        return f"{st}徒歩{walk}分"
    return st or ""


def build_reply(inq: dict, company: str, max_listings: int) -> str:
    crit = inq.get("criteria") or {}
    area = crit.get("ward") or crit.get("city") or crit.get("prefecture") or "ご希望のエリア"
    lines = [f"{company}です。{area}のお問い合わせ、ありがとうございます。"]
    shown = (inq.get("matched_listings") or [])[:max_listings]
    if shown:
        lines.append("以下の物件をご紹介できます:")
        for m in shown:
            name = m.get("building_name") or "物件"
            layout = m.get("layout") or ""
            parts = [p for p in (name, layout, _money(m), _access(m)) if p]
            lines.append("・" + " / ".join(parts))
    lines.append("内見可能です。ご希望の日程を教えていただければ調整いたします。")
    return "\n".join(lines)


async def poll_once(key: str, url: str, limit: int, max_listings: int, dry_run: bool) -> None:
    full_url = f"{url}?agent_key={key}"
    async with streamablehttp_client(full_url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            who = await _one(s, "broker_whoami")
            company = (who or {}).get("broker", {}).get("company") or "担当エージェント"

            tool = "broker_list_inquiries" if dry_run else "broker_get_new_inquiries"
            inqs = await _many(s, tool, {"limit": limit})
            # Only respond to still-open inquiries (skip closed/accepted ones).
            open_inqs = [i for i in inqs if i.get("status") == "open"]
            if not open_inqs:
                _log(f"{company}: no new open inquiries")
                return

            replied, failed = [], []
            for inq in open_inqs:
                iid = inq["inquiry_id"]
                msg = build_reply(inq, company, max_listings)
                lids = [m["id"] for m in (inq.get("matched_listings") or [])][:max_listings]
                if dry_run:
                    _log(f"[dry-run] would reply to inquiry {iid} (thread {inq.get('thread_id')}):\n{msg}")
                    continue
                resp = await _one(s, "broker_respond",
                                  {"inquiry_id": iid, "message": msg, "listing_ids": lids})
                if (resp or {}).get("posted"):
                    replied.append(iid)
                else:
                    failed.append((iid, (resp or {}).get("error")))
            if not dry_run:
                _log(f"{company}: replied to {replied or '[]'}"
                     + (f", failed {failed}" if failed else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Auto-reply to new Fango broker inquiries.")
    ap.add_argument("--once", action="store_true", help="drain once and exit (for cron)")
    ap.add_argument("--interval", type=int, default=120, help="seconds between polls (daemon mode)")
    ap.add_argument("--limit", type=int, default=50, help="max inquiries drained per poll")
    ap.add_argument("--max-listings", type=int, default=3, help="listings to cite per reply")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"MCP endpoint (default {DEFAULT_URL})")
    ap.add_argument("--dry-run", action="store_true", help="preview replies without posting (non-destructive)")
    args = ap.parse_args(argv)

    key = os.environ.get("FANGO_BROKER_KEY", "").strip()
    if not key:
        print("error: set FANGO_BROKER_KEY to your broker agent key.", file=sys.stderr)
        return 2

    async def _cycle():
        try:
            await poll_once(key, args.url, args.limit, args.max_listings, args.dry_run)
        except Exception as exc:  # keep the daemon alive across transient errors
            _log(f"poll error: {exc!r}")

    if args.once or args.dry_run:
        asyncio.run(_cycle())
        return 0

    _log(f"autoresponder started: every {args.interval}s against {args.url}")
    async def _loop():
        while True:
            await _cycle()
            await asyncio.sleep(args.interval)
    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        _log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
