"""Drive both live time boundaries with the shortest window the contract allows.

The constructor enforces a one hour floor on the audit window and on the exit
delay, so this script runs for about an hour by design. It exercises:

  * exit cooldown: an early finalize reverts, a late finalize refunds the deposit.
  * audit expiry: an early expire reverts, a late expire refunds the challenger.
  * a permissionless sweep of any dispute whose window has already closed.

Every transaction hash, receipt status and balance delta lands in
deployment/time_boundaries.json, so the claim can be re-checked on the node.

Usage:
    python scripts/verify_time_boundaries.py
"""

import json
import sys
import time
from pathlib import Path

from eth_account import Account
from genlayer_py import create_client
from genlayer_py.accounts import create_account
from genlayer_py.chains import studionet

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_studionet import (  # noqa: E402
    EVIDENCE_PATH,
    Runner,
    StepFailed,
    load_env,
    make_evm_receipts_resilient,
    read_view,
    send_native,
    slot_digest,
    wait_for_rpc,
)

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "deployment" / "time_boundaries.json"

WAIT_LEAD_SECS = 45
STAKE = 10 ** 17
EXIT_MODEL = "exit-cooldown-model"
EXPIRY_MODEL = "expiry-window-model"


def model_digests(model_id: str) -> list:
    return [slot_digest(model_id + "/" + slot) for slot in ("weights", "dataset", "config", "card")]


def gas_cost(client, tx_hash) -> int:
    """Gas paid by the signer. Corroboration only; a missing receipt reads as zero."""
    try:
        key = tx_hash.hex() if hasattr(tx_hash, "hex") else tx_hash
        receipt = client.w3.eth.get_transaction_receipt(key)
        return int(receipt["gasUsed"]) * int(receipt["effectiveGasPrice"])
    except Exception:  # noqa: BLE001 - gas data is corroboration only
        return 0


def wait_for_credit(client, address, target: int, tries: int = 36):
    """Poll until the wallet holds at least `target`.

    Payouts leave escrow as external EVM messages, which execute after the
    transaction finalizes. Sampling the balance once, right after the receipt,
    races that message and reads the pre credit balance.
    """
    balance = client.w3.eth.get_balance(address)
    for _ in range(tries):
        if balance >= target:
            return balance
        time.sleep(5)
        balance = client.w3.eth.get_balance(address)
    return balance


