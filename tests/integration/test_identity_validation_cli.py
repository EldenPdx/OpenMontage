"""Operator validation entry points fail safely before they need real services."""

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile

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


@pytest.mark.parametrize("global_uv", [False, True], ids=("wheel-scripts-off-path", "independent-path-uv"))
def test_bootstrap_finds_uv_in_its_python_venv_without_activating_that_venv(tmp_path, global_uv):
    host = tmp_path / "host"
    subprocess.run([sys.executable, "-B", "-m", "venv", "--without-pip", str(host)], check=True,
                   capture_output=True, text=True, timeout=20)
    python = host / "bin/python"
    runtime = tmp_path / "application/auth-runtime"
    runtime.mkdir(parents=True)
    shutil.copyfile(ROOT / "auth-runtime/bootstrap.py", runtime / "bootstrap.py")
    recorded = tmp_path / "uv-invocation.json"
    uv = host / "bin/uv"
    uv.write_text(f"#!{python}\nimport json,sys\nfrom pathlib import Path\n"
                  f"Path({str(recorded)!r}).write_text(json.dumps(sys.argv))\nraise SystemExit(57)\n")
    uv.chmod(0o700)
    path = tmp_path / "no-global-uv"
    if global_uv:
        path.mkdir()
        installed = path / "uv"
        shutil.copyfile(uv, installed)
        installed.chmod(0o700)
        uv = installed
    environment = {**os.environ, "PATH": str(path)}
    result = subprocess.run([str(python), "-B", str(runtime / "bootstrap.py"), "--online", "up"],
                            cwd=runtime.parent, env=environment, capture_output=True, text=True, timeout=10)
    assert recorded.is_file(), result.stderr
    assert json.loads(recorded.read_text()) == [str(uv), "venv", "--python", str(python), "--system-site-packages", str(runtime / ".state/venv")]
    assert result.returncode != 0 and result.stderr.strip() == "Private identity bootstrap failed; inspect auth-runtime/.state/bootstrap.log"


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


