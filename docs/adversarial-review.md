# Adversarial review

Scope: `contracts/AIModelIPRegistry.py` at sha256
`21267858bb1b4f6f08fa339bcc2380339664b2834b2fc2d8b66d9aeca13f6eae`, the direct
suite in `tests/`, and the live studionet runs. Written after the code was
finished, then used to change it. This revision covers the steward review from
Joaquin on the v1 payable paths.

## State machine

| Status | Who may act | Transition | Funds |
| --- | --- | --- | --- |
| Model ACTIVE | creator: top up, update listing, request exit; anyone but creator: open dispute; owner: parameters, pause, ownership | PENDING_EXIT, FLAGGED | deposit locked |
| Model PENDING_EXIT | creator: cancel, finalize after unlock, top up, update listing; anyone but creator: open dispute | ACTIVE, DEREGISTERED, FLAGGED | deposit locked |
| Model FLAGGED | creator: top up, update listing, request exit; anyone but creator: open dispute | PENDING_EXIT, FLAGGED | reduced deposit locked |
| Model DEREGISTERED | none | terminal | zero deposit |
| Dispute OPEN | parties: audit before deadline, propose settlement; other party: countersign; anyone: expire at or after deadline | SETTLED_CONSENSUS, SETTLED_MUTUAL, EXPIRED | challenger stake plus model deposit locked |
| Dispute SETTLED_CONSENSUS / SETTLED_MUTUAL | none | terminal | creator leg re-locked, challenger leg paid |
| Dispute EXPIRED | none | terminal | stake refunded, deposit untouched |

## Liveness

- Every locked status has a permissionless or counterparty path out. An OPEN
  dispute can always be expired by anyone once the deadline passes, even if both
  evidence sets are unreachable and the audit cannot run. An audit can be called
  by either party, so a disappeared challenger does not block a creator.
- A model in PENDING_EXIT cannot finalize while a dispute is open, but the
  dispute itself always terminates, so the exit is delayed, never blocked.
- Accepted trade-off: if a creator loses their key, the deposit stays locked.
  There is no admin override by design; an owner key that could move escrow
  would be a larger risk than the lock-up.

## Races

| Pair | Mutual exclusion | Test |
| --- | --- | --- |
| audit vs expire | audit requires `now < deadline`, expire requires `now >= deadline`; both also require status OPEN | `test_audit_and_expiry_guards_are_exclusive_at_the_deadline`, `test_expire_is_allowed_exactly_at_the_deadline` |
| audit vs countersign | both enter `_apply_settlement`, which rejects a non OPEN status and checks the model index | `test_audit_and_mutual_settlement_are_exclusive` |
| audit vs audit | same status guard | `test_audit_cannot_settle_twice` |
| finalize exit vs open dispute | finalize requires the model index empty, open dispute requires the model not deregistered; whichever runs second reverts | `test_exit_blocked_by_open_dispute`, `test_open_dispute_rejects_deregistered_model` |
| propose vs propose | version increases, countersign must present the current version and digest | `test_countersign_rejects_stale_version` |
| settle vs propose | the second call sees a non OPEN dispute | `test_audit_and_mutual_settlement_are_exclusive` |

## Accounting

Invariant: the contract balance equals the locked bonds plus the refundable
custody. Locked bonds are the sum of model deposits plus the sum of open
dispute stakes. Refundable custody is the sum of the per address credits. Every
settlement conserves the pot exactly:
`challenger = pot * bps // 10000`, `creator = pot - challenger`. The creator leg
is re-locked, only the challenger leg leaves. `_pay` ignores non positive
amounts, so a zero leg cannot create a phantom credit. The settlement function
refuses to run if the model deposit fell below the snapshot taken at dispute
open. `check_invariants` recomputes all of it, re-derives every identity, checks
the identity index, sums the refund ledger, checks the `vault_custody_balance`
mirror against the ghost, and treats any difference between the ghost balance
and `locked + refundable`, in either direction, as an error.

Tests: `test_invariants_across_full_lifecycle`,
`test_locked_escrow_tracks_the_ledger_at_every_step`,
`test_repeated_disputes_cannot_overdraw`,
`test_invariants_flag_an_unaccounted_surplus`,
`test_mixed_sequence_keeps_full_accounting`, and the invariant assertions inside
every settlement test.

## Payable failure paths

Value moves before the method body runs, so no payable method may raise while
value is attached. The payable surface is exactly `register_model`,
`top_up_stake` and `open_dispute`. Each validates first and, on refusal, credits
the whole attached amount to `refundable_balances[sender]`, records the reason
in `last_payable_rejection[sender]`, increments `rejected_payable_count`, and
returns `accepted = false`. Value attached to a non payable write is credited
with `VALUE_NOT_ACCEPTED` before the method returns the same rejection record.

