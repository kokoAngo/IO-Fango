"""Agreement-finalization MCP tools.

``fango_accept_proposal`` is the customer side of the deal-formation bridge: it
turns a broker's term proposal into a finalized, on-chain-anchored agreement.
Available to any agent (no broker key needed) — the caller is verified to be the
proposal's customer (keyed agent → its own id; keyless consult caller → the
session's posting identity).
"""
from __future__ import annotations

from typing import Any

from ..auth import resolve_agent
from ..config import load_settings
from ..db import connect


def register(mcp) -> None:

    @mcp.tool()
    def fango_accept_proposal(proposal_id: int, session_id: str | None = None) -> dict[str, Any]:
        """Accept a broker's term proposal → create + anchor the agreement.

        Args:
            proposal_id: From the broker's 条件提示 (see ``broker_propose_terms``).
            session_id: REQUIRED for keyless callers — the ``session_id`` from your
                ``fango_consult`` conversation (it identifies you as the inquiring
                customer). Keyed agents may omit it.

        Returns the agreement id, content hash, anchor status, and an explorer
        URL. Anchoring is best-effort: if the chain is off, the agreement is still
        created (status stays unanchored).
        """
        from ..brokers import proposals
        from ..consult import session as _ss

        conn = connect()
        try:
            # Resolve the customer agent id. Keyed → current agent; keyless →
            # the consult session's posting identity (same as how consult itself
            # attributes keyless callers).
            agent = resolve_agent(conn=conn)
            customer_agent_id = agent.id if agent is not None else None
            if customer_agent_id is None:
                if not session_id:
                    return {"ok": False, "error": "session_id required for keyless callers"}
                sess = _ss.get_session(session_id, conn=conn)
                if sess is None or sess.post_agent_id is None:
                    return {"ok": False, "error": "unknown or empty session"}
                customer_agent_id = sess.post_agent_id

            try:
                res = proposals.accept_proposal(proposal_id, customer_agent_id, conn=conn)
            except proposals.ProposalError as exc:
                return {"ok": False, "error": str(exc)}

            agreement = res["agreement"]
            anchor = res["anchor"] or {}
            base = load_settings().public_base_url or ""
            explorer_url = f"{base}/explorer/{agreement.id}"

            # Post a confirmation into the proposal's thread so both sides + the
            # public forum see the deal closed.
            try:
                from .. import forum_core
                from ..auth import get_or_create_system_agent
                from .. import moderation
                p = proposals.get_proposal(proposal_id, conn=conn)
                if p and p.get("thread_id") and p.get("agreement_type"):
                    forum = "baibai" if p["agreement_type"] == "sale" else "chintai"
                    note = (f"契約成立 ✅ ハッシュ {agreement.content_hash[:12]}… / "
                            f"状態 {agreement.status} / {explorer_url}")
                    note = moderation.screen(note, use_llm=False).text
                    system = get_or_create_system_agent(conn=conn)
                    forum_core.reply(forum, p["thread_id"], note, system.id,
                                     tags=["agreement", "anchored"], conn=conn)
            except Exception:
                pass

            return {
                "ok": True,
                "agreement_id": agreement.id,
                "content_hash": agreement.content_hash,
                "status": agreement.status,
                "explorer_url": explorer_url,
                "anchor": {"status": anchor.get("status"), "tx_hash": anchor.get("tx_hash")},
            }
        finally:
            conn.close()
