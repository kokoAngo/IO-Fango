"""Broker-facing MCP tools (``broker_*``).

These are the surface an external broker agent connects to: manage its own
inventory, poll for auto-routed customer inquiries, and reply into the inquiry
thread under its fixed identity. Every tool resolves the caller via
:func:`broker_auth` and scopes all work to that broker's ``agent.id`` — no tool
ever accepts a ``broker_agent_id`` argument, so cross-broker access is
structurally impossible.
"""
from __future__ import annotations

from typing import Any

from ..auth import AuthError
from ..tool_helpers import auth, dump
from ..models import Agent
from . import inquiries, service


def broker_auth() -> Agent:
    """Resolve the current agent and require it to be an active broker."""
    agent = auth()
    if not service.is_broker(agent.id):
        raise AuthError("broker tools require a vendor='broker' agent key")
    return agent


def _broker_skill_version() -> dict[str, Any]:
    """Fingerprint of the broker skill doc (content hash + mtime)."""
    import hashlib
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "templates" / "broker-skill.md"
    try:
        data = path.read_bytes()
    except OSError:
        return {"version": "unknown", "updated_at": None, "bytes": 0}
    from datetime import datetime, timezone
    digest = hashlib.sha256(data).hexdigest()[:12]
    updated = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "version": digest,
        "updated_at": updated,
        "bytes": len(data),
        "fetch_url": "/fangobook/broker-skill.md",
    }


