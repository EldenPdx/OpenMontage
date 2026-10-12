"""Identity persistence at the agreed real PostgreSQL public storage seam."""

import pytest

from production.contracts import ContractViolation, TaskCreate
from tests.integration.test_studio_repository import repository, repository_factory, snapshot


def test_unassigned_task_is_hidden_from_personal_workspace(repository):
    legacy = repository.create_task(TaskCreate(brief="An existing unassigned production"), snapshot(), "legacy-create-001")
    with pytest.raises(ContractViolation) as failure:
        repository.get_task_for_workspace(legacy.task_id, {"kind": "personal", "owner_id": "user-a"})
    assert failure.value.code == "not_found"
    assert repository.get_task(legacy.task_id) == legacy


def actor(user="user-a", organization=None):
    from production.contracts import Actor, UserIdentity, WorkspaceContext
    identity = UserIdentity(user_id=user, issuer="https://identity.test/oidc", sub=user)
    workspace = WorkspaceContext(kind="organization" if organization else "personal", owner_id=organization or user)
    return Actor(identity=identity, workspace=workspace)


def test_creation_replay_is_scoped_and_commits_owner_creator_and_actor(repository, repository_factory):
    from concurrent.futures import ThreadPoolExecutor
    contexts = [actor(), actor("user-b"), actor(organization="OrgA"), actor(organization="OrgB")]
    request = TaskCreate(brief="A scoped production")
    def create(context):
        return repository.create_task(request, snapshot(), "same-create-key", workspace=context.workspace, actor=context)
    tasks = [create(context) for context in contexts]
    assert len({task.task_id for task in tasks}) == 4
    with ThreadPoolExecutor(max_workers=4) as threads:
        retries = list(threads.map(create, [contexts[0]] * 8))
    assert {task.task_id for task in retries} == {tasks[0].task_id}
    restarted = repository_factory()
    for context, task in zip(contexts, tasks):
        assert task.ownership.creator == context.identity
        assert task.actor == context
        assert restarted.get_task_for_workspace(task.task_id, context.workspace) == task
        assert restarted.list_tasks_for_workspace(context.workspace) == [task]
        assert restarted.project_ownership(task.project_id, context.workspace) == task.ownership
        assert restarted.project_ids_for_workspace(context.workspace) == [task.project_id]
        assert len(restarted.audit_for_workspace(context.workspace, operation_id=task.task_id)) == 1
    with pytest.raises(ContractViolation) as failure:
        restarted.get_task_for_workspace(tasks[0].task_id, contexts[1].workspace)
    assert failure.value.code == "not_found"
    with pytest.raises(ContractViolation) as failure:
        repository.create_task(TaskCreate(brief="Changed payload"), snapshot(), "same-create-key",
                               workspace=contexts[0].workspace, actor=contexts[0])
    assert failure.value.code == "idempotency_conflict"


