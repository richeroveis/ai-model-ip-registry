# Adversarial review

Scope: `contracts/AIModelIPRegistry.py` at sha256
`a42d2a96aafaf7967ee5e4070fc13ec2cc2bf5dd3abfa40baf1e77be091c35f1`, the direct
suite in `tests/`, and the live studionet runs. Written after the code was
finished, then used to change it.

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

Invariant: contract balance equals the sum of model deposits plus the sum of
open dispute stakes. Every settlement conserves the pot exactly:
`challenger = pot * bps // 10000`, `creator = pot - challenger`. The creator leg
is re-locked, only the challenger leg leaves. `_pay` ignores non positive
amounts, so a zero leg cannot create a phantom credit. The settlement function
refuses to run if the model deposit fell below the snapshot taken at dispute
open. `check_invariants` recomputes all of it, re-derives every identity, checks
the identity index, and treats any difference between the ghost balance and
`total_locked`, in either direction, as an error.

Tests: `test_invariants_across_full_lifecycle`,
`test_locked_escrow_tracks_the_ledger_at_every_step`,
`test_repeated_disputes_cannot_overdraw`,
`test_invariants_flag_an_unaccounted_surplus`, and the invariant assertions
inside every settlement test.

## Input validation

- model id: 3 to 128 characters, strict charset.
- digests: 64 lowercase hex after normalising an optional `0x` prefix and upper
  case.
- URLs: https only, 12 to 512 characters, no whitespace, no credentials, no
  loopback, no IP literal, must be a fully qualified host.
- text: non empty and length capped (license 256, note 1024).
- stake: at least the configured floor; a dispute also needs at least the model
  deposit.
- time windows: constructor and owner updates enforce 3600 to 2592000 seconds
  (audit) and 3600 to 7776000 seconds (exit). Values are rejected, not clamped.
- ownership: zero address and the current owner are rejected; two step handover.

Tests: the parametrised digest and URL tests, `test_parameters_are_bounded`,
`test_register_rejects_zero_value`, `test_value_on_non_payable_method_reverts`,
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

OPEN:

10. The registration deposit has no upper bound. The full bond rule makes a very
    large deposit expensive to challenge. This is the intended symmetry (a false
    accusation costs as much as a false claim), but it does mean a well funded
    registrant can make a challenge expensive. Documented, not changed.
11. A creator who loses their key cannot recover the deposit. No admin override
    by design.
12. Prompt injection is mitigated, not proven. Only the tier label is used and
    the amounts are deterministic, but the tier itself remains a model judgment
    that must be agreed by validators.
13. A failed external payout message cannot be observed on chain. The live run
    proves the path credits correctly, but there is no callback for a platform
    level delivery failure.
14. No commit hash or tag exists because the workspace is not a git repository.
    The file sha256 plus the deploy transaction are the binding used instead.
15. A reverted payable call keeps the value in the ghost on studionet and there
    is no sweep. The contract now reports it as an invariant violation, but the
    value cannot be returned to the sender without a soft-error redesign that
    records failed deposits as recoverable credits. Clients must not send value
    to a call they expect to fail.

## Tooling fixes

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
