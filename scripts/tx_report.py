"""Print the decoded transaction receipt and execution trace for a studionet hash.

Evidence helper: the raw receipt is what the explorer shows, so printing it verbatim
makes every claim in docs/evidence.md auditable.

Usage:
    python scripts/tx_report.py <tx_hash> [--trace]
"""

import argparse
import json
import sys
from pathlib import Path

from eth_account import Account
from genlayer_py import create_client
from genlayer_py.chains import studionet

ROOT = Path(__file__).resolve().parent.parent


def load_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("GENLAYER_PRIVATE_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("GENLAYER_PRIVATE_KEY missing from .env")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tx_hash")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--raw", action="store_true", help="dump the whole receipt")
    args = parser.parse_args()

    client = create_client(chain=studionet, account=Account.from_key(load_key()))
    transaction = client.get_transaction(args.tx_hash)

    if args.raw:
        print(json.dumps(transaction, indent=2, default=str))
        return 0

    consensus = transaction.get("consensus_data") or {}
    leaders = consensus.get("leader_receipt") or []
    summary = {
        "hash": args.tx_hash,
        "status": transaction.get("status"),
        "status_name": transaction.get("status_name"),
        "to": transaction.get("to_address"),
        "sender": transaction.get("sender"),
        "result": transaction.get("result"),
    }
    if leaders:
        leader = leaders[0]
        summary["execution_result"] = leader.get("execution_result")
        summary["return_data"] = leader.get("return_data")
        genvm = leader.get("genvm_result") or {}
        summary["stderr"] = (genvm.get("stderr") or "")[:2000]
        summary["stdout"] = (genvm.get("stdout") or "")[:2000]
    print(json.dumps(summary, indent=2, default=str))

    if args.trace:
        trace = client.debug_trace_transaction(args.tx_hash)
        print(json.dumps(trace, indent=2, default=str)[:8000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
