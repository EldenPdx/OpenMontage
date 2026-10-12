"""Populated legacy upgrade and operator transaction seam on isolated PostgreSQL."""

import json
import os
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from production.contracts import ApprovalBinding, ApprovalDecision, ApprovalRequest, ApprovalScope, CallIntent, CostSnapshot, FileReference, FileWriteIntent, StudioEvent, TaskCommand, TaskCreate, TaskRecord, canonical_sha256
from production.repository import PostgresRepository
from tests.integration.test_studio_repository import snapshot


@pytest.fixture
def legacy(tmp_path):
    dsn = os.environ["STUDIO_TEST_DATABASE_URL"]
    schema = "identity_upgrade_" + uuid4().hex
    projects, runtime = tmp_path / "projects", tmp_path / "runtime"
    project = projects / "legacy-project"
    project.mkdir(parents=True)
    (project / "project.json").write_bytes(b'{"project_id":"legacy-project","title":"Legacy"}\n')
    (project / "checkpoint_compose.json").write_bytes(b'{"status":"awaiting_human","revision":3}\n')
    (project / "artifacts").mkdir()
    (project / "artifacts/script.json").write_bytes(b'{"text":"Original canonical bytes"}\n')
    session = runtime / "sessions/task-legacy/run-legacy/session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_bytes(b'{"type":"session","id":"exact-legacy-session"}\n')
    task = TaskRecord(task_id="task-legacy", project_id="legacy-project", run_id="run-legacy", version=7,
                      state="cancelled", request=TaskCreate(brief="Existing production", profile_id="local"), config_snapshot=snapshot(),
                      cost=CostSnapshot(spent_usd_micros=50000, reserved_usd_micros=100000, unknown_call_count=1))
    record = task.model_dump(mode="json", exclude={"ownership", "actor"})
    artifact = FileReference(path="artifacts/script.json", revision=3, sha256=sha256((project / "artifacts/script.json").read_bytes()).hexdigest())
    checkpoint = FileReference(path="checkpoint_compose.json", revision=3, sha256=sha256((project / "checkpoint_compose.json").read_bytes()).hexdigest())
    scope = ApprovalScope(provider="local", model="test-model", budget_usd_micros=10000000, configuration_sha256="a" * 64)
    binding = ApprovalBinding(task_id="task-legacy", run_id="run-legacy", gate_id="gate-legacy", checkpoint_revision=3,
                               checkpoint_sha256=checkpoint.sha256, artifact_revision=3, artifact_sha256=artifact.sha256,
                               scope_sha256=canonical_sha256(scope))
    gate = ApprovalRequest(binding=binding, stage="compose", artifact=artifact, checkpoint=checkpoint, scope=scope, summary="Preserve the displayed legacy evidence")
    call = CallIntent(call_id="call-legacy", task_id="task-legacy", run_id="run-legacy", fence=17, kind="tool", operation="render",
                       provider="local", request_sha256="d" * 64, price_status="unquoted", reserved_usd_micros=100000, status="outcome_unknown")
    intent = FileWriteIntent(intent_id="file-legacy", task_id="task-legacy", run_id="run-legacy", fence=17, target=artifact, status="applied")
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        connection.execute("CREATE TABLE schema_migrations(version integer PRIMARY KEY)")
        for version, name in ((1, "001_control_plane.sql"), (2, "002_execution_journals.sql")):
            connection.execute((Path(__file__).resolve().parents[2] / "production/migrations" / name).read_text())
            connection.execute("INSERT INTO schema_migrations VALUES (%s)", (version,))
        connection.execute("INSERT INTO tasks(task_id,project_id,run_id,version,state,record) VALUES ('task-legacy','legacy-project','run-legacy',7,'cancelled',%s)", (Jsonb(record),))
        connection.execute("INSERT INTO runs(run_id,task_id,project_id,attempt,fence,session,active) VALUES ('run-legacy','task-legacy','legacy-project',2,17,%s,false)", (Jsonb({"path": "task-legacy/run-legacy/session.jsonl", "session_id": "exact-legacy-session"}),))
        connection.execute("INSERT INTO commands(command_id,task_id,run_id,kind,expected_version,idempotency_key,payload,status,fingerprint) VALUES ('command-legacy','task-legacy','run-legacy','cancel',7,'legacy-cancel-key','{}','completed',%s)", ("6f0fd19c62355b7617be0b95a679183a5bd72faff6ebe0e6187aa01ad32825ef",))
        connection.execute("INSERT INTO requests VALUES ('create','legacy-create-key',%s,%s)", ("7c2d9f2c917e0176b87a66dcb3bfc80b3c5b555846af5e84c12910427c358d6d", Jsonb(record)))
        decision = ApprovalDecision(binding=binding, expected_version=6, decision="approve")
        connection.execute("INSERT INTO gates(task_id,run_id,gate_id,request,decision,status,consumed) VALUES ('task-legacy','run-legacy','gate-legacy',%s,%s,'approved',true)", (Jsonb(gate.model_dump(mode="json")), Jsonb(decision.model_dump(mode="json"))))
        event = StudioEvent(event_id=1, task_id="task-legacy", run_id="run-legacy", task_version=7, type="state", data={"state": "cancelled"}, created_at="2026-10-12T00:00:00Z")
        connection.execute("INSERT INTO events(task_id,record) VALUES ('task-legacy',%s)", (Jsonb(event.model_dump(mode="json")),))
        connection.execute("INSERT INTO calls(call_id,task_id,run_id,request_sha256,record,reserved_usd_micros,actual_usd_micros,status) VALUES ('call-legacy','task-legacy','run-legacy',%s,%s,100000,NULL,'outcome_unknown')", ("d" * 64, Jsonb(call.model_dump(mode="json"))))
        connection.execute("INSERT INTO file_intents VALUES ('file-legacy','task-legacy',%s,'applied')", (Jsonb(intent.model_dump(mode="json")),))
    repo = PostgresRepository(dsn, schema=schema)
    try:
        yield repo, projects, runtime
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def journals(repo):
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        return {table: connection.execute(sql.SQL("SELECT to_jsonb(t) FROM {}.{} t").format(sql.Identifier(repo.schema), sql.Identifier(table))).fetchall()
                for table in ("tasks", "runs", "requests", "commands", "gates", "events", "calls", "file_intents")}


