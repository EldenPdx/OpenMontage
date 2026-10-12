"""BFF session and trusted root classification at the real PostgreSQL seam."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import socket
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
import pytest

from production.contracts import ContractViolation, ResourceOwnership, SessionHandle, TaskCreate, WorkspaceContext
from production.identity_store import PostgresIdentityStore
from production.repository import PostgresRepository
from tests.integration.test_identity_persistence import actor, identity_store
from tests.integration.test_studio_repository import repository, repository_factory, snapshot


@pytest.fixture
def session(repository):
    store = identity_store(repository)
    user = store.resolve_user("https://identity.test/oidc", "subject-a")
    handle = store.create_session(user, WorkspaceContext(kind="personal", owner_id=user.user_id),
                                  sdk_storage={"refreshToken": "private-token"})
    return store, handle


def test_stale_logout_after_sdk_update_cannot_revoke_current_session(session):
    store, handle = session
    updated = store.update_session(handle.cookie, expected_revision=handle.session.metadata.revision,
                                   sdk_storage={"refreshToken": "updated-token"})
    with pytest.raises(ContractViolation) as failure:
        store.revoke_session(handle.cookie, expected_revision=handle.session.metadata.revision)
    assert failure.value.code == "auth_context_conflict"
    assert store.get_session(handle.cookie) == updated


def test_current_logout_cas_revokes_once_and_trusted_default_remains_idempotent(session):
    store, handle = session
    store.revoke_session(handle.cookie, expected_revision=handle.session.metadata.revision)
    assert store.get_session(handle.cookie) is None
    with pytest.raises(ContractViolation) as failure:
        store.revoke_session(handle.cookie, expected_revision=handle.session.metadata.revision)
    assert failure.value.code == "auth_context_conflict"
    store.revoke_session(handle.cookie)
    store.revoke_session(None)
    assert store.get_session(handle.cookie) is None


@pytest.mark.parametrize("change", ["rotate", "update"])
@pytest.mark.parametrize("winner", ["change", "logout"])
def test_concurrent_logout_conflicts_with_rotation_or_sdk_update(repository, session, change, winner):
    store, handle = session
    store.remember_verified_organization("OrgA", "Organization A")
    organization = WorkspaceContext(kind="organization", owner_id="OrgA")
    names = {action: "auth_shared26_" + uuid4().hex for action in ("change", "logout")}
    stores = {
        action: PostgresIdentityStore(
            make_conninfo(os.environ["STUDIO_TEST_DATABASE_URL"], application_name=name),
            schema=repository.schema,
            encryption_keys={"test-key": base64.urlsafe_b64decode(os.environ["STUDIO_TEST_SESSION_KEY"])},
            active_key_id="test-key",
        )
        for action, name in names.items()
    }
    revision = handle.session.metadata.revision

    def mutate():
        if change == "rotate":
            return stores["change"].rotate_session(handle.cookie, expected_revision=revision, workspace=organization)
        return stores["change"].update_session(handle.cookie, expected_revision=revision,
                                               sdk_storage={"refreshToken": "updated-token"})

    actions = {
        "change": mutate,
        "logout": lambda: stores["logout"].revoke_session(handle.cookie, expected_revision=revision),
    }
    loser = "logout" if winner == "change" else "change"
    with ThreadPoolExecutor(max_workers=2) as threads, \
            psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as blocker, \
            psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"], autocommit=True) as observer:
        # Queue both public operations on a real row lock, then release the chosen first waiter.
        blocker.execute(sql.SQL("SELECT session_hash FROM {}.auth_sessions WHERE session_hash=%s FOR UPDATE")
                        .format(sql.Identifier(repository.schema)), (handle.session.metadata.session_ref,))

        def wait_for_lock(action):
            deadline = time.monotonic() + 3
            while not observer.execute(
                "SELECT 1 FROM pg_stat_activity WHERE application_name=%s AND wait_event_type='Lock'",
                (names[action],),
            ).fetchone():
                assert time.monotonic() < deadline, "Public session operation did not reach the real PostgreSQL lock"
                time.sleep(0.01)

        first = threads.submit(actions[winner])
        wait_for_lock(winner)
        second = threads.submit(actions[loser])
        wait_for_lock(loser)
        blocker.commit()
        accepted = first.result(timeout=10)
        with pytest.raises(ContractViolation) as failure:
            second.result(timeout=10)
    assert failure.value.code == ("auth_context_conflict" if loser == "logout" else "unauthenticated")
    if winner == "change" and change == "rotate":
        assert store.get_session(handle.cookie) is None
        assert store.get_session(accepted.cookie) == accepted.session
        assert accepted.session.metadata.workspace == organization
    elif winner == "change":
        assert store.get_session(handle.cookie) == accepted
        assert accepted.sdk_storage == {"refreshToken": "updated-token"}
    else:
        assert store.get_session(handle.cookie) is None


@pytest.mark.parametrize("operation", ["logout", "rotate", "update", "touch", "storage", "authority"])
def test_session_write_waiting_past_expiry_cannot_accept_the_old_snapshot(repository, session, operation):
    store, original = session
    handle = store.create_session(original.session.metadata.identity, original.session.metadata.workspace,
                                  sdk_storage={}, absolute_seconds=10, idle_seconds=2)
    name = "auth_expiry26_" + uuid4().hex
    waiting = PostgresIdentityStore(
        make_conninfo(os.environ["STUDIO_TEST_DATABASE_URL"], application_name=name), schema=repository.schema,
        encryption_keys={"test-key": base64.urlsafe_b64decode(os.environ["STUDIO_TEST_SESSION_KEY"])},
        active_key_id="test-key",
    )
    revision = handle.session.metadata.revision
    actions = {
        "logout": lambda: waiting.revoke_session(handle.cookie, expected_revision=revision),
        "rotate": lambda: waiting.rotate_session(handle.cookie, expected_revision=revision,
                                                  workspace=handle.session.metadata.workspace),
        "update": lambda: waiting.update_session(handle.cookie, expected_revision=revision,
                                                  sdk_storage={"idToken": "must-not-write"}),
        "touch": lambda: waiting.touch_session(handle.cookie),
        "storage": lambda: waiting.storage(handle.cookie).set("idToken", "must-not-write"),
        "authority": lambda: waiting.record_authority_verification(handle.cookie, expected_revision=revision,
                                                                     verified_at=datetime.now(timezone.utc)),
    }
    with ThreadPoolExecutor(max_workers=1) as threads, \
            psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as blocker, \
            psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"], autocommit=True) as observer:
        blocker.execute(sql.SQL("SELECT session_hash FROM {}.auth_sessions WHERE session_hash=%s FOR UPDATE")
                        .format(sql.Identifier(repository.schema)), (handle.session.metadata.session_ref,))
        pending = threads.submit(actions[operation])
        deadline = time.monotonic() + 3
        while not observer.execute(
            "SELECT 1 FROM pg_stat_activity WHERE application_name=%s AND wait_event_type='Lock'", (name,),
        ).fetchone():
            assert time.monotonic() < deadline, "Session write did not reach the real PostgreSQL lock"
            time.sleep(0.01)
        time.sleep(2.1)
        blocker.commit()
        with pytest.raises(ContractViolation) as failure:
            pending.result(timeout=10)
    assert failure.value.code == ("auth_context_conflict" if operation == "logout" else "unauthenticated")
    assert store.get_session(handle.cookie) is None


def test_login_interaction_waiting_past_expiry_cannot_be_consumed(repository, session):
    store, old = session
    binding, state = "b" * 43, "expired-consume-wait"
    reference = store.create_login_interaction(
        binding, state=state, nonce="bound-nonce", code_verifier="v" * 43, return_to="/en",
        organization_hint=None, sdk_storage={}, previous_session_cookie=old.cookie, lifetime_seconds=2,
    )
    name = "interaction_consume26_" + uuid4().hex
    waiting = PostgresIdentityStore(
        make_conninfo(os.environ["STUDIO_TEST_DATABASE_URL"], application_name=name), schema=repository.schema,
        encryption_keys={"test-key": base64.urlsafe_b64decode(os.environ["STUDIO_TEST_SESSION_KEY"])},
        active_key_id="test-key",
    )
    with ThreadPoolExecutor(max_workers=1) as threads, \
            psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as blocker, \
            psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"], autocommit=True) as observer:
        blocker.execute(sql.SQL("SELECT reference FROM {}.login_interactions WHERE reference=%s FOR UPDATE")
                        .format(sql.Identifier(repository.schema)), (reference,))
        pending = threads.submit(waiting.consume_login_interaction, binding, state)
        deadline = time.monotonic() + 3
        while not observer.execute(
            "SELECT 1 FROM pg_stat_activity WHERE application_name=%s AND wait_event_type='Lock'", (name,),
        ).fetchone():
            assert time.monotonic() < deadline, "Login consumption did not reach the real PostgreSQL lock"
            time.sleep(0.01)
        time.sleep(2.1)
        blocker.commit()
        with pytest.raises(ContractViolation) as failure:
            pending.result(timeout=10)
    assert failure.value.code == "unauthenticated"
    assert store.get_session(old.cookie) == old.session
    assert store.revoke_user_sessions(old.session.metadata.identity.user_id) == 1


def test_login_completion_waiting_past_expiry_never_creates_a_session_or_revokes_the_old_one(repository, session):
    store, old = session
    binding, state = "c" * 43, "expired-completion-wait"
    reference = store.create_login_interaction(
        binding, state=state, nonce="bound-nonce", code_verifier="v" * 43, return_to="/en",
        organization_hint=None, sdk_storage={}, previous_session_cookie=old.cookie, lifetime_seconds=2,
    )
    consumed = store.consume_login_interaction(binding, state)
    name = "interaction_complete26_" + uuid4().hex
    waiting = PostgresIdentityStore(
        make_conninfo(os.environ["STUDIO_TEST_DATABASE_URL"], application_name=name), schema=repository.schema,
        encryption_keys={"test-key": base64.urlsafe_b64decode(os.environ["STUDIO_TEST_SESSION_KEY"])},
        active_key_id="test-key",
    )
    with ThreadPoolExecutor(max_workers=1) as threads, \
            psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as blocker, \
            psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"], autocommit=True) as observer:
        blocker.execute(sql.SQL("SELECT reference FROM {}.login_interactions WHERE reference=%s FOR UPDATE")
                        .format(sql.Identifier(repository.schema)), (reference,))
        pending = threads.submit(waiting.complete_login, consumed, old.session.metadata.identity,
                                 old.session.metadata.workspace, sdk_storage={"idToken": "must-not-mint"})
        deadline = time.monotonic() + 3
        while not observer.execute(
            "SELECT 1 FROM pg_stat_activity WHERE application_name=%s AND wait_event_type='Lock'", (name,),
        ).fetchone():
            assert time.monotonic() < deadline, "Login completion did not reach the real PostgreSQL lock"
            time.sleep(0.01)
        time.sleep(2.1)
        blocker.commit()
        with pytest.raises(ContractViolation) as failure:
            pending.result(timeout=10)
    assert failure.value.code == "unauthenticated"
    assert store.get_session(old.cookie) == old.session
    assert store.revoke_user_sessions(old.session.metadata.identity.user_id) == 1


def test_concurrent_login_completion_has_one_winner_and_rotates_the_previous_session(repository, session):
    store, old = session
    binding, state = "d" * 43, "concurrent-completion"
    store.create_login_interaction(binding, state=state, nonce="bound-nonce", code_verifier="v" * 43,
                                   return_to="/en", organization_hint=None, sdk_storage={}, previous_session_cookie=old.cookie)
    consumed = store.consume_login_interaction(binding, state)

    def complete(_):
        try:
            return identity_store(repository).complete_login(
                consumed, old.session.metadata.identity, old.session.metadata.workspace,
                sdk_storage={"idToken": "accepted-token"},
            )
        except ContractViolation as failure:
            return failure.code

    with ThreadPoolExecutor(max_workers=4) as threads:
        outcomes = list(threads.map(complete, range(4)))
    winners = [outcome for outcome in outcomes if isinstance(outcome, SessionHandle)]
    assert len(winners) == 1
    assert [outcome for outcome in outcomes if isinstance(outcome, str)] == ["unauthenticated"] * 3
    assert store.get_session(old.cookie) is None
    assert store.get_session(winners[0].cookie) == winners[0].session
    assert winners[0].cookie != old.cookie
    assert winners[0].session.csrf_token != old.session.csrf_token
    assert winners[0].session.metadata.context_id != old.session.metadata.context_id
    assert store.revoke_user_sessions(old.session.metadata.identity.user_id) == 1


def test_concurrent_current_authority_proofs_keep_security_revision_and_monotonic_timestamp(session):
    store, handle = session
    revision = handle.session.metadata.revision
    older = datetime.now(timezone.utc) - timedelta(seconds=2)
    newer = older + timedelta(seconds=1)
    with ThreadPoolExecutor(max_workers=2) as threads:
        pending = [threads.submit(store.record_authority_verification, handle.cookie,
                                  expected_revision=revision, verified_at=stamp) for stamp in (older, newer)]
        verified = [result.result(timeout=10) for result in pending]
    assert all(proof.metadata.revision == revision for proof in verified)
    current = store.get_session(handle.cookie)
    assert current.metadata.authority_verified_at == newer
    late = store.record_authority_verification(handle.cookie, expected_revision=revision, verified_at=older)
    assert late.metadata.authority_verified_at == newer
    assert late.metadata.revision == revision
    assert late.metadata.context_id == handle.session.metadata.context_id
    assert late.csrf_token == handle.session.csrf_token
    assert late.sdk_storage == handle.session.sdk_storage


@pytest.mark.parametrize("winner", ["invalidate", "proof"])
def test_concurrent_authority_invalidation_prevents_stale_proof_from_reauthorizing(repository, session, winner):
    store, handle = session
    revision = handle.session.metadata.revision
    store.record_authority_verification(handle.cookie, expected_revision=revision,
                                        verified_at=datetime.now(timezone.utc) - timedelta(seconds=2))
    names = {action: "auth_proof26_" + uuid4().hex for action in ("invalidate", "proof")}
    stores = {
        action: PostgresIdentityStore(
            make_conninfo(os.environ["STUDIO_TEST_DATABASE_URL"], application_name=name), schema=repository.schema,
            encryption_keys={"test-key": base64.urlsafe_b64decode(os.environ["STUDIO_TEST_SESSION_KEY"])},
            active_key_id="test-key",
        )
        for action, name in names.items()
    }
    actions = {
        "invalidate": lambda: stores["invalidate"].invalidate_authority(
            [handle.session.metadata.identity.user_id], handle.session.metadata.workspace,
        ),
        "proof": lambda: stores["proof"].record_authority_verification(
            handle.cookie, expected_revision=revision, verified_at=datetime.now(timezone.utc),
        ),
    }
    loser = "proof" if winner == "invalidate" else "invalidate"
    with ThreadPoolExecutor(max_workers=2) as threads, \
            psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as blocker, \
            psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"], autocommit=True) as observer:
        blocker.execute(sql.SQL("SELECT session_hash FROM {}.auth_sessions WHERE session_hash=%s FOR UPDATE")
                        .format(sql.Identifier(repository.schema)), (handle.session.metadata.session_ref,))

        def wait_for_lock(action):
            deadline = time.monotonic() + 3
            while not observer.execute(
                "SELECT 1 FROM pg_stat_activity WHERE application_name=%s AND wait_event_type='Lock'", (names[action],),
            ).fetchone():
                assert time.monotonic() < deadline, "Authority operation did not reach the real PostgreSQL lock"
                time.sleep(0.01)

        first = threads.submit(actions[winner])
        wait_for_lock(winner)
        second = threads.submit(actions[loser])
        wait_for_lock(loser)
        if winner == "invalidate":
            blocker.commit()
            assert first.result(timeout=10) == 1
            with pytest.raises(ContractViolation) as failure:
                second.result(timeout=10)
            assert failure.value.code == "auth_context_conflict"
        else:
            blocker.commit()
            assert first.result(timeout=10).metadata.revision == revision
            assert second.result(timeout=10) == 1
    invalidated = store.get_session(handle.cookie)
    assert invalidated.metadata.revision == revision + 1
    assert invalidated.metadata.authority_verified_at is None
    with pytest.raises(ContractViolation) as failure:
        store.record_authority_verification(handle.cookie, expected_revision=revision, verified_at=datetime.now(timezone.utc))
    assert failure.value.code == "auth_context_conflict"
    assert store.get_session(handle.cookie) == invalidated


@pytest.mark.parametrize("operation", ["update", "rotate", "revoke"])
def test_authority_proof_cannot_accept_old_session_state_after_a_security_change(session, operation):
    store, handle = session
    revision = handle.session.metadata.revision
    current_cookie, current = handle.cookie, None
    if operation == "update":
        current = store.update_session(handle.cookie, expected_revision=revision, sdk_storage={"idToken": "new-token"})
        assert current.metadata.revision == revision + 1
    elif operation == "rotate":
        rotated = store.rotate_session(handle.cookie, expected_revision=revision, workspace=handle.session.metadata.workspace)
        current_cookie, current = rotated.cookie, rotated.session
    else:
        store.revoke_session(handle.cookie, expected_revision=revision)
    with pytest.raises(ContractViolation) as failure:
        store.record_authority_verification(handle.cookie, expected_revision=revision, verified_at=datetime.now(timezone.utc))
    assert failure.value.code == ("auth_context_conflict" if operation == "update" else "unauthenticated")
    assert store.get_session(current_cookie) == current


def test_legacy_classification_requires_the_original_trusted_binding(repository):
    # Provisioning captures the receipt before files exist; restoration cannot recapture it.
    expected_binding = repository.database_binding()
    legacy = repository.create_task(TaskCreate(brief="Unassigned legacy production"), snapshot(), "legacy-binding-001")
    assert repository.assert_legacy_root(expected_binding=expected_binding) is None
    with pytest.raises(ContractViolation) as failure:
        repository.assert_legacy_root(expected_binding="00000000-0000-0000-0000-000000000026")
    assert failure.value.code == "identity_unavailable"
    assert repository.get_task(legacy.task_id) == legacy


def test_owned_task_elsewhere_in_schema_denies_legacy_root_even_without_project_row(repository, tmp_path):
    expected_binding = repository.database_binding()
    context = actor()
    elsewhere = PostgresRepository(os.environ["STUDIO_TEST_DATABASE_URL"], schema=repository.schema,
                                   projects_dir=tmp_path / "another-root")
    owned = elsewhere.create_task(TaskCreate(brief="Owned production outside this root"), snapshot(), "owned-elsewhere-001",
                                  workspace=context.workspace, actor=context)
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("DELETE FROM {}.project_owners WHERE project_id=%s")
                           .format(sql.Identifier(repository.schema)), (owned.project_id,))
    with pytest.raises(ContractViolation) as failure:
        repository.assert_legacy_root(expected_binding=expected_binding)
    assert failure.value.code == "identity_unavailable"
    assert repository.get_task_for_workspace(owned.task_id, context.workspace) == owned


def test_owned_hidden_project_without_task_or_local_files_denies_legacy_root(repository):
    expected_binding = repository.database_binding()
    context = actor()
    ownership = ResourceOwnership(workspace=context.workspace, creator=context.identity)
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("INSERT INTO {}.project_owners VALUES (%s,%s,%s,%s)")
                           .format(sql.Identifier(repository.schema)),
                           (".hidden-owned", context.workspace.kind, context.workspace.owner_id,
                            Jsonb(ownership.model_dump(mode="json", context={"persistence": True}))))
    with pytest.raises(ContractViolation) as failure:
        repository.assert_legacy_root(expected_binding=expected_binding)
    assert failure.value.code == "identity_unavailable"
    assert repository.project_ownership(".hidden-owned", context.workspace) == ownership


def test_owned_task_record_with_lost_workspace_index_cannot_be_classified_as_legacy(repository):
    expected_binding = repository.database_binding()
    legacy = repository.create_task(TaskCreate(brief="Restored production record"), snapshot(), "restored-record-001")
    context = actor()
    ownership = ResourceOwnership(workspace=context.workspace, creator=context.identity)
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("UPDATE {}.tasks SET record=record || %s WHERE task_id=%s")
                           .format(sql.Identifier(repository.schema)),
                           (Jsonb({"ownership": ownership.model_dump(mode="json", context={"persistence": True}),
                                   "actor": context.model_dump(mode="json", context={"persistence": True})}), legacy.task_id))
    restored = repository.get_task(legacy.task_id)
    assert restored.ownership == ownership
    with pytest.raises(ContractViolation) as failure:
        repository.assert_legacy_root(expected_binding=expected_binding)
    assert failure.value.code == "identity_unavailable"
    assert repository.get_task(legacy.task_id) == restored


def test_unknown_identity_registry_migration_cannot_prove_legacy_root(repository):
    expected_binding = repository.database_binding()
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("DELETE FROM {}.schema_migrations WHERE version=3")
                           .format(sql.Identifier(repository.schema)))
    with pytest.raises(ContractViolation) as failure:
        repository.assert_legacy_root(expected_binding=expected_binding)
    assert failure.value.code == "identity_unavailable"
    assert repository.database_binding() == expected_binding


@pytest.mark.parametrize("expected_binding", [None, "", "unknown"])
def test_absent_or_unknown_trusted_binding_is_rejected(repository, expected_binding):
    with pytest.raises(ContractViolation) as failure:
        repository.assert_legacy_root(expected_binding=expected_binding)
    assert failure.value.code == "identity_unavailable"


def test_changed_registry_binding_cannot_replace_the_retained_receipt(repository):
    expected_binding = repository.database_binding()
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("UPDATE {}.identity_binding SET binding_id=%s")
                           .format(sql.Identifier(repository.schema)), ("00000000-0000-0000-0000-000000000026",))
    with pytest.raises(ContractViolation) as failure:
        repository.assert_legacy_root(expected_binding=expected_binding)
    assert failure.value.code == "identity_unavailable"
    assert repository.database_binding() == "00000000-0000-0000-0000-000000000026"


@pytest.mark.parametrize("table", ["identity_binding", "project_owners", "tasks", "schema_migrations"])
def test_missing_registry_tables_fail_closed(repository, table):
    expected_binding = repository.database_binding()
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("DROP TABLE {}.{} CASCADE")
                           .format(sql.Identifier(repository.schema), sql.Identifier(table)))
    with pytest.raises(ContractViolation) as failure:
        repository.assert_legacy_root(expected_binding=expected_binding)
    assert failure.value.code == "dependency_unavailable"


def test_missing_registry_binding_row_fails_closed(repository):
    expected_binding = repository.database_binding()
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("DELETE FROM {}.identity_binding").format(sql.Identifier(repository.schema)))
    with pytest.raises(ContractViolation) as failure:
        repository.assert_legacy_root(expected_binding=expected_binding)
    assert failure.value.code == "identity_unavailable"


def test_empty_replacement_registry_cannot_authorize_original_owned_root(repository):
    expected_binding = repository.database_binding()
    context = actor()
    owned = repository.create_task(TaskCreate(brief="Previously protected production"), snapshot(), "original-owned-001",
                                    workspace=context.workspace, actor=context)
    replacement_schema = "identity_replacement_" + uuid4().hex
    replacement = PostgresRepository(os.environ["STUDIO_TEST_DATABASE_URL"], schema=replacement_schema,
                                    projects_dir=repository.projects_dir)
    try:
        replacement.migrate()
        assert replacement.list_tasks() == []
        assert replacement.database_binding() != expected_binding
        with pytest.raises(ContractViolation) as failure:
            replacement.assert_legacy_root(expected_binding=expected_binding)
        assert failure.value.code == "identity_unavailable"
        assert repository.get_task_for_workspace(owned.task_id, context.workspace) == owned
    finally:
        with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(replacement_schema)))


def test_database_outage_cannot_classify_legacy_root(repository):
    expected_binding = repository.database_binding()
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        disconnected = PostgresRepository(
            make_conninfo(os.environ["STUDIO_TEST_DATABASE_URL"], host="127.0.0.1",
                          port=str(unavailable.getsockname()[1])),
            schema=repository.schema,
        )
        with pytest.raises(ContractViolation) as failure:
            disconnected.assert_legacy_root(expected_binding=expected_binding)
    assert failure.value.code == "dependency_unavailable"
    assert repository.database_binding() == expected_binding