| Failure class | Reason codes | Direct tests |
| --- | --- | --- |
| state race, paused | `PAUSED` | `test_paused_registration_is_credited`, `test_paused_dispute_is_credited`, `test_paused_top_up_is_credited` |
| duplicate identity | `DUPLICATE_MODEL_ID`, `DUPLICATE_IDENTITY` | `test_register_rejects_duplicate_model_id`, `test_register_rejects_duplicate_identity` |
| invalid input | `INVALID_MODEL_ID`, `INVALID_*_DIGEST`, `INVALID_*URL`, `INVALID_LICENSE_TERMS`, `INVALID_EVIDENCE_NOTE` | `test_invalid_model_id_is_credited`, `test_invalid_digest_is_credited`, `test_invalid_url_is_credited`, `test_invalid_challenger_evidence_is_credited` |
| wrong or missing target | `UNKNOWN_MODEL`, `DEREGISTERED_MODEL`, `SELF_DISPUTE`, `CALLER_NOT_MODEL_CREATOR` | `test_unknown_model_dispute_is_credited`, `test_self_dispute_is_credited`, `test_top_up_rejects_non_creator` |
| insufficient bond | `INSUFFICIENT_REGISTRATION_BOND`, `INSUFFICIENT_DISPUTE_BOND`, `BOND_BELOW_MODEL_DEPOSIT` | `test_insufficient_registration_bond_is_credited`, `test_open_dispute_requires_a_full_bond`, `test_dispute_rejection_then_valid_dispute_keeps_stake_exact` |
| excess value | not a refusal: exact bond locked, excess credited | `test_registration_excess_is_credited_not_locked`, `test_dispute_excess_is_credited_not_locked` |
| second dispute | `OPEN_DISPUTE_EXISTS` | `test_state_race_dispute_is_credited` |
| non payable with value | `VALUE_NOT_ACCEPTED` | `test_value_on_non_payable_method_is_credited`, `test_value_on_audit_is_credited_and_dispute_stays_open`, `test_value_on_settlement_paths_is_credited` |

Withdrawal: `withdraw_refund(amount)` and `claim_refundable_balance()` move only
the caller's credited balance through the same external EVM path used for
escrow payouts. `test_withdraw_never_touches_the_bond` proves a withdrawal
cannot consume a bond, and `test_withdraw_twice_cannot_double_pay` proves the
balance cannot be spent twice. Partial and full withdrawals are covered by
`test_partial_then_full_withdrawal`.

Live proof: six payable refusals were executed on studionet, all FINALIZED with
`SUCCESS`, and the credits were read back from the contract. Two withdrawals
paid exactly `2.2e17` and `3.9e17` wei to their wallets, and both refundable
balances returned to 0. `docs/evidence.md` sections 4 and 5.

## Input validation

- model id: 3 to 128 characters, strict charset.
- digests: 64 lowercase hex after normalising an optional `0x` prefix and upper
  case.
- URLs: https only, 12 to 512 characters, no whitespace, no credentials, no
  loopback, no IP literal, must be a fully qualified host.
- text: non empty and length capped (license 256, note 1024).
- stake: at least the configured floor; a dispute also needs at least the model
  deposit. The locked bond is exactly the required amount; any excess is
  credited back in the same transaction instead of being locked.
- time windows: constructor and owner updates enforce 3600 to 2592000 seconds
  (audit) and 3600 to 7776000 seconds (exit). Values are rejected, not clamped.
- ownership: zero address and the current owner are rejected; two step handover.

Tests: the parametrised digest and URL tests, `test_parameters_are_bounded`,
`test_register_rejects_zero_value`, `test_value_on_non_payable_method_is_credited`,
`test_open_dispute_requires_a_full_bond`, `test_ownership_is_two_step`.

## Consensus safety

- The only model output that reaches money is the tier label. The split is
  `SPLIT_TABLE[tier]`, the match count is recomputed on chain, and the audit
  digest is recomputed and compared. Tampered split, digest, count, tier and
  identity are all rejected (`test_audit_rejects_tampered_*`,
  `test_audit_recomputes_identity_from_the_record`).
- Leader and each validator run the same body: both render all four URLs and
  both call the LLM. `prompt_comparative` demands exact equality of tier, both
  bps values and match count.
- Fail closed: challenger evidence unavailable means NO_INFRINGEMENT; creator
  evidence unavailable turns NO_INFRINGEMENT into PARTIAL_DERIVATIVE; both
  unavailable is a transient revert, and the dispute can then be expired.
- Identical artifacts cannot be certified independent: 4 of 4 digest matches
  upgrade NO_INFRINGEMENT to PARTIAL_DERIVATIVE
  (`test_audit_full_digest_match_cannot_be_cleared`).
- Evidence text is labelled as untrusted data and the split is table driven, so
  injected instructions cannot move the amounts
  (`test_evidence_text_cannot_change_the_split_policy`). This is mitigation, not
  proof of injection immunity; see OPEN items.
- Direct mode only exercises the leader path. Validator behaviour is covered by
  `test_validator_agrees_and_disagrees` and
  `test_validator_rejects_leader_error`, and the live audit is the only proof of
  real validator agreement.

## Findings

FIXED:

