"""Registration, identity, listing, exit and owner surface tests."""

import hashlib

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


def test_register_model_stores_four_way_identity(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    result = h.register(direct_alice)

    model = h.registry.get_model("model-1")
    assert addr_hex(model["creator"]) == addr_hex(direct_alice)
    assert model["status"] == "ACTIVE"
    assert model["deposit"] == 1000
    assert model["total_staked"] == 1000
    assert model["weights_sha256"] == "a" * 64
    assert model["dataset_sha256"] == "b" * 64
    assert model["config_sha256"] == "c" * 64
    assert model["card_sha256"] == "d" * 64
    assert result["identity_digest"] == model["identity_digest"]

    identity = h.registry.get_four_way_identity("model-1")
    assert identity["identity_valid"] is True
    assert identity["stored_identity_digest"] == identity["derived_identity_digest"]

    config = h.registry.get_config()
    assert config["total_locked"] == 1000
    assert config["model_count"] == 1


def test_identity_digest_matches_manual_sha256(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)

    canonical = "AI-IP-IDENTITY-V1|" + "|".join(["a" * 64, "b" * 64, "c" * 64, "d" * 64])
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    model = h.registry.get_model("model-1")
    assert model["identity_digest"] == expected


def test_verify_sha256_identity_detects_single_slot_tamper(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)

    full = h.registry.verify_sha256_identity("model-1", "a" * 64, "b" * 64, "c" * 64, "d" * 64)
    assert full["full_identity"] is True
    assert full["digest_identity"] is True
    assert full["match_count"] == 4

    tampered = h.registry.verify_sha256_identity("model-1", "a" * 64, "b" * 64, "c" * 64, "e" * 64)
    assert tampered["full_identity"] is False
    assert tampered["partial_identity"] is True
    assert tampered["match_count"] == 3
    assert tampered["slot_matches"]["card_sha256"] is False
    assert tampered["digest_identity"] is False


def test_four_way_identity_is_slot_order_sensitive(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)

    swapped = h.registry.verify_sha256_identity("model-1", "b" * 64, "a" * 64, "c" * 64, "d" * 64)
    assert swapped["match_count"] == 2
    assert swapped["slot_matches"]["weights_sha256"] is False
    assert swapped["slot_matches"]["dataset_sha256"] is False
    assert swapped["digest_identity"] is False


def test_register_normalizes_uppercase_and_prefix(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.as_(direct_alice)
    h.call(
        h.registry.register_model,
        "model-1",
        "0x" + "A" * 64,
        "B" * 64,
        "C" * 64,
        "D" * 64,
        CREATOR_META,
        CREATOR_BENCH,
        "apache-2.0",
        value=1000,
    )
    model = h.registry.get_model("model-1")
    assert model["weights_sha256"] == "a" * 64
    assert model["identity_digest"] == h.registry.get_four_way_identity("model-1")["derived_identity_digest"]


def test_register_rejects_stake_below_minimum(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    with reverted(direct_vm, "stake below minimum registration stake"):
        h.register(direct_alice, stake=999)
    assert h.registry.get_config()["model_count"] == 0


def test_register_rejects_duplicate_model_id(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    with reverted(direct_vm, "model id already registered"):
        h.register(direct_alice)
    assert h.registry.get_config()["model_count"] == 1


def test_register_rejects_duplicate_identity(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)

    # The same four artifacts cannot be claimed under a second model id.
    with reverted(direct_vm, "identity is already registered"):
        h.register(direct_bob, model_id="model-2")
    assert h.registry.get_config()["model_count"] == 1
    assert h.registry.get_config()["total_locked"] == 1000


def test_identity_stays_unique_after_deregistration(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_alice)
    h.call(h.registry.request_deregistration, "model-1")
    direct_vm.warp("2026-01-01T01:00:01Z")
    h.call(h.registry.finalize_deregistration, "model-1")

    with reverted(direct_vm, "identity is already registered"):
        h.register(direct_bob, model_id="model-2")
    assert h.registry.get_config()["model_count"] == 1


@pytest.mark.parametrize(
    "digests",
    [
        ("a" * 63, "b" * 64, "c" * 64, "d" * 64),
        ("a" * 65, "b" * 64, "c" * 64, "d" * 64),
        ("z" * 64, "b" * 64, "c" * 64, "d" * 64),
        ("", "b" * 64, "c" * 64, "d" * 64),
    ],
)
def test_register_rejects_malformed_digest(direct_vm, direct_deploy, direct_alice, digests):
    h = Harness(direct_vm, direct_deploy)
    with reverted(direct_vm, "[EXPECTED]"):
        h.register(direct_alice, digests=digests)


@pytest.mark.parametrize(
    "url",
    [
        "http://creator.example.org/card.json",
        "https://localhost/card.json",
        "https://127.0.0.1/card.json",
        "https://user:pass@creator.example.org/card.json",
        "https://creator.example.org/card.json with space",
        "https://short/x",
        "https://",
    ],
)
def test_register_rejects_unsafe_url(direct_vm, direct_deploy, direct_alice, url):
    h = Harness(direct_vm, direct_deploy)
    with reverted(direct_vm, "metadata url"):
        h.register(direct_alice, metadata=url)


@pytest.mark.parametrize("model_id", ["a", "ab", "bad id", "bad/id", "bad$id", ""])
def test_register_rejects_bad_model_id(direct_vm, direct_deploy, direct_alice, model_id):
    h = Harness(direct_vm, direct_deploy)
    with reverted(direct_vm, "model id"):
        h.register(direct_alice, model_id=model_id)


def test_register_rejects_zero_value(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    with reverted(direct_vm, "stake below minimum registration stake"):
        h.register(direct_alice, stake=0)


def test_top_up_increases_deposit_and_locked(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)

    h.as_(direct_alice)
    result = h.call(h.registry.top_up_stake, "model-1", value=250)
    assert result["deposit"] == 1250

    model = h.registry.get_model("model-1")
    assert model["deposit"] == 1250
    assert model["total_staked"] == 1250
    assert h.registry.get_config()["total_locked"] == 1250


def test_top_up_rejects_non_creator(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_bob)
    with reverted(direct_vm, "caller is not the model creator"):
        h.call(h.registry.top_up_stake, "model-1", value=250)


def test_top_up_rejects_zero_value(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_alice)
    with reverted(direct_vm, "top up must be positive"):
        h.call(h.registry.top_up_stake, "model-1", value=0)


def test_invalid_message_datetime_fails_closed(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_alice)

    # The transaction datetime is the only clock the contract trusts, and the
    # registration path reads it before it writes any state.
    direct_vm.warp("not-a-date")
    with reverted(direct_vm, "transaction datetime is invalid"):
        h.register(direct_alice, model_id="model-2", digests=CLAIM_NONE)
    assert h.registry.get_config()["model_count"] == 1
    assert h.registry.get_config()["total_locked"] == 1000


def test_update_listing_requires_creator(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_bob)
    with reverted(direct_vm, "caller is not the model creator"):
        h.call(h.registry.update_listing, "model-1", CREATOR_META, CREATOR_BENCH, "mit")


def test_update_listing_blocked_while_dispute_open(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.dispute(direct_bob)
    h.as_(direct_alice)
    with reverted(direct_vm, "model already has an open dispute"):
        h.call(h.registry.update_listing, "model-1", CREATOR_META, CREATOR_BENCH, "mit")


def test_update_listing_cannot_change_identity(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    before = h.registry.get_four_way_identity("model-1")

    h.as_(direct_alice)
    h.call(h.registry.update_listing, "model-1", CREATOR_META, "https://creator.example.org/new.json", "mit")

    after = h.registry.get_four_way_identity("model-1")
    assert before["derived_identity_digest"] == after["derived_identity_digest"]
    assert h.registry.get_model("model-1")["benchmark_url"].endswith("/new.json")


def test_open_dispute_records_match_mask(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)

    info = h.dispute(direct_bob, claim=CLAIM_PARTIAL)
    assert info["match_count"] == 3
    assert info["match_mask"] == 0b0111

    evidence = h.registry.get_dispute_evidence(info["dispute_id"])
    assert evidence["match_count"] == 3
    assert evidence["slots"]["card_sha256"] == ["d" * 64, "e" * 64]


def test_claim_matching_nothing_records_zero(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    info = h.dispute(direct_bob, claim=CLAIM_NONE)
    assert info["match_count"] == 0
    assert info["match_mask"] == 0


def test_creator_cannot_dispute_own_model(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    with reverted(direct_vm, "creator cannot dispute their own model"):
        h.dispute(direct_alice)


def test_open_dispute_requires_minimum_stake(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    with reverted(direct_vm, "stake below minimum dispute stake"):
        h.dispute(direct_bob, stake=499)


def test_open_dispute_requires_a_full_bond(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice, stake=1500)

    # The stake clears the global floor but does not cover the deposit it attacks,
    # so a cheap accusation against a large stake is rejected.
    with reverted(direct_vm, "challenger stake must cover the model deposit"):
        h.dispute(direct_bob, stake=1000)
    assert h.registry.get_config()["dispute_count"] == 0

    info = h.dispute(direct_bob, stake=1500)
    assert info["challenger_stake"] == 1500


def test_only_one_open_dispute_per_model(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.dispute(direct_bob)
    with reverted(direct_vm, "model already has an open dispute"):
        h.dispute(direct_bob, claim=CLAIM_FULL)


def test_open_dispute_rejects_unsafe_challenger_url(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    with reverted(direct_vm, "challenger benchmark url"):
        h.dispute(direct_bob, benchmark="http://challenger.example.org/bench.json")


def test_dispute_requires_registered_model(direct_vm, direct_deploy, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    with reverted(direct_vm, "unknown model"):
        h.dispute(direct_bob, model_id="missing-model")


def test_open_dispute_rejects_deregistered_model(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_alice)
    h.call(h.registry.request_deregistration, "model-1")
    direct_vm.warp("2026-01-01T01:00:01Z")
    h.call(h.registry.finalize_deregistration, "model-1")

    with reverted(direct_vm, "model is deregistered"):
        h.dispute(direct_bob)


def test_dispute_totals_are_tracked(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    info = h.dispute(direct_bob)
    model = h.registry.get_model("model-1")
    assert model["disputes_total"] == 1
    assert model["last_dispute_id"] == info["dispute_id"]
    assert model["open_dispute_id"] == info["dispute_id"]
    assert h.registry.get_dispute_ids() == [info["dispute_id"]]


def test_request_deregistration_starts_cooldown(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_alice)

    result = h.call(h.registry.request_deregistration, "model-1")
    assert result["status"] == "PENDING_EXIT"
    model = h.registry.get_model("model-1")
    assert model["status"] == "PENDING_EXIT"
    assert model["exit_unlock_at"] == model["registered_at"] + 3600

    with reverted(direct_vm, "exit delay has not elapsed"):
        h.call(h.registry.finalize_deregistration, "model-1")


def test_finalize_deregistration_after_delay_refunds(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_alice)
    h.call(h.registry.request_deregistration, "model-1")

    direct_vm.warp("2026-01-01T02:00:00Z")
    result = h.call(h.registry.finalize_deregistration, "model-1")
    assert result["refunded"] == 1000
    assert result["status"] == "DEREGISTERED"

    model = h.registry.get_model("model-1")
    assert model["deposit"] == 0
    assert model["status"] == "DEREGISTERED"
    assert h.registry.get_config()["total_locked"] == 0
    assert h.chain.transfers_to(direct_alice) == 1000

    invariants = h.registry.check_invariants(100)
    assert invariants["ok"] is True, invariants["errors"]
    assert invariants["solvency_gap"] == 0


def test_exit_blocked_by_open_dispute(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.dispute(direct_bob)
    h.as_(direct_alice)
    with reverted(direct_vm, "model already has an open dispute"):
        h.call(h.registry.request_deregistration, "model-1")


def test_pending_exit_still_accepts_new_dispute(direct_vm, direct_deploy, direct_alice, direct_bob):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_alice)
    h.call(h.registry.request_deregistration, "model-1")

    # A creator cannot escape arbitration by starting the exit clock and then hiding.
    info = h.dispute(direct_bob)
    assert info["match_count"] == 3

    direct_vm.warp("2026-01-01T05:00:00Z")
    h.as_(direct_alice)
    with reverted(direct_vm, "model already has an open dispute"):
        h.call(h.registry.finalize_deregistration, "model-1")


def test_cancel_deregistration_restores_active(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_alice)
    h.call(h.registry.request_deregistration, "model-1")
    result = h.call(h.registry.cancel_deregistration, "model-1")
    assert result["status"] == "ACTIVE"
    assert h.registry.get_model("model-1")["exit_unlock_at"] == 0


def test_only_owner_sets_parameters(direct_vm, direct_deploy, direct_alice, direct_owner):
    h = Harness(direct_vm, direct_deploy)

    h.as_(direct_alice)
    with reverted(direct_vm, "caller is not the owner"):
        h.call(h.registry.set_parameters, 2000, 800, 7200, 7200)

    h.as_(direct_owner)
    result = h.call(h.registry.set_parameters, 2000, 800, 7200, 7200)
    assert result["min_registration_stake"] == 2000
    assert h.registry.get_config()["audit_window_secs"] == 7200


@pytest.mark.parametrize(
    "arguments",
    [
        (0, 500, 86400, 3600),
        (1000, 0, 86400, 3600),
        (1000, 500, 60, 3600),
        (1000, 500, 86400, 60),
        (1000, 500, 999999999, 3600),
    ],
)
def test_parameters_are_bounded(direct_vm, direct_deploy, direct_owner, arguments):
    h = Harness(direct_vm, direct_deploy)
    h.as_(direct_owner)
    with reverted(direct_vm, "[EXPECTED]"):
        h.call(h.registry.set_parameters, *arguments)


def test_pause_blocks_registration_but_not_audit(direct_vm, direct_deploy, direct_alice, direct_bob,
                                                 direct_owner):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    info = h.dispute(direct_bob)
    h.mock_audit("PARTIAL_DERIVATIVE")

    h.as_(direct_owner)
    h.call(h.registry.set_paused, True)

    h.as_(direct_alice)
    with reverted(direct_vm, "registry is paused"):
        h.register(direct_alice, model_id="model-2")

    h.as_(direct_bob)
    result = h.call(h.registry.audit_model_similarity_live, info["dispute_id"])
    assert result["tier"] == "PARTIAL_DERIVATIVE"


def test_ownership_is_two_step(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    h = Harness(direct_vm, direct_deploy)

    h.as_(direct_alice)
    with reverted(direct_vm, "caller is not the owner"):
        h.call(h.registry.transfer_ownership, direct_bob)

    h.as_(direct_owner)
    h.call(h.registry.transfer_ownership, direct_alice)
    assert addr_hex(h.registry.get_config()["pending_owner"]) == addr_hex(direct_alice)

    # The pending owner is not the owner until they accept.
    h.as_(direct_alice)
    result = h.call(h.registry.accept_ownership)
    assert addr_hex(result["owner"]) == addr_hex(direct_alice)
    assert addr_hex(h.registry.get_config()["owner"]) == addr_hex(direct_alice)


def test_owner_cannot_settle_or_touch_escrow(direct_vm, direct_deploy, direct_alice, direct_bob,
                                             direct_owner):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    info = h.dispute(direct_bob)

    h.as_(direct_owner)
    with reverted(direct_vm, "caller is not a dispute party"):
        h.call(h.registry.audit_model_similarity_live, info["dispute_id"])
    with reverted(direct_vm, "caller is not a dispute party"):
        h.call(h.registry.propose_settlement, info["dispute_id"], 5000, 5000, "owner override")
    with reverted(direct_vm, "caller is not a dispute party"):
        h.call(h.registry.countersign_settlement, info["dispute_id"], 0, "0" * 64)

    assert h.registry.get_dispute(info["dispute_id"])["status"] == "OPEN"
    assert h.registry.get_model("model-1")["deposit"] == 1000


def test_value_on_non_payable_method_reverts(direct_vm, direct_deploy, direct_alice):
    h = Harness(direct_vm, direct_deploy)
    h.register(direct_alice)
    h.as_(direct_alice)
    with reverted(direct_vm, "method does not accept value"):
        h.call(h.registry.update_listing, "model-1", CREATOR_META, CREATOR_BENCH, "mit", value=5)
