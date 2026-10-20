# Evidence: AIModelIPRegistry on studionet

Everything below was produced by commands run in this workspace. Transaction
hashes are the primary evidence; the machine readable copies live in
`deployment/studionet.json`, `deployment/time_boundaries.json` and
`deployment/receipts.json`.

## 1. Source binding

| Item | Value |
| --- | --- |
| Contract file | `contracts/AIModelIPRegistry.py` |
| sha256 | `a42d2a96aafaf7967ee5e4070fc13ec2cc2bf5dd3abfa40baf1e77be091c35f1` |
| Repository tag | `studionet-v1` |
| Runner pin | `# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }` |
| Network | studionet, chain id 61999 |
| Python | 3.13.13 |
| genlayer-py | 0.16.3 |
| genlayer-test | 0.29.2 |
| genvm-lint | 0.11.0 |

The deployed artifact is bound by the file sha256 above, the deploy transaction,
and the `studionet-v1` tag. The contract file is stored verbatim, including its
CRLF line endings, so git EOL normalization cannot change the blob that matches
the deployment. `git cat-file blob
studionet-v1:contracts/AIModelIPRegistry.py` hashes to the value above; an LF
normalized copy of the same file would hash to
`eb77009608decc6847be2458d3a3fe0ec54c3397af34f5b9b5e6323183892413`, so that
variant is deliberately not used. The deploy script re-hashes the file it is
about to deploy, and its recorded hash is identical to the hash of the file in
this workspace.

Quality gates run in this session:

```
$ genvm-lint check contracts/AIModelIPRegistry.py
✓ Lint passed (3 checks)
✓ Validation passed
  Contract: AIModelIPRegistry
  Methods: 24 (9 view, 15 write)

$ python -m pytest -q -p no:cacheprovider
98 passed in 5.26s
```

The 98 tests are direct mode tests: they run the real contract against the
pinned runner library with a mocked chain, web and LLM. They prove the ledger,
the guards and the deterministic backstops. They do not prove validator
agreement; sections 4 and 7 are the live proof of that.

## 2. Deployment

| Item | Value |
| --- | --- |
| Contract | `0xa8a2B23e69D2093fE6f496e59367974572775833` |
| Deploy tx | `0x8dbcc7bdb30289929812925b76481c480d53f0d1e998f0654801a6e17c2fca8c` |
| Deployer and owner | `0x154a452b0755EE812014C1405B15D905BA06f9c7` |
| Constructor args | `100000000000000000, 100000000000000000, 3600, 3600` |
| Split table read back | `NO_INFRINGEMENT [0, 10000]`, `PARTIAL_DERIVATIVE [6500, 3500]`, `EXACT_PLAGIARISM [8500, 1500]` |
| Counters after deploy | models 0, disputes 0, locked 0, paid out 0 |

The constructor floors are 1 hour and ceilings 30 days (audit) and 90 days
(exit). Both live runs use the 1 hour floor.

## 3. Transactions from the main scenario

Status and consensus result were re-read from the node with
`scripts/verify_receipts.py`, which calls `gen_getTransactionByHash` for
Intelligent Contract transactions and `eth_getTransactionReceipt` for the plain
EVM funding transfer. The explorer was not reachable (section 8), so the node is
the verification source.

| Step | Transaction | Status | Consensus | Execution |
| --- | --- | --- | --- | --- |
| deploy | `0x8dbcc7bdb30289929812925b76481c480d53f0d1e998f0654801a6e17c2fca8c` | FINALIZED | MAJORITY_AGREE | SUCCESS |
| register_model | `0x2995eebaf54f7503eb62396375ffd8687b73d4fb71d81fd263ab754500040c2e` | FINALIZED | MAJORITY_AGREE | SUCCESS |
| fund_challenger | `67e1906d3b35b2f07ef74bc4668b81834be948168ac834f5e3d7c314e95aac5f` | EVM_SUCCESS | not applicable | NATIVE_TRANSFER |
| open_dispute | `0x90e2523dc5dcbfedaeafd985df051cc4d01262610b5687a3f0be087a9a6b1ce4` | FINALIZED | MAJORITY_AGREE | SUCCESS |
| audit_model_similarity_live | `0xf4b7c041b7193a6348208f79e1c8b84575439f310e9c146e1a04f6c1f61491a4` | FINALIZED | MAJORITY_AGREE | SUCCESS |
| audit_after_settlement | `0x7bc9083b7d92f01b5625ca06e1f12e765150549b4658abb245d9458b5ab806a4` | FINALIZED | MAJORITY_AGREE | ERROR, expected |
| expire_settled_dispute | `0x09fcae89e8720b90f6043f7d71663ff4a8deaace42fdb776a41182bf676d319f` | FINALIZED | MAJORITY_AGREE | ERROR, expected |
| propose_after_settlement | `0x93d7df296d38210f87a7121fb205131f733d6e6edc026de4a7788095e08eeaa3` | FINALIZED | MAJORITY_AGREE | ERROR, expected |

