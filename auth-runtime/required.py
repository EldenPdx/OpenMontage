"""Required identity pytest runs must collect and pass every reviewed node."""

import hashlib
import json
import os
from pathlib import Path

import pytest

_expected = set()
_collected = set()
_reports = {}


def pytest_addoption(parser):
    parser.addoption("--required-nodes", help="Reviewed JSON node manifest for this required phase")
    parser.addoption("--required-report", help="Private result path; excludes test output and exception text")


def pytest_configure(config):
    path = config.getoption("required_nodes")
    if not path or not config.getoption("required_report"):
        raise pytest.UsageError("Required identity runs need an explicit node manifest and private report")
    try:
        nodes = json.loads(Path(path).read_text())["nodes"]
    except (OSError, ValueError, KeyError):
        raise pytest.UsageError("Required identity node manifest is unavailable or invalid") from None
    if not isinstance(nodes, list) or not nodes or any(not isinstance(node, str) or "::" not in node for node in nodes) or len(nodes) != len(set(nodes)):
        raise pytest.UsageError("Required identity node manifest must contain unique nonempty test IDs")
    _expected.clear()
    _expected.update(nodes)
    _collected.clear()
    _reports.clear()


def pytest_collection_finish(session):
    _collected.update(item.nodeid for item in session.items)


def pytest_runtest_logreport(report):
    if report.nodeid in _expected:
        phases = _reports.setdefault(report.nodeid, {})
        phases.setdefault(report.when, []).append({"outcome": report.outcome, "xfail": hasattr(report, "wasxfail")})


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    complete = _collected == _expected and set(_reports) == _expected
    passed = complete and exitstatus == 0 and all(
        set(phases) == {"setup", "call", "teardown"}
        and all(records and all(record["outcome"] == "passed" and not record["xfail"] for record in records)
                for records in phases.values())
        for phases in _reports.values()
    )
    result = {"passed": passed, "pytest_exitstatus": int(exitstatus), "nodes": _reports,
              "run_id": os.environ.get("AUTH32_VALIDATION_RUN_ID"),
              "missing": sorted(_expected - _collected),
              "unexpected_sha256": sorted(hashlib.sha256(node.encode()).hexdigest() for node in _collected - _expected)}
    path = Path(session.config.getoption("required_report"))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)
    if not passed:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