def test_accepted_command_actor_survives_session_binding_and_later_cancel(repository):
    from production.contracts import ApprovalDecision, SessionReference, TaskCommand, TaskState
    from tests.integration.test_studio_repository import gate_for
    creator, approver, canceller = actor(organization="OrgA"), actor("user-b", "OrgA"), actor("user-c", "OrgA")
    task = repository.create_task(TaskCreate(brief="An organization production"), snapshot(), "owned-create-001",
                                  workspace=creator.workspace, actor=creator)
    claim = repository.claim_command("worker-a")
    assert claim.context.ownership == task.ownership
    assert claim.context.actor == creator == claim.command.actor
    running = repository.transition(task.task_id, TaskState.RUNNING, expected_version=task.version, fence=claim.context.fence)
    gate = gate_for(running, claim)
    awaiting = repository.put_gate(gate, expected_version=running.version, fence=claim.context.fence)
    repository.finish_command(claim)
    decision = ApprovalDecision(expected_version=awaiting.version, binding=gate.binding, decision="approve")
    approved = repository.decide_gate(decision, idempotency_key="shared-operation-key", workspace=approver.workspace, actor=approver)
    assert repository.decide_gate(decision, idempotency_key="shared-operation-key", workspace=approver.workspace, actor=approver) == approved
    with pytest.raises(ContractViolation) as failure:
        foreign = actor("user-b", "OrgB")
        repository.decide_gate(decision, idempotency_key="shared-operation-key", workspace=foreign.workspace, actor=foreign)
    assert failure.value.code == "not_found"
    next_claim = repository.claim_command("worker-b")
    assert next_claim.context.actor == approver == next_claim.command.actor
    running = repository.transition(task.task_id, TaskState.RUNNING, expected_version=approved.version, fence=next_claim.context.fence)
    command = TaskCommand(command_id="owned-cancel", task_id=task.task_id, run_id=task.run_id,
                          kind="cancel", expected_version=running.version, idempotency_key="shared-operation-key")
    accepted = repository.enqueue_command(command, workspace=canceller.workspace, actor=canceller)
    assert accepted.actor == canceller
    assert repository.enqueue_command(command, workspace=canceller.workspace, actor=canceller) == accepted
    bound = repository.bind_session(next_claim.context, SessionReference(path=next_claim.context.session.path, session_id="exact-pi-session"))
    assert bound.actor == approver
    assert repository.get_task(task.task_id).actor == canceller
    with pytest.raises(ContractViolation):
        repository.enqueue_command(command)
    repository.assert_fence(bound)
    with pytest.raises(ContractViolation) as failure:
        repository.assert_fence(bound.model_copy(update={"actor": canceller}))
    assert failure.value.code == "fence_conflict"


def identity_store(repository, *, key_id="test-key", keys=None):
    import base64
    import os
    from production.identity_store import PostgresIdentityStore
    keys = keys or {key_id: base64.urlsafe_b64decode(os.environ["STUDIO_TEST_SESSION_KEY"])}
    return PostgresIdentityStore(os.environ["STUDIO_TEST_DATABASE_URL"], schema=repository.schema,
                                 encryption_keys=keys, active_key_id=key_id)


def test_encrypted_request_specific_storage_survives_restart_without_user_mixup(repository):
    import json
    import os
    import psycopg
    from psycopg import sql
    from production.contracts import WorkspaceContext
    store = identity_store(repository)
    user_a, user_b = [store.resolve_user("https://identity.test/oidc", subject) for subject in ("subject-a", "subject-b")]
    a = store.create_session(user_a, WorkspaceContext(kind="personal", owner_id=user_a.user_id),
                             sdk_storage={"refreshToken": "private-token-a"})
    b = store.create_session(user_b, WorkspaceContext(kind="personal", owner_id=user_b.user_id),
                             sdk_storage={"refreshToken": "private-token-b"})
    restarted = identity_store(repository)
    assert restarted.resolve_user(user_a.issuer, user_a.sub) == user_a
    storage_a, storage_b = restarted.storage(a.cookie), restarted.storage(b.cookie)
    assert storage_a.get("refreshToken") == "private-token-a"
    storage_a.set("idToken", "private-id-token-a")
    assert storage_b.get("idToken") is None
    assert storage_b.get("refreshToken") == "private-token-b"
    assert identity_store(repository).storage(a.cookie).get("idToken") == "private-id-token-a"
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        rows = connection.execute(sql.SQL("SELECT session_hash,storage FROM {}.auth_sessions").format(sql.Identifier(repository.schema))).fetchall()
    encoded = json.dumps(rows)
    assert all(secret not in encoded for secret in (a.cookie, b.cookie, "private-token-a", "private-token-b", "private-id-token-a"))
    assert "private-token-a" not in repr(a)


