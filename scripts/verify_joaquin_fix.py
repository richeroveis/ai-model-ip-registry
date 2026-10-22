"""Live proof of the Joaquin refund remediation on studionet.

Every claim this script makes is read back from a transaction result and from
contract state, not from client return values alone.

Covered, in order:
  1. deploy and read the initial custody summary
  2. valid payable registration (exact bond)
  3. rejected payable: duplicate model id, full value credited
  4. rejected payable: insufficient registration bond
  5. rejected payable: invalid metadata url
  6. rejected payable: paused lifecycle state
  7. accepted registration with an excess, excess credited not locked
  8. rejected payable: insufficient dispute bond
  9. accepted dispute with an excess
 10. rejected payable: second dispute on the same model (state race)
 11. withdraw_refund, exact wallet credit, balance returns to zero
 12. claim_refundable_balance for the creator
 13. live consensus audit, FINALIZED with MAJORITY_AGREE
 14. final custody identity and a source hash check against the deploy calldata

The run is deliberately lean: studionet enforces a shared request budget, so
each step makes the minimum number of reads and every write is submitted once.

Usage:
    python scripts/verify_joaquin_fix.py
"""

import hashlib
import json
import sys
import time
from pathlib import Path

import rlp
from eth_account import Account
from genlayer_py import create_client
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_studionet import (  # noqa: E402
    CONTRACT_PATH,
    EXPLORER_TX,
    ROOT,
    Runner,
    StepFailed,
    contract_address_of,
    load_env,
    local_identity_digest,
    make_evm_receipts_resilient,
    send_native,
    slot_digest,
    wait_for_rpc,
)

EVIDENCE_PATH = ROOT / "deployment" / "joaquin_fix.json"
WEI = 10 ** 17
E16 = WEI // 10
REGISTRATION = WEI
DISPUTE = WEI
AUDIT_WINDOW = 3600
EXIT_DELAY = 3600
TIER_SPLIT = {
    "NO_INFRINGEMENT": [0, 10000],
    "PARTIAL_DERIVATIVE": [6500, 3500],
    "EXACT_PLAGIARISM": [8500, 1500],
}
CREATOR_META = "https://example.com"
CREATOR_BENCH = "https://www.iana.org/help/example-domains"


def expect(condition, message: str) -> None:
    if not condition:
        raise StepFailed(message)


def read_view(client, address: str, function_name: str, args=None):
    """Read a view method with bare calldata and a short retry budget.

    studionet rejects RLP wrapped gen_call payloads above roughly 300 bytes, so
    the bare calldata encoding is sent. Retries are capped at three and a rate
    limit answer is re-raised immediately, so a read cannot eat the request
    budget.
    """
    from genlayer_py.abi import calldata
    from genlayer_py.contracts.utils import make_calldata_object

    payload = calldata.encode(
        make_calldata_object(method=function_name, args=args or [], kwargs=None))
    request = {
        "type": "read",
        "to": address,
        "from": client.local_account.address,
        "data": "0x" + payload.hex(),
        "transaction_hash_variant": "latest-nonfinal",
    }
    last_error = None
    for attempt in range(3):
        try:
            encoded = client.provider.make_request(method="gen_call", params=[request])["result"]
            raw = encoded[2:] if encoded.startswith("0x") else encoded
            return calldata.decode(bytes.fromhex(raw))
        except Exception as exc:  # noqa: BLE001 - transport level retry
            last_error = exc
            if "rate limit" in str(exc).lower():
                raise
            print("  read retry {0} for {1}: {2}".format(
                attempt, function_name, type(exc).__name__))
            time.sleep(4)
    raise StepFailed("{0}: read failed: {1}".format(function_name, last_error))


def make_transport_resilient(attempts: int = 3, delay: float = 3.0) -> None:
    """Retry the signed RPC request itself on TLS or gateway drops.

    Every submission carries an already signed raw transaction, so re-sending
    the same request is idempotent: the node dedupes by transaction hash. Reads
    are idempotent by nature. A rate limit answer is not transient, so it is
    raised immediately instead of multiplying the request count.
    """
    from genlayer_py.provider.provider import GenLayerProvider

    # GenLayerProvider overrides make_request, so patching the base class alone
    # would not intercept the call the client actually uses.
    original = GenLayerProvider.make_request

    def resilient(self, method, params):
        last_error = None
        for attempt in range(attempts):
            try:
                return original(self, method, params)
            except Exception as exc:  # noqa: BLE001 - transport level retry
                last_error = exc
                if "rate limit" in str(exc).lower():
                    raise
                print("  transport retry {0}: {1}".format(attempt, type(exc).__name__))
                time.sleep(delay * (attempt + 1))
        raise last_error

    GenLayerProvider.make_request = resilient


