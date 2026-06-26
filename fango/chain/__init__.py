"""EVM transport for anchoring agreement hashes on chain.

Server-witnessed: FANGO holds one private key and submits the anchoring tx.
Best-effort and disabled-by-default — without FANGO_CHAIN_* config (or without
``web3`` installed) the client reports ``is_configured()`` False and anchoring is
skipped, exactly like the Gemini/Notion integrations degrade.
"""
from .client import ChainClient, Web3ChainClient, get_chain_client, set_chain_client

__all__ = ["ChainClient", "Web3ChainClient", "get_chain_client", "set_chain_client"]