def bytes_on_disk(*roots):
    from production.ownership_guard import MARKER
    return {str(path): path.read_bytes() for root in roots for path in root.rglob("*") if path.is_file() and path.name != MARKER}


def test_populated_v2_upgrade_and_dry_run_preserve_journals_and_canonical_bytes(legacy):
    repo, projects, runtime = legacy
    before, files = journals(repo), bytes_on_disk(projects, runtime)
    assert repo.migrate() == 5
    assert repo.migrate() == 5
    assert repo.get_call("call-legacy").reserved_usd_micros == 100000
    assert repo.approved_gate("task-legacy", "gate-legacy").binding.artifact_sha256 == sha256((projects / "legacy-project/artifacts/script.json").read_bytes()).hexdigest()
    assert len(repo.unresolved_intents("task-legacy")) == 2
    assert repo.events("task-legacy")[0].task_version == 7
    assert repo.create_task(TaskCreate(brief="Existing production", profile_id="local"), snapshot(), "legacy-create-key").task_id == "task-legacy"
    assert repo.enqueue_command(TaskCommand(command_id="retry-legacy", task_id="task-legacy", run_id="run-legacy", kind="cancel", expected_version=7, idempotency_key="legacy-cancel-key")).command_id == "command-legacy"
    after = journals(repo)
    for table in before:
        for old, new in zip(before[table], after[table]):
            assert {key: new[0][key] for key in old[0]} == old[0]
    from production.identity_migrate import OwnershipImporter
    from tests.integration.test_identity_persistence import identity_store
    store = identity_store(repo)
    user = store.resolve_user("https://identity.test/oidc", "legacy-creator")
    importer = OwnershipImporter(repo, projects_dir=projects, runtime_root=runtime)
    mapping = [{"project_id": "legacy-project", "creator_user_id": user.user_id,
                "workspace": {"kind": "personal", "owner_id": user.user_id}}]
    plan = importer.plan(mapping)
    assert not plan.conflicts
    assert plan.items[0].task_id == "task-legacy"
    assert repo.list_tasks_for_workspace(mapping[0]["workspace"]) == []
    assert journals(repo) == after
    assert bytes_on_disk(projects, runtime) == files