class LeanRunner(Runner):
    """Poll less often so the shared request budget lasts the whole run."""

    def _poll(self, label: str, tx_hash, status) -> dict:
        last_error = None
        for attempt in range(4):
            try:
                return self.client.wait_for_transaction_receipt(
                    transaction_hash=tx_hash, status=status, interval=9000, retries=80)
            except Exception as exc:  # noqa: BLE001 - transport level retry
                last_error = exc
                if "rate limit" in str(exc).lower():
                    raise
                print("  poll retry {0} for {1}: {2}".format(
                    attempt, label, type(exc).__name__))
                time.sleep(10)
        raise StepFailed("{0}: polling failed: {1}".format(label, last_error))


def address_key(value) -> str:
    return str(value).lower()


def refundable(client, address: str, who: str) -> int:
    view = read_view(client, address, "get_refundable_balance", [who])
    expect(address_key(view["address"]) == address_key(who), "refund view returned another address")
    return int(view["refundable_balance"])


def rejection(client, address: str, who: str) -> dict:
    return read_view(client, address, "get_last_payable_rejection", [who])


def custody(client, address: str) -> dict:
    return read_view(client, address, "get_custody_accounting_summary")


def assert_custody(client, address: str, expected_locked: int, expected_refundable: int) -> dict:
    summary = custody(client, address)
    expect(summary["locked_bonds"] == expected_locked,
           "locked bonds {0} != {1}".format(summary["locked_bonds"], expected_locked))
    expect(summary["total_refundable_custody"] == expected_refundable,
           "refundable custody {0} != {1}".format(
               summary["total_refundable_custody"], expected_refundable))
    expect(summary["unaccounted_surplus"] == 0,
           "unaccounted surplus is {0}".format(summary["unaccounted_surplus"]))
    expect(summary["royalty_escrow"] == 0, "royalty escrow is not zero")
    expect(summary["accounting_identity_ok"] is True, "custody identity does not hold")
    expect(summary["vault_custody_balance"] == summary["contract_balance"],
           "vault mirror does not match the contract balance")
    expect(summary["contract_balance"] == expected_locked + expected_refundable,
           "contract balance {0} != {1}".format(
               summary["contract_balance"], expected_locked + expected_refundable))
    return summary


def wait_for_credit(client, address: str, before: int, minimum_delta: int, tries: int = 24) -> int:
    """Payouts execute asynchronously after finalization, so poll for the credit."""
    seen = before
    for _ in range(tries):
        seen = client.w3.eth.get_balance(address)
        if seen - before >= minimum_delta:
            return seen
        time.sleep(8)
    return seen


def gas_cost(client, tx_hash) -> int:
    try:
        key = tx_hash.hex() if hasattr(tx_hash, "hex") else tx_hash
        receipt = client.w3.eth.get_transaction_receipt(key)
        return int(receipt["gasUsed"]) * int(receipt["effectiveGasPrice"])
    except Exception:  # noqa: BLE001 - gas data is corroboration only
        return 0


def source_in_deploy_tx(client, tx_hash: str) -> dict:
    tx = client.get_transaction(tx_hash)
    raw = bytes.fromhex(tx["tx_data"][2:] if tx["tx_data"].startswith("0x") else tx["tx_data"])
    code = rlp.decode(raw)[0]
    return {
        "sha256": hashlib.sha256(code).hexdigest(),
        "bytes": len(code),
        "head": code[:48].decode("utf-8", "replace"),
    }


