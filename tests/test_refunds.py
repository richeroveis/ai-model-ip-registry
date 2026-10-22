"""Refund ledger, withdrawal and custody accounting tests.

Every payable entry point must refuse without reverting while value is attached,
credit the whole attached amount to the sender, and keep the custody identity:

    contract balance == locked bonds + refundable custody

These tests drive each failure path named in the steward review.
"""

import pytest

from support import (
    CLAIM_FULL,
    CLAIM_NONE,
    CLAIM_PARTIAL,
    CREATOR_BENCH,
    CREATOR_META,
    Harness,
    addr_hex,
    reverted,
)


def refund_of(h, who):
    return h.registry.get_refundable_balance(addr_hex(who))["refundable_balance"]


def assert_custody(h):
    summary = h.registry.get_custody_accounting_summary()
    assert summary["vault_custody_balance"] == h.balance()
    assert summary["contract_balance"] == h.balance()
    assert summary["unaccounted_surplus"] == 0
    assert summary["royalty_escrow"] == 0
    assert summary["accounting_identity_ok"] is True
    assert (
        summary["vault_custody_balance"]
        == summary["active_registration_bonds"]
        + summary["active_dispute_bonds"]
        + summary["royalty_escrow"]
        + summary["total_refundable_custody"]
    )
    invariants = h.registry.check_invariants(100)
    assert invariants["ok"] is True, invariants["errors"]
    assert invariants["solvency_gap"] == 0
    return summary


def test_duplicate_registration_is_credited_and_withdrawable(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)

    rejected = h.register(direct_alice)
    assert rejected["accepted"] is False
    assert rejected["reason"] == "DUPLICATE_MODEL_ID"
    assert rejected["refund_credited"] == 1000
    assert refund_of(h, direct_alice) == 1000
    assert h.balance() == 2000
    assert_custody(h)

    h.as_(direct_alice)
    result = h.call(h.registry.withdraw_refund, 0)
    assert result["withdrawn"] == 1000
    assert result["remaining"] == 0
    assert refund_of(h, direct_alice) == 0
    assert h.chain.transfers_to(direct_alice) == 1000
    assert h.balance() == 1000
    summary = assert_custody(h)
    assert summary["total_refunded_out"] == 1000
    assert summary["locked_bonds"] == 1000


