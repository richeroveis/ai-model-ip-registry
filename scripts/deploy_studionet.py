"""Deploy AIModelIPRegistry to GenLayer studionet and verify it with real transactions.

The script is intentionally explicit: every step records the transaction hash, the
receipt status and the resulting contract state, so each claim in the evidence
file can be re-checked on the explorer.

Usage:
    python scripts/deploy_studionet.py            # deploy and drive the scenario
    python scripts/deploy_studionet.py --probe    # only print the environment

Credentials come from this project's own .env. Nothing secret is printed.
"""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from eth_account import Account
from genlayer_py import create_client
from genlayer_py.abi import calldata
from genlayer_py.accounts import create_account
from genlayer_py.chains import studionet
from genlayer_py.contracts.utils import make_calldata_object
from genlayer_py.types import TransactionStatus

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = ROOT / "contracts" / "AIModelIPRegistry.py"
EVIDENCE_PATH = ROOT / "deployment" / "studionet.json"
EXPLORER_TX = "https://genlayer-explorer.vercel.app/transactions/{hash}"
EXPLORER_ADDR = "https://genlayer-explorer.vercel.app/address/{address}"

TIER_SPLIT = {
    "NO_INFRINGEMENT": [0, 10000],
    "PARTIAL_DERIVATIVE": [6500, 3500],
    "EXACT_PLAGIARISM": [8500, 1500],
}

GAS_LIMIT = 21000
RETRIES = 240
INTERVAL_MS = 5000


class StepFailed(RuntimeError):
    pass


def load_env() -> dict:
    path = ROOT / ".env"
    if not path.exists():
        raise StepFailed("project .env is missing, refusing to deploy")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    if "GENLAYER_PRIVATE_KEY" not in values:
        raise StepFailed("GENLAYER_PRIVATE_KEY is missing from .env")
    return values


def local_identity_digest(weights: str, dataset: str, config: str, card: str) -> str:
    canonical = "AI-IP-IDENTITY-V1|" + "|".join([weights, dataset, config, card])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def slot_digest(label: str) -> str:
    return hashlib.sha256(("aimodelipregistry/" + label).encode("utf-8")).hexdigest()


def receipt_ok(receipt: dict) -> bool:
    consensus = receipt.get("consensus_data") or {}
    leaders = consensus.get("leader_receipt") or []
    if not leaders:
        return False
    return leaders[0].get("execution_result") == "SUCCESS"


def receipt_error(receipt: dict) -> str:
    consensus = receipt.get("consensus_data") or {}
    leaders = consensus.get("leader_receipt") or []
    if not leaders:
        return "no leader receipt"
    leader = leaders[0]
    genvm = leader.get("genvm_result") or {}
    parts = [
        str(leader.get("execution_result")),
        str(genvm.get("stderr", ""))[:800],
    ]
    return " | ".join(part for part in parts if part and part != "None")


def contract_address_of(receipt: dict) -> str:
    decoded = receipt.get("tx_data_decoded") or {}
    if "contract_address" in decoded and decoded["contract_address"]:
        return str(decoded["contract_address"])
    data = receipt.get("data") or {}
    if "contract_address" in data and data["contract_address"]:
        return str(data["contract_address"])
    raise StepFailed("deploy receipt has no contract address")


def read_view(client, address: str, function_name: str, args=None):
    """Read a view method using bare calldata.

    studionet rejects gen_call payloads above roughly 300 bytes when the SDK has
    wrapped them in the outer RLP list ("RLP string ends with N superfluous
    bytes"), so the bare calldata encoding is sent instead. It is the same codec
    the SDK applies before wrapping, so this is not a different ABI.
    """
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
    for attempt in range(8):
        try:
            encoded = client.provider.make_request(
                method="gen_call", params=[request])["result"]
            raw = encoded[2:] if encoded.startswith("0x") else encoded
            return calldata.decode(bytes.fromhex(raw))
        except Exception as exc:  # noqa: BLE001 - transport level retry
            last_error = exc
            print("  read retry {0} for {1}: {2}".format(
                attempt, function_name, type(exc).__name__))
            time.sleep(10)
    raise StepFailed("{0}: read failed: {1}".format(function_name, last_error))


