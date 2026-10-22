# AIModelIPRegistry

A stake backed model IP registry on GenLayer. Creators commit a four part sha256
identity for a model and lock a deposit against it. Anyone else can challenge
that claim by locking a stake at least as large as the deposit. The dispute is
then settled in exactly one of three ways:

1. a live AI consensus audit that classifies the infringement tier,
2. a strict two party settlement over hash committed terms, or
3. expiry of the audit window, which refunds the challenger and leaves the
   deposit untouched.

The AI layer only classifies. It never decides amounts: every tier maps to a
fixed split table, and the split is re-derived deterministically after
consensus.

No payable path reverts while value is attached. A refused registration,
top-up or dispute returns a structured rejection, records the reason, and
credits the whole attached amount to the sender's refundable balance, which can
be withdrawn at any time.

## Live deployment (studionet)

| Item | Value |
| --- | --- |
| Network | studionet, chain id 61999 |
| Contract | `0xfdE499c7ef1D13BaD93c31598414E5839b6028c2` |
| Deploy tx | `0x3459d17f1f272bfd5b0810f354f628a98a6b089fa1f07230b5b65596f2644a51` |
| Contract file sha256 (LF) | `21267858bb1b4f6f08fa339bcc2380339664b2834b2fc2d8b66d9aeca13f6eae` |
| Runner | `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` |
| Source binding | tag `studionet-v2-joaquin-fix`, plus the source bytes in the deploy calldata |
| Rejected payable paths verified live | 6: duplicate id, short bond, invalid url, paused, short dispute bond, open dispute race |
| Refund recovery verified live | 2 withdrawals, wallet deltas of exactly `220000000000000000` and `390000000000000000` wei, both balances back to 0 |
| Live audit result | `EXACT_PLAGIARISM`, challenger `170000000000000000` wei, creator `30000000000000000` wei, `FINALIZED` and `MAJORITY_AGREE` |
| Custody identity | `locked 130000000000000000 = contract balance`, `refundable 0`, `surplus 0`, `accounting_identity_ok = true` |

Transaction level evidence, including every rejection, both withdrawals and the
four way source hash, is in [docs/evidence.md](docs/evidence.md). The v1
artifacts and the two aborted v2 attempts are summarised in section 11.

## Why GenLayer

The registry needs a judgment that a single backend cannot credibly own: does a
model artifact set infringe another one? Validators independently render both
parties' evidence, ask an LLM for a classification, and compare the stable
decision fields under `gl.eq_principle.prompt_comparative`. The contract then
maps the agreed tier to escrowed money. Neither the owner nor the frontend can
change that mapping.

## State machine

Model status:

```
ACTIVE --request_deregistration--> PENDING_EXIT --finalize_deregistration--> DEREGISTERED
   ^                                    |
   |                                    +--cancel_deregistration--> ACTIVE
   |
   +--audit finds infringement--> FLAGGED (deposit reduced, disputes still allowed)
```

A model cannot enter `PENDING_EXIT` while a dispute is open, cannot finalize
deregistration while a dispute is open, and disputes are still allowed during
`PENDING_EXIT`, so a creator cannot deregister to escape a challenge.

Dispute status:

```
OPEN --audit_model_similarity_live--> SETTLED_CONSENSUS
  |
  +--countersign_settlement--> SETTLED_MUTUAL
  |
  +--expire_dispute (after audit_deadline)--> EXPIRED
```

Both settlement paths call one internal function that enforces the same
invariants: the dispute is open, the model index points at it, the split is a
non negative partition of 10000 basis points, the deposit has not fallen below
the snapshot taken when the dispute opened, and the pot is conserved exactly.

## Money rules

The pot is `model.deposit + dispute.challenger_stake`. Splits are basis points
of the pot:

| Tier | Challenger | Creator |
| --- | --- | --- |
| `NO_INFRINGEMENT` | 0 | 10000 |
| `PARTIAL_DERIVATIVE` | 6500 | 3500 |
| `EXACT_PLAGIARISM` | 8500 | 1500 |

- `challenger_amount = pot * challenger_bps // 10000`, `creator_amount = pot - challenger_amount`,
  so the two legs always sum to the pot with no dust.
- The creator leg is re-locked as the model's new deposit. Only the challenger
  leg leaves escrow.
- A challenger must bond at least `max(min_dispute_stake, model.deposit)`. A
  cheap accusation against a large deposit is not a favourable bet.