def test_session_rotation_is_atomic_and_old_context_and_revoked_storage_fail_closed(repository):
    from concurrent.futures import ThreadPoolExecutor
    from production.contracts import WorkspaceContext
    store = identity_store(repository)
    user = store.resolve_user("https://identity.test/oidc", "subject-a")
    created = store.create_session(user, WorkspaceContext(kind="personal", owner_id=user.user_id), sdk_storage={"refreshToken": "private-token"})
    store.remember_verified_organization("OrgA", "Organization A")
    organization = WorkspaceContext(kind="organization", owner_id="OrgA")
    def rotate(_):
        try:
            return identity_store(repository).rotate_session(created.cookie, expected_revision=created.session.metadata.revision, workspace=organization)
        except ContractViolation:
            return None
    with ThreadPoolExecutor(max_workers=4) as threads:
        results = list(threads.map(rotate, range(4)))
    assert sum(result is not None for result in results) == 1
    rotated = next(result for result in results if result is not None)
    assert store.get_session(created.cookie) is None
    assert rotated.session.metadata.absolute_expires_at == created.session.metadata.absolute_expires_at
    assert rotated.session.metadata.context_id != created.session.metadata.context_id
    assert rotated.session.csrf_token != created.session.csrf_token
    assert store.validate_context(rotated.cookie, rotated.session.csrf_token, rotated.session.metadata.context_id).metadata.workspace == organization
    with pytest.raises(ContractViolation) as failure:
        store.validate_context(rotated.cookie, created.session.csrf_token, created.session.metadata.context_id)
    assert failure.value.code == "auth_context_conflict"
    with pytest.raises(ContractViolation) as failure:
        store.validate_context(rotated.cookie, "wrong-csrf", rotated.session.metadata.context_id)
    assert failure.value.code == "csrf_invalid"
    with pytest.raises(ContractViolation):
        store.update_session(rotated.cookie, expected_revision=999, sdk_storage={"refreshToken": "must-not-write"})
    assert store.storage(rotated.cookie).get("refreshToken") == "private-token"
    store.revoke_session(rotated.cookie)
    store.revoke_session(rotated.cookie)
    assert identity_store(repository).get_session(rotated.cookie) is None
    with pytest.raises(ContractViolation) as failure:
        store.storage(rotated.cookie).set("idToken", "must-not-resurrect")
    assert failure.value.code == "unauthenticated"


def test_login_interaction_consumes_once_and_completes_once_without_partial_session(repository):
    from concurrent.futures import ThreadPoolExecutor
    import secrets
    from production.contracts import WorkspaceContext
    store = identity_store(repository)
    user = store.resolve_user("https://identity.test/oidc", "subject-a")
    workspace = WorkspaceContext(kind="personal", owner_id=user.user_id)
    old = store.create_session(user, workspace, sdk_storage={})
    binding, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    store.create_login_interaction(binding, state=state, nonce="private-nonce", code_verifier="v" * 43,
                                    return_to="/en/studio", organization_hint=None,
                                    sdk_storage={"signInSession": "private-sdk-sign-in"}, previous_session_cookie=old.cookie)
    def consume(_):
        try:
            return identity_store(repository).consume_login_interaction(binding, state)
        except ContractViolation:
            return None
    with ThreadPoolExecutor(max_workers=4) as threads:
        outcomes = list(threads.map(consume, range(4)))
    assert sum(outcome is not None for outcome in outcomes) == 1
    consumed = next(outcome for outcome in outcomes if outcome is not None)
    assert consumed.nonce == "private-nonce"
    assert consumed.sdk_storage["signInSession"] == "private-sdk-sign-in"
    assert store.get_session(binding) is None
    with pytest.raises(ContractViolation):
        store.consume_login_interaction(binding, state)
    completed = store.complete_login(consumed, user, workspace, sdk_storage={"idToken": "verified-test-token"})
    assert store.get_session(old.cookie) is None
    assert store.get_session(completed.cookie).metadata.identity == user
    with pytest.raises(ContractViolation):
        identity_store(repository).complete_login(consumed, user, workspace, sdk_storage={"idToken": "must-not-replay"})