1. Wallet payouts used an internal IC message and never credited the EOA. The
   live run showed the ghost debited and the wallet unchanged. Switched to an
   external EVM message. Evidence: live payout delta of exactly 1.7e17 wei, plus
   the payout assertions in the direct suite.
2. Audit and expiry overlapped at exactly the deadline. Audit now requires
   `now < deadline`, expiry `now >= deadline`.
   `test_audit_and_expiry_guards_are_exclusive_at_the_deadline`.
3. Four of four matching digests could be certified NO_INFRINGEMENT.
   `test_audit_full_digest_match_cannot_be_cleared`.
4. The same four digests could be registered under two model ids, which enables
   a first registrant to freeze out the true author. Duplicate composites are
   now rejected, including after deregistration.
   `test_register_rejects_duplicate_identity`,
   `test_identity_stays_unique_after_deregistration`.
5. Deadlines were read from the process clock. They now come from the
   transaction datetime in the consensus message, so every validator uses the
   same clock.
6. The consensus settlement wrote the amounts but not the `tier` field. Found on
   the first completed live audit, fixed, and the tier is now asserted in the
   three tier tests.
7. A single HTML 502 from `eth_getTransactionReceipt` could abort a run after
   the transaction had landed. The scripts now retry EVM receipt polling.
8. The prose header broke deployment silently. GenVM parses the whole leading
   comment block as the dependency header, so the documentation moved into the
   module docstring.
9. `check_invariants` computed `solvency_gap` but still returned `ok = true`
   when the contract held a surplus. A live probe that reverted a duplicate
   registration left 1e17 in the ghost and exposed it. A nonzero gap in either
   direction is now an error. `test_invariants_flag_an_unaccounted_surplus`.
10. Steward review (Joaquin): a rejected payable call reverted, the storage
    change rolled back, and the attached value stayed in the contract with no
    ledger entry and no recovery path. Every payable path now refuses without
    reverting, records the reason, and credits 100 percent of the attached value
    to the sender, who can call `withdraw_refund` or `claim_refundable_balance`
    at any time. Excess over the required bond is credited in the same
    transaction instead of being locked. New views expose the balance, the last
    reason and the whole custody identity. Tests: all 43 tests in
    `tests/test_refunds.py`, plus the rewritten refusal tests in
    `tests/test_registry.py`. Live: six refusals and two withdrawals with exact
    wallet deltas on `0xfdE499c7ef1D13BaD93c31598414E5839b6028c2`, evidence
    sections 4 and 5.

OPEN:

11. The registration deposit has no upper bound. The full bond rule makes a very
    large deposit expensive to challenge. This is the intended symmetry (a false
    accusation costs as much as a false claim), but it does mean a well funded
    registrant can make a challenge expensive. Documented, not changed.
12. A creator who loses their key cannot recover the deposit. No admin override
    by design. For the same reason, a refund credit belongs to its address: if
    the key is lost, the credit is not recoverable by anyone else.
13. Prompt injection is mitigated, not proven. Only the tier label is used and
    the amounts are deterministic, but the tier itself remains a model judgment
    that must be agreed by validators.
14. A failed external payout message cannot be observed on chain. The live run
    proves the path credits correctly, but there is no callback for a platform
    level delivery failure. Such a failure would leave the value in the ghost
    and appear as a surplus.
15. `record_or_recover_unaccounted_value` lets the owner choose the recipient of
    a measured surplus. It cannot reach a bond or an existing credit and the
    amount is capped at the surplus, but the choice of recipient is a trust
    assumption. It exists only for value that arrived outside the payable
    surface.
16. Value sent by a plain native transfer carries no sender information the
    contract can trust, so it cannot be attributed automatically. With every
    payable path now soft failing, a native transfer or a platform level payout
    failure are the only ways to create an unaccounted surplus.

## Tooling fixes

- A rejected payable call used to revert on studionet, which left the value in
  the ghost and produced no refund ledger. The contract now refuses softly, and
  the direct harness credits value only for a normal return, so the refund tests
  mirror the chain behaviour.
- studionet enforces a shared request budget (`Rate limit exceeded: 500 requests
  per hour`). Retrying a rate limit answer multiplies the problem, so the v2
  script raises it immediately and retries only TLS or gateway drops. Receipt
  polling is also less frequent so a 16 transaction run fits in one budget.
- The boundary script sampled each wallet once, right after the transaction
  receipt. External payouts execute after finalization, so it read the pre credit
  balance and stopped after a correct late finalize. `verify_time_boundaries.py`
  now polls for the credit, and `verify_boundary_closeout.py` completed the
  recorded run: the creator refund is proven by the triggered message
  `0x253f56d5f2d4675aa49d8f66584716e8c7a2be0361aa0c903df6412510135b1f`
  (value `100000000000000000`, `value_credited = true`), and the challenger
  refund is a live delta of exactly `100000000000000000`. Evidence:
  `docs/evidence.md` section 7.
- studionet returns the latest balance for every block tag, so a past balance
  cannot be sampled. Evidence for a past credit has to come from the triggered
  message or from a checkpoint recorded at the time.
