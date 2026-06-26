"""Durable secp256k1 identities for keyed agents.

Each registered (keyed) agent gets a stable secp256k1 keypair whose derived
0x address is its **durable, portable identity** — the foundation for anchoring
the address (not a mutable internal id), agent-signed agreements, and a future
broker-side SBT/reputation layer.

Custody is **hybrid** (per product decision):
  * default **server-custody** — we generate the keypair and store the private
    key Fernet-encrypted under FANGO_IDENTITY_SECRET; we can sign on the agent's
    behalf. Fast, works for pseudonymous (individual-side) agents.
  * **self-custody** — an agent registers its own public key; we store only the
    pubkey + address (privkey_enc NULL) and never hold its private key. True
    non-repudiation; the agent signs locally.

Scope: only keyed agents are provisioned (keyless callers stay ephemeral
pseudonyms). Everything degrades gracefully: without the libs or the secret,
provisioning is a logged no-op and agents work as before.

Curve: secp256k1 (EVM/SBT-compatible) — the same key can later sign EVM txs and
hold an SBT. address = EIP-55 checksum of keccak(pubkey)[-20:].
"""
from __future__ import annotations

import base64
import hashlib
import logging
import sqlite3
from datetime import datetime, timezone

from .config import load_settings
from .db import connect

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# optional libs / secret
# --------------------------------------------------------------------------