def test_organization_intent_retains_serialization_across_restart_and_invalidates_sessions_atomically(repository):
    from datetime import datetime, timezone
    from production.contracts import Actor, MemberDTO, OperationScope, OrganizationOperationResult, OrganizationOperationTarget, WorkspaceContext
    store = identity_store(repository)
    first_user, second_user = [store.resolve_user("https://identity.test/oidc", subject) for subject in ("admin-a", "member-b")]
    workspace = WorkspaceContext(kind="organization", owner_id="OrgA")
    store.remember_verified_organization("OrgA", "Organization A")
    admin = Actor(identity=first_user, workspace=workspace)
    scope = OperationScope(actor=admin, operation="member:role", resource_id=second_user.user_id, idempotency_key="member-change-001")
    target = OrganizationOperationTarget(organization_id="OrgA", user_id=second_user.user_id, role="viewer")
    session = store.create_session(second_user, workspace, sdk_storage={})
    fresh = store.record_authority_verification(session.cookie, expected_revision=1, verified_at=datetime.now(timezone.utc))
    assert fresh.metadata.authority_verified_at is not None
    accepted = store.begin_organization_intent(scope, request_sha256="a" * 64, target=target)
    assert accepted.disposition == "new"
    submitted = store.advance_organization_intent(scope, expected_revision=accepted.intent.revision, stage="role-submitted", status="submitted")
    restarted = identity_store(repository)
    assert restarted.begin_organization_intent(scope, request_sha256="a" * 64, target=target).disposition == "pending"
    other = scope.model_copy(update={"idempotency_key": "member-change-002"})
    with pytest.raises(ContractViolation) as failure:
        restarted.begin_organization_intent(other, request_sha256="b" * 64, target=target)
    assert failure.value.code == "state_conflict"
    assert restarted.get_organization_intent(scope).operation_id == submitted.operation_id
    result = OrganizationOperationResult(status_code=200, body=MemberDTO(user_id=second_user.user_id, name="Member B", email=None, roles=["viewer"]))
    completed = restarted.complete_organization_intent(scope, expected_revision=submitted.revision, result=result,
                                                       affected_user_ids=[second_user.user_id])
    assert completed.status == "committed"
    assert restarted.get_session(session.cookie).metadata.authority_verified_at is None
    replay = restarted.begin_organization_intent(scope, request_sha256="a" * 64, target=target)
    assert replay.disposition == "replay" and replay.intent.result == result
    assert len(repository.audit_for_workspace(workspace, operation_id=completed.operation_id)) == 2
    assert restarted.begin_organization_intent(other, request_sha256="b" * 64, target=target).disposition == "new"