def test_withdraw_refund_default_argument_withdraws_everything(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.register(direct_alice)

    h.as_(direct_alice)
    result = h.call(h.registry.withdraw_refund)
    assert result["accepted"] is True
    assert result["withdrawn"] == 1000
    assert refund_of(h, direct_alice) == 0


def test_claim_refundable_balance_alias(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.register(direct_alice)

    h.as_(direct_alice)
    result = h.call(h.registry.claim_refundable_balance)
    assert result["withdrawn"] == 1000
    assert refund_of(h, direct_alice) == 0


def test_insufficient_registration_bond_is_credited(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    rejected = h.register(direct_alice, stake=400)
    assert rejected["accepted"] is False
    assert rejected["reason"] == "INSUFFICIENT_REGISTRATION_BOND"
    assert rejected["refund_credited"] == 400
    assert refund_of(h, direct_alice) == 400
    assert h.registry.get_config()["model_count"] == 0

    h.as_(direct_alice)
    h.call(h.registry.withdraw_refund, 0)
    assert h.chain.transfers_to(direct_alice) == 400
    assert_custody(h)


def test_paused_registration_is_credited(direct_vm, direct_deploy, direct_alice, direct_owner):
    h = Harness(direct_vm, direct_deploy)
    h.as_(direct_owner)
    h.call(h.registry.set_paused, True)

    rejected = h.register(direct_alice)
    assert rejected["reason"] == "PAUSED"
    assert rejected["refund_credited"] == 1000
    assert refund_of(h, direct_alice) == 1000
    assert h.registry.get_config()["model_count"] == 0
    assert_custody(h)

    h.as_(direct_alice)
    h.call(h.registry.withdraw_refund, 0)
    assert h.chain.transfers_to(direct_alice) == 1000


def test_paused_dispute_is_credited(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_owner)
    h.call(h.registry.set_paused, True)

    rejected = h.dispute(direct_bob)
    assert rejected["reason"] == "PAUSED"
    assert rejected["refund_credited"] == 1000
    assert refund_of(h, direct_bob) == 1000
    assert h.registry.get_config()["dispute_count"] == 0

    h.as_(direct_bob)
    h.call(h.registry.withdraw_refund, 0)
    assert h.chain.transfers_to(direct_bob) == 1000
    assert h.registry.get_config()["total_locked"] == 1000
    assert_custody(h)


def test_paused_top_up_is_credited(direct_vm, direct_deploy, direct_alice, direct_owner):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_owner)
    h.call(h.registry.set_paused, True)

    h.as_(direct_alice)
    rejected = h.call(h.registry.top_up_stake, "model-1", value=300)
    assert rejected["reason"] == "PAUSED"
    assert rejected["refund_credited"] == 300
    assert h.registry.get_model("model-1")["deposit"] == 1000
    assert_custody(h)


def test_invalid_model_id_is_credited(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    rejected = h.register(direct_alice, model_id="bad id")
    assert rejected["reason"] == "INVALID_MODEL_ID"
    assert rejected["refund_credited"] == 1000
    assert refund_of(h, direct_alice) == 1000


@pytest.mark.parametrize(
    "digests, reason",
    [
        (("", "b" * 64, "c" * 64, "d" * 64), "INVALID_WEIGHTS_DIGEST"),
        (("a" * 64, "z" * 64, "c" * 64, "d" * 64), "INVALID_DATASET_DIGEST"),
        (("a" * 64, "b" * 64, "c" * 63, "d" * 64), "INVALID_CONFIG_DIGEST"),
        (("a" * 64, "b" * 64, "c" * 64, "not-a-digest"), "INVALID_CARD_DIGEST"),
    ],
)
def test_invalid_digest_is_credited(direct_vm, direct_deploy, direct_alice, digests, reason):
    h = Harness(direct_vm, direct_deploy)
    rejected = h.register(direct_alice, digests=digests)
    assert rejected["reason"] == reason
    assert rejected["refund_credited"] == 1000
    assert_custody(h)


def test_invalid_url_is_credited(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    rejected = h.register(direct_alice, metadata="http://insecure.example.org/card.json")
    assert rejected["reason"] == "INVALID_METADATA_URL"
    assert rejected["refund_credited"] == 1000

    rejected = h.register(direct_alice, benchmark="https://localhost/bench.json")
    assert rejected["reason"] == "INVALID_BENCHMARK_URL"
    assert rejected["refund_credited"] == 1000
    assert refund_of(h, direct_alice) == 2000
    assert_custody(h)


def test_invalid_license_terms_is_credited(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    rejected = h.register(direct_alice, license_terms="   ")
    assert rejected["reason"] == "INVALID_LICENSE_TERMS"
    assert rejected["refund_credited"] == 1000


def test_unknown_model_dispute_is_credited(direct_vm, direct_deploy, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    rejected = h.dispute(direct_bob, model_id="missing-model")
    assert rejected["reason"] == "UNKNOWN_MODEL"
    assert rejected["refund_credited"] == 1000
    assert refund_of(h, direct_bob) == 1000
    assert_custody(h)


def test_self_dispute_is_credited(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    rejected = h.dispute(direct_alice)
    assert rejected["reason"] == "SELF_DISPUTE"
    assert rejected["refund_credited"] == 1000
    assert_custody(h)


def test_state_race_dispute_is_credited(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.dispute(direct_bob)

    # The model already has an open dispute, so a second claim is refused and
    # its full value is credited instead of being trapped.
    rejected = h.dispute(direct_bob, claim=CLAIM_FULL)
    assert rejected["reason"] == "OPEN_DISPUTE_EXISTS"
    assert rejected["refund_credited"] == 1000
    assert refund_of(h, direct_bob) == 1000
    assert h.registry.get_config()["dispute_count"] == 1
    assert_custody(h)


def test_invalid_challenger_evidence_is_credited(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)

    rejected = h.dispute(direct_bob, metadata="ftp://challenger.example.org/card.json")
    assert rejected["reason"] == "INVALID_CHALLENGER_METADATA_URL"
    assert rejected["refund_credited"] == 1000

    rejected = h.dispute(direct_bob, note="   ")
    assert rejected["reason"] == "INVALID_EVIDENCE_NOTE"
    assert rejected["refund_credited"] == 1000
    assert refund_of(h, direct_bob) == 2000
    assert_custody(h)


def test_registration_excess_is_credited_not_locked(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    result = h.register(direct_alice, stake=1500)
    assert result["accepted"] is True
    assert result["deposit"] == 1000
    assert result["refund_credited"] == 500
    assert h.registry.get_model("model-1")["deposit"] == 1000
    assert h.registry.get_config()["total_locked"] == 1000
    assert refund_of(h, direct_alice) == 500
    summary = assert_custody(h)
    assert summary["vault_custody_balance"] == 1500

    h.as_(direct_alice)
    h.call(h.registry.withdraw_refund, 0)
    assert h.chain.transfers_to(direct_alice) == 500
    assert h.registry.get_model("model-1")["deposit"] == 1000
    assert_custody(h)


def test_dispute_excess_is_credited_not_locked(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    info = h.dispute(direct_bob, stake=1200)
    assert info["accepted"] is True
    assert info["challenger_stake"] == 1000
    assert info["refund_credited"] == 200
    assert h.registry.get_dispute(info["dispute_id"])["challenger_stake"] == 1000
    assert h.registry.get_config()["total_locked"] == 2000
    assert refund_of(h, direct_bob) == 200

    h.as_(direct_bob)
    h.call(h.registry.withdraw_refund, 0)
    assert h.chain.transfers_to(direct_bob) == 200
    assert_custody(h)


def test_top_up_locks_the_whole_amount(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_alice)
    result = h.call(h.registry.top_up_stake, "model-1", value=250)
    assert result["accepted"] is True
    assert result["refund_credited"] == 0
    assert result["deposit"] == 1250
    assert refund_of(h, direct_alice) == 0
    assert_custody(h)


def test_partial_then_full_withdrawal(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.register(direct_alice)

    h.as_(direct_alice)
    first = h.call(h.registry.withdraw_refund, 400)
    assert first["withdrawn"] == 400
    assert first["remaining"] == 600
    assert refund_of(h, direct_alice) == 600

    second = h.call(h.registry.withdraw_refund, 0)
    assert second["withdrawn"] == 600
    assert second["remaining"] == 0
    assert h.chain.transfers_to(direct_alice) == 1000
    assert len(h.chain.transfers) == 2
    assert_custody(h)


def test_withdraw_without_balance_reverts(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.as_(direct_alice)
    with reverted(direct_vm, "no refundable balance"):
        h.call(h.registry.withdraw_refund, 0)


def test_withdraw_above_balance_reverts(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.register(direct_alice)

    h.as_(direct_alice)
    with reverted(direct_vm, "withdrawal exceeds refundable balance"):
        h.call(h.registry.withdraw_refund, 1001)
    assert refund_of(h, direct_alice) == 1000


def test_withdraw_never_touches_the_bond(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.register(direct_alice)

    h.as_(direct_alice)
    h.call(h.registry.withdraw_refund, 0)
    assert h.balance() == 1000
    assert h.registry.get_model("model-1")["deposit"] == 1000
    assert_custody(h)

    # The original bond still leaves escrow through the normal exit path.
    h.call(h.registry.request_deregistration, "model-1")
    direct_vm.warp("2026-01-01T01:00:01Z")
    result = h.call(h.registry.finalize_deregistration, "model-1")
    assert result["refunded"] == 1000
    assert h.chain.transfers_to(direct_alice) == 2000
    assert h.balance() == 0
    assert_custody(h)


def test_withdraw_twice_cannot_double_pay(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.register(direct_alice)

    h.as_(direct_alice)
    h.call(h.registry.withdraw_refund, 0)
    with reverted(direct_vm, "no refundable balance"):
        h.call(h.registry.withdraw_refund, 0)
    assert len(h.chain.transfers) == 1


def test_rejected_payable_emits_no_partial_state(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    before = h.registry.get_config()

    rejected = h.register(direct_alice, metadata="https://localhost/card.json")
    assert rejected["accepted"] is False

    after = h.registry.get_config()
    assert after["model_count"] == before["model_count"]
    assert after["dispute_count"] == before["dispute_count"]
    assert after["total_locked"] == before["total_locked"]
    assert after["total_paid_out"] == before["total_paid_out"]
    assert h.registry.get_model_ids() == []


def test_last_payable_rejection_is_recorded(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)

    h.register(direct_alice)
    view = h.registry.get_last_payable_rejection(addr_hex(direct_alice))
    assert view["reason"] == "DUPLICATE_MODEL_ID"
    assert view["refundable_balance"] == 1000

    h.register(direct_alice, model_id="model-2", metadata="http://insecure.example.org/card.json")
    view = h.registry.get_last_payable_rejection(addr_hex(direct_alice))
    assert view["reason"] == "INVALID_METADATA_URL"
    assert view["refundable_balance"] == 2000
    assert view["total_rejections"] == 2

    # A zero value rejection is still recorded, with no credit.
    h.register(direct_alice, model_id="model-3", stake=0)
    view = h.registry.get_last_payable_rejection(addr_hex(direct_alice))
    assert view["reason"] == "INSUFFICIENT_REGISTRATION_BOND"
    assert view["refundable_balance"] == 2000
    assert view["total_rejections"] == 3


def test_rejection_count_spans_every_payable_path(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.register(direct_alice, digests=CLAIM_NONE)          # duplicate model id
    h.dispute(direct_bob, model_id="missing-model")       # unknown model
    h.dispute(direct_bob, stake=1)                        # dispute bond

    config = h.registry.get_config()
    assert config["rejected_payable_count"] == 3
    assert h.registry.get_last_payable_rejection(addr_hex(direct_bob))["reason"] == \
        "INSUFFICIENT_DISPUTE_BOND"
    assert_custody(h)


def test_retry_after_a_rejection_succeeds(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    rejected = h.register(direct_alice, metadata="http://insecure.example.org/card.json")
    assert rejected["accepted"] is False

    accepted = h.register(direct_alice)
    assert accepted["accepted"] is True
    assert accepted["deposit"] == 1000
    assert refund_of(h, direct_alice) == 1000
    assert h.balance() == 2000
    assert_custody(h)


def test_value_on_audit_is_credited_and_dispute_stays_open(direct_vm, direct_deploy,
                                                           direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    dispute_id = h.dispute(direct_bob)["dispute_id"]

    h.as_(direct_bob)
    result = h.call(h.registry.audit_model_similarity_live, dispute_id, value=7)
    assert result["accepted"] is False
    assert result["reason"] == "VALUE_NOT_ACCEPTED"
    assert result["refund_credited"] == 7
    assert h.registry.get_dispute(dispute_id)["status"] == "OPEN"
    assert h.chain.transfers == []
    assert_custody(h)


def test_value_on_settlement_paths_is_credited(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    dispute_id = h.dispute(direct_bob)["dispute_id"]

    h.as_(direct_alice)
    result = h.call(h.registry.propose_settlement, dispute_id, 5000, 5000, "even", value=9)
    assert result["reason"] == "VALUE_NOT_ACCEPTED"
    assert h.registry.get_dispute(dispute_id)["proposal_version"] == 0

    h.as_(direct_bob)
    result = h.call(h.registry.countersign_settlement, dispute_id, 0, "0" * 64, value=9)
    assert result["reason"] == "VALUE_NOT_ACCEPTED"
    assert h.registry.get_dispute(dispute_id)["status"] == "OPEN"
    assert refund_of(h, direct_bob) == 9


def test_value_on_owner_methods_is_credited(direct_vm, direct_deploy, direct_owner):
    h = Harness(direct_vm, direct_deploy)
    h.as_(direct_owner)
    result = h.call(h.registry.set_paused, True, value=11)
    assert result["reason"] == "VALUE_NOT_ACCEPTED"
    assert result["refund_credited"] == 11
    assert h.registry.get_config()["paused"] is False
    assert refund_of(h, direct_owner) == 11
    assert_custody(h)


def test_custody_summary_reports_every_term(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.dispute(direct_bob)

    summary = assert_custody(h)
    assert summary["active_registration_bonds"] == 1000
    assert summary["active_dispute_bonds"] == 1000
    assert summary["locked_bonds"] == 2000
    assert summary["royalty_escrow"] == 0
    assert summary["total_refundable_custody"] == 0
    assert summary["vault_custody_balance"] == 2000
    assert summary["rejected_payable_count"] == 0


def test_mixed_sequence_keeps_full_accounting(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)

    h.register(direct_alice)
    assert_custody(h)

    h.register(direct_alice, stake=1500)          # duplicate id, refund 1500
    assert_custody(h)
    assert refund_of(h, direct_alice) == 1500

    h.register(direct_alice, model_id="model-2",
               digests=("1" * 64, "2" * 64, "3" * 64, "4" * 64), stake=1000)
    assert_custody(h)

    h.dispute(direct_bob, model_id="model-2", stake=1200)   # 200 excess
    assert_custody(h)
    assert refund_of(h, direct_bob) == 200

    h.dispute(direct_bob, model_id="model-1", stake=1000)
    assert_custody(h)

    h.as_(direct_bob)
    h.call(h.registry.withdraw_refund, 0)
    assert refund_of(h, direct_bob) == 0
    summary = assert_custody(h)
    assert summary["total_refunded_out"] == 200
    assert summary["locked_bonds"] == 4000


def test_unaccounted_surplus_can_be_recovered(direct_vm, direct_deploy, direct_alice, direct_owner):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)

    # Simulate value that reached the contract with no ledger entry, which is
    # what the old revert path left behind.
    key = bytes(h.address.as_bytes)
    direct_vm._balances[key] = direct_vm._balances.get(key, 0) + 250
    invariants = h.registry.check_invariants(100)
    assert invariants["ok"] is False
    assert invariants["solvency_gap"] == 250

    h.as_(direct_owner)
    result = h.call(h.registry.record_or_recover_unaccounted_value, addr_hex(direct_alice))
    assert result["accepted"] is True
    assert result["recovered"] == 250
    assert refund_of(h, direct_alice) == 250
    assert_custody(h)

    h.as_(direct_alice)
    h.call(h.registry.withdraw_refund, 0)
    assert h.chain.transfers_to(direct_alice) == 250
    assert_custody(h)


def test_unaccounted_recovery_requires_owner(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    key = bytes(h.address.as_bytes)
    direct_vm._balances[key] = direct_vm._balances.get(key, 0) + 250

    h.as_(direct_alice)
    with reverted(direct_vm, "caller is not the owner"):
        h.call(h.registry.record_or_recover_unaccounted_value, addr_hex(direct_alice))


def test_unaccounted_recovery_cannot_touch_locked_or_credited_value(direct_vm, direct_deploy,
                                                                    direct_alice, direct_owner):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.register(direct_alice)                 # 1000 credited to alice
    key = bytes(h.address.as_bytes)
    direct_vm._balances[key] = direct_vm._balances.get(key, 0) + 100

    h.as_(direct_owner)
    first = h.call(h.registry.record_or_recover_unaccounted_value, addr_hex(direct_owner))
    assert first["recovered"] == 100

    second = h.call(h.registry.record_or_recover_unaccounted_value, addr_hex(direct_owner))
    assert second["accepted"] is False
    assert second["reason"] == "NO_UNACCOUNTED_SURPLUS"
    assert second["recovered"] == 0

    assert h.registry.get_config()["total_locked"] == 1000
    assert refund_of(h, direct_alice) == 1000
    assert refund_of(h, direct_owner) == 100
    assert_custody(h)


def test_unaccounted_recovery_without_surplus_is_a_noop(direct_vm, direct_deploy, direct_owner):
    h = Harness(direct_vm, direct_deploy)
    h.as_(direct_owner)
    result = h.call(h.registry.record_or_recover_unaccounted_value, addr_hex(direct_owner))
    assert result["accepted"] is False
    assert result["reason"] == "NO_UNACCOUNTED_SURPLUS"
    assert h.chain.transfers == []
    assert_custody(h)


def test_unaccounted_recovery_rejects_bad_address(direct_vm, direct_deploy, direct_owner):
    h = Harness(direct_vm, direct_deploy)
    h.as_(direct_owner)
    with reverted(direct_vm, "address"):
        h.call(h.registry.record_or_recover_unaccounted_value, "not-an-address")


def test_third_party_balance_view_is_zero(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.register(direct_alice)
    assert refund_of(h, direct_bob) == 0
    assert h.registry.get_last_payable_rejection(addr_hex(direct_bob))["reason"] == ""


def test_refund_credits_accumulate_across_rejections(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.register(direct_alice)                                            # duplicate id
    h.register(direct_alice, model_id="model-2",
               digests=("1" * 64, "2" * 64, "3" * 64, "4" * 64), stake=700)
    h.register(direct_alice, model_id="model-3",
               digests=("5" * 64, "6" * 64, "7" * 64, "8" * 64),
               metadata="ftp://insecure.example.org/card.json")
    assert refund_of(h, direct_alice) == 1000 + 700 + 1000
    assert_custody(h)

    h.as_(direct_alice)
    result = h.call(h.registry.withdraw_refund, 0)
    assert result["withdrawn"] == 2700
    assert h.chain.transfers_to(direct_alice) == 2700
    assert_custody(h)


def test_dispute_rejection_then_valid_dispute_keeps_stake_exact(direct_vm, direct_deploy,
                                                               direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)

    rejected = h.dispute(direct_bob, stake=500)
    assert rejected["reason"] == "INSUFFICIENT_DISPUTE_BOND"
    assert refund_of(h, direct_bob) == 500

    info = h.dispute(direct_bob, claim=CLAIM_PARTIAL, stake=1000)
    assert info["accepted"] is True
    assert info["challenger_stake"] == 1000
    assert h.registry.get_dispute(info["dispute_id"])["challenger_stake"] == 1000
    assert refund_of(h, direct_bob) == 500
    assert_custody(h)