def test_validation_plan_keeps_real_login_automatic_and_protocol_checks_pending():
    result = subprocess.run([sys.executable, "-B", "auth-runtime/ci.py", "plan"], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    automatic = {node for definition in plan["phases"].values() for node in definition["nodes"]}
    manual = set(plan["manual_pending"])
    assert "auth-runtime/tests/test_environment.py::test_real_signed_saml_sso_returns_a_verified_logto_identity" in automatic
    assert "auth-runtime/tests/test_bff_oidc_faults.py::test_nonfinite_id_token_date_is_rejected_as_unverified_identity" in manual
    assert automatic.isdisjoint(manual)
    catalog = json.loads((ROOT / "auth-runtime/required-nodes.json").read_text())
    assert automatic | manual == {node for definition in catalog["phases"].values() for node in definition["nodes"]}
    assert plan["suite"] == "automated" and plan["acceptance_status"] == "PENDING"
    assert plan["manual_pending_operations"] == ["dependency-failure-calibration"]
    assert "auth-runtime/tests/test_studio_auth28.py::test_long_verified_result_stops_before_remaining_bytes_after_logout[outage]" in manual
    assert "auth-runtime/tests/test_studio_auth28.py::test_long_verified_result_stops_before_remaining_bytes_after_logout[logout]" in automatic
    assert "auth-runtime/tests/test_sso.py::test_standard_enterprise_logout_returns_to_the_application_signed_out[oidc-A]" in automatic
    assert "auth-runtime/tests/test_sso.py::test_enterprise_secrets_stay_private_and_logout_revokes_the_resource_session[oidc-A]" in manual


def test_restore_plans_use_separate_empty_database_targets():
    plans = {}
    for suite in ("automated", "manual-security"):
        result = subprocess.run([sys.executable, "-B", "auth-runtime/ci.py", "plan", "--suite", suite],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        plans[suite] = json.loads(result.stdout)
    native = plans["automated"]["phases"]["coordinated-native-restore"]["database_config"]
    business = plans["manual-security"]["phases"]["authenticated-restore"]["database_config"]
    assert native == "ci-native-runtime-roles.json" and business == "ci-runtime-roles.json"


def test_default_regression_deselects_manual_checks_and_reports_pending(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    shutil.copyfile(ROOT / "tests/conftest.py", tests / "conftest.py")
    runtime = tmp_path / "auth-runtime"
    runtime.mkdir()
    (runtime / "manual-security.json").write_text(json.dumps({"functions": ["tests/test_entry.py::test_operator_check"]}))
    (tests / "test_entry.py").write_text("def test_ordinary():\n    pass\ndef test_operator_check():\n    assert False, 'manual selection witness'\n")
    environment = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    environment.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run([sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests/"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 deselected" in result.stdout and "manual security PENDING: 1" in result.stdout
    opted = subprocess.run([sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", "--include-manual-security", "tests/"],
                           cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10)
    assert opted.returncode != 0 and "manual selection witness" in opted.stdout


def test_external_review_can_collect_ordinary_tests_with_the_project_plugin():
    environment = {**os.environ, "PYTHONPATH": str(ROOT), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    environment.pop("PYTEST_ADDOPTS", None)
    with tempfile.TemporaryDirectory(prefix="openmontage-review-") as directory:
        (Path(directory) / "test_review.py").write_text("def test_ordinary_review():\n    pass\n")
        result = subprocess.run([sys.executable, "-B", "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider",
                                 "--collect-only", "-q", "test_review.py"], cwd=directory, env=environment,
                                capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 test collected" in result.stdout


def test_required_phase_runs_only_the_exact_reviewed_nodes(tmp_path):
    runtime = tmp_path / "auth-runtime"
    runtime.mkdir()
    for name in ("ci.py", "required.py"):
        shutil.copyfile(ROOT / "auth-runtime" / name, runtime / name)
    (tmp_path / "requirements-auth.txt").write_text("logto==0.2.1\n")
    (tmp_path / "test_entry.py").write_text("def test_reviewed():\n    pass\ndef test_unselected():\n    assert False, 'unselected witness'\n")
    definition = {"runtime": False, "timeout_seconds": 10, "nodes": ["test_entry.py::test_reviewed"]}
    script = "import sys;sys.path.insert(0,'auth-runtime');import ci;ci.phase('tracer'," + repr(definition) + ")"
    environment = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    environment.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    report = json.loads((runtime / ".state/validation/tracer-result.json").read_text())
    assert report["passed"] is True and set(report["nodes"]) == {"test_entry.py::test_reviewed"}


def test_required_phases_use_fresh_private_standard_library_temp_roots(tmp_path):
    runtime = tmp_path / "auth-runtime"
    runtime.mkdir()
    for name in ("ci.py", "required.py"):
        shutil.copyfile(ROOT / "auth-runtime" / name, runtime / name)
    (tmp_path / "requirements-auth.txt").write_text("logto==0.2.1\n")
    old = tmp_path / "caller-temp"
    old.mkdir()
    (old / "retained.txt").write_text("retained caller data")
    (tmp_path / "test_entry.py").write_text(
        "import os,tempfile\nfrom pathlib import Path\nfrom multiprocessing import Manager\ndef test_reviewed():\n"
        "    with tempfile.TemporaryDirectory() as name:\n"
        "        parent=Path(name).parent\n"
        "        assert str(parent)!=os.environ['CALLER_TEMP']\n"
        "        assert parent.stat().st_mode & 0o777 == 0o700\n"
        "        Path('observed.txt').write_text(str(parent))\n"
        "        with Manager() as manager:\n"
        "            values=manager.list();values.append('ordinary media submission')\n"
        "            assert list(values)==['ordinary media submission']\n")
    definition = {"runtime": False, "timeout_seconds": 10, "nodes": ["test_entry.py::test_reviewed"]}
    script = "import sys;sys.path.insert(0,'auth-runtime');import ci;ci.phase('tracer'," + repr(definition) + ")"
    environment = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "TMPDIR": str(old), "CALLER_TEMP": str(old)}
    environment.pop("PYTEST_ADDOPTS", None)
    observed = []
    for _ in range(2):
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=tmp_path, env=environment,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        observed.append((tmp_path / "observed.txt").read_text())
    assert observed[0] != observed[1]
    assert (old / "retained.txt").read_text() == "retained caller data"


@pytest.mark.parametrize("operator_exit", [0, 7], ids=("operator-restored", "operator-failed"))
def test_coordinated_phase_requires_its_recovery_operator_to_succeed(tmp_path, operator_exit):
    runtime = tmp_path / "auth-runtime"
    runtime.mkdir()
    for name in ("ci.py", "required.py"):
        shutil.copyfile(ROOT / "auth-runtime" / name, runtime / name)
    (tmp_path / "requirements-auth.txt").write_text("logto==0.2.1\n")
    (runtime / "native_recovery.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "if sys.argv[1]=='prepare':\n"
        "    directory=Path(sys.argv[sys.argv.index('--control')+1]);directory.mkdir(mode=0o700)\n"
        "    (directory/'plan.json').write_text('{}')\n"
        f"else:\n    raise SystemExit({operator_exit})\n")
    (tmp_path / "test_entry.py").write_text("import os\nfrom pathlib import Path\ndef test_reviewed():\n    assert Path(os.environ['AUTH32_NATIVE_RECOVERY_PLAN']).is_file()\n")
    definition = {"runtime": False, "timeout_seconds": 10, "coordinated_restore": True, "nodes": ["test_entry.py::test_reviewed"]}
    script = "import sys;sys.path.insert(0,'auth-runtime');import ci;ci.phase('tracer'," + repr(definition) + ")"
    environment = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    environment.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=15)
    assert (result.returncode == 0) == (operator_exit == 0), result.stderr
    report = json.loads((runtime / ".state/validation/tracer-result.json").read_text())
    assert report["passed"] is True


def test_sso_fixture_setup_requires_the_private_lifecycle_owner(tmp_path):
    runtime = tmp_path / "auth-runtime"
    runtime.mkdir()
    for name in ("ci.py", "manage.py"):
        shutil.copyfile(ROOT / "auth-runtime" / name, runtime / name)
    (tmp_path / "requirements-auth.txt").write_text("logto==0.2.1\n")
    result = subprocess.run([sys.executable, "-B", str(runtime / "ci.py"), "prepare-sso"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert result.stderr.strip() == "SSO fixture setup requires this checkout's private lifecycle-owner state"


@pytest.mark.parametrize("memberships", [{"A": ["editor"]}, {"A": ["viewer"], "B": ["editor"]}], ids=("oidc-editor", "saml-cross-member"))
def test_recorded_sso_proof_uses_the_recovery_consumer_schema(memberships):
    # This checks the record interface only; it is not a Native sign-in result.
    record = {"native_subject": "observed-native-subject", "connector_id": "fixture-connector",
              "sso_identity_id": "persistent-name-id", "sso_issuer": "https://idp.example/realms/fixture"}
    script = "import sys,json;sys.path.insert(0,'auth-runtime');import ci;print(json.dumps(ci.sso_subject_proof(" + repr(record) + "," + repr(memberships) + ")))"
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"native_subject": "observed-native-subject", "connector_id": "fixture-connector",
                                       "identity_id": "persistent-name-id", "issuer": "https://idp.example/realms/fixture",
                                       "memberships": memberships}


def test_cleanup_refuses_a_live_native_recovery_operator(tmp_path):
    runtime = tmp_path / "auth-runtime"
    runtime.mkdir()
    shutil.copyfile(ROOT / "auth-runtime/ci.py", runtime / "ci.py")
    (tmp_path / "requirements-auth.txt").write_text("logto==0.2.1\n")
    (runtime / "native_recovery.py").write_text("import time\ntime.sleep(30)\n")
    evidence = runtime / ".state/validation"
    evidence.mkdir(parents=True)
    plan = evidence / ("long-plan-segment-" * 8) / "plan.json"
    plan.parent.mkdir()
    plan.write_text("{}")
    operator = subprocess.Popen([sys.executable, "-B", "auth-runtime/native_recovery.py", "serve", "--plan", str(plan)], cwd=tmp_path)
    try:
        (evidence / "active-native-recovery.json").write_text(json.dumps({"pid": operator.pid, "plan": str(plan)}))
        result = subprocess.run([sys.executable, "-B", str(runtime / "ci.py"), "cleanup"], cwd=tmp_path,
                                env={**os.environ, "COLUMNS": "60"},
                                capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert result.stderr.strip() == "Native recovery operator is still active; verify original recovery before cleanup"
    finally:
        operator.terminate()
        operator.wait(timeout=5)


def test_published_automatic_success_does_not_claim_manual_acceptance(tmp_path):
    runtime = tmp_path / "auth-runtime"
    runtime.mkdir()
    shutil.copyfile(ROOT / "auth-runtime/ci.py", runtime / "ci.py")
    (tmp_path / "requirements-auth.txt").write_text("logto==0.2.1\n")
    manual_node = "test_protocol.py::test_operator_check"
    automatic_node = "test_login.py::test_ordinary_login"
    catalog = {"pending_owners": [30, 32], "phases": {"login": {"nodes": [automatic_node, manual_node]}}}
    (runtime / "required-nodes.json").write_text(json.dumps(catalog))
    (runtime / "manual-security.json").write_text(json.dumps({"functions": [manual_node], "checklist": ["Human protocol acceptance"]}))
    for arguments in (["init", "-q"], ["config", "user.name", "Identity CLI Test"], ["config", "user.email", "identity-cli@openmontage.test"],
                      ["add", "."], ["commit", "-qm", "Fixture for published outcomes"]):
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    evidence = runtime / ".state/validation"
    evidence.mkdir(parents=True)
    summary = {"candidate": sha, "passed": True, "run_id": "automatic-run", "suite": "automated"}
    for name in ("summary", "summary-automated"):
        (evidence / (name + ".json")).write_text(json.dumps(summary))
    phases = {stage: [{"outcome": "passed", "xfail": False}] for stage in ("setup", "call", "teardown")}
    (evidence / "login-result.json").write_text(json.dumps({"passed": True, "run_id": "automatic-run", "nodes": {automatic_node: phases}}))
    result = subprocess.run([sys.executable, "-B", str(runtime / "ci.py"), "publish"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    report = json.loads((evidence / "public/outcomes.json").read_text())
    assert report["automated_passed"] is True and report["completed"] is False
    assert report["manual_pending"] == [manual_node]
    assert report["acceptance_status"] == "PENDING" and report["acceptance_pending_owners"] == [30, 32]


def test_a_failed_new_run_cannot_publish_a_previous_success_receipt(tmp_path):
    runtime = tmp_path / "auth-runtime"
    runtime.mkdir()
    shutil.copyfile(ROOT / "auth-runtime/ci.py", runtime / "ci.py")
    (tmp_path / "requirements-auth.txt").write_text("logto==0.2.1\n")
    node = "test_input.py::test_manual"
    (runtime / "required-nodes.json").write_text(json.dumps({"pending_owners": [32], "phases": {"manual": {"nodes": [node]}}}))
    (runtime / "manual-security.json").write_text(json.dumps({"functions": [node], "checklist": ["Human acceptance"]}))
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
