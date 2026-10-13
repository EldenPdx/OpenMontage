"""Session-wide test safety net.

**No test may open a network connection.** Provider tools bill per call, so a
test that reaches a real endpoint costs the developer money — silently, and
every time CI runs. This blocks outbound sockets for the whole test session.

The guard is at the socket layer on purpose. Patching `requests` only covers
tools that use `requests`; the fleet also talks to vendor SDKs (google-cloud,
openai, boto3), `httpx`, and raw `urllib`. Everything bottoms out in
`socket.connect`, so that is where the wall goes.

Loopback is still allowed — local servers, ffmpeg RPC, and Backlot fixtures need it.

To write a test that genuinely hits a live API:

    @pytest.mark.live_api
    def test_real_call():
        ...

Marked tests are **skipped by default** and only run with the env flag set:

    OPENMONTAGE_ALLOW_NETWORK=1 pytest -m live_api

Limitation: this guards the pytest process. A test that shells out to a
subprocess (node, ffmpeg, npx) is outside its reach — don't call paid APIs
from a subprocess in tests.
"""

from __future__ import annotations

import os
import socket
import json
from pathlib import Path

import pytest

_ALLOW_ENV_FLAG = "OPENMONTAGE_ALLOW_NETWORK"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


class NetworkCallInTestError(RuntimeError):
    """Raised when a test tries to open a non-loopback connection."""


def _network_allowed() -> bool:
    return os.environ.get(_ALLOW_ENV_FLAG, "").strip().lower() in {"1", "true", "yes"}


def _is_loopback(address) -> bool:
    """True for loopback TCP/UDP targets and for AF_UNIX socket paths."""
    if isinstance(address, (str, bytes)):
        return True  # AF_UNIX / abstract socket — local by definition
    if not isinstance(address, (tuple, list)) or not address:
        return True  # unrecognised shape; let the real call decide
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("utf-8", "replace")
    if not isinstance(host, str):
        return False
    host = host.strip("[]").lower()
    if host in _LOOPBACK_HOSTS:
        return True
    return host.startswith("127.")


def _blocked(address) -> NetworkCallInTestError:
    return NetworkCallInTestError(
        f"Blocked a network connection to {address!r} during a test.\n"
        f"\n"
        f"Tests must not call real endpoints — provider APIs bill per request.\n"
        f"Mock the transport instead (see tests/tools/test_atlas_video.py for the\n"
        f"fake-`requests` pattern), or mark the test @pytest.mark.live_api and run\n"
        f"it deliberately with {_ALLOW_ENV_FLAG}=1."
    )


@pytest.fixture(scope="session", autouse=True)
def _block_network():
    """Refuse non-loopback sockets for the entire session."""
    if _network_allowed():
        yield
        return

    def guarded_connect(self, address, *args, **kwargs):
        if not _is_loopback(address):
            raise _blocked(address)
        return _real_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):
        if not _is_loopback(address):
            raise _blocked(address)
        return _real_connect_ex(self, address, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):
        if not _is_loopback(address):
            raise _blocked(address)
        return _real_create_connection(address, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create_connection
    try:
        # A loopback proxy can otherwise relay paid traffic past the socket guard.
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("NO_PROXY", "*")
            environment.setenv("no_proxy", "*")
            yield
    finally:
        socket.socket.connect = _real_connect
        socket.socket.connect_ex = _real_connect_ex
        socket.create_connection = _real_create_connection


def pytest_addoption(parser):
    parser.addoption("--include-manual-security", action="store_true",
                     help="Human-operated run including the explicit manual security manifest")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_api: test performs a real, billable API call. Skipped unless "
        f"{_ALLOW_ENV_FLAG}=1 is set.",
    )


def pytest_collection_modifyitems(config, items):
    """Keep manual security acceptance visible without running it by default."""
    try:
        path = Path(__file__).resolve().parents[1] / "auth-runtime/manual-security.json"
        manual = json.loads(path.read_text())
        functions = manual["functions"]
        variants = set(manual.get("nodes", []))
        if not functions or len(set(functions)) != len(functions) or any("::" not in name for name in functions):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise pytest.UsageError("Explicit manual security manifest is missing or invalid") from None
    if not config.getoption("include_manual_security"):
        pending = []
        for item in items:
            if item.path.is_relative_to(path.parents[1]):
                node = item.path.relative_to(path.parents[1]).as_posix() + "::" + item.nodeid.split("::", 1)[1]
                if node.split("[", 1)[0] in functions or node in variants:
                    pending.append(item)
        if pending:
            config._manual_security_pending = len(pending)
            config.hook.pytest_deselected(items=pending)
            items[:] = [item for item in items if item not in pending]
    if _network_allowed():
        return
    skip = pytest.mark.skip(
        reason=f"live API test — costs money; set {_ALLOW_ENV_FLAG}=1 to run"
    )
    for item in items:
        if "live_api" in item.keywords:
            item.add_marker(skip)


def pytest_terminal_summary(terminalreporter):
    pending = getattr(terminalreporter.config, "_manual_security_pending", 0)
    if pending:
        terminalreporter.write_line(f"manual security PENDING: {pending} nodes deselected; human acceptance required")
