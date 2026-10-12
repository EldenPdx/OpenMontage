"""Operator-only ownership imports; file truth and existing journals are preserved."""

from contextlib import ExitStack
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from pydantic import Field

from production.contracts import (
    ContractModel, ContractViolation, FileReference, ResourceOwnership, SessionReference, Sha256, TaskRecord, UserIdentity,
    WorkspaceContext, canonical_sha256,
)
from production.task_service import file_sha256
from production.ownership_guard import MARKER, GuardConflict, mark_authenticated


class OwnershipMapping(ContractModel):
    project_id: str = Field(min_length=1, max_length=256)
    task_id: str | None = None
    workspace: WorkspaceContext
    creator_user_id: str = Field(min_length=1, max_length=256)


class PlannedOwnership(ContractModel):
    mapping: OwnershipMapping
    task_id: str | None
    ownership: ResourceOwnership
    current: ResourceOwnership | None


class OwnershipPlan(ContractModel):
    items: list[PlannedOwnership]
    conflicts: list[str]
    files: dict[str, Sha256]
    mapping_sha256: Sha256
    journal_sha256: Sha256
    snapshot_sha256: Sha256


class AuthorityEvidence(ContractModel):
    mapping_sha256: Sha256
    checked_at: datetime
    identities: list[UserIdentity]
    organization_ids: list[str]


class BackupEvidence(ContractModel):
    directory: Path = Field(repr=False)
    schema_name: str
    binding: str
    mapping_sha256: Sha256
    snapshot_sha256: Sha256
    dump_sha256: Sha256
    files: dict[str, Sha256]


class OwnershipImportResult(ContractModel):
    import_id: str | None
    changed: int