def test_invitation_ticket_is_hashed_and_acceptance_commits_once_with_its_intent(repository):
    from datetime import datetime, timedelta, timezone
    import secrets
    from production.contracts import Actor, InvitationDTO, InvitationTransition, OperationScope, OrganizationDTO, OrganizationOperationResult, OrganizationOperationTarget, WorkspaceContext
    store = identity_store(repository)
    store.remember_verified_organization("OrgA", "Organization A")
    admin_user, invitee = [store.resolve_user("https://identity.test/oidc", subject) for subject in ("admin-a", "invitee-b")]
    organization = WorkspaceContext(kind="organization", owner_id="OrgA")
    scope = OperationScope(actor=Actor(identity=admin_user, workspace=organization), operation="invitation:create", resource_id="OrgA", idempotency_key="invite-create-001")
    target = OrganizationOperationTarget(organization_id="OrgA", email="invitee@example.test", role="editor")
    first = store.begin_organization_intent(scope, request_sha256="c" * 64, target=target).intent
    first = store.advance_organization_intent(scope, expected_revision=first.revision, stage="invite-submitted", status="submitted")
    invitation = InvitationDTO(invitation_id="invitation-a", organization_id="OrgA", email="invitee@example.test", role="editor", expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    ticket = secrets.token_urlsafe(32)
    stored = store.store_invitation(scope, expected_revision=first.revision, invitation=invitation, ticket=ticket, intended_user_id=invitee.user_id)
    assert ticket not in stored.model_dump_json()
    store.complete_organization_intent(scope, expected_revision=first.revision, result=OrganizationOperationResult(status_code=201, body=invitation))
    assert identity_store(repository).invitation_for_ticket(ticket).invitation_id == "invitation-a"
    acceptance = OperationScope(actor=Actor(identity=invitee, workspace=WorkspaceContext(kind="personal", owner_id=invitee.user_id)), operation="invitation:accept", resource_id="invitation-a", idempotency_key="invite-accept-001")
    accepted = store.begin_organization_intent(acceptance, request_sha256="d" * 64, target=OrganizationOperationTarget(organization_id="OrgA", invitation_id="invitation-a", user_id=invitee.user_id)).intent
    accepted = store.advance_organization_intent(acceptance, expected_revision=accepted.revision, stage="accept-submitted", status="submitted")
    transition = InvitationTransition(invitation_id="invitation-a", status="accepted")
    result = OrganizationOperationResult(status_code=200, body=OrganizationDTO(organization_id="OrgA", name="Organization A", roles=["editor"]))
    committed = store.complete_organization_intent(acceptance, expected_revision=accepted.revision, result=result, affected_user_ids=[invitee.user_id], invitation_transition=transition)
    assert committed.status == "committed"
    with pytest.raises(ContractViolation) as failure:
        store.invitation_for_ticket(ticket)
    assert failure.value.code == "invitation_invalid"
    assert store.begin_organization_intent(acceptance, request_sha256="d" * 64, target=accepted.target).disposition == "replay"


@pytest.mark.parametrize("field", ["refreshToken", "id_token", "client_secret", "ticket", "sdk_storage"])
def test_business_records_reject_identity_secrets_without_state_or_command_changes(repository, field):
    from production.contracts import TaskCommand
    task = repository.create_task(TaskCreate(brief="A legacy local production"), snapshot(), "secret-boundary-create")
    command = TaskCommand(command_id="unsafe-identity", task_id=task.task_id, run_id=task.run_id, kind="cancel",
                          expected_version=task.version, idempotency_key="unsafe-identity-key", payload={field: "private-test-secret"})
    with pytest.raises(ContractViolation) as failure:
        repository.enqueue_command(command)
    assert failure.value.code == "forbidden"
    assert repository.get_task(task.task_id) == task
    assert repository.claim_command("worker-a").command.kind == "start"


def test_unknown_organization_outcome_keeps_recovery_and_durable_owner(repository):
    from production.contracts import ErrorDTO, OperationScope, OrganizationOperationResult, OrganizationOperationTarget
    store = identity_store(repository)
    store.remember_verified_organization("OrgA", "Organization A")
    scope = OperationScope(actor=actor(organization="OrgA"), operation="member:remove", resource_id="member-b", idempotency_key="unknown-removal-001")
    target = OrganizationOperationTarget(organization_id="OrgA", user_id="member-b")
    intent = store.begin_organization_intent(scope, request_sha256="e" * 64, target=target).intent
    intent = store.advance_organization_intent(scope, expected_revision=intent.revision, stage="removal-submitted", status="submitted")
    result = OrganizationOperationResult(status_code=503, body=ErrorDTO(code="outcome_unknown", message="Remote outcome needs reconciliation", retryable=True, recovery_actions=["reconcile"]))
    uncertain = store.complete_organization_intent(scope, expected_revision=intent.revision, result=result)
    assert uncertain.status == "recovery_required"
    assert identity_store(repository).begin_organization_intent(scope, request_sha256="e" * 64, target=target).disposition == "pending"
    with pytest.raises(ContractViolation) as failure:
        store.begin_organization_intent(scope.model_copy(update={"idempotency_key": "unknown-removal-002"}), request_sha256="f" * 64, target=target)
    assert failure.value.code == "state_conflict"
    terminal = store.complete_organization_intent(scope, expected_revision=uncertain.revision,
                                                 result=OrganizationOperationResult(status_code=204))
    assert terminal.status == "committed"
    assert store.begin_organization_intent(scope.model_copy(update={"idempotency_key": "unknown-removal-002"}), request_sha256="f" * 64, target=target).disposition == "new"


@pytest.mark.parametrize("operation", ["invitation:accept", "invitation:cancel"])
def test_successful_invitation_completion_derives_required_ticket_transition(repository, operation):
    from datetime import datetime, timedelta, timezone
    import secrets
    from production.contracts import Actor, InvitationDTO, OperationScope, OrganizationDTO, OrganizationOperationResult, OrganizationOperationTarget, WorkspaceContext
    store = identity_store(repository)
    store.remember_verified_organization("OrgA", "Organization A")
    admin = actor(organization="OrgA")
    user = store.resolve_user("https://identity.test/oidc", "invitee")
    creation = OperationScope(actor=admin, operation="invitation:create", resource_id="OrgA", idempotency_key="invite-create-implicit")
    invite_target = OrganizationOperationTarget(organization_id="OrgA", email="invitee@example.test", role="editor")
    intent = store.begin_organization_intent(creation, request_sha256="a" * 64, target=invite_target).intent
    intent = store.advance_organization_intent(creation, expected_revision=intent.revision, stage="invite-submitted", status="submitted")
    invitation = InvitationDTO(invitation_id="invitation-a", organization_id="OrgA", email="invitee@example.test", role="editor", expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    ticket = secrets.token_urlsafe(32)
    store.store_invitation(creation, expected_revision=intent.revision, invitation=invitation, ticket=ticket, intended_user_id=user.user_id)
    store.complete_organization_intent(creation, expected_revision=intent.revision, result=OrganizationOperationResult(status_code=201, body=invitation))
    operation_actor = Actor(identity=user, workspace=WorkspaceContext(kind="personal", owner_id=user.user_id)) if operation == "invitation:accept" else admin
    scope = OperationScope(actor=operation_actor, operation=operation, resource_id="invitation-a", idempotency_key="invite-finish-implicit")
    target = OrganizationOperationTarget(organization_id="OrgA", invitation_id="invitation-a", user_id=user.user_id if operation == "invitation:accept" else None)
    intent = store.begin_organization_intent(scope, request_sha256="b" * 64, target=target).intent
    intent = store.advance_organization_intent(scope, expected_revision=intent.revision, stage="finish-submitted", status="submitted")
    body = OrganizationDTO(organization_id="OrgA", name="Organization A", roles=["editor"]) if operation == "invitation:accept" else invitation.model_copy(update={"status": "cancelled"})
    completed = store.complete_organization_intent(scope, expected_revision=intent.revision, result=OrganizationOperationResult(status_code=200, body=body))
    assert completed.status == "committed"
    with pytest.raises(ContractViolation) as failure:
        identity_store(repository).invitation_for_ticket(ticket)
    assert failure.value.code == "invitation_invalid"


@pytest.mark.parametrize("decision_kind", ["approve", "revise"])
def test_same_actor_key_is_independent_across_successive_approval_gates(repository, decision_kind):
    from production.contracts import ApprovalDecision, TaskState
    from tests.integration.test_studio_repository import gate_for
    context = actor(organization="OrgA")
    task = repository.create_task(TaskCreate(brief="A production with successive gates"), snapshot(), "successive-gates-create",
                                  workspace=context.workspace, actor=context)
    claim = repository.claim_command("worker-a")
    running = repository.transition(task.task_id, TaskState.RUNNING, expected_version=task.version, fence=claim.context.fence)
    commands = []
    for gate_id in ("gate-first", "gate-second"):
        gate = gate_for(running, claim)
        gate = gate.model_copy(update={"binding": gate.binding.model_copy(update={"gate_id": gate_id})})
        awaiting = repository.put_gate(gate, expected_version=running.version, fence=claim.context.fence)
        repository.finish_command(claim)
        decision = ApprovalDecision(expected_version=awaiting.version, binding=gate.binding, decision=decision_kind,
                                     comment="Revise the current proposal" if decision_kind == "revise" else "")
        accepted = repository.decide_gate(decision, idempotency_key="same-gate-key", workspace=context.workspace, actor=context)
        assert repository.decide_gate(decision, idempotency_key="same-gate-key", workspace=context.workspace, actor=context) == accepted
        claim = repository.claim_command("worker-a")
        assert claim.command.actor == context
        assert claim.command.payload["gate_id"] == gate_id
        commands.append(claim.command.command_id)
        running = repository.transition(task.task_id, TaskState.RUNNING, expected_version=accepted.version, fence=claim.context.fence)
    assert len(set(commands)) == 2


def test_owned_creation_marks_root_and_project_and_failed_commit_retains_protection(repository, tmp_path):
    import os
    from production.ownership_guard import MARKER, has_guard
    from production.repository import PostgresRepository
    root = tmp_path / "owned-projects"
    repo = PostgresRepository(os.environ["STUDIO_TEST_DATABASE_URL"], schema=repository.schema, projects_dir=root)
    context = actor()
    task = repo.create_task(TaskCreate(brief="A guarded owned production"), snapshot(), "guarded-create-001",
                            workspace=context.workspace, actor=context)
    assert has_guard(root) and has_guard(root / task.project_id)
    before = (root / MARKER).read_bytes()
    bad = root / "bad-root"
    bad.mkdir()
    (bad / MARKER).write_bytes(b"inconsistent-classification")
    failed = PostgresRepository(os.environ["STUDIO_TEST_DATABASE_URL"], schema=repository.schema, projects_dir=bad)
    with pytest.raises(ContractViolation) as failure:
        failed.create_task(TaskCreate(brief="Must not commit"), snapshot(), "guarded-create-002", workspace=context.workspace, actor=context)
    assert failure.value.code == "file_conflict"
    assert has_guard(bad)
    assert (root / MARKER).read_bytes() == before
    assert len(repo.list_tasks_for_workspace(context.workspace)) == 1


def test_ciphertext_swap_tamper_wrong_key_and_rotation_key_retention_fail_closed(repository):
    import base64
    import os
    import secrets
    import psycopg
    from psycopg import sql
    from production.contracts import WorkspaceContext
    store = identity_store(repository)
    a_user, b_user = [store.resolve_user("https://identity.test/oidc", subject) for subject in ("crypto-a", "crypto-b")]
    a = store.create_session(a_user, WorkspaceContext(kind="personal", owner_id=a_user.user_id), sdk_storage={"refreshToken": "private-crypto-a"})
    b = store.create_session(b_user, WorkspaceContext(kind="personal", owner_id=b_user.user_id), sdk_storage={"refreshToken": "private-crypto-b"})
    key = base64.urlsafe_b64decode(os.environ["STUDIO_TEST_SESSION_KEY"])
    wrong_key = bytes([key[0] ^ 1]) + key[1:]
    with pytest.raises(ContractViolation) as failure:
        identity_store(repository, keys={"test-key": wrong_key}).get_session(a.cookie)
    assert failure.value.code == "identity_unavailable"
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("UPDATE {}.auth_sessions SET storage=(SELECT storage FROM {}.auth_sessions WHERE session_hash=%s) WHERE session_hash=%s").format(sql.Identifier(repository.schema), sql.Identifier(repository.schema)), (a.session.metadata.session_ref, b.session.metadata.session_ref))
    with pytest.raises(ContractViolation) as failure:
        store.get_session(b.cookie)
    assert failure.value.code == "identity_unavailable"
    assert store.storage(a.cookie).get("refreshToken") == "private-crypto-a"
    next_key = secrets.token_bytes(32)
    rolling = identity_store(repository, key_id="next-key", keys={"test-key": key, "next-key": next_key})
    rotated = rolling.rotate_session(a.cookie, expected_revision=1, workspace=a.session.metadata.workspace)
    assert rolling.storage(rotated.cookie).get("refreshToken") == "private-crypto-a"
    with pytest.raises(ContractViolation) as failure:
        store.get_session(rotated.cookie)
    assert failure.value.code == "identity_unavailable"
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("UPDATE {}.auth_sessions SET storage=jsonb_set(storage,'{{ciphertext}}','\"broken\"'::jsonb) WHERE session_hash=%s").format(sql.Identifier(repository.schema)), (rotated.session.metadata.session_ref,))
    with pytest.raises(ContractViolation) as failure:
        rolling.get_session(rotated.cookie)
    assert failure.value.code == "identity_unavailable"


