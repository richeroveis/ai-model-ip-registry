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

## Live deployment (studionet)

| Item | Value |
| --- | --- |
| Network | studionet, chain id 61999 |
| Contract | `0xa8a2B23e69D2093fE6f496e59367974572775833` |
| Deploy tx | `0x8dbcc7bdb30289929812925b76481c480d53f0d1e998f0654801a6e17c2fca8c` |
| Contract file sha256 | `a42d2a96aafaf7967ee5e4070fc13ec2cc2bf5dd3abfa40baf1e77be091c35f1` |
| Runner | `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` |
| Source binding | tag `studionet-v1` in this repository |
| Live audit result | `EXACT_PLAGIARISM`, challenger `170000000000000000` wei, creator `30000000000000000` wei |
| Wallet payout delta | exactly `170000000000000000` wei, credited to the challenger wallet |
| Accounting invariant | `total_locked = 30000000000000000 = contract balance`, `solvency_gap = 0` |
| Time boundaries | early finalize and early expire revert; late finalize and late expire refund exactly, final locked `130000000000000000` wei |

Transaction level evidence, including the four way identity read back from the
contract and the one hour time boundary run, is in
[docs/evidence.md](docs/evidence.md).

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
or a committed identity. There is no unilateral override anywhere in the
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

Views:

| Method | Purpose |
| --- | --- |
| `get_config` | Parameters, counters, split table |
| `get_model` / `get_model_ids` | Model records and index |
| `get_dispute` / `get_dispute_ids` | Dispute records and index |
| `get_dispute_evidence` | Both evidence sets side by side |
| `get_four_way_identity` | Re-derived identity and validity |
| `verify_sha256_identity` | Compare a supplied four tuple |
| `check_invariants` | Full ledger and identity sweep |

## Repository layout

```
contracts/AIModelIPRegistry.py     the contract
tests/test_registry.py             registration, validation, lifecycle, owner surface
tests/test_settlement.py           audit, mutual settlement, expiry, invariants
tests/support.py                   direct mode harness and chain hook
conftest.py                        Windows fd0 fix and the direct mode EVM stub
scripts/deploy_studionet.py        deploy and drive the live scenario
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

Direct tests (98 tests, no network):

```
python -m pytest -q -p no:cacheprovider
```

Lint and static validation:

```
genvm-lint check contracts/AIModelIPRegistry.py
```

Expected output: `Lint passed (3 checks)` and `Validation passed, 24 methods
(9 view, 15 write)`.

Deploy and drive the live scenario. The script reads `GENLAYER_PRIVATE_KEY`
from this project's own `.env`, never prints it, and writes
`deployment/studionet.json`:

```
python scripts/deploy_studionet.py
python scripts/verify_receipts.py
```

The time boundary script runs for about an hour because the contract enforces
one hour floors on both the audit window and the exit delay:

```
python scripts/verify_time_boundaries.py
```

## Notes for reviewers

- Line 1 of the contract is the pinned runner header. GenVM parses the entire
  leading comment block as that JSON header, so all prose lives in the module
  docstring. Adding a comment line after the header silently breaks deployment.
- `contracts/AIModelIPRegistry.py` is stored byte for byte as deployed,
  including CRLF line endings. `.gitattributes` exempts that one file from EOL
  normalization so its blob keeps the sha256 above; an LF normalized copy would
  hash differently. Every other text file is normalized to LF.
- `gen_call` on studionet rejects RLP wrapped payloads above roughly 300 bytes.
  `scripts/deploy_studionet.py` sends bare calldata for reads, which the node
  accepts and which is the same codec output before wrapping.
- gltest direct mode has no EVM layer. `conftest.py` installs a small stub for
  `gl.evm` so the ledger tests can assert payout amounts. The live run is the
  proof of the real external message path.
- The explorer deployment was paused during the evidence run, so every
  transaction was re-verified through the studionet JSON-RPC instead. The
  explorer links are still recorded in the evidence files.
- Payouts are external EVM messages that execute after the transaction
  finalizes, so a balance sampled once right after the receipt can still be the
  pre credit value. The scenario scripts poll for the credit and, where
  studionet does not serve historical balances, prove it from the triggered
  message.

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
- On studionet, a payable call that reverts keeps the value in the contract's
  ghost. Clients and scripts must not attach value to a call they expect to
  fail. `check_invariants` reports any such surplus as an invariant violation.
