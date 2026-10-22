# Evidence: AIModelIPRegistry on studionet (v2, Joaquin remediation)

Everything below was produced by commands run in this workspace. Transaction
hashes are the primary evidence. Machine readable copies live in
`deployment/joaquin_fix.json`, `deployment/receipts_v2.json` and
`deployment/time_boundaries.json`.

## 1. Source binding, four ways

| Binding | Value |
| --- | --- |
| Contract file (LF bytes on disk) | `contracts/AIModelIPRegistry.py` |
| sha256 of the file | `21267858bb1b4f6f08fa339bcc2380339664b2834b2fc2d8b66d9aeca13f6eae` |
| Repository tag | `studionet-v2-joaquin-fix` |
| Runner pin | `# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }` |
| Network | studionet, chain id 61999 |

The deploy transaction carries the source as the first item of its RLP payload.
`scripts/verify_joaquin_fix.py` decodes that item and hashes it:
`sha256 = 21267858bb1b4f6f08fa339bcc2380339664b2834b2fc2d8b66d9aeca13f6eae`,
87038 bytes. The same value is asserted for the git blob and for the blob on
`origin/main` after the push:

```
sha256(local file) == sha256(git blob) == sha256(origin/main blob)
                   == sha256(source in deploy calldata)
```

The file is stored as LF. There is no EOL exception in `.gitattributes` for this
version, so the working file, the blob and the deployed source are the same
bytes. This differs from v1: the v1 file was CRLF on disk, which hashed to
`a42d2a96...`, while the deployed source was LF and hashed to `eb770096...`.
That mismatch is why v1 could not claim a four way match.

## 2. Quality gates

```
$ genvm-lint check contracts/AIModelIPRegistry.py
Lint passed (3 checks)
Validation passed
  Contract: AIModelIPRegistry
  Methods: 30 (12 view, 18 write)

$ python -m pytest -q -p no:cacheprovider
142 passed in 16.86s
```

The 142 tests are direct mode tests: the real contract against the pinned
runner library with a mocked chain, web and LLM. They prove every guard, every
refund credit, every withdrawal and the deterministic backstops. They do not
prove validator agreement. Section 6 is the live proof of that.

## 3. Deployment

| Item | Value |
| --- | --- |
| Contract | `0xfdE499c7ef1D13BaD93c31598414E5839b6028c2` |
| Deploy tx | `0x3459d17f1f272bfd5b0810f354f628a98a6b089fa1f07230b5b65596f2644a51` |
| Deployer and owner | `0x154a452b0755EE812014C1405B15D905BA06f9c7` |
| Constructor args | `100000000000000000, 100000000000000000, 3600, 3600` |
| Deploy source sha256 read back from the transaction | `21267858bb1b4f6f08fa339bcc2380339664b2834b2fc2d8b66d9aeca13f6eae` |
| Counters after deploy | locked 0, refundable 0, rejected 0, paid out 0 |
| Initial custody identity | `accounting_identity_ok = true` |

The constructor keeps the v1 bounds: both stakes positive, audit window 1 hour
to 30 days, exit delay 1 hour to 90 days.

## 4. Refund remediation scenario, 16 transactions

Every row was re-read from the node after the fact with
`python scripts/verify_receipts.py --sources deployment/joaquin_fix.json --out deployment/receipts_v2.json`.
All 16 are FINALIZED with `MAJORITY_AGREE` and `SUCCESS`; the native funding row
is an EVM transfer with receipt status 1.