class OwnershipImporter:
    def __init__(self, repository, *, projects_dir, runtime_root):
        self.repository = repository
        self.projects_dir = Path(projects_dir).resolve()
        self.runtime_root = Path(runtime_root).resolve()

    def _project(self, project_id):
        if project_id in {".", ".."} or any(character in project_id for character in "/\\:\x00"):
            raise ContractViolation("Invalid historical project reference")
        project = self.projects_dir / project_id
        if not project.is_dir() or project.resolve() != project or not (project / "project.json").is_file():
            raise ContractViolation("Historical project is missing or aliased", "not_found")
        try:
            marker = json.loads((project / "project.json").read_text())
            if marker.get("project_id") != project_id:
                raise ValueError
        except (ValueError, OSError, AttributeError):
            raise ContractViolation("Historical project marker is inconsistent", "file_conflict") from None
        return project

    @staticmethod
    def _writer_lock(project, stack):
        path = project / ".studio-writer.lock"
        if path.exists():
            if path.is_symlink():
                raise ContractViolation("Historical writer lock is aliased", "file_conflict")
            descriptor = os.open(path, os.O_RDWR)
            stack.callback(os.close, descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ContractViolation("Historical project has an active writer", "state_conflict") from None

    def _plan(self, connection, mappings, *, locks=False, stack=None):
        items, conflicts, files, journal = [], [], {}, {}
        seen = set()
        for mapping in mappings:
            if mapping.project_id in seen:
                conflicts.append(f"Duplicate project mapping: {mapping.project_id}")
                continue
            seen.add(mapping.project_id)
            try:
                project = self._project(mapping.project_id)
                self._writer_lock(project, stack)
                identity_row = connection.execute("SELECT * FROM identity_users WHERE user_id=%s", (mapping.creator_user_id,)).fetchone()
                if identity_row is None:
                    raise ContractViolation("Historical creator is unknown", "not_found")
                identity = UserIdentity.model_validate(identity_row)
                if mapping.workspace.kind == "personal" and mapping.workspace.owner_id != identity.user_id:
                    raise ContractViolation("Personal owner differs from mapped creator")
                if mapping.workspace.kind == "organization" and connection.execute("SELECT 1 FROM known_organizations WHERE organization_id=%s", (mapping.workspace.owner_id,)).fetchone() is None:
                    raise ContractViolation("Historical organization is unknown", "not_found")
                ownership = ResourceOwnership(workspace=mapping.workspace, creator=identity)
                row = connection.execute("SELECT * FROM tasks WHERE project_id=%s" + (" FOR UPDATE" if locks else ""), (mapping.project_id,)).fetchone()
                task_id = row["task_id"] if row else None
                if mapping.task_id is not None and mapping.task_id != task_id:
                    raise ContractViolation("Historical task and project do not match", "file_conflict")
                owner = connection.execute("SELECT ownership FROM project_owners WHERE project_id=%s" + (" FOR UPDATE" if locks else ""), (mapping.project_id,)).fetchone()
                current = ResourceOwnership.model_validate(owner["ownership"]) if owner and owner["ownership"] else None
                if current is not None and current != ownership:
                    raise ContractViolation("Historical ownership conflicts", "state_conflict")
                if row:
                    task = TaskRecord.model_validate(row["record"])
                    if task.ownership is not None and task.ownership != ownership:
                        raise ContractViolation("Historical task ownership conflicts", "state_conflict")
                    run = connection.execute("SELECT * FROM runs WHERE run_id=%s" + (" FOR UPDATE" if locks else ""), (row["run_id"],)).fetchone()
                    if run is None or run["active"] or run["lease_worker"] is not None or connection.execute("SELECT 1 FROM commands WHERE task_id=%s AND status IN ('pending','claimed')", (task_id,)).fetchone():
                        raise ContractViolation("Historical resource has active or queued work", "state_conflict")
                    session = (self.runtime_root / "sessions" / run["session"]["path"])
                    if session.resolve() != session or not session.is_relative_to(self.runtime_root / "sessions") or not session.is_file():
                        raise ContractViolation("Historical Pi session reference is missing or aliased", "file_conflict")
                    files["runtime/sessions/" + run["session"]["path"]] = file_sha256(session)
                    marker = project / ".studio-owner.json"
                    if marker.exists() and json.loads(marker.read_text()) != {"task_id": task_id, "run_id": row["run_id"]}:
                        raise ContractViolation("Historical writer ownership marker conflicts", "file_conflict")
                    for table in ("tasks", "runs", "commands", "gates", "events", "calls", "file_intents"):
                        records = connection.execute(sql.SQL("SELECT to_jsonb(t) AS value FROM {} t WHERE task_id=%s ORDER BY to_jsonb(t)::text").format(sql.Identifier(table)), (task_id,)).fetchall()
                        journal[mapping.project_id + ":" + table] = [record["value"] for record in records]
                    records = connection.execute("SELECT to_jsonb(r) AS value FROM requests r WHERE response->>'task_id'=%s ORDER BY scope,idempotency_key", (task_id,)).fetchall()
                    journal[mapping.project_id + ":requests"] = [record["value"] for record in records]
                elif (project / ".studio-owner.json").exists():
                    raise ContractViolation("Historical managed project lacks its task", "file_conflict")
                for path in sorted(project.rglob("*")):
                    if path.is_symlink():
                        raise ContractViolation("Historical project contains an alias", "file_conflict")
                    if path.is_file() and path.name != MARKER:
                        files["projects/" + path.relative_to(self.projects_dir).as_posix()] = file_sha256(path)
                items.append(PlannedOwnership(mapping=mapping, task_id=task_id, ownership=ownership, current=current))
            except (ContractViolation, OSError, ValueError, KeyError) as error:
                conflicts.append(f"{mapping.project_id}: {str(error)[:200] if isinstance(error, ContractViolation) else 'Historical reference cannot be verified'}")
        mapping_hash = canonical_sha256({"mappings": [mapping.model_dump(mode="json") for mapping in mappings]})
        journal_hash = canonical_sha256(journal)
        snapshot = canonical_sha256({"mapping": mapping_hash, "journal": journal_hash, "files": files,
                                     "items": [item.model_dump(mode="json") for item in items], "conflicts": conflicts})
        return OwnershipPlan(items=items, conflicts=conflicts, files=files, mapping_sha256=mapping_hash,
                             journal_sha256=journal_hash, snapshot_sha256=snapshot)

    def plan(self, mappings):
        mappings = sorted([OwnershipMapping.model_validate(mapping) for mapping in mappings], key=lambda mapping: mapping.project_id)
        if not mappings or len(mappings) > 1000:
            raise ContractViolation("Provide 1 to 1000 explicit ownership mappings")
        with ExitStack() as stack, self.repository._connection() as connection:
            return self._plan(connection, mappings, stack=stack)

    def _postgres(self, command, *, postgres_container=None, stdin=None, stdout=None):
        info = conninfo_to_dict(self.repository._dsn)
        environment = dict(os.environ)
        for key in ("PGSERVICE", "PGSERVICEFILE", "PGHOSTADDR", "PGOPTIONS", "PGPASSFILE"):
            environment.pop(key, None)
        environment.update(PGHOST=info.get("host", "127.0.0.1"), PGPORT=info.get("port", "5432"),
                           PGDATABASE=info["dbname"], PGUSER=info["user"], PGPASSWORD=info.get("password", ""))
        if postgres_container:
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", postgres_container):
                raise ContractViolation("Invalid trusted PostgreSQL container")
            environment.update(PGHOST="127.0.0.1", PGPORT="5432")
            prefix = ["docker", "exec", "-i"]
            for key in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"):
                prefix += ["--env", key]
            command = prefix + [postgres_container] + command
        try:
            completed = subprocess.run(command, env=environment, stdin=stdin, stdout=stdout or subprocess.PIPE,
                                       stderr=subprocess.PIPE, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise ContractViolation("PostgreSQL backup operation unavailable", "dependency_unavailable") from None
        if completed.returncode:
            raise ContractViolation("PostgreSQL backup operation failed", "dependency_unavailable")
        return completed.stdout

    def _database_references(self, connection):
        tasks = connection.execute("SELECT task_id,project_id,record FROM tasks").fetchall()
        projects = {row["project_id"] for row in tasks}
        projects.update(row["project_id"] for row in connection.execute("SELECT project_id FROM project_owners"))
        references = set()

        def file_references(value, project_id):
            if isinstance(value, dict):
                if {"path", "revision", "sha256"} <= value.keys():
                    reference = FileReference.model_validate({key: value[key] for key in ("path", "revision", "sha256")})
                    references.add("projects/" + project_id + "/" + reference.path)
                for child in value.values():
                    file_references(child, project_id)
            elif isinstance(value, list):
                for child in value:
                    file_references(child, project_id)

        for row in tasks:
            task = TaskRecord.model_validate(row["record"])
            if (task.task_id, task.project_id) != (row["task_id"], row["project_id"]):
                raise ContractViolation("Database task/project references differ", "file_conflict")
            file_references(row["record"], row["project_id"])
        for row in connection.execute("SELECT project_id,session FROM runs"):
            projects.add(row["project_id"])
            session = SessionReference.model_validate(row["session"])
            references.add("runtime/sessions/" + session.path)
        for row in connection.execute("SELECT response FROM requests"):
            if "project_id" in row["response"]:
                project_id = row["response"]["project_id"]
                projects.add(project_id)
                file_references(row["response"], project_id)
        for table, column, condition in (("gates", "request", ""), ("events", "record", ""),
                                         ("calls", "record", ""), ("file_intents", "record", " WHERE r.status IN ('applied','reconciled')")):
            records = connection.execute(sql.SQL("SELECT t.project_id,r.{} AS record FROM {} r JOIN tasks t USING(task_id)" + condition)
                                         .format(sql.Identifier(column), sql.Identifier(table)))
            for row in records:
                file_references(row["record"], row["project_id"])
        references.update("projects/" + project_id + "/project.json" for project_id in projects)
        return projects, references

    def backup(self, plan, directory, *, postgres_container=None):
        plan = OwnershipPlan.model_validate(plan)
        if plan.conflicts:
            raise ContractViolation("Resolve all import conflicts before backup", "state_conflict")
        directory = Path(directory).absolute()
        if directory.exists() or directory.is_symlink():
            raise ContractViolation("Backup destination must be new", "file_conflict")
        directory.mkdir(parents=True, mode=0o700)
        try:
            with ExitStack() as stack, self.repository._connection() as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                current = self._plan(connection, [item.mapping for item in plan.items], stack=stack)
                if current.snapshot_sha256 != plan.snapshot_sha256:
                    raise ContractViolation("Import plan changed before backup", "state_conflict")
                if connection.execute("SELECT 1 FROM runs WHERE active OR lease_worker IS NOT NULL UNION ALL SELECT 1 FROM commands WHERE status IN ('pending','claimed') LIMIT 1").fetchone():
                    raise ContractViolation("Schema backup requires all restored work to be quiescent", "state_conflict")
                projects, references = self._database_references(connection)
                mapped = {item.mapping.project_id for item in plan.items}
                files = dict(plan.files)
                for project_id in sorted(projects | mapped):
                    project = self._project(project_id)
                    if project_id not in mapped:
                        self._writer_lock(project, stack)
                    for path in sorted(project.rglob("*")):
                        if path.is_symlink():
                            raise ContractViolation("Backup project contains an alias", "file_conflict")
                        if path.is_file() and path.name != MARKER and not path.name.startswith(MARKER + ".pending-"):
                            files["projects/" + path.relative_to(self.projects_dir).as_posix()] = file_sha256(path)
                for reference in references:
                    files[reference] = file_sha256(self._source_file(reference))
                snapshot = connection.execute("SELECT pg_export_snapshot() AS snapshot").fetchone()["snapshot"]
                binding = str(connection.execute("SELECT binding_id FROM identity_binding WHERE singleton").fetchone()["binding_id"])
                dump = directory / "database.dump"
                descriptor = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    self._postgres(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--schema=" + self.repository.schema,
                                    "--snapshot=" + snapshot], postgres_container=postgres_container, stdout=output)
                    output.flush()
                    os.fsync(output.fileno())
                for reference in files:
                    source = self._source_file(reference)
                    target = directory / reference
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(source, target)
                    target.chmod(0o600)
                backup = BackupEvidence(directory=directory, schema_name=self.repository.schema, binding=binding,
                                        mapping_sha256=plan.mapping_sha256, snapshot_sha256=plan.snapshot_sha256,
                                        dump_sha256=file_sha256(dump), files=files)
            descriptor = os.open(directory / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(backup.model_dump_json(indent=2) + "\n")
                output.flush()
                os.fsync(output.fileno())
            self._check_backup(backup)
            return backup
        except (OSError, ContractViolation):
            # Remove only the newly created incomplete backup, never source data.
            shutil.rmtree(directory)
            raise

    def _source_file(self, reference):
        prefix, _, relative = reference.partition("/")
        root = self.projects_dir if prefix == "projects" else self.runtime_root if prefix == "runtime" else None
        if root is None:
            raise ContractViolation("Invalid backup file reference", "file_conflict")
        path = root / relative
        if path.resolve() != path or not path.is_relative_to(root) or not path.is_file():
            raise ContractViolation("Backup source is missing or aliased", "file_conflict")
        return path

    def _check_backup(self, backup):
        backup = BackupEvidence.model_validate(backup)
        directory = backup.directory
        if directory.resolve() != directory or not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) & 0o077:
            raise ContractViolation("Backup directory is unsafe", "file_conflict")
        try:
            manifest = directory / "manifest.json"
            if manifest.is_symlink() or stat.S_IMODE(manifest.stat().st_mode) & 0o077:
                raise ValueError
            saved = BackupEvidence.model_validate_json(manifest.read_text())
            dump = directory / "database.dump"
            if saved != backup or backup.schema_name != self.repository.schema or dump.is_symlink() or file_sha256(dump) != backup.dump_sha256:
                raise ValueError
            with dump.open("rb") as content:
                if content.read(5) != b"PGDMP":
                    raise ValueError
            for reference, digest in backup.files.items():
                path = directory / reference
                if path.resolve() != path or not path.is_relative_to(directory) or not path.is_file() or file_sha256(path) != digest:
                    raise ValueError
        except (ValueError, OSError):
            raise ContractViolation("Backup manifest or bytes cannot be verified", "file_conflict") from None
        return backup

    @staticmethod
    def _authority(plan, authority):
        authority = AuthorityEvidence.model_validate(authority)
        now = datetime.now(timezone.utc)
        if authority.checked_at.tzinfo is None or not 0 <= (now - authority.checked_at).total_seconds() <= 30 or authority.mapping_sha256 != plan.mapping_sha256:
            raise ContractViolation("Current authoritative import evidence required", "identity_unavailable")
        identities = {identity.user_id: identity for identity in authority.identities}
        organizations = set(authority.organization_ids)
        if any(identities.get(item.ownership.creator.user_id) != item.ownership.creator or
               (item.ownership.workspace.kind == "organization" and item.ownership.workspace.owner_id not in organizations) for item in plan.items):
            raise ContractViolation("Authoritative import targets differ from plan", "forbidden")

    def apply(self, plan, backup, *, authority):
        plan, backup = OwnershipPlan.model_validate(plan), self._check_backup(backup)
        if plan.conflicts or len(plan.items) == 0:
            raise ContractViolation("Resolve all import conflicts before apply", "state_conflict")
        self._authority(plan, authority)
        if backup.mapping_sha256 != plan.mapping_sha256:
            raise ContractViolation("Backup does not match import plan", "file_conflict")
        with ExitStack() as stack, self.repository._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.repository.schema + ":ownership-import",))
            current = self._plan(connection, sorted([item.mapping for item in plan.items], key=lambda mapping: mapping.project_id), locks=True, stack=stack)
            if current.conflicts:
                raise ContractViolation("Import conflict or active writer; no ownership changed", "state_conflict")
            binding = str(connection.execute("SELECT binding_id FROM identity_binding WHERE singleton").fetchone()["binding_id"])
            if binding != backup.binding:
                raise ContractViolation("Backup data binding differs from database", "file_conflict")
            changed = [item for item in current.items if item.current is None]
            if not changed:
                return OwnershipImportResult(import_id=None, changed=0)
            if current.snapshot_sha256 != plan.snapshot_sha256 or backup.snapshot_sha256 != plan.snapshot_sha256:
                raise ContractViolation("Import snapshot changed; plan and back up again", "state_conflict")
            try:
                mark_authenticated(self.projects_dir, binding=binding)
                for item in current.items:
                    mark_authenticated(self._project(item.mapping.project_id), binding=binding, workspace=item.ownership.workspace.model_dump())
            except (GuardConflict, OSError):
                raise ContractViolation("Import protection failed; guards retained, no ownership changed", "file_conflict") from None
            for item in changed:
                workspace = item.ownership.workspace
                connection.execute("INSERT INTO project_owners VALUES (%s,%s,%s,%s) ON CONFLICT(project_id) DO UPDATE SET workspace_kind=excluded.workspace_kind,owner_id=excluded.owner_id,ownership=excluded.ownership",
                                   (item.mapping.project_id, workspace.kind, workspace.owner_id, Jsonb(item.ownership.model_dump(mode="json"))))
                if item.task_id:
                    record = connection.execute("SELECT record FROM tasks WHERE task_id=%s", (item.task_id,)).fetchone()["record"]
                    record["ownership"] = item.ownership.model_dump(mode="json")
                    TaskRecord.model_validate(record)
                    connection.execute("UPDATE tasks SET workspace_kind=%s,owner_id=%s,record=%s WHERE task_id=%s", (workspace.kind, workspace.owner_id, Jsonb(record), item.task_id))
            import_id = "import-" + uuid4().hex
            audit = {"import_id": import_id, "operator_uid": os.getuid(), "operator_euid": os.geteuid(),
                     "operator_name": pwd.getpwuid(os.getuid()).pw_name, "command": "python -m production.identity_migrate apply",
                     "mapping_sha256": plan.mapping_sha256, "backup_sha256": backup.dump_sha256,
                     "created_at": datetime.now(timezone.utc).isoformat(), "items": [item.model_dump(mode="json") for item in changed]}
            connection.execute("INSERT INTO ownership_import_audit VALUES (%s,%s)", (import_id, Jsonb(audit)))
            return OwnershipImportResult(import_id=import_id, changed=len(changed))

    def import_audit(self, import_id):
        with self.repository._connection() as connection:
            row = connection.execute("SELECT record FROM ownership_import_audit WHERE import_id=%s", (import_id,)).fetchone()
            if row is None:
                raise ContractViolation("Import evidence not found", "not_found")
            return row["record"]

    def restore(self, backup, *, postgres_container=None):
        backup = self._check_backup(backup)
        destinations = {}
        for reference, digest in backup.files.items():
            prefix, _, relative = reference.partition("/")
            root = self.projects_dir if prefix == "projects" else self.runtime_root if prefix == "runtime" else None
            if root is None:
                raise ContractViolation("Invalid restore file reference", "file_conflict")
            destination = root / relative
            if destination.resolve() != destination or not destination.is_relative_to(root) or (destination.exists() and (not destination.is_file() or file_sha256(destination) != digest)):
                raise ContractViolation("Restore files conflict with existing bytes or aliases", "file_conflict")
            destinations[reference] = destination
        with self.repository._connection() as connection:
            if connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (self.repository.schema,)).fetchone():
                raise ContractViolation("Restore requires an empty isolated target schema", "state_conflict")
        try:
            mark_authenticated(self.projects_dir, binding=backup.binding)
        except (GuardConflict, OSError):
            raise ContractViolation("Restore protection failed; existing guards retained", "file_conflict") from None
        with (backup.directory / "database.dump").open("rb") as content:
            self._postgres(["pg_restore", "--dbname=", "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction"], postgres_container=postgres_container, stdin=content)
        if self.repository.database_binding() != backup.binding:
            raise ContractViolation("Restored data binding does not match backup", "file_conflict")
        with self.repository._connection() as connection:
            owners = connection.execute("SELECT project_id,ownership FROM project_owners WHERE ownership IS NOT NULL").fetchall()
            _, references = self._database_references(connection)
        if not references <= backup.files.keys():
            raise ContractViolation("Restored database references are missing from archive; authenticated protection retained", "file_conflict")
        try:
            for owner in owners:
                ownership = ResourceOwnership.model_validate(owner["ownership"])
                mark_authenticated(self.projects_dir / owner["project_id"], binding=backup.binding, workspace=ownership.workspace.model_dump())
            for reference, destination in destinations.items():
                if destination.exists():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output, (backup.directory / reference).open("rb") as source:
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                if file_sha256(destination) != backup.files[reference]:
                    raise ContractViolation("Restored file verification failed", "file_conflict")
            for reference in references:
                destination = destinations[reference]
                if not destination.is_file() or file_sha256(destination) != backup.files[reference]:
                    raise ContractViolation("Restored references remain incomplete; authenticated protection retained", "file_conflict")
        except (GuardConflict, OSError):
            raise ContractViolation("Restore files remain incomplete; authenticated protection retained", "file_conflict") from None


