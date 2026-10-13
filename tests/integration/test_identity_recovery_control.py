"""The private operator handshake must restore originals after any failed drill."""

from concurrent.futures import ThreadPoolExecutor
import json
import time

import pytest


def write(path, value):
    pending = path.with_name(path.name + ".helper.pending")
    pending.write_text(json.dumps(value))
    pending.chmod(0o600)
    pending.replace(path)


def operator(directory, run, manifest_hash, *, reject=False, quiescence_failure=False):
    for request, response in (("source_quiesced", "native_quiesced"),
                              ("snapshot_allowed", "restored_clones_ready"),
                              ("restore_original", "original_ready")):
        deadline = time.monotonic() + 3
        while not (directory / "request.json").exists() or json.loads((directory / "request.json").read_text())["phase"] != request:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        write(directory / "ack.json", {"schema_version": 1, "run_id": "f" * 32 if reject and request == "source_quiesced" else run,
              "phase": response, "manifest_sha256": manifest_hash})
        if (reject or quiescence_failure) and request == "source_quiesced":
            break
    if reject or quiescence_failure:
        deadline = time.monotonic() + 3
        while json.loads((directory / "request.json").read_text())["phase"] != "restore_original":
            assert time.monotonic() < deadline
            time.sleep(0.005)
        write(directory / "ack.json", {"schema_version": 1, "run_id": run, "phase": "original_ready", "manifest_sha256": manifest_hash})


@pytest.mark.parametrize("failure", ["success", "quiesced", "restored"], ids=("success", "quiescence-check-fails", "restored-check-fails"))
def test_root_control_requires_observed_quiescence_and_original_recovery_even_on_failure(tmp_path, failure):
    from tests.fixtures.auth32_recovery_control import RootRecoveryWindow
    tmp_path.chmod(0o700)
    window = RootRecoveryWindow(tmp_path, "a" * 32, "b" * 64, timeout=3)
    observed = []

    def observe():
        observed.append("quiesced")
        if failure == "quiesced":
            raise RuntimeError("controlled restored check")

    with ThreadPoolExecutor(max_workers=1) as pool:
        helper = pool.submit(operator, tmp_path, window.run_id, window.manifest_hash, quiescence_failure=failure == "quiesced")
        if failure != "success":
            with pytest.raises(RuntimeError, match="controlled restored check"):
                with window.clones(observe):
                    raise RuntimeError("controlled restored check")
        else:
            with window.clones(observe):
                observed.append("clones")
        helper.result(timeout=3)
    assert observed == ["quiesced"] + (["clones"] if failure == "success" else [])
    assert json.loads((tmp_path / "request.json").read_text())["phase"] == "restore_original"
    assert json.loads((tmp_path / "ack.json").read_text())["phase"] == "original_ready"
    assert (tmp_path / "request.json").stat().st_mode & 0o777 == 0o600
    assert window.original_ready is True


def test_root_control_rejects_a_stale_acknowledgment_and_still_requests_original_recovery(tmp_path):
    from tests.fixtures.auth32_recovery_control import RootRecoveryWindow
    tmp_path.chmod(0o700)
    window = RootRecoveryWindow(tmp_path, "a" * 32, "b" * 64, timeout=3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        helper = pool.submit(operator, tmp_path, window.run_id, window.manifest_hash, reject=True)
        with pytest.raises(RuntimeError, match="acknowledgment does not match"):
            with window.clones(lambda: pytest.fail("Stale acknowledgment must not permit observation")):
                pytest.fail("Stale acknowledgment must not permit restored checks")
        helper.result(timeout=3)


@pytest.mark.parametrize("kind", ["public-file", "symlink", "unfinished-previous-run"])
def test_recovery_plan_rejects_public_aliased_or_retained_control_before_any_service_operation(tmp_path, monkeypatch, kind):
    from tests.fixtures.auth32_recovery_control import recovery_plan
    directory = tmp_path / "control"
    directory.mkdir(mode=0o700)
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    plan = directory / "plan.json"
    write(plan, {"schema_version": 1, "run_id": "a" * 32, "owner_state_dir": str(owner)})
    if kind == "public-file":
        plan.chmod(0o644)
    elif kind == "symlink":
        alias = directory / "alias.json"
        alias.symlink_to(plan)
        plan = alias
    else:
        write(directory / "request.json", {"phase": "restore_original"})
    monkeypatch.setenv("AUTH32_NATIVE_RECOVERY_PLAN", str(plan))
    with pytest.raises(RuntimeError):
        recovery_plan()


def test_missing_recovery_owner_is_an_explicit_failure_before_native_or_business_work(monkeypatch):
    from tests.fixtures.auth32_recovery_control import recovery_plan
    monkeypatch.delenv("AUTH32_NATIVE_RECOVERY_PLAN", raising=False)
    with pytest.raises(RuntimeError, match="AUTH32_NATIVE_RECOVERY_PLAN"):
        recovery_plan()


def test_fresh_private_fixed_recovery_plan_can_be_read_without_any_native_dependency(tmp_path, monkeypatch):
    from tests.fixtures.auth32_recovery_control import recovery_plan
    tmp_path.chmod(0o700)
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    value = {"schema_version": 1, "run_id": "a" * 32, "owner_state_dir": str(owner)}
    path = tmp_path / "plan.json"
    write(path, value)
    monkeypatch.setenv("AUTH32_NATIVE_RECOVERY_PLAN", str(path))
    assert recovery_plan() == (tmp_path, value)


@pytest.mark.parametrize("container,database", [("a" * 64, "foreign"), ("owner-alias", "logto"), ("--privileged", "keycloak")],
                         ids=("foreign-database", "aliased-container", "option-container"))
def test_native_inventory_cannot_observe_an_unapproved_database_or_container(monkeypatch, container, database):
    from tests.fixtures import auth32_native_recovery

    def no_service_operation(*args, **kwargs):
        pytest.fail("Unapproved Native observation reached the service boundary")

    monkeypatch.setattr(auth32_native_recovery, "native_command", no_service_operation)
    with pytest.raises(RuntimeError, match="restricted to the original owner databases"):
        auth32_native_recovery.native_sql_snapshot(container, database)
