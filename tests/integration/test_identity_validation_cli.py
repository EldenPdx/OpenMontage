"""Operator validation entry points fail safely before they need real services."""

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_identity_bootstrap_help_documents_explicit_online_permission(tmp_path):
    environment = {**os.environ, "PATH": str(tmp_path)}
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        environment.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-B", "auth-runtime/bootstrap.py", "--help"],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "--online" in result.stdout
    assert "offline" in result.stdout


def test_operator_lifecycle_has_explicit_original_vendor_seal_commands():
    result = subprocess.run([sys.executable, "-B", "auth-runtime/manage.py", "--help"], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "sso-prepare" in result.stdout and "sso-check" in result.stdout


def test_docker_nanosecond_startup_timestamp_is_preserved_as_a_utc_instant():
    script = "import sys;sys.path.insert(0,'auth-runtime');import manage;print(manage.docker_started_at('2026-10-12T20:23:34.123456789Z').isoformat())"
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout.strip() == "2026-10-12T20:23:34.123456+00:00"


def run_required(tmp_path, body, *, nodes=None, extra=()):
    (tmp_path / "test_input.py").write_text(body)
    (tmp_path / "required.json").write_text(json.dumps({"nodes": nodes or ["test_input.py::test_required"]}))
    report = tmp_path / "result.json"
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "auth-runtime"), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    environment.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-p", "required", "-p", "no:cacheprovider", "--rootdir=.", "--tb=no",
         "--required-nodes=required.json", "--required-report=result.json", *extra, "test_input.py"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15,
    )
    assert report.is_file(), result.stderr
    return result, json.loads(report.read_text())


def test_required_identity_skip_is_failure_even_when_pytest_would_pass(tmp_path):
    result, report = run_required(tmp_path, "import pytest\ndef test_required():\n    pytest.skip('dependency missing')\n")
    assert result.returncode != 0
    assert report["passed"] is False
    assert report["nodes"]["test_input.py::test_required"]["call"][0]["outcome"] == "skipped"


@pytest.mark.parametrize("body", [
    "import pytest\n@pytest.mark.xfail\ndef test_required():\n    assert False\n",
    "import pytest\n@pytest.mark.xfail\ndef test_required():\n    pass\n",
    "import pytest\n@pytest.fixture(autouse=True)\ndef dependency():\n    pytest.skip('missing')\ndef test_required():\n    pass\n",
    "import pytest\n@pytest.fixture(autouse=True)\ndef dependency():\n    yield\n    assert False\ndef test_required():\n    pass\n",
], ids=("xfail", "xpass", "setup-skip", "teardown-failure"))
def test_required_identity_xfail_xpass_and_fixture_gaps_cannot_pass(tmp_path, body):
    result, report = run_required(tmp_path, body)
    assert result.returncode != 0
    assert report["passed"] is False


@pytest.mark.parametrize("extra", [("--collect-only",), ("-k", "absent")], ids=("collect-only", "deselected"))
def test_collected_only_or_deselected_identity_nodes_cannot_pass(tmp_path, extra):
    result, report = run_required(tmp_path, "def test_required():\n    pass\n", extra=extra)
    assert result.returncode != 0
    assert report["passed"] is False


def test_missing_reviewed_identity_node_cannot_pass(tmp_path):
    result, report = run_required(tmp_path, "def test_required():\n    pass\n",
                                  nodes=["test_input.py::test_required", "test_input.py::test_removed"])
    assert result.returncode != 0
    assert report["missing"] == ["test_input.py::test_removed"]


def test_required_report_keeps_failure_text_and_captured_credentials_private(tmp_path):
    sentinel = "temporary-credential-sentinel"
    result, report = run_required(tmp_path, f"def test_required():\n    print('{sentinel}')\n    assert False, '{sentinel}'\n")
    assert result.returncode != 0
    assert sentinel not in json.dumps(report)
    assert (tmp_path / "result.json").stat().st_mode & 0o777 == 0o600


def test_every_required_identity_phase_must_pass_before_reporting_success(tmp_path):
    result, report = run_required(tmp_path, "def test_required():\n    assert 2 + 3 == 5\n")
    assert result.returncode == 0
    assert report["passed"] is True
    assert set(report["nodes"]["test_input.py::test_required"]) == {"setup", "call", "teardown"}


def test_validation_cli_names_a_missing_library_and_never_exposes_its_exception():
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "auth-runtime")}
    result = subprocess.run(
        [sys.executable, "-S", "-B", "auth-runtime/ci.py", "dependencies", "--dependency", "library:logto"],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert result.stderr.strip() == "Required dependency failed: library:logto"
    assert "Traceback" not in result.stderr


def test_a_failed_new_run_cannot_publish_a_previous_success_receipt(tmp_path):
    runtime = tmp_path / "auth-runtime"
    runtime.mkdir()
    shutil.copyfile(ROOT / "auth-runtime/ci.py", runtime / "ci.py")
    (tmp_path / "requirements-auth.txt").write_text("logto==0.2.1\n")
    (runtime / "required-nodes.json").write_text(json.dumps({"pending_owners": [32], "phases": {}}))
    for arguments in (["init", "-q"], ["config", "user.name", "Identity CLI Test"], ["config", "user.email", "identity-cli@openmontage.test"],
                      ["add", "."], ["commit", "-qm", "Fixture for CLI safety"]):
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    evidence = runtime / ".state/validation"
    evidence.mkdir(parents=True)
    (evidence / "summary.json").write_text(json.dumps({"candidate": sha, "passed": True, "run_id": "previous-run"}))
    failed = subprocess.run([sys.executable, "-B", str(runtime / "ci.py"), "run", "--candidate", "wrong-candidate"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert failed.returncode != 0
    published = subprocess.run([sys.executable, "-B", str(runtime / "ci.py"), "publish"], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert published.returncode == 0
    report = json.loads((evidence / "public/outcomes.json").read_text())
    assert report["completed"] is False
    assert report["run_id"] != "previous-run"


@pytest.mark.parametrize("online", [False, True], ids=("offline-default", "explicit-online"))
def test_cold_image_transport_needs_explicit_download_permission(tmp_path, monkeypatch, online):
    spec = importlib.util.spec_from_file_location("identity_download_policy", ROOT / "auth-runtime/images.py")
    images = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(images)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setattr(images.subprocess, "check_output", lambda *args, **kwargs: '{"Os":"linux","Arch":"amd64"}')

    class NetworkBoundaryReached(Exception):
        pass

    def transport(*args, **kwargs):
        raise NetworkBoundaryReached

    monkeypatch.setattr(images.requests.Session, "get", transport)
    if online:
        with pytest.raises(NetworkBoundaryReached):
            images.stage_images(tmp_path, online=True)
    else:
        with pytest.raises(RuntimeError, match="explicit --online"):
            images.stage_images(tmp_path)