| Step | Transaction | Result |
| --- | --- | --- |
| deploy | `0x3459d17f1f272bfd5b0810f354f628a98a6b089fa1f07230b5b65596f2644a51` | FINALIZED, SUCCESS |
| register_model (exact bond) | `0x079138821b868a2da731f5bd430b58d42e1f696c5fefc13478acd2de2d438a07` | FINALIZED, SUCCESS |
| reject duplicate model id | `0xd64a4c0893ee2339a031025c3f732bff50deefb38dbff044ed29fa9fc29c3928` | FINALIZED, SUCCESS, `accepted = false` |
| reject insufficient registration bond | `0xf6cd36747b139122f896ba0bab8a0f83055f3ebbe7b5856ec1f3dd9d599c53f5` | FINALIZED, SUCCESS, `accepted = false` |
| reject invalid metadata url | `0xfbbcd427e4a4d0d86a0c72adbd6cf4502087fa78795e705851f6a3f56b23fe22` | FINALIZED, SUCCESS, `accepted = false` |
| set_paused(true) | `0x578d4ee88e94dc7c42714324eba1ad67cbcac7739f173b146dd3a887bc44ddaf` | FINALIZED, SUCCESS |
| reject registration while paused | `0x825198c5071f002eb50df5aec9c594be8f9514fdcbe48a14625e783ed3a7949c` | FINALIZED, SUCCESS, `accepted = false` |
| set_paused(false) | `0xbd10da661ca43bb8fe22b3ef971f12edf590b9cdc087197909c302e90636b3f0` | FINALIZED, SUCCESS |
| register with excess (1.5e17 sent) | `0x0ad4e3e846e68c8f347766789f719b9e8ee6c998b2cded4089a59356c5567cff` | FINALIZED, SUCCESS, deposit 1e17, 5e16 credited |
| fund challenger (5e17) | `9afa1c3450b2e3728b1c6e43fa4cf3be7e413543096e829bbd6cde36691f6dce` | EVM_SUCCESS |
| reject low dispute bond (5e16) | `0xa85cf0ba0bd97fa216f3a518cdb1d13ec184ff855b23183231d2c938c4ac4f4f` | FINALIZED, SUCCESS, `accepted = false` |
| open dispute with excess (1.7e17 sent) | `0xc9ab4bac73211d36abc9b60c1eab7133da456244e4125f84fc70650c6e141b7c` | FINALIZED, SUCCESS, stake 1e17, 7e16 credited |
| reject second dispute (state race) | `0xc75e46a68e5d7692f984a89a97de9c4ba28dd8bb881a2021a27b58ce8379d62f` | FINALIZED, SUCCESS, `accepted = false` |
| withdraw_refund (challenger) | `0x11e0076440be165269251047dd0d4779345c6883f073c4e6173197aa7e53a6df` | FINALIZED, SUCCESS, 2.2e17 paid |
| claim_refundable_balance (creator) | `0x7d442e20a3aa8cdfab332141449e4dc4092e10994fb7939122416b6b042a188f` | FINALIZED, SUCCESS, 3.9e17 paid |
| audit_model_similarity_live | `0xdd83ff0475d7f122a309bf471c2bbac49c1133c715445588d6bf95b01c639bdd` | FINALIZED, MAJORITY_AGREE, SUCCESS |

No rejection reverted. Every rejection returned a structured record, and the
attached value was credited in the same transaction.

## 5. Refund ledger, read back from the contract

Credits after each rejection, before any withdrawal:

| Rejection | Reason code | Value attached | Refundable balance |
| --- | --- | --- | --- |
| duplicate model id | `DUPLICATE_MODEL_ID` | 1e17 | 1e17 |
| short registration bond | `INSUFFICIENT_REGISTRATION_BOND` | 4e16 | 1.4e17 |
| invalid metadata url | `INVALID_METADATA_URL` | 1e17 | 2.4e17 |
| paused registration | `PAUSED` | 1e17 | 3.4e17 |
| low dispute bond | `INSUFFICIENT_DISPUTE_BOND` | 5e16 | challenger 5e16 |
| second dispute on the same model | `OPEN_DISPUTE_EXISTS` | 1e17 | challenger 2.2e17 |

Withdrawals, proven by the wallet balance delta:

| Account | Requested | Balance before | Balance after | Net delta |
| --- | --- | --- | --- | --- |
| challenger | `220000000000000000` | `180000000000000000` | `400000000000000000` | exactly `220000000000000000` |
| creator | `390000000000000000` | `99550000000000000000` | `99940000000000000000` | exactly `390000000000000000` |

After both withdrawals `get_refundable_balance` returns 0 for both addresses,
`total_refunded_out = 610000000000000000`, and the six rejected calls remain
visible through `get_last_payable_rejection`:

- creator: `PAUSED`, then zero balance,
- challenger: `OPEN_DISPUTE_EXISTS`, then zero balance,
- global `rejected_payable_count = 6`.

## 6. Live consensus audit

Dispute `dsp-1` on model `refund-beta-3b`:

| Field | Value |
| --- | --- |
| Tier | `EXACT_PLAGIARISM` |
| Status | `SETTLED_CONSENSUS` |
| Pot | `200000000000000000` wei (deposit 1e17 + challenger stake 1e17) |
| Challenger leg | `170000000000000000` wei (8500 bps) |
| Creator leg | `30000000000000000` wei (1500 bps) |
| Transaction | `0xdd83ff0475d7f122a309bf471c2bbac49c1133c715445588d6bf95b01c639bdd` |
| Status and consensus | `FINALIZED`, `MAJORITY_AGREE` |
| Leader execution | `SUCCESS` |

