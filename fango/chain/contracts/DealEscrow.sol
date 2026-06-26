// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Minimal ERC-20 surface the escrow needs.
interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// @title DealEscrow
/// @notice Earnest-money (保証金) escrow for FANGO agent↔agent agreements. FANGO
///         (the deployer) is the arbiter: it opens escrows, and on outcome calls
///         settle (refund both), slash (loser's stake → winner), or cancel
///         (refund both). Each escrow references an off-chain agreement's SHA-256
///         content hash, linking economic stake to tamper-proof evidence.
/// @dev    Deploy ONCE; address → FANGO_CHAIN_ESCROW_ADDR. Stakes are pulled via
///         transferFrom (party must approve first). Checks-effects-interactions +
///         a reentrancy lock guard every token movement.
contract DealEscrow {
    address public immutable owner;

    enum Status { None, Open, Funded, Settled, Slashed, Cancelled, Expired }

    struct Escrow {
        bytes32 agreementHash;
        address token;
        address partyA;
        address partyB;
        uint256 depositA;
        uint256 depositB;
        bool    fundedA;
        bool    fundedB;
        uint64  deadline;
        Status  status;
    }

    uint256 public nextId = 1;
    mapping(uint256 => Escrow) public escrows;

    uint8 private _locked = 1;
    modifier nonReentrant() {
        require(_locked == 1, "REENTRANT");
        _locked = 2; _; _locked = 1;
    }
    modifier onlyOwner() { if (msg.sender != owner) revert NotOwner(); _; }

    event Opened(uint256 indexed id, bytes32 indexed agreementHash, address token,
                 address partyA, address partyB, uint256 depositA, uint256 depositB, uint64 deadline);
    event Deposited(uint256 indexed id, address indexed party, uint256 amount, bool fullyFunded);
    event Settled(uint256 indexed id, uint256 refundA, uint256 refundB);
    event Slashed(uint256 indexed id, address indexed loser, address indexed winner, uint256 amount);
    event Cancelled(uint256 indexed id, uint256 refundA, uint256 refundB);
    event Expired(uint256 indexed id, uint256 refundA, uint256 refundB);

    error NotOwner();
    error BadState(uint256 id, Status have);
    error NotParty(uint256 id);
    error AlreadyFunded(uint256 id);
    error NotYetExpired(uint256 id);
    error LoserNotParty(uint256 id);

    constructor() { owner = msg.sender; }

    /// @notice Open an escrow for an agreement. Owner-only.
    function open(
        bytes32 agreementHash, address token,
        address partyA, address partyB,
        uint256 depositA, uint256 depositB, uint64 deadline
    ) external onlyOwner returns (uint256 id) {
        require(partyA != address(0) && partyB != address(0) && token != address(0), "ZERO_ADDR");
        id = nextId++;
        escrows[id] = Escrow(agreementHash, token, partyA, partyB,
                             depositA, depositB, false, false, deadline, Status.Open);
        emit Opened(id, agreementHash, token, partyA, partyB, depositA, depositB, deadline);
    }

    /// @notice A party stakes its deposit (pulled via transferFrom; approve first).
    function deposit(uint256 id) external nonReentrant {
        Escrow storage e = escrows[id];
        if (e.status != Status.Open) revert BadState(id, e.status);
        uint256 amount;
        if (msg.sender == e.partyA)      { if (e.fundedA) revert AlreadyFunded(id); amount = e.depositA; e.fundedA = true; }
        else if (msg.sender == e.partyB) { if (e.fundedB) revert AlreadyFunded(id); amount = e.depositB; e.fundedB = true; }
        else revert NotParty(id);

        bool full = e.fundedA && e.fundedB;
        if (full) e.status = Status.Funded;            // effects before interaction
        require(IERC20(e.token).transferFrom(msg.sender, address(this), amount), "PULL_FAIL");
        emit Deposited(id, msg.sender, amount, full);
    }

    /// @notice Clean completion: refund both stakes. Owner-only.
    function settle(uint256 id) external onlyOwner nonReentrant {
        Escrow storage e = escrows[id];
        if (e.status != Status.Funded) revert BadState(id, e.status);
        e.status = Status.Settled;
        IERC20 t = IERC20(e.token);
        require(t.transfer(e.partyA, e.depositA), "REFUND_A");
        require(t.transfer(e.partyB, e.depositB), "REFUND_B");
        emit Settled(id, e.depositA, e.depositB);
    }

    /// @notice Renege: loser's stake goes to the winner; winner's own stake is
    ///         returned (so winner receives winnerStake + loserStake). Owner-only.
    function slash(uint256 id, address loser) external onlyOwner nonReentrant {
        Escrow storage e = escrows[id];
        if (e.status != Status.Funded) revert BadState(id, e.status);
        address winner;
        uint256 loserStake; uint256 winnerStake;
        if (loser == e.partyA)      { winner = e.partyB; loserStake = e.depositA; winnerStake = e.depositB; }
        else if (loser == e.partyB) { winner = e.partyA; loserStake = e.depositB; winnerStake = e.depositA; }
        else revert LoserNotParty(id);
        e.status = Status.Slashed;
        require(IERC20(e.token).transfer(winner, winnerStake + loserStake), "PAYOUT");
        emit Slashed(id, loser, winner, loserStake);
    }

    /// @notice Arbiter cancels (e.g. mutual abort): refund whatever is staked.
    function cancel(uint256 id) external onlyOwner nonReentrant {
        Escrow storage e = escrows[id];
        if (e.status != Status.Open && e.status != Status.Funded) revert BadState(id, e.status);
        uint256 ra = e.fundedA ? e.depositA : 0;
        uint256 rb = e.fundedB ? e.depositB : 0;
        e.status = Status.Cancelled;
        _refundStaked(e);
        emit Cancelled(id, ra, rb);
    }

    /// @notice After the deadline with funding incomplete, anyone may trigger a
    ///         refund of whatever was deposited. Permissionless safety valve.
    function refundExpired(uint256 id) external nonReentrant {
        Escrow storage e = escrows[id];
        if (e.status != Status.Open) revert BadState(id, e.status);
        if (block.timestamp < e.deadline) revert NotYetExpired(id);
        uint256 ra = e.fundedA ? e.depositA : 0;
        uint256 rb = e.fundedB ? e.depositB : 0;
        e.status = Status.Expired;
        _refundStaked(e);
        emit Expired(id, ra, rb);
    }

    function _refundStaked(Escrow storage e) private {
        IERC20 t = IERC20(e.token);
        if (e.fundedA) { e.fundedA = false; require(t.transfer(e.partyA, e.depositA), "R_A"); }
        if (e.fundedB) { e.fundedB = false; require(t.transfer(e.partyB, e.depositB), "R_B"); }
    }

    function getEscrow(uint256 id) external view returns (Escrow memory) { return escrows[id]; }
}