def main() -> int:
    if not EVIDENCE_PATH.exists():
        raise StepFailed("deployment/studionet.json is missing, run the deploy first")
    deployed = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    address = deployed["contract_address"]

    env = load_env()
    deployer = Account.from_key(env["GENLAYER_PRIVATE_KEY"])
    client = create_client(chain=studionet, account=deployer)
    make_evm_receipts_resilient()
    wait_for_rpc(client)

    evidence = {
        "network": "studionet",
        "chain_id": studionet.id,
        "contract_address": address,
        "explorer_address": "https://genlayer-explorer.vercel.app/address/{0}".format(address),
        "contract_sha256": deployed["contract_sha256"],
        "transactions": [],
        "window_secs": deployed["constructor_args"]["audit_window_secs"],
        "exit_delay_secs": deployed["constructor_args"]["exit_delay_secs"],
    }
    runner = Runner(client, evidence)

    print("[1] fund a dedicated challenger")
    challenger = create_account()
    funding_hash = send_native(client, deployer, challenger.address, 2 * STAKE)
    runner.record_native("fund_boundary_challenger", funding_hash)
    challenger_client = create_client(chain=studionet, account=challenger)
    evidence["challenger"] = {"address": challenger.address}

    print("[2] register the exit model and start its cooldown")
    exit_digests = model_digests(EXIT_MODEL)
    register_exit_hash = client.write_contract(
        address=address,
        function_name="register_model",
        args=[EXIT_MODEL, exit_digests[0], exit_digests[1], exit_digests[2], exit_digests[3],
              "https://example.com", "https://www.iana.org/help/example-domains", "apache-2.0"],
        value=STAKE,
    )
    runner.wait("register_exit_model", register_exit_hash)

    request_hash = client.write_contract(
        address=address, function_name="request_deregistration", args=[EXIT_MODEL])
    runner.wait("request_deregistration", request_hash)
    pending = read_view(client, address, "get_model", [EXIT_MODEL])
    evidence["exit_model_pending"] = pending
    exit_unlock_at = pending["exit_unlock_at"]

    early_exit_hash = client.write_contract(
        address=address, function_name="finalize_deregistration", args=[EXIT_MODEL])
    runner.expect_revert("finalize_deregistration_early", early_exit_hash)

    print("[3] register the expiry model and open a dispute on it")
    expiry_digests = model_digests(EXPIRY_MODEL)
    register_expiry_hash = client.write_contract(
        address=address,
        function_name="register_model",
        args=[EXPIRY_MODEL, expiry_digests[0], expiry_digests[1], expiry_digests[2],
              expiry_digests[3], "https://example.com",
              "https://www.iana.org/help/example-domains", "apache-2.0"],
        value=STAKE,
    )
    runner.wait("register_expiry_model", register_expiry_hash)

    wait_for_rpc(challenger_client)
    dispute_hash = challenger_client.write_contract(
        address=address,
        function_name="open_dispute",
        args=[EXPIRY_MODEL, expiry_digests[0], expiry_digests[1], expiry_digests[2],
              expiry_digests[3], "https://example.com",
              "https://www.iana.org/help/example-domains", "window boundary check"],
        value=STAKE,
    )
    runner.wait("open_dispute", dispute_hash)
    dispute_id = read_view(client, address, "get_dispute_ids")[-1]
    opened = read_view(client, address, "get_dispute", [dispute_id])
    evidence["dispute_opened"] = opened
    audit_deadline = opened["audit_deadline"]
    if opened["match_count"] != 4:
        raise StepFailed("boundary dispute claim did not match all four digests")
    if opened["challenger_stake"] != STAKE:
        raise StepFailed("boundary dispute stake was not recorded")
    evidence["dispute_id"] = dispute_id

    early_expire_hash = challenger_client.write_contract(
        address=address, function_name="expire_dispute", args=[dispute_id])
    runner.expect_revert("expire_dispute_early", early_expire_hash)

    print("[4] wait for the one hour windows to close")
    target = max(exit_unlock_at, audit_deadline) + WAIT_LEAD_SECS
    now = int(time.time())
    while now < target:
        remaining = target - now
        print("  waiting {0}s (about {1} min)".format(remaining, remaining // 60))
        time.sleep(min(remaining, 300))
        now = int(time.time())

    print("[5] late finalize refunds the creator deposit")
    creator_before = client.w3.eth.get_balance(deployer.address)
    finalize_hash = client.write_contract(
        address=address, function_name="finalize_deregistration", args=[EXIT_MODEL])
    runner.wait("finalize_deregistration_late", finalize_hash)
    finalize_gas = gas_cost(client, finalize_hash)
    creator_after = wait_for_credit(
        client, deployer.address, creator_before - finalize_gas + STAKE)
    creator_delta = creator_after - creator_before + finalize_gas
    deregistered = read_view(client, address, "get_model", [EXIT_MODEL])
    evidence["exit_model_final"] = deregistered
    evidence["creator_refund"] = {
        "before_wei": creator_before,
        "after_wei": creator_after,
        "gas_cost_wei": finalize_gas,
        "net_delta_wei": creator_delta,
    }
    if deregistered["status"] != "DEREGISTERED":
        raise StepFailed("late finalize did not deregister the model")
    if creator_delta != STAKE:
        raise StepFailed("late finalize refund {0} does not equal the stake".format(creator_delta))

    print("[6] late expire refunds the challenger stake")
    challenger_before = client.w3.eth.get_balance(challenger.address)
    expire_hash = challenger_client.write_contract(
        address=address, function_name="expire_dispute", args=[dispute_id])
    runner.wait("expire_dispute_late", expire_hash)
    expire_gas = gas_cost(client, expire_hash)
    challenger_after = wait_for_credit(
        client, challenger.address, challenger_before - expire_gas + STAKE)
    challenger_delta = challenger_after - challenger_before + expire_gas
    expired = read_view(client, address, "get_dispute", [dispute_id])
    evidence["dispute_final"] = expired
    evidence["challenger_refund"] = {
        "before_wei": challenger_before,
        "after_wei": challenger_after,
        "gas_cost_wei": expire_gas,
        "net_delta_wei": challenger_delta,
    }
    if expired["status"] != "EXPIRED":
        raise StepFailed("late expire did not expire the dispute")
    if challenger_delta != STAKE:
        raise StepFailed("late expire refund {0} does not equal the stake".format(challenger_delta))

    print("[7] sweep any dispute whose window has already closed")
    swept = []
    for other_id in read_view(client, address, "get_dispute_ids"):
        record = read_view(client, address, "get_dispute", [other_id])
        if record["status"] != "OPEN":
            continue
        if int(time.time()) < record["audit_deadline"]:
            continue
        sweep_hash = challenger_client.write_contract(
            address=address, function_name="expire_dispute", args=[other_id])
        runner.wait("sweep_open_dispute", sweep_hash)
        swept.append(read_view(client, address, "get_dispute", [other_id]))
    evidence["swept_disputes"] = swept

    print("[8] accounting invariant after both boundaries")
    invariants = read_view(client, address, "check_invariants", [200])
    contract_balance = client.w3.eth.get_balance(address)
    evidence["invariants"] = invariants
    evidence["contract_balance_wei"] = contract_balance
    if not invariants["ok"]:
        raise StepFailed("invariant check failed: {0}".format(invariants["errors"]))
    if invariants["total_locked"] != contract_balance:
        raise StepFailed("contract balance does not equal the locked escrow")
    if invariants["solvency_gap"] != 0:
        raise StepFailed("solvency gap is not zero")

    print("[9] verify the four way identity is still intact")
    identity = read_view(client, address, "get_four_way_identity", [EXIT_MODEL])
    evidence["identity_after_deregistration"] = identity
    if not identity["identity_valid"]:
        raise StepFailed("identity digest no longer validates")

    evidence["verified"] = {
        "exit_early_reverted": True,
        "exit_late_refunded": True,
        "expire_early_reverted": True,
        "expire_late_refunded": True,
        "contract_balance_matches_locked": True,
        "invariant_ok": True,
        "transactions_recorded": len(runner.txs),
    }
    OUT_PATH.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("\nboundary evidence written to {0}".format(OUT_PATH))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StepFailed as exc:
        print("FAILED: {0}".format(exc))
        raise SystemExit(1)