def test_real_idle_absolute_and_interaction_expiry_never_extend_or_authenticate(repository):
    import secrets
    import time
    from production.contracts import WorkspaceContext
    store = identity_store(repository)
    user = store.resolve_user("https://identity.test/oidc", "expiring-user")
    workspace = WorkspaceContext(kind="personal", owner_id=user.user_id)
    idle = store.create_session(user, workspace, sdk_storage={}, absolute_seconds=10, idle_seconds=1)
    absolute = store.create_session(user, workspace, sdk_storage={}, absolute_seconds=1, idle_seconds=1)
    touched = store.touch_session(absolute.cookie)
    assert touched.metadata.absolute_expires_at == absolute.session.metadata.absolute_expires_at
    binding, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    store.create_login_interaction(binding, state=state, nonce="nonce", code_verifier="v" * 43, return_to="/en/studio", organization_hint=None, sdk_storage={}, lifetime_seconds=1)
    time.sleep(1.1)
    assert store.get_session(idle.cookie) is None and store.get_session(absolute.cookie) is None
    with pytest.raises(ContractViolation):
        store.touch_session(absolute.cookie)
    with pytest.raises(ContractViolation):
        store.consume_login_interaction(binding, state)
    assert store.get_session(binding) is None


def test_prepared_denial_releases_serialization_but_submitted_intent_cannot_be_rejected(repository):
    from production.contracts import ErrorDTO, OperationScope, OrganizationOperationResult, OrganizationOperationTarget
    store = identity_store(repository)
    store.remember_verified_organization("OrgA", "Organization A")
    scope = OperationScope(actor=actor(organization="OrgA"), operation="member:remove", resource_id="member-b", idempotency_key="prepared-denial-001")
    target = OrganizationOperationTarget(organization_id="OrgA", user_id="member-b")
    intent = store.begin_organization_intent(scope, request_sha256="a" * 64, target=target).intent
    result = OrganizationOperationResult(status_code=409, body=ErrorDTO(code="last_admin", message="Last administrator cannot be removed"))
    denied = store.reject_prepared_organization_intent(scope, expected_revision=intent.revision, result=result)
    assert denied.status == "failed"
    next_scope = scope.model_copy(update={"idempotency_key": "prepared-denial-002"})
    next_intent = store.begin_organization_intent(next_scope, request_sha256="b" * 64, target=target).intent
    submitted = store.advance_organization_intent(next_scope, expected_revision=next_intent.revision, stage="remove-submitted", status="submitted")
    with pytest.raises(ContractViolation):
        store.reject_prepared_organization_intent(next_scope, expected_revision=submitted.revision, result=result)
    assert store.get_organization_intent(next_scope).status == "submitted"


