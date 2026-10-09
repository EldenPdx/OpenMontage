"""Repository behavior against a real PostgreSQL schema, never a memory substitute."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import time
from uuid import uuid4

import pytest

from production.contracts import (
    ApprovalBinding, ApprovalDecision, ApprovalRequest, ApprovalScope, ConfigSnapshot,
    CallIntent, ContractViolation, FileReference, FileWriteIntent, SessionReference,
    TaskCommand, TaskCreate, TaskState, canonical_sha256,
)


@pytest.fixture
def repository_factory():
    try:
        import psycopg
    except ImportError:
        if os.environ.get("STUDIO_REQUIRE_INTEGRATION"):
            pytest.fail("Studio integration requires psycopg")
        pytest.skip("Install psycopg for real PostgreSQL integration")
    from psycopg import sql
    from production.repository import PostgresRepository

    dsn = os.environ.get("STUDIO_TEST_DATABASE_URL") or os.environ.get("STUDIO_DATABASE_URL")
    local = Path(__file__).resolve().parents[2] / ".runtime/studio/database-local.env"
    if not dsn and local.is_file():
        from dotenv import dotenv_values
        dsn = dotenv_values(local).get("STUDIO_DATABASE_URL")
    if not dsn:
        if os.environ.get("STUDIO_REQUIRE_INTEGRATION"):
            pytest.fail("Studio integration requires a real PostgreSQL database")
        pytest.skip("Set STUDIO_TEST_DATABASE_URL for real PostgreSQL integration")
    schema = "studio_test_" + uuid4().hex
    def connect(*, previous_version=False, missing_schema=False):
        if previous_version:
            with psycopg.connect(dsn) as connection:
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                connection.execute("CREATE TABLE schema_migrations (version integer PRIMARY KEY)")
                migration = Path(__file__).resolve().parents[2] / "production/migrations/001_control_plane.sql"
                connection.execute(migration.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migrations VALUES (1)")
        repo = PostgresRepository(dsn, schema=schema + "_missing" if missing_schema else schema)
        if not missing_schema:
            repo.migrate()
        return repo

    try:
        yield connect
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def repository(repository_factory):
    return repository_factory()


def snapshot():
    return ConfigSnapshot(
        profile_id="local", provider="local", model="test-model", api="openai-responses",
        configuration_sha256="a" * 64, price_status="quoted",
    )


def create(repository, *, key="create-task-1", budget=10_000_000):
    return repository.create_task(TaskCreate(brief="A short local video", budget_usd_micros=budget), snapshot(), key)


def test_migration_and_concurrent_creation_are_idempotent_and_durable(repository, repository_factory):
    with ThreadPoolExecutor(max_workers=4) as workers:
        tasks = list(workers.map(lambda _: create(repository), range(8)))
    assert len({task.task_id for task in tasks}) == 1
    assert tasks[0].state == TaskState.QUEUED
    repository.migrate()
    restarted = repository_factory()
    assert restarted.get_task(tasks[0].task_id) == tasks[0]
    assert len(restarted.list_tasks()) == 1
    with pytest.raises(ContractViolation, match="idempotency"):
        restarted.create_task(TaskCreate(brief="Different request"), snapshot(), "create-task-1")


def test_two_workers_claim_once_and_stale_fences_cannot_mutate(repository):
    task = create(repository)
    with ThreadPoolExecutor(max_workers=2) as workers:
        claims = list(workers.map(repository.claim_command, ["worker-a", "worker-b"]))
    claim = next(item for item in claims if item is not None)
    assert sum(item is not None for item in claims) == 1
    assert claim.command.kind == "start"
    assert claim.context.task_id == task.task_id
    running = repository.transition(task.task_id, TaskState.RUNNING, expected_version=1, fence=claim.context.fence)
    assert running.version == 2
    refreshed = repository.heartbeat(claim)
    assert refreshed.context == claim.context
    with pytest.raises(ContractViolation, match="fence"):
        repository.assert_fence(claim.context.model_copy(update={"fence": claim.context.fence - 1}))
    with pytest.raises(ContractViolation, match="version"):
        repository.transition(task.task_id, TaskState.FAILED, expected_version=1, fence=claim.context.fence)
    repository.finish_command(claim)
    repository.finish_command(claim)
    with pytest.raises(ContractViolation, match="fence"):
        repository.assert_fence(claim.context)
    assert repository.claim_command("worker-c") is None
    events = repository.events(task.task_id)
    assert [event.type for event in events] == ["state", "state"]
    assert repository.events(task.task_id, after=events[0].event_id) == events[1:]


def gate_for(task, claim):
    scope = ApprovalScope(provider="local", model="test-model", budget_usd_micros=task.cost.budget_usd_micros, configuration_sha256="a" * 64)
    binding = ApprovalBinding(task_id=task.task_id, run_id=task.run_id, gate_id="proposal-1", checkpoint_revision=1, checkpoint_sha256="b" * 64, artifact_revision=1, artifact_sha256="c" * 64, scope_sha256=canonical_sha256(scope))
    return ApprovalRequest(binding=binding, stage="proposal", artifact=FileReference(path="artifacts/proposal_packet.json", revision=1, sha256="c" * 64), checkpoint=FileReference(path="checkpoint_proposal.json", revision=1, sha256="b" * 64), scope=scope, summary="Review the proposed local video")


def test_approval_is_version_bound_consumed_once_and_requeues_atomically(repository, repository_factory):
    task = create(repository)
    claim = repository.claim_command("worker-a")
    running = repository.transition(task.task_id, TaskState.RUNNING, expected_version=1, fence=claim.context.fence)
    gate = gate_for(running, claim)
    awaiting = repository.put_gate(gate, expected_version=running.version, fence=claim.context.fence)
    assert repository.approved_gate(task.task_id, gate.binding.gate_id) is None
    repository.finish_command(claim)
    restarted = repository_factory()
    assert restarted.get_task(task.task_id).approval == gate
    stale = ApprovalDecision(expected_version=awaiting.version - 1, binding=gate.binding, decision="approve")
    with pytest.raises(ContractViolation, match="version"):
        restarted.decide_gate(stale, idempotency_key="approve-stale")
    decision = stale.model_copy(update={"expected_version": awaiting.version})
    evidence_checks = []

    def validate_evidence(task, current_gate):
        evidence_checks.append(current_gate.binding)

    approved = restarted.decide_gate(decision, idempotency_key="approve-current", validate_evidence=validate_evidence)
    assert restarted.approved_gate(task.task_id, gate.binding.gate_id).binding == gate.binding
    assert approved.state == TaskState.QUEUED
    assert restarted.decide_gate(decision, idempotency_key="approve-current", validate_evidence=validate_evidence) == approved
    assert evidence_checks == [gate.binding]
    with pytest.raises(ContractViolation):
        restarted.decide_gate(decision, idempotency_key="approve-again")
    resumed = restarted.claim_command("worker-b")
    assert resumed.command.kind == "continue"
    assert resumed.command.payload["gate_id"] == gate.binding.gate_id
    assert restarted.claim_command("worker-c") is None


@pytest.mark.parametrize("decision_kind", ["reject", "abort"])
def test_reject_blocks_without_cancel_and_abort_queues_cancel_preserving_fees(repository, repository_factory, decision_kind):
    task = create(repository)
    claim = repository.claim_command("worker-a")
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=task.version, fence=claim.context.fence)
    spent = repository.reserve_call(intent_for(task, claim, "spent-media", 400_000))
    repository.update_call(spent.model_copy(update={"status": "settled", "actual_usd_micros": 250_000}))
    hold = repository.reserve_call(intent_for(task, claim, "held-media", 200_000))
    repository.update_call(hold.model_copy(update={"status": "outcome_unknown"}))
    running = repository.get_task(task.task_id)
    gate = gate_for(running, claim)
    awaiting = repository.put_gate(gate, expected_version=running.version, fence=claim.context.fence)
    repository.finish_command(claim)
    restarted = repository_factory()
    decision = ApprovalDecision(expected_version=awaiting.version, binding=gate.binding, decision=decision_kind)
    with pytest.raises(ContractViolation, match="version"):
        restarted.decide_gate(decision.model_copy(update={"expected_version": awaiting.version - 1}), idempotency_key="stale-decision")
    changed_binding = gate.binding.model_copy(update={"artifact_sha256": "e" * 64})
    with pytest.raises(ContractViolation, match="Approval"):
        restarted.decide_gate(decision.model_copy(update={"binding": changed_binding}), idempotency_key="wrong-artifact")
    evidence = []
    result = restarted.decide_gate(decision, idempotency_key="human-decision", validate_evidence=lambda task, current: evidence.append(current.binding))
    assert result.state == (TaskState.BLOCKED if decision_kind == "reject" else TaskState.CANCEL_REQUESTED)
    assert result.approval.status == ("rejected" if decision_kind == "reject" else "aborted")
    assert result.approval.binding == gate.binding
    assert result.cost == awaiting.cost
    assert (result.cost.spent_usd_micros, result.cost.reserved_usd_micros, result.cost.unknown_call_count) == (250_000, 200_000, 1)
    assert restarted.decide_gate(decision, idempotency_key="human-decision", validate_evidence=lambda task, current: evidence.append(current.binding)) == result
    assert evidence == [gate.binding]
    assert restarted.get_task(task.task_id) == result
    assert restarted.approved_gate(task.task_id, gate.binding.gate_id) is None
    with pytest.raises(ContractViolation, match="pending approval"):
        restarted.decide_gate(decision.model_copy(update={"expected_version": result.version}), idempotency_key="decide-again")
    if decision_kind == "reject":
        assert result.error.code == "approval_conflict"
        assert "resume" in result.error.recovery_actions
        assert restarted.claim_command("worker-b") is None
        assert restarted.pending_cancel(task.task_id) is None
    else:
        pending = restarted.claim_command("worker-b")
        assert pending.command.kind == "cancel"
        assert pending.command.payload["gate_id"] == gate.binding.gate_id
        assert restarted.claim_command("worker-c") is None
        restarted.finish_command(pending)


def intent_for(task, claim, call_id, amount):
    return CallIntent(call_id=call_id, task_id=task.task_id, run_id=task.run_id, fence=claim.context.fence, kind="tool", operation="generate", provider="local", model="test-model", request_sha256="d" * 64, price_status="quoted", reserved_usd_micros=amount)


def test_budget_reservations_are_atomic_and_unknown_outcomes_keep_the_hold(repository, repository_factory):
    task = create(repository, budget=1_000_000)
    claim = repository.claim_command("worker-a")
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=1, fence=claim.context.fence)

    def reserve(call_id):
        try:
            return repository.reserve_call(intent_for(task, claim, call_id, 700_000))
        except ContractViolation as error:
            assert error.code == "budget_exceeded"
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        calls = list(workers.map(reserve, ["call-a", "call-b"]))
    call = next(item for item in calls if item is not None)
    assert sum(item is not None for item in calls) == 1
    assert repository.reserve_call(call) == call
    assert repository.get_task(task.task_id).cost.reserved_usd_micros == 700_000
    settled = call.model_copy(update={"status": "settled", "actual_usd_micros": 400_000})
    assert repository.update_call(settled) == settled
    assert repository.update_call(settled) == settled
    unknown = repository.reserve_call(intent_for(task, claim, "call-unknown", 500_000))
    unknown = unknown.model_copy(update={"status": "outcome_unknown"})
    repository.update_call(unknown)
    restarted = repository_factory()
    assert restarted.get_call("call-unknown") == unknown
    cost = restarted.get_task(task.task_id).cost
    assert (cost.spent_usd_micros, cost.reserved_usd_micros, cost.unknown_call_count) == (400_000, 500_000, 1)
    with pytest.raises(ContractViolation, match="submit"):
        restarted.update_call(unknown.model_copy(update={"status": "submitted"}))
    received = restarted.reserve_call(intent_for(task, claim, "call-unpriced-received", 50_000).model_copy(update={"price_status": "unquoted"}))
    restarted.update_call(received.model_copy(update={"status": "receipted", "usage": {"total": 15}}))
    cost = restarted.get_task(task.task_id).cost
    assert (cost.unknown_call_count, cost.price_status, cost.reserved_usd_micros) == (2, "unquoted", 550_000)


def test_file_intent_and_exact_session_survive_expired_worker_reconciliation(repository, repository_factory):
    task = create(repository)
    claim = repository.claim_command("worker-a", lease_seconds=1)
    running = repository.transition(task.task_id, TaskState.RUNNING, expected_version=1, fence=claim.context.fence)
    session = SessionReference(path=claim.context.session.path, session_id="exact-session")
    assert repository.bind_session(claim.context, session).session == session
    intent = FileWriteIntent(intent_id="file-a", task_id=task.task_id, run_id=task.run_id, fence=claim.context.fence, target=FileReference(path="artifacts/script.json", revision=1, sha256="e" * 64))
    assert repository.record_file_intent(intent) == intent
    applied = intent.model_copy(update={"status": "applied"})
    repository.record_file_intent(applied)
    call = repository.reserve_call(intent_for(task, claim, "call-pending", 100_000))
    repository.update_call(call.model_copy(update={"status": "submitted"}))
    time.sleep(1.1)
    restarted = repository_factory()
    assert restarted.claim_command("worker-b") is None
    assert restarted.recover_expired_leases() == 1
    recovering = restarted.get_task(task.task_id)
    assert recovering.state == TaskState.RECOVERY_REQUIRED
    assert restarted.get_call("call-pending").status == "outcome_unknown"
    unresolved = restarted.unresolved_intents(task.task_id)
    assert {item.call_id if isinstance(item, CallIntent) else item.intent_id for item in unresolved} == {"call-pending", "file-a"}
    with pytest.raises(ContractViolation, match="fence"):
        restarted.assert_fence(claim.context)
    with pytest.raises(ContractViolation, match="reconciliation"):
        restarted.enqueue_command(TaskCommand(command_id="resume-a", task_id=task.task_id, run_id=task.run_id, kind="resume", expected_version=recovering.version, idempotency_key="resume-unknown"))
    assert restarted.recover_expired_leases() == 0
    assert restarted.claim_command("worker-c") is None
    context = restarted.recovery_context(task.task_id)
    assert context.session == session
    with pytest.raises(ContractViolation, match="exit"):
        restarted.release_recovered_lease(task.task_id, fence=context.fence, terminated=False)
    restarted.release_recovered_lease(task.task_id, fence=context.fence, terminated=True)
    restarted.record_file_intent(applied.model_copy(update={"fence": context.fence, "status": "reconciled"}))
    unknown = restarted.get_call(call.call_id)
    restarted.update_call(unknown.model_copy(update={"fence": context.fence, "status": "failed", "actual_usd_micros": 0}))
    resume = TaskCommand(command_id="resume-b", task_id=task.task_id, run_id=task.run_id, kind="resume", expected_version=recovering.version, idempotency_key="resume-cleared")
    queued = restarted.enqueue_command(resume)
    assert restarted.enqueue_command(resume) == queued
    recovered_claim = restarted.claim_command("worker-d")
    assert recovered_claim.command.kind == "resume"
    assert recovered_claim.context.session == session


def test_cancel_is_atomic_and_idempotent_and_releases_global_slot_after_stop(repository):
    first = create(repository)
    second = create(repository, key="create-task-2")
    claim = repository.claim_command("worker-a")
    assert repository.claim_command("worker-b") is None
    running = repository.transition(first.task_id, TaskState.RUNNING, expected_version=1, fence=claim.context.fence)
    call = repository.reserve_call(intent_for(first, claim, "call-before-cancel", 10_000))
    cancel = TaskCommand(command_id="cancel-a", task_id=first.task_id, run_id=first.run_id, kind="cancel", expected_version=running.version, idempotency_key="cancel-client-1", payload={"reason": "Stop this video"})
    queued_cancel = repository.enqueue_command(cancel)
    assert repository.enqueue_command(cancel) == queued_cancel
    cancelled_request = repository.get_task(first.task_id)
    assert cancelled_request.state == TaskState.CANCEL_REQUESTED
    with pytest.raises(ContractViolation, match="idempotency"):
        repository.enqueue_command(cancel.model_copy(update={"payload": {"reason": "Different request"}}))
    with pytest.raises(ContractViolation, match="authorized"):
        repository.reserve_call(call)
    repository.update_call(call.model_copy(update={"status": "settled", "actual_usd_micros": 10_000}))
    cancelled = repository.transition(first.task_id, TaskState.CANCELLED, expected_version=cancelled_request.version, fence=claim.context.fence)
    repository.finish_command(claim)
    replayed = repository.enqueue_command(cancel)
    assert replayed.command_id == queued_cancel.command_id
    assert replayed.status == "failed"
    assert repository.get_task(first.task_id) == cancelled
    next_claim = repository.claim_command("worker-b")
    assert next_claim.context.task_id == second.task_id


def test_profile_turn_limits_and_credential_records_fail_before_reservation(repository):
    task = repository.create_task(TaskCreate(brief="A local video"), snapshot().model_copy(update={"max_turns": 1}), "create-turn-limit")
    unsafe = TaskCommand(command_id="unsafe-command", task_id=task.task_id, run_id=task.run_id, kind="cancel", expected_version=task.version, idempotency_key="unsafe-secret-1", payload={"env": {"NEW_API_KEY": "must-not-persist"}})
    with pytest.raises(ContractViolation, match="Credentials"):
        repository.enqueue_command(unsafe)
    assert repository.get_task(task.task_id) == task
    claim = repository.claim_command("worker-a")
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=1, fence=claim.context.fence)
    model = intent_for(task, claim, "model-first", 100).model_copy(update={"kind": "model", "operation": "inference"})
    wrong_profile = model.model_copy(update={"provider": "other"})
    with pytest.raises(ContractViolation, match="profile"):
        repository.reserve_call(wrong_profile)
    repository.reserve_call(model)
    with pytest.raises(ContractViolation, match="turn limit"):
        repository.reserve_call(model.model_copy(update={"call_id": "model-second"}))
    assert repository.get_task(task.task_id).cost.reserved_usd_micros == 100


def test_existing_control_plane_upgrades_to_call_and_file_journals(repository_factory):
    repository = repository_factory(previous_version=True)
    assert repository.migrate() == 2
    task = create(repository)
    claim = repository.claim_command("worker-a")
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=1, fence=claim.context.fence)
    call = repository.reserve_call(intent_for(task, claim, "upgraded-call", 100))
    assert repository.get_call(call.call_id) == call
    file = FileWriteIntent(intent_id="upgraded-file", task_id=task.task_id, run_id=task.run_id, fence=claim.context.fence, target=FileReference(path="artifacts/script.json", revision=1, sha256="f" * 64))
    assert repository.record_file_intent(file) == file


def test_event_cursor_rejects_unknown_future_and_other_task_ids(repository):
    first = create(repository)
    second = create(repository, key="create-cursor-2")
    cursor = repository.events(first.task_id)[0].event_id
    assert repository.events(first.task_id, after=cursor) == []
    for task_id, after in [(first.task_id, 99_999_999), (second.task_id, cursor)]:
        with pytest.raises(ContractViolation) as error:
            repository.events(task_id, after=after)
        assert error.value.code == "cursor_expired"


def test_cancel_survives_expired_worker_recovery_without_refunding_unknown_call(repository):
    task = create(repository)
    claim = repository.claim_command("worker-a", lease_seconds=1)
    running = repository.transition(task.task_id, TaskState.RUNNING, expected_version=1, fence=claim.context.fence)
    repository.reserve_call(intent_for(task, claim, "cancel-unknown", 100_000))
    command = TaskCommand(command_id="cancel-recovery", task_id=task.task_id, run_id=task.run_id, kind="cancel", expected_version=running.version, idempotency_key="cancel-recovery-client")
    repository.enqueue_command(command)
    time.sleep(1.1)
    repository.recover_expired_leases()
    assert repository.pending_cancel(task.task_id).command_id == command.command_id
    context = repository.recovery_context(task.task_id)
    repository.release_recovered_lease(task.task_id, fence=context.fence, terminated=True)
    cancelled = repository.get_task(task.task_id)
    assert cancelled.state == TaskState.CANCELLED
    assert cancelled.cost.reserved_usd_micros == 100_000
    assert repository.pending_cancel(task.task_id) is None


def test_database_query_failure_has_safe_service_error_without_sql_or_credentials(repository_factory):
    unavailable = repository_factory(missing_schema=True)
    with pytest.raises(ContractViolation) as error:
        unavailable.get_task("task-unreachable")
    assert error.value.code == "dependency_unavailable"
    assert "SELECT" not in str(error.value)
    assert "postgresql://" not in str(error.value)
