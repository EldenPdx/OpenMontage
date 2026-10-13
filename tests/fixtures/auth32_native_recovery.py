"""Real read-only Native observations and a dedicated restricted-role recovery BFF."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import re
import stat
import subprocess
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from playwright.sync_api import sync_playwright

from backlot.server import create_app
from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.auth.config import AuthConfig
from production.worker import Worker
from tests.browser.test_studio_submission import serving
from tests.fixtures.auth30 import ORIGIN, SsoBrowser
from tests.fixtures.auth32_recovery_control import read_private


def native_command(arguments, *, stdin=None):
    try:
        result = subprocess.run(["docker", *arguments], input=stdin, capture_output=True, timeout=20)
        if result.returncode:
            raise RuntimeError
        return result.stdout
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        raise RuntimeError("Native recovery observation failed; inspect private operator diagnostics") from None


def native_state(owner, *, quiesced=False):
    """No lifecycle operations; verify actual database targets, pinned images and seal."""
    loaded = read_private(owner / "images/loaded.json")
    result = {}
    for service, pin in (("identity-postgres", "postgres"), ("logto", "logto"), ("keycloak", "keycloak")):
        ids = native_command(["ps", "--all", "--quiet", "--filter", "label=com.docker.compose.project=openmontage-auth24",
                              "--filter", "label=com.docker.compose.service=" + service]).decode().split()
        if len(ids) != 1 or not re.fullmatch(r"[a-f0-9]{12,64}", ids[0]):
            raise RuntimeError("Recovery requires exactly one recorded Native service")
        container = json.loads(native_command(["inspect", ids[0]]))[0]
        state = container["State"]
        running = not quiesced or service == "identity-postgres"
        assert state["Running"] is running and state["Paused"] is False, "Native writer quiescence or healthy recovery was not observed"
        if running:
            assert state["Health"]["Status"] == "healthy", "Original or cloned Native service must be healthy"
        assert container["Image"] == loaded[pin]["image_id"], "Native recovery must retain its verified image"
        environment = dict(entry.split("=", 1) for entry in container["Config"]["Env"] if "=" in entry)
        value = {"image_id": container["Image"], "container_id": container["Id"]}
        if service == "logto":
            value["database"] = urlsplit(environment["DB_URL"]).path.removeprefix("/")
            targets = ("main-YCQ7ZWUE.js", "openmontage-sso.mjs")
            mounts = {entry["Destination"]: entry for entry in container["Mounts"]}
            value["seal"] = {}
            for name in targets:
                target = "/etc/logto/packages/core/build/" + name
                mount = mounts[target]
                assert mount["Type"] == "bind" and mount["RW"] is False
                source = owner / "sso" / name
                assert Path(mount["Source"]) == source
                value["seal"][name] = sha256(source.read_bytes()).hexdigest()
                if running:
                    script = "const fs=require('node:fs'),crypto=require('node:crypto');process.stdout.write(crypto.createHash('sha256').update(fs.readFileSync(process.argv[1])).digest('hex'))"
                    actual = native_command(["exec", container["Id"], "node", "-e", script, target]).decode()
                    assert actual == value["seal"][name], "Actual Native policy differs from its sealed recovery input"
        elif service == "keycloak":
            value["database"] = urlsplit(environment["KC_DB_URL"].removeprefix("jdbc:")).path.removeprefix("/")
        result[service] = value
    return result


def native_public_keys(fixture):
    if fixture.idp["saml"]["metadata_url"] != "http://127.0.0.1:8180/realms/auth30/protocol/saml/descriptor":
        raise RuntimeError("Recovery only accepts its fixed local standard IdP metadata")
    with httpx.Client(trust_env=False, timeout=10) as client:
        discovery = client.get(fixture._data["issuer"] + "/.well-known/openid-configuration")
        discovery.raise_for_status()
        metadata = discovery.json()
        assert metadata["issuer"] == fixture._data["issuer"]
        assert metadata["jwks_uri"] == "http://127.0.0.1:3010/oidc/jwks"
        keys = client.get(metadata["jwks_uri"])
        keys.raise_for_status()
        saml = client.get(fixture.idp["saml"]["metadata_url"])
        saml.raise_for_status()
        return {"logto_jwks_sha256": sha256(json.dumps(keys.json(), sort_keys=True).encode()).hexdigest(),
                "keycloak_saml_metadata_sha256": sha256(saml.content).hexdigest()}


def private_file_hashes(owner):
    paths = [owner / name for name in ("credentials.json", "pg-bootstrap.password", "logto-bootstrap.env", "logto.env", "keycloak.env")]
    paths += [*sorted(owner.glob("fixture*.json")), *sorted(owner.glob("idp-*.json"))]
    return {str(path): sha256(path.read_bytes()).hexdigest() for path in paths}


def files_snapshot(directory):
    return {path.relative_to(directory).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


def business_snapshot(configuration, schema):
    """Read every table and sequence of this test's own retained schema only."""
    if not re.fullmatch(r"auth32_test_[a-f0-9]{32}", schema):
        raise RuntimeError("Recovery observation is restricted to this test schema")
    result = {}
    with psycopg.connect(configuration["bootstrap_database_url"], options="-c default_transaction_read_only=on") as connection:
        tables = connection.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename", (schema,)).fetchall()
        if not tables:
            raise RuntimeError("Original business schema was lost during recovery")
        for (name,) in tables:
            rows = connection.execute(sql.SQL("SELECT to_jsonb(t)::text FROM {}.{} t ORDER BY to_jsonb(t)::text").format(sql.Identifier(schema), sql.Identifier(name))).fetchall()
            result[name] = sha256(json.dumps(rows).encode()).hexdigest()
        sequences = connection.execute("SELECT sequencename FROM pg_sequences WHERE schemaname=%s ORDER BY sequencename", (schema,)).fetchall()
        for (name,) in sequences:
            value = connection.execute(sql.SQL("SELECT last_value,is_called FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(name))).fetchone()
            result["sequence:" + name] = sha256(json.dumps(value).encode()).hexdigest()
    return result


def native_sql_snapshot(container, database):
    """Fixed read-only observation of the original pinned Native databases only."""
    if not re.fullmatch(r"[a-f0-9]{64}", container) or database not in {"auth_logto", "auth_keycloak"}:
        raise RuntimeError("Native retention observation is restricted to the original owner databases")
    prefix = ["exec", "--env", "PGOPTIONS=-c default_transaction_read_only=on", container,
              "psql", "-X", "-qAt", "-U", "auth_bootstrap", "-d", database, "-v", "ON_ERROR_STOP=1", "-c"]

    def query(statement):
        return native_command([*prefix, statement])

    table_names = query("SELECT schemaname||'.'||tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') AND schemaname NOT LIKE 'pg_toast%' ORDER BY schemaname,tablename").decode().splitlines()
    sequence_names = query("SELECT schemaname||'.'||sequencename FROM pg_sequences WHERE schemaname NOT IN ('pg_catalog','information_schema') AND schemaname NOT LIKE 'pg_toast%' ORDER BY schemaname,sequencename").decode().splitlines()
    if not table_names or any(not re.fullmatch(r"public\.[a-z_][a-z0-9_]*", name) for name in [*table_names, *sequence_names]):
        raise RuntimeError("Pinned Native database inventory differs from the recovery contract")
    tables = {name: sha256(query('SELECT to_jsonb(t)::text FROM public."' + name.split(".")[1] + '" t ORDER BY to_jsonb(t)::text')).hexdigest()
              for name in table_names}
    sequences = {name: sha256(query('SELECT last_value,is_called FROM public."' + name.split(".")[1] + '"')).hexdigest()
                 for name in sequence_names}
    large_objects = sha256(query("SELECT loid,pageno,encode(data,'hex') FROM pg_largeobject ORDER BY loid,pageno")).hexdigest()
    return {"tables": tables, "sequences": sequences, "large_objects": large_objects}


def recovery_target(configuration):
    target = {**configuration, **{name + "_database_url": configuration["target_" + name + "_database_url"]
                                 for name in ("bootstrap", "bff", "worker")}}
    source_accounts = [conninfo_to_dict(configuration[name + "_database_url"]) for name in ("bootstrap", "bff", "worker")]
    target_accounts = [conninfo_to_dict(target[name + "_database_url"]) for name in ("bootstrap", "bff", "worker")]
    if (len({account["dbname"] for account in target_accounts}) != 1
        or target_accounts[0]["dbname"] == source_accounts[0]["dbname"]
        or any(any(source.get(field) != restored.get(field) for field in ("host", "port", "user"))
               for source, restored in zip(source_accounts, target_accounts))):
        raise RuntimeError("Recovery target requires a separate empty owned database and the same restricted principals")
    with psycopg.connect(target["bootstrap_database_url"], options="-c default_transaction_read_only=on") as connection:
        if connection.execute("SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') AND schemaname NOT LIKE 'pg_toast%')").fetchone() != (False,):
            raise RuntimeError("Recovery business target is not empty; retain previous recovery data and provision a fresh target")
    return target


def verify_native_archives(directory, run_id, manifest_hash, sources, clones, postgres_container, observed_source):
    """Check whole-database custom archives and frozen SQL inventories, never bool receipts."""
    receipt = read_private(directory / "native-archives.json")
    assert receipt["schema_version"] == 1 and receipt["run_id"] == run_id and receipt["manifest_sha256"] == manifest_hash
    assert set(receipt["databases"]) == {"logto", "keycloak"}
    for name, source in sources.items():
        record = receipt["databases"][name]
        assert record["source_database"] == source and record["clone_database"] == clones[name]
        inventory = record["source_frozen"]
        assert inventory and inventory == record["source_after_clone"] == observed_source[name]["tables"], "Source Native database changed while cloned services were exercised"
        assert record["source_sequences_frozen"] == record["source_sequences_after_clone"] == observed_source[name]["sequences"]
        assert record["source_large_objects_frozen"] == record["source_large_objects_after_clone"] == observed_source[name]["large_objects"]
        assert all(re.fullmatch(r"public\.[a-z_][a-z0-9_]*", table) and re.fullmatch(r"[a-f0-9]{64}", digest)
                   for table, digest in inventory.items())
        dump = directory / (name + ".dump")
        assert dump.resolve() == dump and stat.S_ISREG(dump.lstat().st_mode), "Native archive must not be aliased"
        payload = dump.read_bytes()
        assert dump.stat().st_mode & 0o077 == 0 and payload[:5] == b"PGDMP"
        assert sha256(payload).hexdigest() == record["dump_sha256"]
        toc = native_command(["exec", "-i", postgres_container, "pg_restore", "--list"], stdin=payload).decode()
        restored_tables = {"public." + match for match in re.findall(r"TABLE DATA public ([a-z_][a-z0-9_]*) ", toc)}
        assert restored_tables == set(inventory), "Native backup omitted a source table or private key relation"


class RecoverySso(SsoBrowser):
    def headers(self, context, key):
        me = self.me(context)
        return {"Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"], "X-Auth-Context": me["context_id"], "Idempotency-Key": key}

    def run_worker(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lambda: asyncio.run(self.worker.run_once())).result(timeout=90)

    def complete(self, context):
        task = self.create(context)
        for turn in range(8):
            self.run_worker()
            response = context.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"])
            assert response.status == 200
            task = response.json()
            if task["state"] == "succeeded":
                return task
            assert task["state"] == "awaiting_approval", "Owned recovery production must reach its real gate"
            binding = task["approval"]["binding"]
            response = context.request.post(ORIGIN + "/api/studio/tasks/" + task["task_id"] + "/approvals/" + binding["gate_id"] + "/decision",
                headers=self.headers(context, "auth32-recovery-approval-" + str(turn)),
                data={"binding": binding, "expected_version": task["version"], "decision": "approve"})
            assert response.status == 202
        raise RuntimeError("Real recovery production did not complete")

    def principal(self, context, protocol):
        me, actual = self.me(context), self.record_principal(context, protocol)
        proof = self.fixture._data["sso_subjects"][protocol]
        assert actual["native_subject"] == proof["native_subject"]
        assert actual["connector_id"] == proof["connector_id"] and actual["sso_identity_id"] == proof["identity_id"]
        assert actual["sso_issuer"] == proof["issuer"]
        assert me["workspace"]["key"] == "organization:" + self.fixture.organizations["A"]
        assert me["role"] in proof["memberships"]["A"] and "resource:read" in me["permissions"]
        if protocol == "saml":
            assert actual["signed_saml_name_id_format"] == "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"
            assert actual["xml_signature_count"] == 2
        return {"user_id": me["user"]["user_id"], "role": me["role"], "permissions": sorted(me["permissions"]),
                "native_subject": actual["native_subject"], "identity_id": actual["sso_identity_id"]}


@contextmanager
def recovery_bff(root, configuration, repository, fixture, monkeypatch, *, studio_configuration=None, key=None, production=False):
    from tests.fixtures.studio.model_server import model_server
    from tests.fixtures.studio.production_flow import ProductionFlow, production_registry
    from tests.fixtures.studio.repository import require_pi
    from production.repository import PostgresRepository
    from contextlib import ExitStack

    with ExitStack() as stack:
        registry = None
        if production:
            require_pi()
            flow = ProductionFlow(repository, repository.projects_dir)
            url, _ = stack.enter_context(model_server(flow))
            profile = PiProfile(provider="controlled", base_url=url, model="local-model", credential_env="AUTH28_MODEL_KEY",
                                reasoning=False, thinking_level="off", price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
            studio_configuration = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
            registry, _ = production_registry()
            stack.callback(registry.fixture_manager.shutdown)
        ledger = fixture._data
        values = fixture.private_app_configuration()
        assert ledger["namespace"] == "auth30" and values["redirect_uri"] == ORIGIN + "/api/auth/callback"
        values.update(mode="authenticated", origin=ORIGIN, http_development=True,
            management_endpoint=values["endpoint"], management_resource=ledger["management"]["resource"],
            management_app_id=ledger["management"]["app_id"], management_app_secret=ledger["management"]["app_secret"],
            database_url=configuration["bff_database_url"], database_schema=repository.schema,
            encryption_keys={"auth32": key or configuration["session_encryption_key"]}, active_key_id="auth32")
        from backlot import projects_api, state
        monkeypatch.setattr(projects_api, "PROJECTS_DIR", repository.projects_dir)
        monkeypatch.setattr(state, "PROJECTS_DIR", repository.projects_dir)
        app = create_app(studio_repository=repository, studio_config=studio_configuration,
                         studio_environment={"AUTH28_MODEL_KEY": "private-auth32-controlled-model"}, auth_config=AuthConfig.model_validate(values))
        stack.enter_context(serving(app, port=4765))
        playwright = stack.enter_context(sync_playwright())
        browser = playwright.chromium.launch(headless=True)
        stack.callback(browser.close)
        sso = RecoverySso(browser, fixture, repository, app)
        worker = PostgresRepository(configuration["worker_database_url"], schema=repository.schema, projects_dir=repository.projects_dir)
        sso.worker = Worker(worker, studio_configuration, root / "worker-runtime", worker.projects_dir,
                            environment={"AUTH28_MODEL_KEY": "private-auth32-controlled-model"}, **({"registry": registry} if registry else {}))
        yield sso, studio_configuration