def test_explicit_import_backup_atomic_apply_reapply_and_real_restore(legacy, tmp_path):
    from datetime import datetime, timezone
    from production.identity_migrate import AuthorityEvidence, OwnershipImporter
    from tests.integration.test_identity_persistence import identity_store
    repo, projects, runtime = legacy
    repo.migrate()
    user = identity_store(repo).resolve_user("https://identity.test/oidc", "legacy-creator")
    importer = OwnershipImporter(repo, projects_dir=projects, runtime_root=runtime)
    mapping = [{"project_id": "legacy-project", "creator_user_id": user.user_id,
                "workspace": {"kind": "personal", "owner_id": user.user_id}}]
    plan = importer.plan(mapping)
    container = json.loads(Path('/tmp/openmontage-auth-20261012/persistence-private.json').read_text())["container"]
    backup = importer.backup(plan, tmp_path / "private-backup", postgres_container=container)
    files = bytes_on_disk(projects, runtime)
    # This seam receives trusted authority data; it does not assert an OIDC/Logto round trip.
    evidence = AuthorityEvidence(mapping_sha256=plan.mapping_sha256, checked_at=datetime.now(timezone.utc), identities=[user], organization_ids=[])
    imported = importer.apply(plan, backup, authority=evidence)
    assert imported.changed == 1
    owned = repo.get_task_for_workspace("task-legacy", mapping[0]["workspace"])
    assert owned.ownership.creator == user and owned.actor is None
    assert owned.version == 7 and owned.cost.unknown_call_count == 1
    assert importer.apply(plan, backup, authority=evidence).changed == 0
    audit = importer.import_audit(imported.import_id)
    assert audit["operator_uid"] == os.getuid()
    assert "actor" not in audit
    assert bytes_on_disk(projects, runtime) == files
    from production.ownership_guard import has_guard
    assert has_guard(projects) and has_guard(projects / "legacy-project")
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(repo.schema)))
    importer.restore(backup, postgres_container=container)
    assert repo.get_task("task-legacy").ownership is None
    assert repo.get_task("task-legacy").cost.reserved_usd_micros == 100000
    assert bytes_on_disk(projects, runtime) == files
    assert has_guard(projects) and has_guard(projects / "legacy-project")


def test_operator_cli_refuses_apply_without_real_authority_before_any_assignment(legacy, tmp_path):
    import subprocess
    import sys
    from production.identity_migrate import OwnershipImporter
    from tests.integration.test_identity_persistence import identity_store
    repo, projects, runtime = legacy
    repo.migrate()
    user = identity_store(repo).resolve_user("https://identity.test/oidc", "legacy-creator")
    mapping = [{"project_id": "legacy-project", "creator_user_id": user.user_id, "workspace": {"kind": "personal", "owner_id": user.user_id}}]
    importer = OwnershipImporter(repo, projects_dir=projects, runtime_root=runtime)
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(json.dumps(mapping))
    plan = importer.plan(mapping)
    private = json.loads(Path('/tmp/openmontage-auth-20261012/persistence-private.json').read_text())
    backup = importer.backup(plan, tmp_path / "backup", postgres_container=private["container"])
    configuration = tmp_path / "operator-private.json"
    configuration.write_text(json.dumps({"database_url": os.environ["STUDIO_TEST_DATABASE_URL"]}))
    configuration.chmod(0o600)
    command = [sys.executable, "-B", "-m", "production.identity_migrate", "apply", "--private-config", str(configuration),
               "--schema", repo.schema, "--projects-root", str(projects), "--runtime-root", str(runtime),
               "--mapping", str(mapping_file), "--backup", str(backup.directory)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "identity_unavailable" in result.stderr
    assert os.environ["STUDIO_TEST_DATABASE_URL"] not in result.stderr + result.stdout
    assert repo.get_task("task-legacy").ownership is None


def test_restore_recreates_only_missing_test_files_and_guards_before_exposure(legacy, tmp_path):
    import shutil
    from production.identity_migrate import OwnershipImporter
    from production.ownership_guard import has_guard
    from tests.integration.test_identity_persistence import identity_store
    repo, projects, runtime = legacy
    repo.migrate()
    user = identity_store(repo).resolve_user("https://identity.test/oidc", "legacy-creator")
    importer = OwnershipImporter(repo, projects_dir=projects, runtime_root=runtime)
    mapping = [{"project_id": "legacy-project", "creator_user_id": user.user_id, "workspace": {"kind": "personal", "owner_id": user.user_id}}]
    before = bytes_on_disk(projects, runtime)
    container = json.loads(Path('/tmp/openmontage-auth-20261012/persistence-private.json').read_text())["container"]
    backup = importer.backup(importer.plan(mapping), tmp_path / "backup", postgres_container=container)
    shutil.rmtree(projects)
    shutil.rmtree(runtime)
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(repo.schema)))
    importer.restore(backup, postgres_container=container)
    assert bytes_on_disk(projects, runtime) == before
    assert has_guard(projects)
    assert repo.get_task("task-legacy").ownership is None