The model ended with `deposit = 3e16`, `status = FLAGGED`, `disputes_lost = 1`.
The tier is stored on the dispute, so a reader does not need the event log.

## 7. Custody identity

`get_custody_accounting_summary()` after the audit:

```
vault_custody_balance     = 130000000000000000
contract_balance          = 130000000000000000
active_registration_bonds = 130000000000000000
active_dispute_bonds      = 0
royalty_escrow            = 0
total_refundable_custody  = 0
unaccounted_surplus       = 0
accounting_identity_ok    = true
total_paid_out            = 170000000000000000
total_refunded_out        = 610000000000000000
rejected_payable_count    = 6
```

`check_invariants(200)`:

```
ok = true, error_count = 0
model_deposits = 130000000000000000
open_dispute_stakes = 0
expected_locked = 130000000000000000
total_locked = 130000000000000000
refundable_sum = 0
contract_balance = 130000000000000000
solvency_gap = 0
scanned_models = 2, scanned_disputes = 1, scanned_refund_holders = 2
complete = true
```

The identity enforced by both views is

```
vault_custody_balance
  == active_registration_bonds + active_dispute_bonds + royalty_escrow + total_refundable_custody
  == contract balance
```

There is no royalty path in this registry, so `royalty_escrow` is a constant 0
that keeps the identity explicit if one is ever added.

## 8. Four way sha256 identity

Model `refund-alpha-7b`:

| Slot | sha256 |
| --- | --- |
| weights | `d8ec724bb48ab00bbbdf627ff52c4a87a1789b93477f963f342fba67e79f7efe` |
| dataset | `6db7ed7d71936e23438975b0cc87ad9476b08548fbb07be747b5c6cb828980b4` |
| config | `6ea1974531d15a71e2c74ab659857911b3bad1430a1693516ac8d2f55e9fabf2` |
| card | `7133cbd23fecc157ba5c5e0e0185ac2248e3e02af2a33d1b1da87cd067633ff1` |
| composite identity | `063f3adaf36fa5de1e8ed57c316dd448fa1744201fb229cca959867703b583f4` |

The exact values are also stored in `deployment/joaquin_fix.json` under
`model_a`. The contract re-derives the composite
`sha256("AI-IP-IDENTITY-V1|w|d|c|k")` and `get_four_way_identity` returned
`identity_valid = true` with the stored and derived digests identical. The
dispute claim matched all four slots (`match_count = 4`, `match_mask = 15`),
which is what the audit digest binds.

## 9. Explorer

The public explorer was still unavailable during this session, so every
transaction in section 4 was re-read from the studionet JSON-RPC with
`scripts/verify_receipts.py`. The explorer links are recorded in
`deployment/joaquin_fix.json` for a reviewer who wants to open them later.

## 10. Reproduction

```
python -m pytest -q -p no:cacheprovider
genvm-lint check contracts/AIModelIPRegistry.py
python scripts/verify_joaquin_fix.py
python scripts/verify_receipts.py --sources deployment/joaquin_fix.json --out deployment/receipts_v2.json
```

`verify_joaquin_fix.py` reads `GENLAYER_PRIVATE_KEY` from this project's own
`.env`, never prints it, derives the second signer in memory, and writes
`deployment/joaquin_fix.json`. The `.env` file is listed in `.gitignore` and is
not committed.

## 11. History and aborted attempts

- v1 (contract `0xa8a2B23e69D2093fE6f496e59367974572775833`, tag
  `studionet-v1`) is the version reviewed by the steward. Its main run, the one
  hour boundary run and the receipt set are still in `deployment/studionet.json`,
  `deployment/time_boundaries.json` and `deployment/receipts.json`.
- Two v2 attempts aborted on studionet transport failures before completing and
  are not part of this evidence: `0xaBbE6E2b12Bb78B400De4BA258c542beCfe4D2a1`
  (TLS outage during a read; its challenger key was not retained, so the credit
  on that address is only withdrawable by that address) and
  `0xab1710c037DA7Ba54a0c24eE43F9C8B5b53740C0` (aborted at an unpause call when
  the shared request budget was exhausted). Neither affects the deployed
  contract above.
