"""Re-verify every recorded transaction against the studionet JSON-RPC.

Reads the transaction hashes from deployment/studionet.json and
deployment/time_boundaries.json, fetches each receipt again from the node and
records status, consensus result and the leader execution result. The explorer
links in the evidence files point at the same transactions; this script is the
machine check that does not depend on the explorer UI being reachable.

Usage:
    python scripts/verify_receipts.py
"""

import argparse
import json
import sys
import time
from pathlib import Path

from eth_account import Account
from genlayer_py import create_client
from genlayer_py.chains import studionet

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_studionet import EXPLORER_TX, load_env  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [
    ROOT / "deployment" / "studionet.json",
    ROOT / "deployment" / "time_boundaries.json",
]
OUT_PATH = ROOT / "deployment" / "receipts.json"


def fetch(client, tx_hash: str) -> dict:
    last_error = None
    for attempt in range(8):
        try:
            return client.get_transaction(tx_hash)
        except Exception as exc:  # noqa: BLE001 - transport level retry
            last_error = exc
            print("  retry {0}: {1}".format(attempt, type(exc).__name__))
            time.sleep(10)
    raise SystemExit("could not fetch {0}: {1}".format(tx_hash, last_error))


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-verify recorded transactions")
    parser.add_argument("--sources", default=",".join(str(path.relative_to(ROOT)) for path in SOURCES),
                        help="comma separated evidence files")
    parser.add_argument("--out", default=str(OUT_PATH.relative_to(ROOT)),
                        help="output receipt file")
    args = parser.parse_args()
    sources = [ROOT / item.strip() for item in args.sources.split(",") if item.strip()]
    out_path = ROOT / args.out

    client = create_client(chain=studionet, account=Account.from_key(load_env()["GENLAYER_PRIVATE_KEY"]))
    rows = []
    for source in sources:
        if not source.exists():
            continue
        payload = json.loads(source.read_text(encoding="utf-8"))
        for entry in payload.get("transactions", []):
            tx_hash = entry["hash"]
            row = {
                "label": entry["label"],
                "source": source.name,
                "hash": tx_hash,
                "explorer": entry.get("explorer") or EXPLORER_TX.format(hash=tx_hash),
            }
            if entry.get("execution_result") == "NATIVE_TRANSFER":
                # Plain EVM transfers live only on the chain layer.
                receipt = client.w3.eth.get_transaction_receipt(tx_hash)
                row.update(
                    {
                        "status": "EVM_SUCCESS" if int(receipt["status"]) == 1 else "EVM_FAILED",
                        "result": "NATIVE_TRANSFER",
                        "execution_result": "NATIVE_TRANSFER",
                        "value": str(int(receipt.get("value", 0))),
                        "block_number": int(receipt["blockNumber"]),
                    }
                )
            else:
                tx = fetch(client, tx_hash)
                leader = ((tx.get("consensus_data") or {}).get("leader_receipt") or [{}])[0]
                row.update(
                    {
                        "status": tx.get("status_name"),
                        "result": tx.get("result_name"),
                        "execution_result": leader.get("execution_result"),
                        "value": tx.get("value"),
                        "value_credited": tx.get("value_credited"),
                        "messages": tx.get("messages"),
                        "triggered_transactions": tx.get("triggered_transactions"),
                    }
                )
            rows.append(row)
            print("{0:<32} {1:<12} {2:<18} {3}".format(
                row["label"], row["status"], row["result"], row["execution_result"]))

    out_path.write_text(json.dumps({"network": "studionet", "transactions": rows}, indent=2,
                                   sort_keys=True) + "\n", encoding="utf-8")
    print("\n{0} receipts verified, written to {1}".format(len(rows), out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