def _eth():
    """Lazy import of eth-account + eth-keys (optional [identity] extra)."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        from eth_keys import keys
        return Account, encode_defunct, keys
    except ImportError:
        return None


def _fernet():
    """Fernet built from FANGO_IDENTITY_SECRET (None if unset or lib missing)."""
    secret = load_settings().identity_secret
    if not secret:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    # Derive a stable 32-byte urlsafe key from the secret.
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def libs_available() -> bool:
    return _eth() is not None


def server_custody_available() -> bool:
    """Can we mint + encrypt a custodial identity right now?"""
    return _eth() is not None and _fernet() is not None


# --------------------------------------------------------------------------
# key helpers
# --------------------------------------------------------------------------

def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _pubkey_hex_from_priv(priv_bytes: bytes) -> str:
    _A, _e, keys = _eth()
    return keys.PrivateKey(priv_bytes).public_key.to_hex()


def address_from_pubkey(pubkey_hex: str) -> str:
    """EIP-55 checksum address from a secp256k1 public key hex. Raises on bad input."""
    bundle = _eth()
    if bundle is None:
        raise RuntimeError("identity libs not installed")
    _Account, _e, keys = bundle
    raw = pubkey_hex[2:] if pubkey_hex.startswith("0x") else pubkey_hex
    return keys.PublicKey(bytes.fromhex(raw)).to_checksum_address()


# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------

def get_identity(agent_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT eth_address, pubkey, key_custody, identity_created_at"
            " FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if not row or not row["eth_address"]:
            return None
        return {"address": row["eth_address"], "pubkey": row["pubkey"],
                "custody": row["key_custody"], "created_at": row["identity_created_at"]}
    finally:
        if owns:
            conn.close()


def address_for(agent_id: int, conn: sqlite3.Connection | None = None) -> str | None:
    ident = get_identity(agent_id, conn=conn)
    return ident["address"] if ident else None


def provision_server_identity(agent_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    """Mint + store a custodial identity for ``agent_id`` (idempotent, best-effort).

    No-op (returns None) if identity is unavailable, the agent already has one,
    or the agent doesn't exist. Never raises — agent creation must not break.
    """
    bundle = _eth()
    fernet = _fernet()
    if bundle is None or fernet is None:
        log.debug("identity provisioning skipped (libs/secret unavailable)")
        return None
    Account, _e, _keys = bundle
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        existing = get_identity(agent_id, conn=conn)
        if existing:
            return existing
        acct = Account.create()
        priv = bytes(acct.key)
        pubkey = _pubkey_hex_from_priv(priv)
        address = acct.address                      # already EIP-55 checksummed
        privkey_enc = fernet.encrypt(priv).decode("ascii")
        conn.execute(
            "UPDATE agents SET eth_address=?, pubkey=?, privkey_enc=?, key_custody='server',"
            " identity_created_at=? WHERE id=? AND eth_address IS NULL",
            (address, pubkey, privkey_enc, _now_iso(), agent_id),
        )
        if owns:
            conn.commit()       # caller-managed when a conn is passed in
        return {"address": address, "pubkey": pubkey, "custody": "server"}
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("server identity provisioning failed for agent %s: %s", agent_id, exc)
        return None
    finally:
        if owns:
            conn.close()


class IdentityError(Exception):
    pass


def register_self_identity(agent_id: int, pubkey_hex: str,
                           conn: sqlite3.Connection | None = None) -> dict:
    """Bind an agent-supplied public key (self-custody). We store only the pubkey
    + derived address and drop any server-held private key. Raises IdentityError
    on a bad key or address collision."""
    if _eth() is None:
        raise IdentityError("identity libraries not installed")
    try:
        address = address_from_pubkey(pubkey_hex)
    except Exception as exc:
        raise IdentityError(f"invalid public key: {exc}")
    norm_pubkey = pubkey_hex if pubkey_hex.startswith("0x") else "0x" + pubkey_hex

    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        clash = conn.execute(
            "SELECT id FROM agents WHERE eth_address = ? AND id != ?", (address, agent_id)
        ).fetchone()
        if clash:
            raise IdentityError("address already registered to another agent")
        conn.execute(
            "UPDATE agents SET eth_address=?, pubkey=?, privkey_enc=NULL, key_custody='self',"
            " identity_created_at=? WHERE id=?",
            (address, norm_pubkey, _now_iso(), agent_id),
        )
        if owns:
            conn.commit()
        return {"address": address, "custody": "self"}
    finally:
        if owns:
            conn.close()


# --------------------------------------------------------------------------
# signing (custodial; foundation for agent-signed agreements)
# --------------------------------------------------------------------------

def _load_priv(agent_id: int, conn: sqlite3.Connection):
    """Decrypt a server-custody agent's private key. Returns bytes or None
    (self-custody / no key / no secret / libs missing)."""
    fernet = _fernet()
    if _eth() is None or fernet is None:
        return None
    row = conn.execute(
        "SELECT privkey_enc, key_custody FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if not row or row["key_custody"] != "server" or not row["privkey_enc"]:
        return None
    return fernet.decrypt(row["privkey_enc"].encode("ascii"))


def sign_and_send_tx(agent_id: int, contract_fn, *, w3, chain_id: int,
                     value: int = 0, conn: sqlite3.Connection | None = None) -> dict:
    """Sign + broadcast an EVM tx FROM a server-custody agent's address.

    ``contract_fn`` is an already-bound web3 ContractFunction (e.g.
    ``token.functions.approve(spender, amount)``). Returns an ``anchor()``-shaped
    ``{status, tx_hash, block_number, error}`` dict; **never raises**. Self-custody
    or keyless agents return ``{"status":"skipped"}`` — we never hold their key,
    they must fund externally. ``w3`` is passed in so identity stays chain-agnostic.
    """
    bundle = _eth()
    if bundle is None:
        return {"status": "skipped", "reason": "identity libs not installed"}
    Account, _e, _keys = bundle
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        priv = _load_priv(agent_id, conn)
        if priv is None:
            return {"status": "skipped", "reason": "self-custody or no key (fund externally)"}
        acct = Account.from_key(priv)
        nonce = w3.eth.get_transaction_count(acct.address, "pending")
        base = {"from": acct.address, "nonce": nonce, "chainId": chain_id, "value": value}
        try:
            gas = contract_fn.estimate_gas({"from": acct.address, "value": value})
        except Exception:
            gas = 200000
        base["gas"] = int(gas * 1.2)
        # EIP-1559 where supported, else legacy gasPrice (ganache accepts both).
        try:
            base["maxFeePerGas"] = w3.eth.gas_price * 2
            base["maxPriorityFeePerGas"] = w3.to_wei(1.5, "gwei")
            tx = contract_fn.build_transaction(base)
        except Exception:
            base.pop("maxFeePerGas", None); base.pop("maxPriorityFeePerGas", None)
            base["gasPrice"] = w3.eth.gas_price
            tx = contract_fn.build_transaction(base)
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        ok = receipt["status"] == 1
        return {"status": "confirmed" if ok else "failed",
                "tx_hash": tx_hash.hex(), "block_number": receipt["blockNumber"],
                "from": acct.address, "error": None if ok else "transaction reverted"}
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning("custodial tx failed for agent %s: %s", agent_id, exc)
        return {"status": "failed", "tx_hash": None, "block_number": None, "error": str(exc)}
    finally:
        if owns:
            conn.close()


def sign_message(agent_id: int, message: str, conn: sqlite3.Connection | None = None) -> str | None:
    """Sign a UTF-8 message with the agent's custodial key (EIP-191). Returns the
    0x signature, or None if the agent is self-custody / has no key / libs absent.
    Self-custody agents must sign locally — we never hold their key."""
    bundle = _eth()
    fernet = _fernet()
    if bundle is None or fernet is None:
        return None
    Account, encode_defunct, _keys = bundle
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT privkey_enc, key_custody FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if not row or row["key_custody"] != "server" or not row["privkey_enc"]:
            return None
        priv = fernet.decrypt(row["privkey_enc"].encode("ascii"))
        signed = Account.sign_message(encode_defunct(text=message), private_key=priv)
        return signed.signature.hex()
    except Exception as exc:  # pragma: no cover
        log.warning("sign_message failed for agent %s: %s", agent_id, exc)
        return None
    finally:
        if owns:
            conn.close()
