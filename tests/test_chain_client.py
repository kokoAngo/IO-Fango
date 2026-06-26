"""Chain client degrades gracefully with no node / no config."""
from __future__ import annotations

from fango.config import ChainSettings
from fango.chain.client import Web3ChainClient


def _empty_settings():
    return ChainSettings(rpc_url=None, private_key=None, contract_addr=None,
                         escrow_addr=None, token_addr=None, token_decimals=18,
                         chain_id=31337, confirmations=1, confirm_timeout_sec=10)


def test_unconfigured_client_is_off():
    c = Web3ChainClient(settings=_empty_settings())
    assert c.is_configured() is False
    assert c.escrow_configured() is False
    assert c.anchor("0x" + "0" * 64, {"listing_id": 1, "party_a": 1, "party_b": 2})["status"] == "skipped"
    assert c.verify("0x" + "0" * 64) is None
    assert c.open_escrow(agreement_hash="0x" + "0" * 64, token="0x0", party_a="0x0",
                         party_b="0x0", deposit_a=1, deposit_b=1, deadline_ts=0)["status"] == "skipped"


def _names(path):
    import json
    from pathlib import Path
    return {e.get("name") for e in json.loads(Path(path).read_text("utf-8"))}


def test_abis_are_valid_json():
    import fango.chain.client as mod
    assert {"anchor", "isAnchored", "Anchored"} <= _names(mod._ABI_PATH)
    assert {"open", "deposit", "settle", "slash", "cancel", "refundExpired",
            "getEscrow", "Opened", "Settled", "Slashed"} <= _names(mod._ESCROW_ABI_PATH)
    assert {"approve", "transfer", "transferFrom", "balanceOf", "allowance"} <= _names(mod._ERC20_ABI_PATH)