def verify_authority(plan, configuration):
    """Read only the fixed Logto user/organization resources before operator apply."""
    issuer = configuration.get("authority_issuer")
    endpoint = configuration.get("management_endpoint")
    token = configuration.get("management_access_token")
    if not all(isinstance(value, str) and value for value in (issuer, endpoint, token)):
        raise ContractViolation("Real identity authority must be configured for apply", "identity_unavailable")
    parsed = urlparse(endpoint)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"})):
        raise ContractViolation("Invalid trusted identity authority endpoint", "identity_unavailable")
    identities = {item.ownership.creator.user_id: item.ownership.creator for item in plan.items}
    organizations = {item.ownership.workspace.owner_id for item in plan.items if item.ownership.workspace.kind == "organization"}
    if any(identity.issuer != issuer for identity in identities.values()):
        raise ContractViolation("Historical identity issuer differs from trusted authority", "forbidden")
    for resource, identifier in [("users", identity.sub) for identity in identities.values()] + [("organizations", identifier) for identifier in organizations]:
        request = Request(endpoint.rstrip("/") + "/" + resource + "/" + quote(identifier, safe=""),
                          headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read(1_048_577))
            if not isinstance(payload, dict) or payload.get("id") != identifier:
                raise ValueError
        except (OSError, ValueError):
            raise ContractViolation("Identity authority cannot confirm historical targets", "identity_unavailable") from None
    return AuthorityEvidence(mapping_sha256=plan.mapping_sha256, checked_at=datetime.now(timezone.utc),
                             identities=list(identities.values()), organization_ids=sorted(organizations))