- The challenger payout is an external EVM message from the contract's ghost,
  which is the only path that credits a wallet on GenLayer. An internal
  `emit_transfer` to an EOA is dispatched as a contract call, fails in the
  contract not found handler, and is never used here.
- `expire_dispute` refunds the challenger stake in full. Nothing is slashed.
- `total_locked` and `total_paid_out` are updated on every transition and are
  checked by `check_invariants`.

## Refunds and payable safety

GenLayer moves attached value before the method body runs. A raise would
roll back the ledger entry while the value stays in the contract, so every
payable entry point is written to refuse without reverting.

The payable surface is exactly three methods: `register_model`, `top_up_stake`
and `open_dispute`. Each one validates first and, on any refusal, calls one
internal helper that:

1. credits the full attached value to `refundable_balances[sender]`,
2. increments `total_refundable_custody` and the `vault_custody_balance` mirror,
3. stores the reason in `last_payable_rejection[sender]` and increments
   `rejected_payable_count`,
4. emits `PayableRejected` and returns
   `{accepted: false, reason, refund_credited, refundable_balance}`.

Refusal reasons covered: `PAUSED`, `DUPLICATE_MODEL_ID`, `DUPLICATE_IDENTITY`,
`INVALID_MODEL_ID`, `INVALID_*_DIGEST`, `INVALID_METADATA_URL`,
`INVALID_BENCHMARK_URL`, `INVALID_CHALLENGER_*_URL`, `INVALID_LICENSE_TERMS`,
`INVALID_EVIDENCE_NOTE`, `INVALID_TRANSACTION_CLOCK`, `UNKNOWN_MODEL`,
`DEREGISTERED_MODEL`, `SELF_DISPUTE`, `OPEN_DISPUTE_EXISTS`,
`INSUFFICIENT_REGISTRATION_BOND`, `INSUFFICIENT_DISPUTE_BOND`,
`BOND_BELOW_MODEL_DEPOSIT`, `CALLER_NOT_MODEL_CREATOR` and
`TOP_UP_MUST_BE_POSITIVE`. Value attached to any non payable write (for example
`update_listing` or `audit_model_similarity_live`) is credited the same way with
`VALUE_NOT_ACCEPTED`.

Excess value is never locked. Registration locks exactly
`min_registration_stake`; a dispute locks exactly
`max(min_dispute_stake, model.deposit)`. Anything above that is credited back in
the same transaction. `top_up_stake` is intentional value, so all of it is
locked.

Recovery paths:

- `withdraw_refund(amount)` pays the caller, or the whole balance when `amount`
  is 0, through the external EVM message path. `claim_refundable_balance()` is
  the no argument alias.
- `record_or_recover_unaccounted_value(recipient)` is owner only. It converts a
  measured surplus (contract balance minus locked bonds minus refundable
  custody) into a refund credit for `recipient`. It is capped at that surplus
  and can never touch a bond or an existing credit. It exists for value that
  arrived outside the payable surface, for example a plain native transfer to
  the contract.

Custody identity, enforced by `check_invariants` and
`get_custody_accounting_summary`:

```
vault_custody_balance
  == active_registration_bonds + active_dispute_bonds + royalty_escrow + total_refundable_custody
  == contract balance
```

`royalty_escrow` is a constant 0 because the registry has no royalty path. Any
nonzero surplus or shortfall makes `check_invariants` fail.

## Consensus design

`audit_model_similarity_live(dispute_id)`:

Deadlines are measured from the transaction datetime carried in the consensus
message, so the leader and every validator use the same clock.

1. Re-derives the four way identity from the stored record. A tampered field is
   rejected before any model call.
2. Recomputes the committed digest match count and rejects a mismatch with the
   dispute snapshot.
3. Renders the creator and challenger metadata and benchmark URLs with
   `gl.nondet.web.render`.
4. Asks for a JSON verdict and reconciles it deterministically:
   - if the challenger served no evidence, the tier becomes `NO_INFRINGEMENT`,
   - if the creator served no evidence, `NO_INFRINGEMENT` is upgraded to
     `PARTIAL_DERIVATIVE`,
   - `EXACT_PLAGIARISM` requires at least one committed digest match,
   - `NO_INFRINGEMENT` is upgraded to `PARTIAL_DERIVATIVE` when all four digests
     match, so identical artifacts cannot be certified as independent.