The three ERROR rows are the negative boundary checks: the same dispute cannot
be audited twice, an already settled dispute cannot be expired, and a
settlement cannot be proposed after the dispute closed. The node does not
expose the revert text, so the transaction result is the evidence; the exact
messages are asserted by the direct tests.

## 4. Live consensus audit

Dispute `dsp-1` on model `llm-alpha-7b`:

| Field | Value |
| --- | --- |
| Tier | `EXACT_PLAGIARISM` |
| Status | `SETTLED_CONSENSUS` |
| Pot | `200000000000000000` wei (deposit 1e17 + challenger stake 1e17) |
| Challenger leg | `170000000000000000` wei (8500 bps) |
| Creator leg | `30000000000000000` wei (1500 bps) |
| Audit digest | `fd5ab08b31bd8d5b8a270bff7f126c8e60b810588b57b35407b85424f60321ca` |
| Adjustment | empty, no reconciliation was needed |
| Rationale | "All four SHA-256 digests (card, config, dataset, weights) match exactly, and the challenger admits verbatim redistribution of the published weights." |

The model ended with `deposit = 30000000000000000`, `status = FLAGGED`,
`disputes_lost = 1` and an empty `open_dispute_id`. The dispute record stores the
tier in `tier`, so the settlement is readable without replaying the event log.

Wallet movement, read from the EVM layer around the audit transaction:

| Item | Value |
| --- | --- |
| Challenger balance before | `100000000000000000` wei (after staking 1e17 from a 2e17 funding) |
| Challenger balance after | `270000000000000000` wei |
| Gas cost (effective gas price on studionet) | 0 |
| Net payout delta | exactly `170000000000000000` wei |
| Contract (ghost) balance after | `30000000000000000` wei |

This is the check that found a real defect. The first live attempt used
`gl.get_contract_at(to).emit_transfer(...)`, which posts an internal message.
The recipient is a wallet, so the message was dispatched as a contract call,
failed in the `contract_not_found_handler`, and the value was debited from the
contract without being credited. The contract now pays through
`gl.evm.contract_interface(...).emit_transfer(...)`, an external message the
ghost executes on the chain layer. The probe run and this run both show the
wallet balance credited by the exact amount.

## 5. Four way sha256 identity

The registered identity was built from four artifact digests:

| Slot | sha256 |
| --- | --- |
| weights | `1475fd49c82f3dbdee52c15c6494e6c27f9bfa1eb82bcc99d34612eab2cad919` |
| dataset | `0b8727d9f38e4d97b65e1ee5029e7fe6ba87a7f8b6903a6d6b0543ea48693fd7` |
| config | `e4135547fbaa18b0a3502e68bb79ab8b03d599f9ff43becd66af1fac6313e0a2` |
| card | `c81e196390d1de7410dd3018c1e9369126fb0a207bf077b4c1a675040f771cb8` |

Composite: `sha256("AI-IP-IDENTITY-V1|weights|dataset|config|card")` =
`213a65f1c3869af12442638f3898d87656006b351b20bdbda072ade5381ab9a3`.

On chain reads:

- `get_four_way_identity`: `identity_valid = true`, derived and stored digests
  both equal the composite above.
- `verify_sha256_identity` with the four digests: `full_identity = true`,
  `digest_identity = true`, `match_mask = 15`, `match_count = 4`, supplied and
  registered composites identical.
- The audit re-derives the composite before it runs and rejects a mismatch with
  `registered identity digest mismatch`.
- `register_model` rejects a composite that already has an owner, so an
  identical four tuple cannot be registered twice. This was verified live on an
  earlier deployment and is covered by two direct tests.

## 6. Accounting invariant

`check_invariants(200)` after the main scenario:

```
ok = true, error_count = 0
model_deposits = 30000000000000000
open_dispute_stakes = 0
expected_locked = 30000000000000000
total_locked = 30000000000000000
contract_balance = 30000000000000000
total_paid_out = 170000000000000000
solvency_gap = 0
open_disputes = 0, settled_disputes = 1, complete = true
```

The invariant is `contract balance == sum of model deposits + sum of open
dispute stakes`, with every settlement conserving the pot exactly. A gap in
either direction makes `ok` false; the `solvency_gap` field reports the signed
difference. It is checked by the contract after the fact and asserted after each
step in the direct tests.

## 7. Time boundaries

Both windows are floored at one hour by the constructor, so the live run uses
exactly 3600 seconds. The early attempts were sent before the window closed and
the late attempts after it.