def test_import_conflict_after_backup_aborts_entire_batch_without_touching_journals(legacy, tmp_path):
    from datetime import datetime, timezone
    from production.contracts import ContractViolation
    from production.identity_migrate import AuthorityEvidence, OwnershipImporter
    from tests.integration.test_identity_persistence import identity_store
    repo, projects, runtime = legacy
    repo.migrate()
    store = identity_store(repo)
    user, foreign = [store.resolve_user("https://identity.test/oidc", subject) for subject in ("creator", "foreign")]
    extra = projects / "cli-project"
    extra.mkdir()
    (extra / "project.json").write_bytes(b'{"project_id":"cli-project"}\n')
    mapping = [{"project_id": name, "creator_user_id": user.user_id, "workspace": {"kind": "personal", "owner_id": user.user_id}} for name in ("legacy-project", "cli-project")]
    importer = OwnershipImporter(repo, projects_dir=projects, runtime_root=runtime)
    plan = importer.plan(mapping)
    container = json.loads(Path('/tmp/openmontage-auth-20261012/persistence-private.json').read_text())["container"]
    backup = importer.backup(plan, tmp_path / "backup", postgres_container=container)
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        foreign_owner = {"workspace": {"kind": "personal", "owner_id": foreign.user_id}, "creator": foreign.model_dump()}
        connection.execute(sql.SQL("INSERT INTO {}.project_owners VALUES ('cli-project','personal',%s,%s)").format(sql.Identifier(repo.schema)), (foreign.user_id, Jsonb(foreign_owner)))
    before, files = journals(repo), bytes_on_disk(projects, runtime)
    evidence = AuthorityEvidence(mapping_sha256=plan.mapping_sha256, checked_at=datetime.now(timezone.utc), identities=[user], organization_ids=[])
    with pytest.raises(ContractViolation) as failure:
        importer.apply(plan, backup, authority=evidence)
    assert failure.value.code == "state_conflict"
    assert repo.get_task("task-legacy").ownership is None
    assert journals(repo) == before
    assert bytes_on_disk(projects, runtime) == files


@pytest.mark.parametrize("condition", ["active", "queued", "unknown-creator", "unknown-organization"])
def test_dry_run_refuses_active_work_and_unknown_targets(legacy, condition):
    from production.identity_migrate import OwnershipImporter
    from tests.integration.test_identity_persistence import identity_store
    repo, projects, runtime = legacy
    repo.migrate()
    user = identity_store(repo).resolve_user("https://identity.test/oidc", "creator")
    if condition in {"active", "queued"}:
        with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
            table, change = ("runs", "active=true") if condition == "active" else ("commands", "status='pending'")
            connection.execute(sql.SQL("UPDATE {}.{} SET " + change).format(sql.Identifier(repo.schema), sql.Identifier(table)))
    mapping = [{"project_id": "legacy-project", "creator_user_id": "missing-user" if condition == "unknown-creator" else user.user_id,
                "workspace": {"kind": "organization" if condition == "unknown-organization" else "personal", "owner_id": "missing-org" if condition == "unknown-organization" else user.user_id}}]
    before, files = journals(repo), bytes_on_disk(projects, runtime)
    assert OwnershipImporter(repo, projects_dir=projects, runtime_root=runtime).plan(mapping).conflicts
    assert journals(repo) == before and bytes_on_disk(projects, runtime) == files


def test_corrupt_backup_and_failed_guard_never_partially_assign(legacy, tmp_path):
    from datetime import datetime, timezone
    from production.contracts import ContractViolation
    from production.identity_migrate import AuthorityEvidence, OwnershipImporter
    from production.ownership_guard import MARKER, has_guard
    from tests.integration.test_identity_persistence import identity_store
    repo, projects, runtime = legacy
    repo.migrate()
    user = identity_store(repo).resolve_user("https://identity.test/oidc", "creator")
    importer = OwnershipImporter(repo, projects_dir=projects, runtime_root=runtime)
    mapping = [{"project_id": "legacy-project", "creator_user_id": user.user_id, "workspace": {"kind": "personal", "owner_id": user.user_id}}]
    plan = importer.plan(mapping)
    container = json.loads(Path('/tmp/openmontage-auth-20261012/persistence-private.json').read_text())["container"]
    backup = importer.backup(plan, tmp_path / "backup", postgres_container=container)
    evidence = AuthorityEvidence(mapping_sha256=plan.mapping_sha256, checked_at=datetime.now(timezone.utc), identities=[user], organization_ids=[])
    dump = backup.directory / "database.dump"
    original = dump.read_bytes()
    dump.write_bytes(original + b"corruption")
    with pytest.raises(ContractViolation) as failure:
        importer.apply(plan, backup, authority=evidence)
    assert failure.value.code == "file_conflict"
    dump.write_bytes(original)
    (projects / "legacy-project" / MARKER).write_bytes(b"invalid classification")
    before = journals(repo)
    with pytest.raises(ContractViolation) as failure:
        importer.apply(plan, backup, authority=evidence)
    assert failure.value.code == "file_conflict"
    assert has_guard(projects) and has_guard(projects / "legacy-project")
    assert journals(repo) == before