5. Runs `gl.eq_principle.prompt_comparative` with a principle that demands exact
   equality of `tier`, `challenger_bps`, `creator_bps` and `match_count`.
6. After consensus, rejects any verdict whose tier is unknown, whose split is
   not exactly `SPLIT_TABLE[tier]`, whose legs do not sum to 10000, whose match
   count differs from the recomputed value, or whose audit digest differs from
   the one re-derived on chain.

The audit digest binds model id, dispute id, tier, both split legs, match count
and the four identity digests into one sha256 value that is stored on chain.

## Four way sha256 identity

Each model commits four independent artifact digests (weights, dataset,
training config, model card), each a 64 character lowercase sha256 hex string.
They are bound by a composite:

```
identity_digest = sha256("AI-IP-IDENTITY-V1|w|d|c|k")
```

`get_four_way_identity(model_id)` re-derives the composite and reports whether
it still matches the stored value. `verify_sha256_identity(model_id, w, d, c, k)`
compares a supplied four tuple against the commitment and returns per slot
results plus the supplied composite. Both views are read only and cannot be
used to change the commitment. `update_listing` can change URLs and license
terms but never the four digests.

A composite identity can be registered once. `register_model` rejects a
composite that already has an owner, including after the first model was
deregistered, so the same four artifacts cannot be claimed under two model ids.
This closes the simplest first-registrant attack: a second claim on identical
artifacts has to go through `open_dispute`, where the committed digests and the
audit decide it.

## Owner surface

The owner can update the four bounded parameters (both stake floors, the audit
window between 1 hour and 30 days, the exit delay between 1 hour and 90 days),
pause registration and disputes, and hand over ownership in two steps. The
owner cannot touch escrow, disputes, settlement terms, payouts, the split table
or a committed identity. The only value movement the owner can trigger is
`record_or_recover_unaccounted_value`, which is capped at a measured surplus
that no ledger entry attributes to anyone, and therefore cannot reach a bond or
a refund credit. There is no unilateral settlement override anywhere in the
contract.

## Contract API

Writes:

| Method | Purpose |
| --- | --- |
| `register_model` (payable) | Commit the four digests, URLs and license, lock the deposit |
| `top_up_stake` (payable) | Increase a model deposit, including while a dispute is open (it grows the pot) |
| `update_listing` | Change URLs and license terms, never the identity |
| `request_deregistration` | Start the exit cooldown |
| `cancel_deregistration` | Return to ACTIVE during the cooldown |
| `finalize_deregistration` | After the cooldown, refund the deposit |
| `open_dispute` (payable) | Lock a full bond and snapshot the claim |
| `audit_model_similarity_live` | Live consensus audit and settlement |
| `propose_settlement` | Commit settlement terms with a version and digest |
| `countersign_settlement` | Accept identical terms by the other party |
| `expire_dispute` | Refund the challenger after the audit deadline |
| `set_parameters` | Owner, bounded |
| `set_paused` | Owner |
| `transfer_ownership` / `accept_ownership` | Two step handover |
| `withdraw_refund(amount)` | Pay a credited refund, whole balance when amount is 0 |
| `claim_refundable_balance` | No argument alias for a full withdrawal |
| `record_or_recover_unaccounted_value(recipient)` | Owner only, capped at the measured surplus |

Views:

| Method | Purpose |
| --- | --- |
| `get_config` | Parameters, counters, split table |
| `get_model` / `get_model_ids` | Model records and index |
| `get_dispute` / `get_dispute_ids` | Dispute records and index |
| `get_dispute_evidence` | Both evidence sets side by side |
| `get_four_way_identity` | Re-derived identity and validity |
| `verify_sha256_identity` | Compare a supplied four tuple |
| `get_refundable_balance(address)` | Credited refund balance and last reason |
| `get_last_payable_rejection(address)` | Last refusal code and counters |
| `get_custody_accounting_summary` | Every custody term and the identity flag |
| `check_invariants` | Full ledger, refund and identity sweep |

## Repository layout

