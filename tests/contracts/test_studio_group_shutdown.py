import multiprocessing
import os
import signal
import select
import subprocess
import time

import pytest

from production.recovery import process_identity
from production.tool_bridge import ProductionToolBridge
from tools.tool_registry import ToolRegistry


def zombie_group(connection, *, live_descendant=False):
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    zombie = os.fork()
    if zombie == 0:
        os._exit(0)
    live = None
    if live_descendant:
        ready, announce = os.pipe()
        live = os.fork()
        if live == 0:
            os.close(ready)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.write(announce, b"ready")
            os.close(announce)
            while True:
                time.sleep(1)
        os.close(announce)
        assert select.select([ready], [], [], 5)[0]
        assert os.read(ready, 5) == b"ready"
        os.close(ready)
    connection.send({"zombie": zombie, "live": live})
    while True:
        time.sleep(1)


@pytest.mark.skipif(os.name != "posix", reason="Managed Studio processes require POSIX")
@pytest.mark.parametrize("live_descendant", [False, True])
def test_stop_confirms_no_live_writer_even_when_init_has_not_reaped_zombies(tmp_path, live_descendant):
    parent, child = multiprocessing.get_context("fork").Pipe()
    process = multiprocessing.get_context("fork").Process(target=zombie_group, args=(child,), kwargs={"live_descendant": live_descendant})
    process.start()
    child.close()
    try:
        assert parent.poll(5), "Process group did not become ready"
        parent.recv()
        process.studio_identity = process_identity(process.pid)
        bridge = ProductionToolBridge(None, tmp_path, registry=ToolRegistry())
        bridge.jobs["process-test"] = process
        started = time.monotonic()
        assert bridge.stop() is True
        assert time.monotonic() - started < 3
        states = subprocess.check_output(["ps", "-eo", "pid=,pgid=,stat="], text=True)
        members = [line.split() for line in states.splitlines() if len(line.split()) == 3 and line.split()[1] == str(process.pid)]
        assert all(member[2].startswith(("Z", "X")) for member in members)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.join(timeout=2)
        parent.close()


@pytest.mark.parametrize("failure", ["command", "malformed", "permission"])
def test_unreadable_os_group_state_retains_writer_ownership(monkeypatch, failure):
    from production import recovery
    monkeypatch.setattr(recovery.os, "killpg", lambda *_: None)
    if failure == "permission":
        def unavailable(*args, **kwargs):
            raise PermissionError("process table unavailable")
        monkeypatch.setattr(recovery.subprocess, "run", unavailable)
    else:
        result = subprocess.CompletedProcess([], 1 if failure == "command" else 0,
                                             stdout="" if failure == "command" else "unexpected process row")
        monkeypatch.setattr(recovery.subprocess, "run", lambda *args, **kwargs: result)
    assert recovery._group_exists(12345) is True
