// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title AgreementRegistry
/// @notice Hash-anchor registry for FANGO finalized agreements. Stores only the
///         SHA-256 hash of an off-chain canonical record plus minimal non-PII
///         metadata (internal agent ids + listing id). The original terms never
///         touch the chain.
/// @dev Deploy ONCE; put the deployed address in FANGO_CHAIN_CONTRACT_ADDR. Only
///      the deployer (the server's witness key) may anchor.
contract AgreementRegistry {
    address public immutable owner;

    /// @notice hash => block timestamp it was first anchored (0 == never).
    mapping(bytes32 => uint256) public anchoredAt;

    event Anchored(
        bytes32 indexed hash,
        uint256 listingId,
        uint256 partyA,
        uint256 partyB,
        uint256 ts
    );

    error AlreadyAnchored(bytes32 hash);
    error NotOwner();

    constructor() {
        owner = msg.sender;
    }

    /// @notice Anchor one agreement hash with non-PII metadata.
    /// @dev Reverts AlreadyAnchored on a repeat (on-chain idempotency backstop;
    ///      the server also dedups off-chain via a UNIQUE column).
    function anchor(
        bytes32 hash,
        uint256 listingId,
        uint256 partyA,
        uint256 partyB
    ) external {
        if (msg.sender != owner) revert NotOwner();
        if (anchoredAt[hash] != 0) revert AlreadyAnchored(hash);
        anchoredAt[hash] = block.timestamp;
        emit Anchored(hash, listingId, partyA, partyB, block.timestamp);
    }

    /// @notice Verifier view: (isAnchored, anchoredTimestamp).
    function isAnchored(bytes32 hash) external view returns (bool, uint256) {
        uint256 ts = anchoredAt[hash];
        return (ts != 0, ts);
    }
}
