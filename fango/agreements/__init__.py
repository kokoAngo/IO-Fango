"""Finalized agreements: canonical record, hashing, and on-chain anchoring.

A settled agent↔agent deal is recorded as an immutable canonical JSON record;
only its SHA-256 hash is anchored on chain (see :mod:`fango.chain`) so a party
can't later renege on agreed terms. Domain logic lives here; the EVM transport
lives in :mod:`fango.chain`.
"""
