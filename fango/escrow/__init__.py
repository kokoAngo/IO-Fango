"""Earnest-money escrow (保証金): ERC-20 deposits staked against agreements.

Adds economic enforcement on top of anchoring's evidence — each party stakes a
deposit; clean completion refunds both, renege slashes the loser's stake to the
winner. FANGO is the arbiter. See fango/chain/contracts/DealEscrow.sol.
"""