```
contracts/AIModelIPRegistry.py     the contract
tests/test_registry.py             registration, validation, lifecycle, owner surface
tests/test_settlement.py           audit, mutual settlement, expiry, invariants
tests/test_refunds.py              every payable refusal, withdrawal and custody term
tests/support.py                   direct mode harness and chain hook
conftest.py                        Windows fd0 fix and the direct mode EVM stub
scripts/deploy_studionet.py        deploy and drive the v1 live scenario
scripts/verify_joaquin_fix.py      deploy and drive the v2 refund scenario
scripts/verify_time_boundaries.py  live early and late time boundary run
scripts/verify_boundary_closeout.py  finish the recorded boundary run and verify its credits
scripts/verify_receipts.py         re-check every tx against the node RPC
scripts/tx_report.py               decode one transaction receipt or trace
deployment/                        machine readable evidence
docs/evidence.md                   human readable evidence
docs/adversarial-review.md         state machine, races, findings, open items
```

## Running locally

Python 3.13 and the pinned tool versions:

```
python -m pip install "genlayer-py==0.16.3" "genlayer-test==0.29.2" genvm-lint
```

Direct tests (142 tests, no network):

```
python -m pytest -q -p no:cacheprovider
```

Lint and static validation:

```
genvm-lint check contracts/AIModelIPRegistry.py
```

Expected output: `Lint passed (3 checks)` and `Validation passed, 30 methods
(12 view, 18 write)`.

Deploy and drive the live scenario. The scripts read `GENLAYER_PRIVATE_KEY`
from this project's own `.env`, never print it, and write their evidence file:

```
python scripts/verify_joaquin_fix.py
python scripts/verify_receipts.py --sources deployment/joaquin_fix.json --out deployment/receipts_v2.json
```

The v1 scenario remains reproducible with `python scripts/deploy_studionet.py`.

The time boundary script runs for about an hour because the contract enforces
one hour floors on both the audit window and the exit delay:

```
python scripts/verify_time_boundaries.py
```

## Notes for reviewers

- Line 1 of the contract is the pinned runner header. GenVM parses the entire
  leading comment block as that JSON header, so all prose lives in the module
  docstring. Adding a comment line after the header silently breaks deployment.
- `contracts/AIModelIPRegistry.py` is stored as LF and `.gitattributes` applies
  `* text=auto eol=lf` to everything. The working file, the git blob, the blob
  on `origin/main` and the source item in the deploy transaction all hash to
  `21267858...`. v1 was CRLF on disk but LF in the deploy calldata, which is why
  the two hashes differed there.
- `gen_call` on studionet rejects RLP wrapped payloads above roughly 300 bytes.
  The scripts send bare calldata for reads, which the node accepts and which is
  the same codec output before wrapping.
- studionet shares a request budget across callers and answers with
  `Rate limit exceeded: 500 requests per hour` once it is spent. The v2 script
  retries only TLS and gateway drops, never a rate limit answer, and polls
  receipts less often so a run fits in one budget.
- gltest direct mode has no EVM layer. `conftest.py` installs a small stub for
  `gl.evm` so the ledger tests can assert payout amounts. The live run is the
  proof of the real external message path.
- The explorer deployment was unavailable during the evidence run, so every
  transaction was re-verified through the studionet JSON-RPC instead. The
  explorer links are still recorded in the evidence files.
- Payouts are external EVM messages that execute after the transaction
  finalizes, so a balance sampled once right after the receipt can still be the
  pre credit value. The scenario scripts poll for the credit.

## Limits

- The audit is a classification aid, not a legal finding. The on chain penalty
  table is fixed, and the appeal path is a new dispute on the same model, not a
  contract level override.
- Evidence URLs must be publicly reachable by validators. If both sides are
  unreachable the audit reverts as transient and the dispute can be expired
  after the deadline.
- A model that lost a dispute stays registered with its reduced deposit, a
  `FLAGGED` status and an incremented `disputes_lost` counter. Deregistration
  stays available after the cooldown.
- The contract has no upgrade path. A new version means a new deployment and a
  new identity commitment.
- Every payable refusal is recoverable by the sender. A value transfer that is
  not a contract call at all (a plain native transfer to the contract address)
  is not attributed to any sender; `check_invariants` reports it as a surplus
  and `record_or_recover_unaccounted_value` can turn it into a refund credit
  for a chosen address. That is the only reason the owner has any value moving
  function, and it cannot touch a bond or an existing credit.
- An external payout message that fails at the platform layer cannot be
  observed by the contract. The live run proves the path credits correctly; a
  delivery failure would leave the value in the ghost and show up as a surplus.