def main():
    parser = argparse.ArgumentParser(description="Trusted ownership dry-run, backup, import and isolated restore")
    parser.add_argument("command", choices=("plan", "backup", "apply", "restore"))
    parser.add_argument("--private-config", required=True)
    parser.add_argument("--schema", default="studio")
    parser.add_argument("--projects-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--mapping")
    parser.add_argument("--backup")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        path = Path(args.private_config)
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ContractViolation("Operator configuration must be a private regular 0600 file", "forbidden")
        configuration = json.loads(path.read_text())
        from production.repository import PostgresRepository
        dsn = configuration.get("database_url") or configuration.get("STUDIO_TEST_DATABASE_URL")
        if not isinstance(dsn, str) or not dsn:
            raise ContractViolation("Operator database must be configured privately", "dependency_unavailable")
        repository = PostgresRepository(dsn, schema=args.schema)
        importer = OwnershipImporter(repository, projects_dir=args.projects_root, runtime_root=args.runtime_root)
        container = configuration.get("postgres_container") or configuration.get("container")
        if args.command == "restore":
            if not args.backup:
                raise ContractViolation("Provide the validated backup directory")
            backup = BackupEvidence.model_validate_json((Path(args.backup) / "manifest.json").read_text())
            importer.restore(backup, postgres_container=container)
            print("Isolated database restore complete; authenticated guards remain in place")
            return
        if not args.mapping:
            raise ContractViolation("Provide an explicit ownership mapping file")
        plan = importer.plan(json.loads(Path(args.mapping).read_text()))
        if args.command == "plan":
            if args.output:
                descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    output.write(plan.model_dump_json(indent=2) + "\n")
            print(json.dumps({"resources": len(plan.items), "conflicts": plan.conflicts}))
        elif args.command == "backup":
            if not args.backup:
                raise ContractViolation("Provide a new private backup directory")
            importer.backup(plan, args.backup, postgres_container=container)
            print("Private database, canonical files and Pi-reference backup verified")
        else:
            if not args.backup:
                raise ContractViolation("Provide the validated backup directory")
            authority = verify_authority(plan, configuration)
            backup = BackupEvidence.model_validate_json((Path(args.backup) / "manifest.json").read_text())
            result = importer.apply(plan, backup, authority=authority)
            print(result.model_dump_json())
    except ContractViolation as error:
        parser.exit(1, f"Ownership {error.code}: {error}\n")
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, "Ownership invalid_input: Operator input or private file cannot be verified\n")


if __name__ == "__main__":
    main()