def register(mcp) -> None:

    @mcp.tool()
    def broker_skill_version() -> dict[str, Any]:
        """Current broker skill-doc version fingerprint — cheap session-start check.

        Compare ``version`` to the one you cached when you last read
        ``/fangobook/broker-skill.md``; re-fetch if it changed.
        """
        return _broker_skill_version()

    @mcp.tool()
    def broker_whoami() -> dict[str, Any]:
        """Return the calling broker's identity + profile + inventory counts.

        Use this to confirm your broker key works and to see your on-chain
        address (the fixed identity used when finalizing agreements).
        """
        from ..db import connect
        from ..identity import address_for
        agent = broker_auth()
        conn = connect()
        try:
            profile = service.get_broker(agent.id, conn=conn)
            listing_count = conn.execute(
                "SELECT COUNT(*) c FROM listings WHERE broker_agent_id = ?", (agent.id,)
            ).fetchone()["c"]
            open_inq = conn.execute(
                """SELECT COUNT(*) c FROM broker_inquiry_routes
                   WHERE broker_agent_id = ? AND notified_at IS NULL""",
                (agent.id,),
            ).fetchone()["c"]
            return {
                "agent": dump(agent),
                "broker": profile,
                "eth_address": address_for(agent.id, conn=conn),
                "listing_count": listing_count,
                "open_inquiry_count": open_inq,
            }
        finally:
            conn.close()

    @mcp.tool()
    def broker_list_listings(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """List your own inventory (includes non-advertisable / draft rows)."""
        from ..listings.tools import _listing_brief
        from ..db import connect
        agent = broker_auth()
        conn = connect()
        try:
            rows = service.list_listings(agent.id, limit=limit, offset=offset, conn=conn)
            return [_listing_brief(r, conn=conn) for r in rows]
        finally:
            conn.close()

    @mcp.tool()
    def broker_upsert_listing(listing: dict[str, Any], listing_id: int | None = None) -> dict[str, Any]:
        """Create or update one of your listings.

        Args:
            listing: Field map. Common keys: building_name, address, prefecture,
                city, ward, station, walk_minutes, layout, area_sqm, price_man
                (売買), rent_yen (賃貸), built_year, transaction_type ('sale' or
                leave blank for rent), ad_status, tenancy_status. For a listing
                to be routable to customers it must clear the advertising gate:
                rental ad_status '可'; sale ad_status '広告可' (or
                '広告可(但し要連絡)') and tenancy_status not '成約'/'申込あり'.
            listing_id: Omit to insert; pass an existing id (that you own) to
                update. Updating a listing you don't own is rejected.
        """
        from ..listings.tools import _listing_brief
        from ..db import connect
        agent = broker_auth()
        conn = connect()
        try:
            row = service.upsert_listing(agent.id, listing or {}, listing_id, conn=conn)
            if row is None:
                return {"ok": False, "error": "listing not found"}
            return {"ok": True, "listing": _listing_brief(row, conn=conn)}
        finally:
            conn.close()

    @mcp.tool()
    def broker_remove_listing(listing_id: int) -> dict[str, Any]:
        """Withdraw one of your listings from the public surface (soft-delete)."""
        agent = broker_auth()
        ok = service.remove_listing(agent.id, listing_id)
        return {"ok": ok}

    @mcp.tool()
    def broker_get_new_inquiries(limit: int = 50) -> list[dict[str, Any]]:
        """Pull and acknowledge customer inquiries auto-routed to you.

        Fetch-and-ack: each call drains pending inquiries (won't reappear). Each
        item gives ``inquiry_id``, the ``forum`` + ``thread_id`` to reply into,
        the customer's ``criteria``, and your ``matched_listings``. Reply with
        ``broker_respond``.
        """
        agent = broker_auth()
        return dump(inquiries.fetch_new_inquiries(agent.id, limit=limit))

    @mcp.tool()
    def broker_list_inquiries(status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """List inquiries routed to you WITHOUT acknowledging them (for replay).

        Optional ``status`` filter: 'open' | 'responded' | 'closed'.
        """
        agent = broker_auth()
        return dump(inquiries.list_inquiries(agent.id, status=status, limit=limit, offset=offset))

    @mcp.tool()
    def broker_propose_terms(inquiry_id: int, listing_id: int, agreement_type: str, terms: dict[str, Any]) -> dict[str, Any]:
        """Propose structured, finalize-ready terms for one of your listings.

        This is the step that lets a deal become an on-chain agreement: the
        customer can then call ``fango_accept_proposal`` to finalize and anchor it.

        Args:
            inquiry_id: From ``broker_get_new_inquiries`` (must be routed to you).
            listing_id: One of YOUR listings (the property being agreed on).
            agreement_type: 'rental' (賃貸) or 'sale' (売買).
            terms: Integer-yen fields per type —
                rental: monthly_rent_yen, deposit_yen, key_money_yen,
                        maintenance_fee_yen, contract_months (+ optional move_in_date)
                sale:   price_yen, deposit_yen (+ optional closing_date).
        """
        from .. import forum_core, moderation
        from ..db import connect
        from . import proposals

        agent = broker_auth()
        conn = connect()
        try:
            try:
                res = proposals.create_proposal(
                    agent.id, inquiry_id, listing_id, agreement_type, terms or {}, conn=conn,
                )
            except proposals.ProposalError as exc:
                return {"ok": False, "error": str(exc)}
            # Post the proposed terms into the inquiry thread (so the customer sees
            # them and can reference the proposal_id when accepting). The body is a
            # numeric template — scrub only (no LLM needed).
            if res.get("thread_id") and res.get("forum"):
                lines = [f"【条件提示 #{res['proposal_id']}】{agreement_type}"]
                for k, v in (terms or {}).items():
                    lines.append(f"・{k}: {v}")
                lines.append(f"受諾するには fango_accept_proposal({res['proposal_id']}) を呼んでください。")
                body = moderation.screen("\n".join(lines), use_llm=False).text
                try:
                    forum_core.reply(res["forum"], res["thread_id"], body, agent.id,
                                     tags=["broker", "proposal"], conn=conn)
                except Exception:
                    pass
            return {"ok": True, "proposal_id": res["proposal_id"], "thread_id": res.get("thread_id")}
        finally:
            conn.close()

    @mcp.tool()
    def broker_respond(inquiry_id: int, message: str, listing_ids: list[int] | None = None) -> dict[str, Any]:
        """Reply to a routed inquiry, posting into its thread under your identity.

        Args:
            inquiry_id: From ``broker_get_new_inquiries``. Must be routed to you.
            message: Your reply (a quote / availability / 内見 offer). Personal
                contact info is scrubbed before publishing.
            listing_ids: Listings to attach (default: the inquiry's matched set).
                Each must be yours and advertisable, or it's skipped.
        """
        from .. import forum_core, moderation
        from ..db import connect

        agent = broker_auth()
        conn = connect()
        try:
            inq = inquiries.get_routed_inquiry(inquiry_id, agent.id, conn=conn)
            if inq is None:
                raise AuthError(f"inquiry {inquiry_id} is not routed to this broker")
            if not inq.get("thread_id") or not inq.get("forum"):
                return {"posted": False, "error": "inquiry has no thread to reply into"}

            # Brokers are an authenticated, vetted commercial tier — quotes,
            # availability, and 内見 offers ARE their job, so they must NOT be run
            # through the general anti-solicitation LLM moderator (it rejects
            # exactly that legitimate content). PII scrub only: don't leak contact
            # info. (A broker-specific policy that still blocks illegal /
            # discriminatory content could be layered on later.)
            if not (message or "").strip():
                return {"posted": False, "error": "empty message"}
            scr = moderation.screen(message, use_llm=False, conn=conn)
            if not scr.approved:
                return {"posted": False, "error": scr.reason}
            body = scr.text

            post = forum_core.reply(
                inq["forum"], inq["thread_id"], body, agent.id,
                tags=["broker", "quote"], conn=conn,
            )

            to_attach = listing_ids if listing_ids is not None else inq.get("matched_listing_ids", [])
            attached: list[int] = []
            for lid in to_attach or []:
                owner = conn.execute(
                    "SELECT broker_agent_id FROM listings WHERE id = ?", (lid,)
                ).fetchone()
                if owner is None or owner["broker_agent_id"] != agent.id:
                    continue  # only attach your own listings
                try:
                    forum_core.attach_listing(post.id, lid, conn=conn)
                    attached.append(lid)
                except Exception:
                    pass  # advertisability gate may reject draft rows — skip
            inquiries.mark_responded(inquiry_id, agent.id, conn=conn)
            return {
                "posted": True,
                "forum": inq["forum"],
                "thread_id": inq["thread_id"],
                "post_id": post.id,
                "attached_listing_ids": attached,
            }
        finally:
            conn.close()
