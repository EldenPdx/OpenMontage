"""Coordinate a disposable identity restore while preserving the original services."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def write_private(path, value):
    from tests.fixtures.auth32_recovery_control import write_private as publish
    publish(path, value)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare(control, owner):
    if owner.resolve() != owner or not owner.is_dir() or owner.stat().st_mode & 0o077:
        raise RuntimeError("Recovery requires a private lifecycle-owner state directory")
    if control.resolve() != control:
        raise RuntimeError("Recovery control directory must not be aliased")
    control.mkdir(mode=0o700)
    value = {"schema_version": 1, "run_id": secrets.token_hex(16), "owner_state_dir": str(owner)}
    write_private(control / "plan.json", value)
    return control / "plan.json"


class RecoveryOperator:
    def __init__(self, plan, timeout):
        from tests.fixtures.auth32_recovery_control import read_private
        self.read = read_private
        self.plan = self.read(plan)
        if set(self.plan) != {"schema_version", "run_id", "owner_state_dir"} or self.plan["schema_version"] != 1 or not re.fullmatch(r"[a-f0-9]{32}", self.plan["run_id"]):
            raise RuntimeError("Invalid fixed recovery plan")
        self.control, self.owner = plan.parent, Path(self.plan["owner_state_dir"])
        if self.owner.resolve() != self.owner or self.owner.name != ".state" or self.owner.parent.name != "auth-runtime" or self.owner.stat().st_mode & 0o077:
            raise RuntimeError("Recovery requires the actual private lifecycle owner")
        if self.control.resolve() != self.control or self.control.stat().st_mode & 0o077:
            raise RuntimeError("Recovery control must be private")
        self.owner_root = self.owner.parent.parent
        self.deadline = time.monotonic() + timeout
        self.manifest = None
        self.manifest_hash = None
        self.originals = {}
        self.stopped = False
        self.archives = {}
        self.log = os.fdopen(os.open(self.control / "operator.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")

    def command(self, arguments, *, payload=None, timeout=120):
        environment = {**os.environ, "AUTH_STATE_DIR": str(self.owner)}
        result = subprocess.run(arguments, cwd=self.owner_root, env=environment, input=payload,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        self.log.write(result.stderr)
        self.log.flush()
        if result.returncode:
            raise RuntimeError("Native recovery operation failed; retain private operator diagnostics")
        return result.stdout

    def compose(self, *arguments):
        files = ["--file", str(self.owner_root / "compose.auth.yaml")]
        override = self.owner / "sso-compose.override.json"
        if override.exists():
            self.read(override)
            files += ["--file", str(override)]
        self.command(["docker", "compose", "--project-name", "openmontage-auth24", *files, *arguments], timeout=180)

    def query(self, database, statement):
        return self.command(["docker", "exec", self.postgres, "psql", "-X", "-qAt", "-U", "auth_bootstrap",
            "-d", database, "-v", "ON_ERROR_STOP=1", "-c", statement])

    def acknowledge(self, phase):
        write_private(self.control / "ack.json", {"schema_version": 1, "run_id": self.plan["run_id"],
            "phase": phase, "manifest_sha256": self.manifest_hash})

    def wait_request(self, phase):
        while time.monotonic() < self.deadline:
            path = self.control / "request.json"
            if path.exists():
                value = self.read(path)
                if set(value) != {"schema_version", "run_id", "phase", "manifest_sha256"} or value["schema_version"] != 1 or value["run_id"] != self.plan["run_id"] or not re.fullmatch(r"[a-f0-9]{64}", value["manifest_sha256"]):
                    raise RuntimeError("Recovery request does not match the fixed plan")
                if self.manifest_hash and value["manifest_sha256"] != self.manifest_hash:
                    raise RuntimeError("Recovery manifest changed")
                if value["phase"] == phase:
                    return value
                if value["phase"] == "restore_original":
                    raise RuntimeError("Recovery test requested early original restoration")
            time.sleep(0.05)
        raise RuntimeError("Recovery operator request timed out")

    def verify_manifest(self, request):
        from tests.fixtures.auth32_native_recovery import native_state, private_file_hashes, recovery_target
        path = self.control / "manifest.json"
        self.manifest_hash = sha256(path.read_bytes()).hexdigest()
        if self.manifest_hash != request["manifest_sha256"]:
            raise RuntimeError("Recovery manifest does not match the exact request")
        self.manifest = self.read(path)
        value = self.manifest
        if value["schema_version"] != 1 or value["run_id"] != self.plan["run_id"] or value["owner_state_dir"] != str(self.owner) or not re.fullmatch(r"auth32_test_[a-f0-9]{32}", value["schema"]):
            raise RuntimeError("Recovery manifest does not belong to this drill")
        for name, basename in {"source_root": "source", "target_root": "target", "source_operator": "source-operator.json",
            "target_operator": "target-operator.json", "mapping": "mapping.json", "business_backup": "business-backup"}.items():
            if value[name] != str(self.control / basename) or Path(value[name]).is_symlink():
                raise RuntimeError("Recovery manifest has an unapproved path")
        if value["native_sources"] != {"logto": "auth_logto", "keycloak": "auth_keycloak"} or value["native_clones"] != {name: "auth32_restore_" + name + "_" + self.plan["run_id"] for name in ("logto", "keycloak")}:
            raise RuntimeError("Recovery databases do not match the fixed disposable plan")
        baseline = native_state(self.owner)
        if baseline != value["native_baseline"] or private_file_hashes(self.owner) != value["retained_private_files"]:
            raise RuntimeError("Recovery source or private inputs changed")
        self.postgres = baseline["identity-postgres"]["container_id"]
        for service in ("logto", "keycloak", "identity-postgres"):
            inspected = json.loads(self.command(["docker", "inspect", baseline[service]["container_id"]]))[0]
            labels = inspected["Config"]["Labels"]
            if labels.get("com.docker.compose.project.working_dir") != str(self.owner_root) or labels.get("org.openmontage.auth-runtime") != "24-v1":
                raise RuntimeError("Recovery cannot operate another checkout's services")
        from tests.fixtures.auth32_roles import private_role_configuration
        configuration = private_role_configuration()
        restored_configuration = recovery_target(configuration)
        for side, expected in (("source", configuration), ("target", restored_configuration)):
            if self.read(Path(value[side + "_operator"])) != {"database_url": expected["bootstrap_database_url"], "postgres_container": expected["postgres_container"]}:
                raise RuntimeError("Business recovery operator does not match the assigned runtime configuration")
        for service, filename in (("logto", "logto.env"), ("keycloak", "keycloak.env")):
            path = self.owner / filename
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise RuntimeError("Recovery Native environment is not private")
            self.originals[service] = path.read_bytes()
            backup = self.control / (filename + ".original")
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(self.originals[service])
                output.flush()
                os.fsync(output.fileno())
        for clone in value["native_clones"].values():
            if self.query("postgres", "SELECT 1 FROM pg_database WHERE datname='" + clone + "'").strip():
                raise RuntimeError("Recovery Native clone already exists")

    def snapshot_restore(self):
        from tests.fixtures.auth32_native_recovery import native_sql_snapshot
        frozen = self.read(self.control / "source-native-observed.json")
        for service, database in self.manifest["native_sources"].items():
            observed = native_sql_snapshot(self.postgres, database)
            if observed != frozen[service]:
                raise RuntimeError("Recovery source was not frozen at the observed snapshot")
            payload = self.command(["docker", "exec", self.postgres, "pg_dump", "-U", "auth_bootstrap", "-Fc", "-d", database])
            if not payload.startswith(b"PGDMP"):
                raise RuntimeError("Native recovery archive is not a whole custom archive")
            descriptor = os.open(self.control / (service + ".dump"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            clone = self.manifest["native_clones"][service]
            role = "auth_" + service
            self.query("postgres", 'CREATE DATABASE "' + clone + '" OWNER "' + role + '"')
            self.query("postgres", 'REVOKE ALL ON DATABASE "' + clone + '" FROM PUBLIC')
            self.query("postgres", 'GRANT CONNECT ON DATABASE "' + clone + '" TO "' + role + '"')
            if service == "logto":
                tenants = self.query(database, "SELECT db_user FROM public.tenants ORDER BY db_user").decode().splitlines()
                for tenant in tenants:
                    if not re.fullmatch(r"logto_tenant_auth_logto[a-z0-9_]*", tenant):
                        raise RuntimeError("Unknown Native tenant role")
                    self.query("postgres", 'GRANT CONNECT ON DATABASE "' + clone + '" TO "' + tenant + '"')
            self.command(["docker", "exec", "-i", self.postgres, "pg_restore", "-U", "auth_bootstrap", "--exit-on-error", "--single-transaction", "-d", clone], payload=payload)
            self.archives[service] = {"source_database": database, "clone_database": clone, "dump_sha256": sha256(payload).hexdigest(),
                "source_frozen": observed["tables"], "source_sequences_frozen": observed["sequences"], "source_large_objects_frozen": observed["large_objects"]}
        for command, side in (("backup", "source"), ("restore", "target")):
            arguments = [sys.executable, "-B", "-m", "production.identity_migrate", command,
                "--private-config", self.manifest[side + "_operator"], "--schema", self.manifest["schema"],
                "--projects-root", str(Path(self.manifest[side + "_root"]) / "projects"),
                "--runtime-root", str(Path(self.manifest[side + "_root"]) / "worker-runtime"), "--backup", self.manifest["business_backup"]]
            if command == "backup":
                arguments += ["--mapping", self.manifest["mapping"]]
            self.command(arguments)
        for service, filename, key in (("logto", "logto.env", "DB_URL"), ("keycloak", "keycloak.env", "KC_DB_URL")):
            lines = self.originals[service].decode().splitlines()
            changed = []
            for line in lines:
                if line.startswith(key + "="):
                    url = line.split("=", 1)[1]
                    if urlsplit(url.removeprefix("jdbc:")).path != "/" + self.manifest["native_sources"][service]:
                        raise RuntimeError("Native environment source changed")
                    line = key + "=" + url.rsplit("/", 1)[0] + "/" + self.manifest["native_clones"][service]
                changed.append(line)
            self.publish_environment(filename, ("\n".join(changed) + "\n").encode())
        self.compose("up", "--detach", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "150", "--pull", "never", "--no-build", "keycloak", "logto")

    def publish_environment(self, filename, value):
        path = self.owner / filename
        descriptor, temporary = tempfile.mkstemp(prefix="." + filename + ".recovery-", dir=self.owner)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            parent = os.open(self.owner, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def restore_original(self):
        from tests.fixtures.auth32_native_recovery import native_sql_snapshot, native_state
        # Let an interrupted drill finish its bounded recovery before accepting another signal.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        recovery_failed = False
        try:
            self.compose("stop", "logto", "keycloak")
        except Exception:
            recovery_failed = True
        retention_failed = False
        for service, record in self.archives.items():
            try:
                observed = native_sql_snapshot(self.postgres, record["source_database"])
                record.update(source_after_clone=observed["tables"], source_sequences_after_clone=observed["sequences"], source_large_objects_after_clone=observed["large_objects"])
                retention_failed |= (record["source_frozen"], record["source_sequences_frozen"], record["source_large_objects_frozen"]) != (observed["tables"], observed["sequences"], observed["large_objects"])
            except Exception:
                retention_failed = True
        for service, filename in (("logto", "logto.env"), ("keycloak", "keycloak.env")):
            try:
                self.publish_environment(filename, self.originals[service])
            except Exception:
                recovery_failed = True
        try:
            self.compose("up", "--detach", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "150", "--pull", "never", "--no-build", "keycloak", "logto")
        except Exception:
            recovery_failed = True
        observed = native_state(self.owner)
        if any(observed[name]["database"] != database for name, database in self.manifest["native_sources"].items()):
            raise RuntimeError("Original Native database targets were not restored")
        write_private(self.control / "native-archives.json", {"schema_version": 1, "run_id": self.plan["run_id"],
            "manifest_sha256": self.manifest_hash, "databases": self.archives})
        self.stopped = False
        self.acknowledge("original_ready")
        if retention_failed or recovery_failed:
            raise RuntimeError("Original services recovered, but the recovery operation did not fully pass")

    def serve(self):
        try:
            request = self.wait_request("source_quiesced")
            self.verify_manifest(request)
            self.stopped = True
            self.compose("stop", "logto", "keycloak")
            self.acknowledge("native_quiesced")
            self.wait_request("snapshot_allowed")
            self.snapshot_restore()
            self.acknowledge("restored_clones_ready")
            self.wait_request("restore_original")
        finally:
            try:
                if self.stopped:
                    self.restore_original()
            finally:
                self.log.close()


def main():
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "serve"))
    parser.add_argument("--owner-state", type=Path)
    parser.add_argument("--control", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--timeout", type=int, default=1200)
    arguments = parser.parse_args()
    try:
        if arguments.command == "prepare":
            if not arguments.control or not arguments.owner_state:
                parser.error("prepare needs --control and --owner-state")
            print(prepare(arguments.control.absolute(), arguments.owner_state.absolute()))
        else:
            if not arguments.plan or not 1 <= arguments.timeout <= 3600:
                parser.error("serve needs --plan and a bounded timeout")
            def interrupted(signum, frame):
                raise RuntimeError("Recovery operator was interrupted")
            signal.signal(signal.SIGTERM, interrupted)
            RecoveryOperator(arguments.plan.absolute(), arguments.timeout).serve()
    except (Exception, KeyboardInterrupt):
        raise SystemExit("Native recovery did not pass; retain private diagnostics and verify original services") from None


if __name__ == "__main__":
    main()
