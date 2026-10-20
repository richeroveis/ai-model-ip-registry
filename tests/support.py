"""Shared helpers for the AIModelIPRegistry direct test suite."""

import json
import sys

CONTRACT = "contracts/AIModelIPRegistry.py"

# Registered identity slots.
W = "a" * 64
D = "b" * 64
C = "c" * 64
K = "d" * 64

# Challenger claim that matches three of four slots.
CLAIM_PARTIAL = (W, D, C, "e" * 64)
# Challenger claim that matches nothing.
CLAIM_NONE = ("1" * 64, "2" * 64, "3" * 64, "4" * 64)
# Challenger claim that matches everything.
CLAIM_FULL = (W, D, C, K)

CREATOR_META = "https://creator.example.org/model-card.json"
CREATOR_BENCH = "https://creator.example.org/benchmark.json"
CHALLENGER_META = "https://challenger.example.org/derivative-card.json"
CHALLENGER_BENCH = "https://challenger.example.org/benchmark.json"

REGISTRATION_STAKE = 1000
DISPUTE_STAKE = 1000
AUDIT_WINDOW = 86400
EXIT_DELAY = 3600
START_TIME = "2026-01-01T00:00:00Z"


def addr_hex(value) -> str:
    """Normalise an Address, a raw 20 byte value or a string to lowercase hex."""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return str(value).lower()


class Chain:
    """Records the messages a contract emits so payouts can be asserted exactly."""

    def __init__(self, vm):
        self.vm = vm
        self.transfers = []
        self.events = []
        self.nlp_verdicts = []
        self.raw = []

    def install(self) -> "Chain":
        self.vm._gl_call_hook = self._hook
        return self

    def _hook(self, vm, request):
        self.raw.append(request)
        if "PostMessage" in request:
            message = request["PostMessage"]
            value = int(message.get("value", 0))
            self.transfers.append(
                {
                    "address": message.get("address"),
                    "value": value,
                    "calldata": message.get("calldata"),
                    "on": message.get("on"),
                }
            )
            # The direct runner does not settle emitted value transfers, so mirror
            # the chain debit here to keep the solvency invariant meaningful.
            contract = getattr(vm, "_contract_address", None)
            if contract is not None and value > 0:
                key = bytes(contract)
                vm._balances[key] = vm._balances.get(key, 0) - value
            return {"ok": None}
        if "EmitEvent" in request:
            self.events.append(request["EmitEvent"])
            return {"ok": None}
        if "ExecPromptTemplate" in request:
            if not self.nlp_verdicts:
                raise AssertionError("equivalence prompt was not expected")
            return {"ok": self.nlp_verdicts.pop(0)}
        return None

    def total_transferred(self) -> int:
        return sum(item["value"] for item in self.transfers)

    def transfers_to(self, address) -> int:
        target = addr_hex(address)
        return sum(item["value"] for item in self.transfers if addr_hex(item["address"]) == target)


class Harness:
    """Thin wrapper that keeps the value plumbing out of individual tests."""

    def __init__(self, vm, deploy, registration_stake=REGISTRATION_STAKE,
                 dispute_stake=DISPUTE_STAKE, audit_window=AUDIT_WINDOW,
                 exit_delay=EXIT_DELAY):
        self.vm = vm
        self.vm.warp(START_TIME)
        self.registry = deploy(CONTRACT, registration_stake, dispute_stake, audit_window, exit_delay)
        self.chain = Chain(vm).install()

    @property
    def address(self):
        return self.registry.address

    def as_(self, who):
        self.vm.sender = who
        return self

    def call(self, fn, *args, value=0, **kwargs):
        self.vm.value = value
        try:
            result = fn(*args, **kwargs)
        finally:
            self.vm.value = 0
        if value > 0:
            # The direct runner does not move value into the callee, so mirror the
            # chain credit here. It happens after the call so a reverted payable
            # method does not leave stray balance behind.
            key = bytes(self.address.as_bytes)
            self.vm._balances[key] = self.vm._balances.get(key, 0) + value
        return result

    def balance(self) -> int:
        return self.vm._balances.get(bytes(self.address.as_bytes), 0)

    def register(self, creator, model_id="model-1", digests=(W, D, C, K),
                 stake=REGISTRATION_STAKE, metadata=CREATOR_META, benchmark=CREATOR_BENCH,
                 license_terms="apache-2.0"):
        self.as_(creator)
        return self.call(
            self.registry.register_model,
            model_id,
            digests[0],
            digests[1],
            digests[2],
            digests[3],
            metadata,
            benchmark,
            license_terms,
            value=stake,
        )

    def dispute(self, challenger, model_id="model-1", claim=CLAIM_PARTIAL,
                stake=DISPUTE_STAKE, note="unauthorized derivative",
                metadata=CHALLENGER_META, benchmark=CHALLENGER_BENCH):
        self.as_(challenger)
        return self.call(
            self.registry.open_dispute,
            model_id,
            claim[0],
            claim[1],
            claim[2],
            claim[3],
            metadata,
            benchmark,
            note,
            value=stake,
        )

    def mock_audit(self, tier, creator_body="registrant benchmark output",
                   challenger_body="challenger benchmark output", rationale="evidence based"):
        self.vm.clear_mocks()
        self.vm.mock_web(r"creator\.example\.org", {"status": 200, "body": creator_body})
        self.vm.mock_web(r"challenger\.example\.org", {"status": 200, "body": challenger_body})
        self.vm.mock_llm(
            r"impartial model IP arbitration",
            json.dumps({"tier": tier, "rationale": rationale}),
        )

    def contract_module(self):
        return sys.modules[type(self.registry).__module__]


def reverted(vm, fragment):
    return vm.expect_revert(fragment)
