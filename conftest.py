"""Shared pytest configuration for the AIModelIPRegistry direct test suite.

The direct runner in gltest injects the transaction message by replacing fd 0 with
a temp file and then unlinking that file while fd 0 is still open. POSIX allows
this, Windows does not, so the whole direct suite fails with WinError 32 on
Windows. The patch below keeps the exact same behaviour and defers the unlink to
process exit.

The direct runner also has no EVM layer, so `gl.evm` is unavailable there even
though the pinned runner supports it. The stub below restores the one operation
the contract needs (an external value transfer to an EOA) and posts the same
PostMessage request the internal path uses, so the test chain hook observes the
exact payout amount. The live studionet run is what proves the real external
message path.
"""

import atexit
import os
import sys
import tempfile
import types
import pytest

_WINDOWS_TEMP_FILES = []


def _win_inject_message_to_fd0(vm) -> None:
    from genlayer.py import calldata
    from genlayer.py.types import Address

    sender_addr = vm.sender
    if isinstance(sender_addr, bytes):
        sender_addr = Address(sender_addr)

    contract_addr = vm._contract_address
    if isinstance(contract_addr, bytes):
        contract_addr = Address(contract_addr)

    origin_addr = vm.origin
    if isinstance(origin_addr, bytes):
        origin_addr = Address(origin_addr)

    message_data = {
        "contract_address": contract_addr,
        "sender_address": sender_addr,
        "origin_address": origin_addr,
        "stack": [],
        "value": vm._value,
        "datetime": vm._datetime,
        "is_init": False,
        "chain_id": vm._chain_id,
        "entry_kind": 0,
        "entry_data": b"",
        "entry_stage_data": None,
    }

    encoded = calldata.encode(message_data)

    fd, path = tempfile.mkstemp()
    os.write(fd, encoded)
    os.lseek(fd, 0, os.SEEK_SET)

    original_stdin = os.dup(0)
    vm._original_stdin_fd = original_stdin

    os.dup2(fd, 0)
    os.close(fd)
    _WINDOWS_TEMP_FILES.append(path)


def _cleanup_temp_files() -> None:
    for path in _WINDOWS_TEMP_FILES:
        try:
            os.unlink(path)
        except OSError:
            pass
    _WINDOWS_TEMP_FILES.clear()


if sys.platform == "win32":
    import gltest.direct.loader as _loader

    _loader._inject_message_to_fd0 = _win_inject_message_to_fd0
    atexit.register(_cleanup_temp_files)


# gltest refreshes sender, origin and value on warp, but not the message datetime.
# The contract reads the transaction datetime from the message, so mirror it here.
import gltest.direct.vm as _direct_vm  # noqa: E402

_original_refresh = _direct_vm.VMContext._refresh_gl_message


def _refresh_with_datetime(self) -> None:
    _original_refresh(self)
    gl_module = sys.modules.get("genlayer.gl")
    raw = getattr(gl_module, "message_raw", None) if gl_module is not None else None
    if isinstance(raw, dict):
        raw["datetime"] = self._datetime


_direct_vm.VMContext._refresh_gl_message = _refresh_with_datetime


def _install_evm_stub() -> None:
    """Provide genlayer.gl._internal.eth, which only the GenVM binary ships."""
    name = "genlayer.gl._internal.eth"
    if name in sys.modules:
        return
    module = types.ModuleType(name)

    def evm_contract_interface(declaration):
        class _Proxy:
            __slots__ = ("address",)

            def __init__(self, address, *args, **kwargs):
                self.address = address

            def emit_transfer(self, *, value):
                import _genlayer_wasi as wasi
                import genlayer.py.calldata as calldata

                wasi.gl_call(
                    calldata.encode(
                        {
                            "PostMessage": {
                                "address": self.address,
                                "calldata": None,
                                "value": value,
                                "on": "finalized",
                            }
                        }
                    )
                )

        def factory(address, *args, **kwargs):
            return _Proxy(address)

        return factory

    module.evm_contract_interface = evm_contract_interface
    sys.modules[name] = module


# The contract imports gl.evm lazily, but the module path must exist before the
# first payout so the lazy loader resolves it against this stub.
_install_evm_stub()


@pytest.fixture
def contract_path() -> str:
    return "contracts/AIModelIPRegistry.py"
