"""Owned creation and supported restoration retain protection on real PostgreSQL."""

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from production.contracts import ContractViolation, TaskCreate
from production.identity_migrate import AuthorityEvidence, OwnershipImporter
from production.ownership_guard import MARKER, has_guard
from tests.integration.test_identity_migration import bytes_on_disk, legacy
from tests.integration.test_identity_persistence import actor, identity_store
from tests.integration.test_studio_repository import repository, repository_factory, snapshot


def test_owned_creation_keeps_root_and_project_protection_when_internal_guards_are_lost(repository):
    context = actor()
    task = repository.create_task(TaskCreate(brief="Protected production"), snapshot(), "retained-owned-001",
                                  workspace=context.workspace, actor=context)
    root = repository.projects_dir
    project = root / task.project_id
    canonical = project / "project.json"
    canonical.write_bytes(b'{"title":"Unchanged production"}\n')
    (root / MARKER).unlink()
    (project / MARKER).unlink()
    assert has_guard(root) and has_guard(project)
    assert repository.get_task_for_workspace(task.task_id, context.workspace) == task
    assert canonical.read_bytes() == b'{"title":"Unchanged production"}\n'


@pytest.mark.parametrize("fault", ["write", "fsync"])
def test_owned_creation_never_commits_when_the_external_witness_cannot_be_persisted(repository, monkeypatch, fault):
    root = repository.projects_dir
    open_file = os.open
    sync_file = os.fsync
    parent = root.parent.stat()

    def read_only_parent(path, *args, **kwargs):
        if Path(path).parent == root.parent:
            raise PermissionError("Witness parent is read-only")
        return open_file(path, *args, **kwargs)

    def unsyncable_parent(descriptor):
        entry = os.fstat(descriptor)
        if stat.S_ISDIR(entry.st_mode) and (entry.st_dev, entry.st_ino) == (parent.st_dev, parent.st_ino):
            raise OSError("Witness parent cannot be fsynced")
        return sync_file(descriptor)

    context = actor()
    with monkeypatch.context() as failure:
        if fault == "write":
            failure.setattr(os, "open", read_only_parent)
        else:
            failure.setattr(os, "fsync", unsyncable_parent)
        with pytest.raises(ContractViolation) as denied:
            repository.create_task(TaskCreate(brief="Must not commit"), snapshot(), "retained-failed-001",
                                   workspace=context.workspace, actor=context)
    assert denied.value.code == "file_conflict"
    assert repository.list_tasks() == []
    assert not (root / MARKER).exists()
    accepted = repository.create_task(TaskCreate(brief="Must not commit"), snapshot(), "retained-failed-001",
                                      workspace=context.workspace, actor=context)
    assert repository.get_task_for_workspace(accepted.task_id, context.workspace) == accepted
    assert has_guard(root)


def test_owned_backup_restores_into_witness_only_roots_and_keeps_canonical_and_pi_bytes(legacy, tmp_path):
    import psycopg
    from psycopg import sql
    repo, projects, runtime = legacy
    repo.migrate()
    user = identity_store(repo).resolve_user("https://identity.test/oidc", "retained-creator")
    importer = OwnershipImporter(repo, projects_dir=projects, runtime_root=runtime)
    mappings = [{"project_id": "legacy-project", "creator_user_id": user.user_id,
                 "workspace": {"kind": "personal", "owner_id": user.user_id}}]
    plan = importer.plan(mappings)
    container = os.environ["STUDIO_TEST_POSTGRES_CONTAINER"]
    before_import = importer.backup(plan, tmp_path / "before-import", postgres_container=container)
    importer.apply(plan, before_import, authority=AuthorityEvidence(
        mapping_sha256=plan.mapping_sha256, checked_at=datetime.now(timezone.utc), identities=[user], organization_ids=[],
    ))
    owned = repo.get_task_for_workspace("task-legacy", mappings[0]["workspace"])
    backup = importer.backup(importer.plan(mappings), tmp_path / "owned-backup", postgres_container=container)
    original = bytes_on_disk(projects, runtime)
    project = projects / "legacy-project"
    (projects / MARKER).unlink()
    (project / MARKER).unlink()
    assert has_guard(projects) and has_guard(project)
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(repo.schema)))
    importer.restore(backup, postgres_container=container)
    assert repo.get_task_for_workspace("task-legacy", mappings[0]["workspace"]) == owned
    assert bytes_on_disk(projects, runtime) == original
    (projects / MARKER).unlink()
    (project / MARKER).unlink()
    assert has_guard(projects) and has_guard(project)