| Transition | Window closes | Early attempt | Late attempt |
| --- | --- | --- | --- |
| `finalize_deregistration` | `exit_unlock_at` 2026-10-19T23:00:31Z | `0x7877a63a441053d9d521e473806b4ef291eeb70ee21280d14dc014f18420fa21` FINALIZED, ERROR | `0x39074a0137b1b22c2c4f00b70d992aaa4639abd95a9ff1a3378706e65017b090` FINALIZED, SUCCESS at 23:01:58Z, 87 s after the window |
| `expire_dispute` | `audit_deadline` 2026-10-19T23:01:09Z | `0xdf073247586b45d29337234183710323078e261e7c6f12830b5412770ceae145` FINALIZED, ERROR | `0xc0748aa05b7ad94ece17339534df1d5539e714007244274c826b3aa682f160f0` FINALIZED, SUCCESS at 23:10:04Z, 535 s after the window |

Late results, read back from the contract and the node:

- Exit model: `DEREGISTERED`, deposit 0. The 1e17 wei refund left escrow as the
external message `0x253f56d5f2d4675aa49d8f66584716e8c7a2be0361aa0c903df6412510135b1f`:
FINALIZED, from the contract to the creator, value `100000000000000000`,
`value_credited = true`, EVM receipt status 1. studionet returns the latest
balance for every block tag (blocks 1, 100 and 1000 all return the current
balance), so this credit is proven by the triggered message rather than by a
past balance sample.
- Expiry dispute: `EXPIRED`, `challenger_amount = 100000000000000000`,
`creator_amount = 0`. The challenger wallet moved from `100000000000000000` to
`200000000000000000` wei, a live delta of exactly the stake. The model deposit
stayed at `100000000000000000`.
- Final ledger: `total_locked = contract balance = 130000000000000000 wei`,
`solvency_gap = 0`, 0 open disputes, 3 models and 2 settled disputes scanned,
`complete = true`, and the identity of the deregistered model still validates.

Artifact: `deployment/time_boundaries.json`. All nine boundary transactions were
re-read from the node and appear in `deployment/receipts.json`.

The first pass stopped after the late finalize because it sampled the wallet
once, before the asynchronous external payout executed. The transition itself was
correct. `scripts/verify_boundary_closeout.py` verifies the credit from the
triggered message and runs the late expiry; `scripts/verify_time_boundaries.py`
now polls for the credit so a fresh run does not race the payout.

## 8. Explorer verification

The public explorer was unavailable during this session:

```
https://genlayer-explorer.vercel.app/api/transactions/<hash>
  503 The deployment is currently unavailable  DEPLOYMENT_PAUSED
https://explorer.genlayer.com/api/transactions/<hash>
  403 Cloudflare challenge
```

Every transaction was therefore re-read from the studionet JSON-RPC with
`scripts/verify_receipts.py`, and the raw results are stored in
`deployment/receipts.json`. The explorer links are still recorded in
`deployment/studionet.json` so a reviewer can open them when the deployment is
back.

## 9. Reproduction

```
python -m pytest -q -p no:cacheprovider
genvm-lint check contracts/AIModelIPRegistry.py
python scripts/deploy_studionet.py
python scripts/verify_receipts.py
python scripts/verify_time_boundaries.py
```

`deploy_studionet.py` reads `GENLAYER_PRIVATE_KEY` from this project's own
`.env`, never prints it, and writes `deployment/studionet.json`. The `.env`
file is listed in `.gitignore` and must never be committed.

## 10. Live findings worth carrying forward

1. `gl.get_contract_at(addr).emit_transfer` is an internal IC message. It
   cannot pay a wallet: the message executes as a contract call and dies in
   `contract_not_found_handler` after the ghost has been debited. External
   messages through `gl.evm.contract_interface` are the path that credits an
   EOA.
2. GenVM parses the whole leading comment block as the dependency header. Any
   comment line after the pinned header breaks deployment with no stderr. The
   prose lives in the module docstring for this reason.
3. `gen_call` rejects RLP wrapped payloads above roughly 300 bytes on
   studionet. Reads in the scripts send bare calldata, which is the same codec
   output before wrapping.
4. studionet intermittently answers `eth_getTransactionReceipt` with an HTML
   502. The scripts retry EVM receipt polling and GenLayer receipt polling; a
   retry is safe because it only re-reads a hash that was already submitted.
5. A reverted payable call keeps the value in the ghost on studionet. A probe
   sent 1e17 to a duplicate registration that reverted: the receipt was
   FINALIZED with `execution_result = ERROR` and `value_credited = true`, the
   sender balance fell by 1e17, and the contract balance rose by 1e17 with no
   ledger entry. Clients must therefore not send value to a call they expect to
   fail. `check_invariants` now treats a surplus as an invariant violation, and
   the final deployment in this file has `solvency_gap = 0`.