def main() -> int:
    env = load_env()
    deployer = Account.from_key(env["GENLAYER_PRIVATE_KEY"])
    client = create_client(chain=studionet, account=deployer)
    make_evm_receipts_resilient()
    make_transport_resilient()

    source_bytes = CONTRACT_PATH.read_bytes()
    expect(source_bytes.startswith(b'# { "Depends": "py-genlayer:'),
           "pinned runner header is missing")
    expect(b"\r\n" not in source_bytes, "contract file is not LF")
    source_hash = hashlib.sha256(source_bytes).hexdigest()

    print("network       : studionet ({0})".format(studionet.rpc_urls["default"]["http"][0]))
    print("deployer      : {0}".format(deployer.address))
    print("source sha256 : {0}".format(source_hash))

    evidence = {
        "network": "studionet",
        "chain_id": studionet.id,
        "deployer": deployer.address,
        "contract_file": str(CONTRACT_PATH.relative_to(ROOT)),
        "contract_sha256": source_hash,
        "contract_bytes": len(source_bytes),
        "constructor_args": {
            "min_registration_stake": REGISTRATION,
            "min_dispute_stake": DISPUTE,
            "audit_window_secs": AUDIT_WINDOW,
            "exit_delay_secs": EXIT_DELAY,
        },
        "transactions": [],
    }
    runner = LeanRunner(client, evidence)

    print("[1] deploy")
    wait_for_rpc(client)
    deploy_hash = client.deploy_contract(
        code=source_bytes,
        args=[REGISTRATION, DISPUTE, AUDIT_WINDOW, EXIT_DELAY],
    )
    deploy_receipt = runner.wait("deploy", deploy_hash, status=TransactionStatus.FINALIZED)
    address = contract_address_of(deploy_receipt)
    evidence["contract_address"] = address
    evidence["explorer_address"] = "https://genlayer-explorer.vercel.app/address/{0}".format(address)
    deploy_hex = deploy_hash.hex() if hasattr(deploy_hash, "hex") else deploy_hash
    deploy_sha = source_in_deploy_tx(client, deploy_hex)
    evidence["deploy_source"] = deploy_sha
    print("  contract {0}".format(address))
    print("  deploy tx source sha256 {0}".format(deploy_sha["sha256"]))
    expect(deploy_sha["sha256"] == source_hash,
           "deploy transaction source hash does not match the local file")

    initial = custody(client, address)
    evidence["custody_initial"] = initial
    expect(initial["locked_bonds"] == 0 and initial["total_refundable_custody"] == 0,
           "fresh contract is not empty")
    expect(initial["accounting_identity_ok"] is True, "fresh custody identity does not hold")

    print("[2] valid payable registration")
    weights_a = slot_digest("refund-alpha-7b/weights")
    dataset_a = slot_digest("refund-alpha-7b/dataset")
    config_a = slot_digest("refund-alpha-7b/config")
    card_a = slot_digest("refund-alpha-7b/card")
    identity_a = local_identity_digest(weights_a, dataset_a, config_a, card_a)
    registration = client.write_contract(
        address=address,
        function_name="register_model",
        args=["refund-alpha-7b", weights_a, dataset_a, config_a, card_a,
              CREATOR_META, CREATOR_BENCH, "apache-2.0"],
        value=REGISTRATION,
    )
    runner.wait("register_model", registration)
    model_a = read_view(client, address, "get_model", ["refund-alpha-7b"])
    expect(model_a["deposit"] == REGISTRATION, "registration locked the wrong bond")
    expect(read_view(client, address, "get_four_way_identity", ["refund-alpha-7b"])[
        "derived_identity_digest"] == identity_a, "registered identity does not match")
    evidence["model_a"] = model_a
    assert_custody(client, address, REGISTRATION, 0)

    print("[3] rejected: duplicate model id")
    duplicate = client.write_contract(
        address=address,
        function_name="register_model",
        args=["refund-alpha-7b", weights_a, dataset_a, config_a, card_a,
              CREATOR_META, CREATOR_BENCH, "apache-2.0"],
        value=REGISTRATION,
    )
    runner.wait("reject_duplicate_model_id", duplicate)
    credited = refundable(client, address, deployer.address)
    evidence["duplicate_credit"] = credited
    expect(credited == REGISTRATION, "duplicate registration was not fully credited")
    expect(rejection(client, address, deployer.address)["reason"] == "DUPLICATE_MODEL_ID",
           "duplicate rejection reason is wrong")
    assert_custody(client, address, REGISTRATION, REGISTRATION)

    print("[4] rejected: insufficient registration bond")
    weights_b = slot_digest("refund-beta-3b/weights")
    dataset_b = slot_digest("refund-beta-3b/dataset")
    config_b = slot_digest("refund-beta-3b/config")
    card_b = slot_digest("refund-beta-3b/card")
    short = client.write_contract(
        address=address,
        function_name="register_model",
        args=["refund-beta-3b", weights_b, dataset_b, config_b, card_b,
              CREATOR_META, CREATOR_BENCH, "apache-2.0"],
        value=4 * E16,
    )
    runner.wait("reject_insufficient_registration_bond", short)
    expect(refundable(client, address, deployer.address) == REGISTRATION + 4 * E16,
           "short registration bond was not fully credited")
    expect(rejection(client, address, deployer.address)["reason"] == "INSUFFICIENT_REGISTRATION_BOND",
           "bond rejection reason is wrong")
    assert_custody(client, address, REGISTRATION, REGISTRATION + 4 * E16)

    print("[5] rejected: invalid metadata url")
    bad_url = client.write_contract(
        address=address,
        function_name="register_model",
        args=["refund-gamma-1b", slot_digest("refund-gamma-1b/weights"), dataset_b,
              config_b, card_b, "https://localhost/card.json", CREATOR_BENCH, "apache-2.0"],
        value=REGISTRATION,
    )
    runner.wait("reject_invalid_metadata_url", bad_url)
    expect(rejection(client, address, deployer.address)["reason"] == "INVALID_METADATA_URL",
           "invalid url rejection reason is wrong")
    expect(refundable(client, address, deployer.address) == 2 * REGISTRATION + 4 * E16,
           "invalid input was not fully credited")
    assert_custody(client, address, REGISTRATION, 2 * REGISTRATION + 4 * E16)

    print("[6] rejected: paused lifecycle state")
    pause_hash = client.write_contract(address=address, function_name="set_paused", args=[True])
    runner.wait("set_paused_true", pause_hash)
    paused = client.write_contract(
        address=address,
        function_name="register_model",
        args=["refund-delta-1b", slot_digest("refund-delta-1b/weights"),
              slot_digest("refund-delta-1b/dataset"), slot_digest("refund-delta-1b/config"),
              slot_digest("refund-delta-1b/card"), CREATOR_META, CREATOR_BENCH, "apache-2.0"],
        value=REGISTRATION,
    )
    runner.wait("reject_paused_registration", paused)
    expect(rejection(client, address, deployer.address)["reason"] == "PAUSED",
           "paused rejection reason is wrong")
    expect(refundable(client, address, deployer.address) == 3 * REGISTRATION + 4 * E16,
           "paused registration was not fully credited")
    unpause_hash = client.write_contract(address=address, function_name="set_paused", args=[False])
    runner.wait("set_paused_false", unpause_hash)

    print("[7] accepted registration with excess")
    excess_registration = client.write_contract(
        address=address,
        function_name="register_model",
        args=["refund-beta-3b", weights_b, dataset_b, config_b, card_b,
              CREATOR_META, CREATOR_BENCH, "apache-2.0"],
        value=REGISTRATION + 5 * E16,
    )
    runner.wait("register_with_excess", excess_registration)
    model_b = read_view(client, address, "get_model", ["refund-beta-3b"])
    expect(model_b["deposit"] == REGISTRATION, "excess was locked into the bond")
    expect(refundable(client, address, deployer.address) == 39 * E16,
           "registration excess was not credited exactly")
    assert_custody(client, address, 2 * REGISTRATION, 39 * E16)

    print("[8] challenger funding and rejected low dispute bond")
    # A deterministic second signer keeps the run reproducible without storing a
    # second secret: the key is derived in memory from this project's deploy key.
    seed = hashlib.sha256((env["GENLAYER_PRIVATE_KEY"] + "|joaquin-fix-challenger")
                          .encode("utf-8")).hexdigest()
    challenger = Account.from_key(seed)
    funding_hash = send_native(client, deployer, challenger.address, 5 * WEI)
    runner.record_native("fund_challenger", funding_hash)
    challenger_client = create_client(chain=studionet, account=challenger)
    low_bond = challenger_client.write_contract(
        address=address,
        function_name="open_dispute",
        args=["refund-beta-3b", weights_b, dataset_b, config_b, card_b,
              CREATOR_META, CREATOR_BENCH, "low bond attempt"],
        value=5 * E16,
    )
    runner.wait("reject_insufficient_dispute_bond", low_bond)
    expect(refundable(client, address, challenger.address) == 5 * E16,
           "low dispute bond was not fully credited")
    expect(rejection(client, address, challenger.address)["reason"] == "INSUFFICIENT_DISPUTE_BOND",
           "dispute bond rejection reason is wrong")
    assert_custody(client, address, 2 * REGISTRATION, 39 * E16 + 5 * E16)

    print("[9] accepted dispute with excess")
    dispute_hash = challenger_client.write_contract(
        address=address,
        function_name="open_dispute",
        args=["refund-beta-3b", weights_b, dataset_b, config_b, card_b,
              CREATOR_META, CREATOR_BENCH, "verbatim redistribution of the weights"],
        value=REGISTRATION + 7 * E16,
    )
    runner.wait("open_dispute_with_excess", dispute_hash)
    dispute_id = read_view(client, address, "get_dispute_ids")[-1]
    dispute_open = read_view(client, address, "get_dispute", [dispute_id])
    expect(dispute_open["challenger_stake"] == REGISTRATION, "dispute locked the wrong stake")
    expect(dispute_open["match_count"] == 4, "claim does not match the committed identity")
    expect(refundable(client, address, challenger.address) == 12 * E16,
           "dispute excess was not credited exactly")
    evidence["dispute_id"] = dispute_id
    evidence["dispute_open"] = dispute_open
    assert_custody(client, address, 3 * REGISTRATION, 39 * E16 + 12 * E16)

    print("[10] rejected: second dispute on the same model")
    racing = challenger_client.write_contract(
        address=address,
        function_name="open_dispute",
        args=["refund-beta-3b", weights_b, dataset_b, config_b, card_b,
              CREATOR_META, CREATOR_BENCH, "state race"],
        value=REGISTRATION,
    )
    runner.wait("reject_open_dispute_exists", racing)
    expect(rejection(client, address, challenger.address)["reason"] == "OPEN_DISPUTE_EXISTS",
           "state race rejection reason is wrong")
    expect(refundable(client, address, challenger.address) == 22 * E16,
           "state race rejection was not fully credited")
    locked_before_audit = 3 * REGISTRATION
    challenger_credit = 22 * E16
    assert_custody(client, address, locked_before_audit, 39 * E16 + challenger_credit)

    print("[11] withdraw_refund for the challenger")
    before = client.w3.eth.get_balance(challenger.address)
    wait_for_rpc(challenger_client)
    withdraw_hash = challenger_client.write_contract(
        address=address, function_name="withdraw_refund", args=[0])
    runner.wait("withdraw_refund_challenger", withdraw_hash, status=TransactionStatus.FINALIZED)
    expect(refundable(client, address, challenger.address) == 0,
           "challenger refund balance is not zero after withdrawal")
    after = wait_for_credit(client, challenger.address, before, challenger_credit - E16)
    cost = gas_cost(client, withdraw_hash)
    delta = after - before + cost
    evidence["challenger_withdrawal"] = {
        "requested": challenger_credit,
        "balance_before": before,
        "balance_after": after,
        "gas": cost,
        "net_delta": delta,
    }
    expect(delta == challenger_credit,
           "challenger wallet delta {0} != {1}".format(delta, challenger_credit))
    assert_custody(client, address, locked_before_audit, 39 * E16)

    print("[12] claim_refundable_balance for the creator")
    before = client.w3.eth.get_balance(deployer.address)
    creator_credit = refundable(client, address, deployer.address)
    expect(creator_credit == 39 * E16, "creator refund credit is wrong")
    wait_for_rpc(client)
    claim_hash = client.write_contract(
        address=address, function_name="claim_refundable_balance", args=[])
    runner.wait("claim_refundable_balance_creator", claim_hash, status=TransactionStatus.FINALIZED)
    expect(refundable(client, address, deployer.address) == 0,
           "creator refund balance is not zero after withdrawal")
    after = wait_for_credit(client, deployer.address, before, creator_credit - E16)
    cost = gas_cost(client, claim_hash)
    delta = after - before + cost
    evidence["creator_withdrawal"] = {
        "requested": creator_credit,
        "balance_before": before,
        "balance_after": after,
        "gas": cost,
        "net_delta": delta,
    }
    expect(delta == creator_credit,
           "creator wallet delta {0} != {1}".format(delta, creator_credit))
    assert_custody(client, address, locked_before_audit, 0)

    print("[13] live consensus audit")
    wait_for_rpc(challenger_client)
    audit_hash = challenger_client.write_contract(
        address=address, function_name="audit_model_similarity_live", args=[dispute_id])
    runner.wait("audit_model_similarity_live", audit_hash, status=TransactionStatus.FINALIZED)
    audit_hex = audit_hash.hex() if hasattr(audit_hash, "hex") else audit_hash
    audit_tx = client.get_transaction(audit_hex)
    evidence["audit"] = {
        "hash": audit_hex,
        "status": audit_tx.get("status_name"),
        "result": audit_tx.get("result_name"),
        "execution_result": ((audit_tx.get("consensus_data") or {}).get("leader_receipt")
                             or [{}])[0].get("execution_result"),
    }
    expect(audit_tx.get("status_name") == "FINALIZED", "audit transaction is not finalized")
    expect(audit_tx.get("result_name") == "MAJORITY_AGREE", "audit consensus is not MAJORITY_AGREE")

    dispute_final = read_view(client, address, "get_dispute", [dispute_id])
    tier = dispute_final["tier"]
    expect(dispute_final["status"] == "SETTLED_CONSENSUS", "audit did not settle the dispute")
    expect(tier in TIER_SPLIT, "unexpected tier after audit")
    pot = int(dispute_open["challenger_stake"]) + int(model_b["deposit"])
    expected_challenger = pot * TIER_SPLIT[tier][0] // 10000
    expected_creator = pot - expected_challenger
    expect(dispute_final["challenger_amount"] == expected_challenger,
           "challenger payout does not match the policy table")
    expect(dispute_final["creator_amount"] == expected_creator,
           "creator payout does not match the policy table")
    expect(dispute_final["challenger_amount"] + dispute_final["creator_amount"] == pot,
           "settlement does not conserve the pot")
    evidence["dispute_final"] = dispute_final

    model_b_final = read_view(client, address, "get_model", ["refund-beta-3b"])
    expect(model_b_final["deposit"] == expected_creator, "creator leg was not relocked")
    evidence["model_b_final"] = model_b_final
    print("  tier {0} challenger {1} creator {2}".format(
        tier, dispute_final["challenger_amount"], dispute_final["creator_amount"]))

    print("[14] final accounting")
    final_locked = WEI + expected_creator
    summary = assert_custody(client, address, final_locked, 0)
    evidence["custody_final"] = summary
    invariants = read_view(client, address, "check_invariants", [200])
    evidence["invariants"] = invariants
    expect(invariants["ok"] is True, "invariant check failed: {0}".format(invariants["errors"]))
    expect(invariants["refundable_sum"] == 0, "refundable ledger is not empty")
    expect(invariants["accounting_identity_ok"] is True, "accounting identity failed")
    expect(summary["rejected_payable_count"] == 6,
           "rejected payable count {0} != 6".format(summary["rejected_payable_count"]))
    expect(summary["total_refunded_out"] == 61 * E16,
           "total refunded out is wrong")

    deployer_view = rejection(client, address, deployer.address)
    challenger_view = rejection(client, address, challenger.address)
    evidence["rejections"] = {
        "deployer_last": deployer_view,
        "challenger_last": challenger_view,
    }
    expect(deployer_view["reason"] == "PAUSED", "deployer last rejection is wrong")
    expect(challenger_view["reason"] == "OPEN_DISPUTE_EXISTS", "challenger last rejection is wrong")

    evidence["verified"] = {
        "contract_sha256": source_hash,
        "deploy_source_sha256": deploy_sha["sha256"],
        "source_matches_deploy_tx": deploy_sha["sha256"] == source_hash,
        "rejected_payable_paths": 6,
        "full_value_recovered": True,
        "withdrawals_verified": 2,
        "audit_consensus": audit_tx.get("result_name"),
        "tier": tier,
        "split_matches_policy": True,
        "pot_conserved": True,
        "invariant_ok": invariants["ok"],
        "transactions_recorded": len(runner.txs),
    }
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("\nevidence written to {0}".format(EVIDENCE_PATH))
    print("contract {0}".format(address))
    print("deploy   {0}".format(EXPLORER_TX.format(hash=deploy_hex)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StepFailed as exc:
        print("FAILED: {0}".format(exc))
        raise SystemExit(1)
