"""Chain client degrades gracefully with no node / no config."""
from __future__ import annotations

from fango.config import ChainSettings
from fango.chain.client import Web3ChainClient


def _empty_settings():
    return ChainSettings(rpc_url=None, private_key=None, contract_addr=None,
                         chain_id=31337, confirmations=1, confirm_timeout_sec=10)


def test_unconfigured_client_is_off():
    c = Web3ChainClient(settings=_empty_settings())
    assert c.is_configured() is False
    assert c.anchor("0x" + "0" * 64, {"listing_id": 1, "party_a": 1, "party_b": 2})["status"] == "skipped"
    assert c.verify("0x" + "0" * 64) is None


def test_abi_is_valid_json():
    import json
    from pathlib import Path
    import fango.chain.client as mod
    abi = json.loads(Path(mod._ABI_PATH).read_text("utf-8"))
    names = {e.get("name") for e in abi}
    assert {"anchor", "isAnchored", "Anchored"} <= names