class Runner:
    def __init__(self, client, evidence: dict):
        self.client = client
        self.evidence = evidence
        self.txs = []

    def record(self, label: str, tx_hash, receipt: dict) -> dict:
        tx_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
        entry = {
            "label": label,
            "hash": tx_hash,
            "status": str(receipt.get("status")),
            "execution_result": ((receipt.get("consensus_data") or {}).get("leader_receipt") or [{}])[0].get(
                "execution_result"
            ),
            "explorer": EXPLORER_TX.format(hash=tx_hash),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.txs.append(entry)
        self.evidence["transactions"] = self.txs
        print("  tx {0} {1} -> {2}".format(label, entry["execution_result"], tx_hash))
        return entry

    def record_native(self, label: str, tx_hash: str) -> None:
        entry = {
            "label": label,
            "hash": tx_hash,
            "status": "FINALIZED",
            "execution_result": "NATIVE_TRANSFER",
            "explorer": EXPLORER_TX.format(hash=tx_hash),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.txs.append(entry)
        self.evidence["transactions"] = self.txs
        print("  tx {0} -> {1}".format(label, tx_hash))

    def wait(self, label: str, tx_hash, status=TransactionStatus.ACCEPTED) -> dict:
        receipt = self._poll(label, tx_hash, status)
        self.record(label, tx_hash, receipt)
        if not receipt_ok(receipt):
            raise StepFailed("{0} failed: {1}".format(label, receipt_error(receipt)))
        return receipt

    def _poll(self, label: str, tx_hash, status) -> dict:
        """Poll with retries so a transient RPC drop does not lose the run."""
        last_error = None
        for attempt in range(8):
            try:
                return self.client.wait_for_transaction_receipt(
                    transaction_hash=tx_hash, status=status,
                    interval=INTERVAL_MS, retries=RETRIES,
                )
            except Exception as exc:  # noqa: BLE001 - transport level retry
                last_error = exc
                print("  poll retry {0} for {1}: {2}".format(attempt, label, type(exc).__name__))
                time.sleep(15)
        raise StepFailed("{0}: polling failed: {1}".format(label, last_error))

    def expect_revert(self, label: str, tx_hash) -> dict:
        receipt = self._poll(label, tx_hash, TransactionStatus.ACCEPTED)
        entry = self.record(label, tx_hash, receipt)
        if receipt_ok(receipt):
            raise StepFailed(label + " was expected to revert but succeeded")
        return entry


def wait_for_rpc(client, attempts: int = 30, delay: int = 20) -> None:
    """Block until the RPC answers a trivial read, so a submission is not lost mid-flight."""
    for attempt in range(attempts):
        try:
            client.w3.eth.block_number
            return
        except Exception as exc:  # noqa: BLE001 - transport level retry
            print("  waiting for rpc ({0}): {1}".format(attempt, type(exc).__name__))
            time.sleep(delay)
    raise StepFailed("rpc did not become available")


def make_evm_receipts_resilient(attempts: int = 12, delay: int = 10) -> None:
    """Retry EVM receipt polling inside the SDK.

    studionet occasionally answers eth_getTransactionReceipt with an HTML 502.
    The SDK polls the EVM receipt inside write_contract, outside our own retry
    loop, so one bad gateway response used to lose a run even when the
    transaction had already landed.
    """
    from web3.eth import Eth

    original = Eth.wait_for_transaction_receipt

    def resilient(self, transaction_hash, timeout=120, poll_latency=0.1):
        last_error = None
        for attempt in range(attempts):
            try:
                return original(self, transaction_hash, timeout=min(timeout, 30),
                                poll_latency=poll_latency)
            except Exception as exc:  # noqa: BLE001 - transport level retry
                last_error = exc
                print("  evm receipt retry {0}: {1}".format(attempt, type(exc).__name__))
                time.sleep(delay)
        raise last_error

    Eth.wait_for_transaction_receipt = resilient


def send_native(client, sender, to_address: str, amount_wei: int) -> str:
    web3 = client.w3
    gas_price = web3.eth.gas_price
    transaction = {
        "to": to_address,
        "value": amount_wei,
        "gas": GAS_LIMIT,
        "maxFeePerGas": gas_price * 2,
        "maxPriorityFeePerGas": gas_price,
        "nonce": web3.eth.get_transaction_count(sender.address),
        "chainId": studionet.id,
        "type": 2,
    }
    signed = sender.sign_transaction(transaction)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    return tx_hash.hex()


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy AIModelIPRegistry to studionet")
    parser.add_argument("--probe", action="store_true", help="print the environment only")
    parser.add_argument("--registration-stake", type=int, default=10 ** 17)
    parser.add_argument("--dispute-stake", type=int, default=10 ** 17)
    parser.add_argument("--audit-window", type=int, default=3600)
    parser.add_argument("--exit-delay", type=int, default=3600)
    args = parser.parse_args()

    env = load_env()
    deployer = Account.from_key(env["GENLAYER_PRIVATE_KEY"])
    client = create_client(chain=studionet, account=deployer)
    make_evm_receipts_resilient()

    print("network        : studionet ({0})".format(studionet.rpc_urls["default"]["http"][0]))
    print("deployer       : {0}".format(deployer.address))
    print("balance (wei)  : {0}".format(client.w3.eth.get_balance(deployer.address)))
    if args.probe:
        return 0

    if not CONTRACT_PATH.exists():
        raise StepFailed("contract file not found")
    source = CONTRACT_PATH.read_text(encoding="utf-8")
    if not source.startswith('# { "Depends": "py-genlayer:'):
        raise StepFailed("contract is missing the pinned runner header")
    source_hash = hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()

    evidence = {
        "network": "studionet",
        "chain_id": studionet.id,
        "deployer": deployer.address,
        "contract_file": str(CONTRACT_PATH.relative_to(ROOT)),
        "contract_sha256": source_hash,
        "constructor_args": {
            "min_registration_stake": args.registration_stake,
            "min_dispute_stake": args.dispute_stake,
            "audit_window_secs": args.audit_window,
            "exit_delay_secs": args.exit_delay,
        },
        "transactions": [],
    }
    runner = Runner(client, evidence)

    print("[1] deploy")
    wait_for_rpc(client)
    deploy_hash = client.deploy_contract(
        code=source.encode("utf-8"),
        args=[args.registration_stake, args.dispute_stake, args.audit_window, args.exit_delay],
    )
    deploy_receipt = runner.wait("deploy", deploy_hash, status=TransactionStatus.FINALIZED)
    address = contract_address_of(deploy_receipt)
    evidence["contract_address"] = address
    evidence["explorer_address"] = EXPLORER_ADDR.format(address=address)
    print("  contract {0}".format(address))

    config = read_view(client, address, "get_config")
    evidence["config_after_deploy"] = config
    if config["owner"].lower() != deployer.address.lower():
        raise StepFailed("owner is not the deployer")
    if config["split_table"] != TIER_SPLIT:
        raise StepFailed("on-chain split table does not match the local policy")

    print("[2] register model")
    weights = slot_digest("llm-alpha-7b/weights")
    dataset = slot_digest("llm-alpha-7b/dataset")
    config_digest = slot_digest("llm-alpha-7b/config")
    card = slot_digest("llm-alpha-7b/card")
    expected_identity = local_identity_digest(weights, dataset, config_digest, card)
    evidence["identity"] = {
        "weights_sha256": weights,
        "dataset_sha256": dataset,
        "config_sha256": config_digest,
        "card_sha256": card,
        "expected_identity_digest": expected_identity,
    }

    wait_for_rpc(client)
    register_hash = client.write_contract(
        address=address,
        function_name="register_model",
        args=[
            "llm-alpha-7b",
            weights,
            dataset,
            config_digest,
            card,
            "https://example.com",
            "https://www.iana.org/help/example-domains",
            "apache-2.0",
        ],
        value=args.registration_stake,
    )
    runner.wait("register_model", register_hash)

    print("[3] verify the 4 way sha256 identity")
    identity_view = read_view(client, address, "get_four_way_identity", ["llm-alpha-7b"])
    verify_view = read_view(client, address, "verify_sha256_identity",
                            ["llm-alpha-7b", weights, dataset, config_digest, card])
    evidence["identity_onchain"] = identity_view
    evidence["identity_verification"] = verify_view
    if not identity_view["identity_valid"]:
        raise StepFailed("on-chain identity digest self check failed")
    if identity_view["derived_identity_digest"] != expected_identity:
        raise StepFailed("on-chain identity digest does not match the local sha256")
    if not verify_view["full_identity"] or not verify_view["digest_identity"]:
        raise StepFailed("4 way identity verification failed")
    print("  identity {0}".format(expected_identity))

    print("[4] challenger account and stake funding")
    challenger = create_account()
    funding_hash = send_native(client, deployer, challenger.address, 2 * 10 ** 17)
    runner.record_native("fund_challenger", funding_hash)
    challenger_balance = client.w3.eth.get_balance(challenger.address)
    evidence["challenger"] = {"address": challenger.address, "balance_wei": challenger_balance}
    if challenger_balance < args.dispute_stake:
        raise StepFailed("challenger was not funded")
    print("  challenger {0} balance {1}".format(challenger.address, challenger_balance))

    print("[5] open dispute with a full 4 way claim")
    challenger_client = create_client(chain=studionet, account=challenger)
    wait_for_rpc(challenger_client)
    dispute_hash = challenger_client.write_contract(
        address=address,
        function_name="open_dispute",
        args=[
            "llm-alpha-7b",
            weights,
            dataset,
            config_digest,
            card,
            "https://example.com",
            "https://www.iana.org/help/example-domains",
            "verbatim redistribution of the published weights",
        ],
        value=args.dispute_stake,
    )
    runner.wait("open_dispute", dispute_hash)
    dispute_ids = read_view(client, address, "get_dispute_ids")
    dispute_id = dispute_ids[-1]
    evidence["dispute_id"] = dispute_id
    dispute_view = read_view(client, address, "get_dispute", [dispute_id])
    evidence["dispute_after_open"] = dispute_view
    if dispute_view["match_count"] != 4:
        raise StepFailed("claim did not match all four committed digests")
    print("  dispute {0} match_count {1}".format(dispute_id, dispute_view["match_count"]))

    print("[6] live consensus audit")
    challenger_balance_before = client.w3.eth.get_balance(challenger.address)
    wait_for_rpc(challenger_client)
    audit_hash = challenger_client.write_contract(
        address=address,
        function_name="audit_model_similarity_live",
        args=[dispute_id],
    )
    runner.wait("audit_model_similarity_live", audit_hash, status=TransactionStatus.FINALIZED)

    dispute_final = read_view(client, address, "get_dispute", [dispute_id])
    model_final = read_view(client, address, "get_model", ["llm-alpha-7b"])
    evidence["dispute_final"] = dispute_final
    evidence["model_final"] = model_final
    if dispute_final["status"] != "SETTLED_CONSENSUS":
        raise StepFailed("audit did not settle the dispute")
    tier = dispute_final["tier"]
    expected = TIER_SPLIT.get(tier)
    if expected is None:
        raise StepFailed("unknown tier after audit")
    pot = args.registration_stake + args.dispute_stake
    expected_challenger = pot * expected[0] // 10000
    expected_creator = pot - expected_challenger
    if dispute_final["challenger_amount"] != expected_challenger:
        raise StepFailed("challenger payout does not match the policy table")
    if dispute_final["creator_amount"] != expected_creator:
        raise StepFailed("creator payout does not match the policy table")
    if dispute_final["challenger_amount"] + dispute_final["creator_amount"] != pot:
        raise StepFailed("settlement does not conserve the pot")
    if model_final["deposit"] != expected_creator:
        raise StepFailed("model deposit was not reduced to the creator share")

    # The payout leaves escrow as a native credit. Poll until the challenger
    # balance reflects it, then require the exact delta (net of audit gas).
    challenger_balance_after = challenger_balance_before
    for _ in range(24):
        challenger_balance_after = client.w3.eth.get_balance(challenger.address)
        if challenger_balance_after > challenger_balance_before:
            break
        time.sleep(5)
    gas_cost = 0
    try:
        audit_receipt = client.w3.eth.get_transaction_receipt(
            audit_hash.hex() if hasattr(audit_hash, "hex") else audit_hash)
        gas_cost = int(audit_receipt["gasUsed"]) * int(audit_receipt["effectiveGasPrice"])
    except Exception:  # noqa: BLE001 - gas data is corroboration only
        gas_cost = 0
    net_delta = challenger_balance_after - challenger_balance_before + gas_cost
    evidence["challenger"].update(
        {
            "balance_before_audit_wei": challenger_balance_before,
            "balance_after_audit_wei": challenger_balance_after,
            "audit_gas_cost_wei": gas_cost,
            "net_payout_wei": net_delta,
        }
    )
    if net_delta != expected_challenger:
        raise StepFailed(
            "native payout delta {0} does not equal the escrow payout {1}".format(
                net_delta, expected_challenger))
    print("  tier {0} challenger {1} creator {2}".format(
        tier, dispute_final["challenger_amount"], dispute_final["creator_amount"]))
    print("  native payout delta {0}".format(net_delta))

    print("[7] boundary checks")
    wait_for_rpc(challenger_client)
    early_audit_hash = challenger_client.write_contract(
        address=address,
        function_name="audit_model_similarity_live",
        args=[dispute_id],
    )
    runner.expect_revert("audit_after_settlement", early_audit_hash)

    early_expire_hash = challenger_client.write_contract(
        address=address,
        function_name="expire_dispute",
        args=[dispute_id],
    )
    runner.expect_revert("expire_settled_dispute", early_expire_hash)

    late_proposal_hash = challenger_client.write_contract(
        address=address,
        function_name="propose_settlement",
        args=[dispute_id, 5000, 5000, "late proposal"],
    )
    runner.expect_revert("propose_after_settlement", late_proposal_hash)

    print("[8] accounting invariant")
    invariants = read_view(client, address, "check_invariants", [200])
    evidence["invariants"] = invariants
    if not invariants["ok"]:
        raise StepFailed("invariant check failed: {0}".format(invariants["errors"]))
    if invariants["total_locked"] != model_final["deposit"]:
        raise StepFailed("locked escrow does not equal the model deposit")
    if invariants["total_locked"] + dispute_final["challenger_amount"] != pot:
        raise StepFailed("escrow plus payout does not equal the original pot")
    contract_balance = 0
    for _ in range(24):
        contract_balance = client.w3.eth.get_balance(address)
        if contract_balance == invariants["total_locked"]:
            break
        time.sleep(5)
    evidence["contract_balance_wei"] = contract_balance
    if contract_balance != invariants["total_locked"]:
        raise StepFailed(
            "contract balance {0} does not equal locked escrow {1}".format(
                contract_balance, invariants["total_locked"]))
    print("  total_locked {0} solvency_gap {1}".format(
        invariants["total_locked"], invariants["solvency_gap"]))

    evidence["verified"] = {
        "contract_sha256": source_hash,
        "four_way_identity": True,
        "tier": tier,
        "split_matches_policy": True,
        "pot_conserved": True,
        "invariant_ok": invariants["ok"],
        "transactions_recorded": len(runner.txs),
    }
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("\nevidence written to {0}".format(EVIDENCE_PATH))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StepFailed as exc:
        print("FAILED: {0}".format(exc))
        raise SystemExit(1)