def test_member_completion_derives_target_session_invalidation(repository):
    from datetime import datetime, timezone
    from production.contracts import Actor, MemberDTO, OperationScope, OrganizationOperationResult, OrganizationOperationTarget, WorkspaceContext
    store = identity_store(repository)
    store.remember_verified_organization("OrgA", "Organization A")
    user = store.resolve_user("https://identity.test/oidc", "target-member")
    workspace = WorkspaceContext(kind="organization", owner_id="OrgA")
    session = store.create_session(user, workspace, sdk_storage={})
    store.record_authority_verification(session.cookie, expected_revision=1, verified_at=datetime.now(timezone.utc))
    scope = OperationScope(actor=actor(organization="OrgA"), operation="member:role", resource_id=user.user_id, idempotency_key="implicit-invalidation")
    target = OrganizationOperationTarget(organization_id="OrgA", user_id=user.user_id, role="viewer")
    intent = store.begin_organization_intent(scope, request_sha256="a" * 64, target=target).intent
    intent = store.advance_organization_intent(scope, expected_revision=intent.revision, stage="role-submitted", status="submitted")
    result = OrganizationOperationResult(status_code=200, body=MemberDTO(user_id=user.user_id, roles=["viewer"]))
    store.complete_organization_intent(scope, expected_revision=intent.revision, result=result)
    assert store.get_session(session.cookie).metadata.authority_verified_at is None
