"""Run required identity acceptance in the exclusively owned disposable environment."""

import argparse
import base64
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "auth-runtime/.state"
EVIDENCE = STATE / "validation"
PACKAGES = {line.split("==")[0]: line.split("==")[1] for line in (ROOT / "requirements-auth.txt").read_text().splitlines() if "==" in line}
DEPENDENCIES = ("logto", "identity-postgres", "business-postgres", "idp", "mail", "pi", "ffmpeg", "chromium", "node", "docker-compose")


def private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def validation_plan(suite):
    catalog = json.loads((ROOT / "auth-runtime/required-nodes.json").read_text())
    manual = json.loads((ROOT / "auth-runtime/manual-security.json").read_text())
    functions = set(manual["functions"])
    manual_variants = set(manual.get("nodes", []))
    all_nodes = [node for definition in catalog["phases"].values() for node in definition["nodes"]]
    if len(all_nodes) != len(set(all_nodes)) or not functions or functions - {node.split("[", 1)[0] for node in all_nodes} or manual_variants - set(all_nodes):
        raise RuntimeError("Required identity catalog must uniquely account for every explicit manual security function")
    phases = {}
    manual_nodes = []
    for name, definition in catalog["phases"].items():
        selected = []
        for node in definition["nodes"]:
            is_manual = node.split("[", 1)[0] in functions or node in manual_variants
            if is_manual:
                manual_nodes.append(node)
            if is_manual == (suite == "manual-security"):
                selected.append(node)
        if selected:
            phases[name] = {**definition, "nodes": selected, "suite": suite}
    return {"suite": suite, "phases": phases, "manual_pending": sorted(manual_nodes),
            "manual_pending_operations": ["dependency-failure-calibration"],
            "manual_regression_functions": sorted(functions), "manual_checklist": manual["checklist"],
            "acceptance_pending_owners": catalog["pending_owners"], "acceptance_status": "PENDING"}


def execute(name, arguments, *, environment=None, timeout=300, expected_success=True):
    EVIDENCE.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(EVIDENCE / (name + ".log"), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as log:
        result = subprocess.run(arguments, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    if (result.returncode == 0) != expected_success:
        raise RuntimeError("Required identity operation failed: " + name)
    return result.returncode


def dependencies(selected):
    for dependency in selected:
        try:
            if dependency.startswith("library:"):
                package = dependency.split(":", 1)[1]
                if version(package) != PACKAGES[package]:
                    raise RuntimeError("Pinned package drift")
            elif dependency in {"identity-postgres", "business-postgres"}:
                import psycopg
                if dependency == "identity-postgres":
                    import manage
                    credentials = json.loads((STATE / "credentials.json").read_text())
                    dsn = manage.database_url(credentials)
                else:
                    dsn = os.environ["STUDIO_TEST_DATABASE_URL"]
                with psycopg.connect(dsn, connect_timeout=3, options="-c statement_timeout=3000") as connection:
                    if not connection.execute("SHOW server_version").fetchone()[0].startswith("17.11"):
                        raise RuntimeError("PostgreSQL version drift")
            elif dependency in {"logto", "idp", "mail"}:
                import httpx
                url = {"logto": "http://127.0.0.1:3010/oidc/.well-known/openid-configuration",
                       "idp": "http://127.0.0.1:8180/realms/master/.well-known/openid-configuration",
                       "mail": "http://127.0.0.1:8025/api/v1/messages"}[dependency]
                with httpx.Client(trust_env=False, timeout=3) as client:
                    response = client.get(url)
                    response.raise_for_status()
                    if dependency != "mail" and response.json()["issuer"] != url.split("/.well-known/")[0]:
                        raise RuntimeError("Identity issuer drift")
            elif dependency == "pi":
                execute("dependency-pi", [sys.executable, "-B", "pi-runtime/check.py"], timeout=90)
            elif dependency == "ffmpeg":
                if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                    raise RuntimeError("Media binary missing")
                execute("dependency-ffmpeg", ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=duration=0.1:size=128x72:rate=10",
                    "-f", "lavfi", "-i", "sine=duration=0.1", "-c:v", "libx264", "-c:a", "aac", "-f", "null", "-"], timeout=30)
                execute("dependency-ffprobe", ["ffprobe", "-version"], timeout=10)
            elif dependency == "chromium":
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True, timeout=15000)
                    try:
                        browser.new_page().goto("about:blank")
                    finally:
                        browser.close()
            else:
                command = ["node", "--version"] if dependency == "node" else ["docker", "compose", "version"]
                execute("dependency-" + dependency, command, timeout=15)
        except Exception:
            raise RuntimeError("Required dependency failed: " + dependency) from None


def business_account():
    """A test login can create isolated schemas without any server administration rights."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    bootstrap = os.environ["STUDIO_TEST_BOOTSTRAP_DATABASE_URL"]
    passwords = {role: secrets.token_urlsafe(32) for role in ("auth32_test", "auth32_bootstrap", "auth32_bff", "auth32_worker")}
    with psycopg.connect(bootstrap, autocommit=True, connect_timeout=5) as connection:
        if connection.execute("SELECT current_database(),current_user").fetchone() != ("studio_identity", "auth32_ci_bootstrap"):
            raise RuntimeError("Identity CI bootstrap requires its dedicated studio_identity database and auth32_ci_bootstrap role")
        database = connection.execute("SELECT current_database()").fetchone()[0]
        connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
        for role, password in passwords.items():
            if connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                raise RuntimeError("Fresh identity CI business role already exists")
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS").format(sql.Identifier(role), sql.Literal(password)))
            privileges = sql.SQL("CONNECT,CREATE") if role in {"auth32_test", "auth32_bootstrap"} else sql.SQL("CONNECT")
            connection.execute(sql.SQL("GRANT {} ON DATABASE {} TO {}").format(privileges, sql.Identifier(database), sql.Identifier(role)))
        for target in ("studio_identity_restore", "studio_identity_native_restore"):
            if connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (target,)).fetchone():
                raise RuntimeError("Fresh identity CI restore database already exists")
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
            connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(target)))
            for role in ("auth32_bootstrap", "auth32_bff", "auth32_worker"):
                privileges = sql.SQL("CONNECT,CREATE") if role == "auth32_bootstrap" else sql.SQL("CONNECT")
                connection.execute(sql.SQL("GRANT {} ON DATABASE {} TO {}").format(privileges, sql.Identifier(target), sql.Identifier(role)))
    fields = conninfo_to_dict(bootstrap)
    dsns = {role: make_conninfo(**{**fields, "user": role, "password": password}) for role, password in passwords.items()}
    os.environ["STUDIO_TEST_DATABASE_URL"] = dsns["auth32_test"]
    os.environ["STUDIO_TEST_SESSION_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    private_json(STATE / "ci-business.json", {key: os.environ[key] for key in ("STUDIO_TEST_DATABASE_URL", "STUDIO_TEST_SESSION_KEY")})
    role_path = STATE / "ci-runtime-roles.json"
    role_configuration = {"bootstrap_database_url": dsns["auth32_bootstrap"], "bff_database_url": dsns["auth32_bff"], "worker_database_url": dsns["auth32_worker"],
                          "session_encryption_key": os.environ["STUDIO_TEST_SESSION_KEY"], "postgres_container": os.environ["STUDIO_TEST_POSTGRES_CONTAINER"]}
    role_configuration.update({"target_" + field + "_database_url": make_conninfo(**{**fields, "dbname": "studio_identity_restore", "user": "auth32_" + field,
                               "password": passwords["auth32_" + field]}) for field in ("bootstrap", "bff", "worker")})
    private_json(role_path, role_configuration)
    native_configuration = {**role_configuration, **{"target_" + field + "_database_url": make_conninfo(
        **{**fields, "dbname": "studio_identity_native_restore", "user": "auth32_" + field, "password": passwords["auth32_" + field]})
        for field in ("bootstrap", "bff", "worker")}}
    private_json(STATE / "ci-native-runtime-roles.json", native_configuration)
    os.environ["AUTH32_DATABASE_CONFIG_FILE"] = str(role_path)
    with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
        flags = connection.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
        if flags != (False, False, False, False, False):
            raise RuntimeError("Identity CI business account is not restricted")


def owned_container(service):
    import manage
    manage.assert_ownership()
    value = manage.compose("ps", "--quiet", service).strip()
    if not re.fullmatch(r"[a-f0-9]{12,64}", value):
        raise RuntimeError("Required owned identity container is missing")
    return value


def validate_business_container(container, authorized_id, dsn, identity=None):
    """Match the explicit fresh CI service to the actual BFF PostgreSQL connection."""
    from psycopg.conninfo import conninfo_to_dict
    error = "Required business PostgreSQL container does not match its dedicated CI connection"
    try:
        fields = conninfo_to_dict(dsn)
        environment = dict(entry.split("=", 1) for entry in container["Config"]["Env"] if "=" in entry)
        ports = container["NetworkSettings"]["Ports"]["5432/tcp"]
        if (not re.fullmatch(r"[a-f0-9]{64}", authorized_id) or container["Id"] != authorized_id
            or environment.get("POSTGRES_DB") != "studio_identity" or environment.get("POSTGRES_USER") != "auth32_ci_bootstrap"
            or not container["State"]["Running"] or container["State"]["Paused"] or container["State"]["Health"]["Status"] != "healthy"
            or fields.get("dbname") != "studio_identity" or fields.get("user") != "auth32_test" or fields.get("host") != "127.0.0.1"
            or not ports or any(entry["HostIp"] not in {"0.0.0.0", "127.0.0.1", "::"} or entry["HostPort"] != fields.get("port") for entry in ports)
            or identity is not None and identity != ("studio_identity", "auth32_test", 5432)):
            raise RuntimeError(error)
        return authorized_id
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(error) from None


def business_outage_container():
    import psycopg
    identifier = os.environ.get("STUDIO_TEST_POSTGRES_CONTAINER", "")
    if not re.fullmatch(r"[a-f0-9]{64}", identifier):
        raise RuntimeError("Required business PostgreSQL service ID is missing")
    container = json.loads(subprocess.check_output(["docker", "inspect", identifier], text=True, stderr=subprocess.PIPE, timeout=15))[0]
    dsn = os.environ["STUDIO_TEST_DATABASE_URL"]
    validate_business_container(container, identifier, dsn)
    with psycopg.connect(dsn, connect_timeout=3, options="-c statement_timeout=3000") as connection:
        identity = connection.execute("SELECT current_database(),current_user,inet_server_port()").fetchone()
    return validate_business_container(container, identifier, dsn, identity)


def outage_container(service):
    return business_outage_container() if service == "postgres" else owned_container("logto")


def dependency_failures():
    cli = ROOT / ".runtime/pi/source/packages/coding-agent/dist/bundle/cli.js"
    if not cli.resolve().is_relative_to(ROOT / ".runtime/pi"):
        raise RuntimeError("Missing-dependency probes require an exclusively owned Pi installation")
    records = []
    for dependency, service in (("logto", "logto"), ("identity-postgres", "identity-postgres"), ("business-postgres", None), ("idp", "keycloak"), ("mail", "mailpit")):
        dependencies([dependency])
        container = owned_container(service) if service else business_outage_container()
        try:
            execute("pause-" + dependency, ["docker", "pause", container], timeout=15)
            execute("missing-" + dependency, [sys.executable, "-B", "auth-runtime/ci.py", "dependencies", "--dependency", dependency], timeout=20, expected_success=False)
        finally:
            execute("resume-" + dependency, ["docker", "unpause", container], timeout=15)
        dependencies([dependency])
        records.append({"dependency": dependency, "positive_before_after": True, "actual_service_fault_rejected": True})
    for package in PACKAGES:
        name = "library:" + package
        dependencies([name])
        execute("missing-library-" + package, [sys.executable, "-S", "-B", "auth-runtime/ci.py", "dependencies", "--dependency", name],
                environment={**os.environ, "PYTHONPATH": str(ROOT / "auth-runtime")}, timeout=15, expected_success=False)
        records.append({"dependency": name, "installed_control": True, "absent_library_rejected": True})
    for dependency in ("ffmpeg", "chromium", "pi"):
        dependencies([dependency])
        environment = dict(os.environ)
        hidden = cli.with_name("cli.js.validation-missing")
        try:
            if dependency == "ffmpeg":
                environment["PATH"] = str(EVIDENCE / "missing-binaries")
            elif dependency == "chromium":
                environment["PLAYWRIGHT_BROWSERS_PATH"] = str(EVIDENCE / "missing-browser")
            else:
                if hidden.exists():
                    raise RuntimeError("Retained Pi missing-dependency probe requires recovery")
                cli.rename(hidden)
            execute("missing-" + dependency, [sys.executable, "-B", "auth-runtime/ci.py", "dependencies", "--dependency", dependency], environment=environment, timeout=30, expected_success=False)
        finally:
            if dependency == "pi" and hidden.exists():
                hidden.rename(cli)
        dependencies([dependency])
        records.append({"dependency": dependency, "positive_before_after": True, "absent_binary_rejected": True})
    private_json(EVIDENCE / "dependency-failures.json", records)


def phase(name, definition):
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "auth-runtime"))),
                   "STUDIO_REQUIRE_INTEGRATION": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1",
                   "AUTH_SSO_VENDOR_ROOT": str(STATE / "sso-vendor")}
    for key in ("OPENMONTAGE_ALLOW_NETWORK", "STUDIO_XVAN_LIVE_SMOKE", "STUDIO_XVAN_PAID_APPROVED", "OPENMONTAGE_AUTH_CONFIG_FILE", "OPENMONTAGE_AUTH_MODE", "PYTEST_ADDOPTS"):
        environment.pop(key, None)
    if definition.get("database_config"):
        environment["AUTH32_DATABASE_CONFIG_FILE"] = str(STATE / definition["database_config"])
    namespace = definition.get("namespace")
    if namespace:
        origin = definition["origin"]
        execute("seed-" + namespace, [sys.executable, "-B", "auth-runtime/manage.py", "seed", "--namespace", namespace, "--app-origin", origin], timeout=120)
        sys.path.insert(0, str(ROOT))
        from tests.fixtures.identity import load_fixture
        path = STATE / ("fixture.json" if namespace == "auth24" else "fixture-" + namespace + ".json")
        fixture = load_fixture(path)
        fixture.app_for_origin(origin)
        if namespace == "auth32":
            fixture.app_for_origin("http://127.0.0.1:4762")
            from tests.fixtures.auth32_tls import ORIGIN as tls_origin
            name_tls = namespace + ":tls:4766"
            applications = [item for item in fixture.api.items("/api/applications") if item["name"] == name_tls]
            if len(applications) > 1:
                raise RuntimeError("Duplicate HTTPS validation applications")
            callbacks = {"redirectUris": [tls_origin + "/api/auth/callback"], "postLogoutRedirectUris": [tls_origin + "/en"]}
            application = applications[0] if applications else fixture.api.request("POST", "/api/applications", {
                "name": name_tls, "type": "Traditional", "oidcClientMetadata": callbacks, "customData": {"auth24": namespace}})
            fixture.api.request("PATCH", "/api/applications/" + application["id"], {"oidcClientMetadata": callbacks})
            if tls_origin not in fixture._data.setdefault("origin_apps", {}):
                secret = fixture.api.request("POST", "/api/applications/" + application["id"] + "/secrets", {"name": "auth32-https"})["value"]
                fixture._data["origin_apps"][tls_origin] = {"app_id": application["id"], "app_secret": secret, "endpoint": "http://127.0.0.1:3010",
                    "issuer": fixture._data["issuer"], "redirect_uri": callbacks["redirectUris"][0], "post_logout_redirect_uri": callbacks["postLogoutRedirectUris"][0]}
                fixture._data["resources"].append({"path": "/api/applications/" + application["id"], "key": "name", "name": name_tls})
                private_json(path, fixture._data)
        environment["AUTH_TEST_FIXTURE"] = str(path)
        if namespace == "auth30":
            execute("prepare-sso-" + name, [sys.executable, "-B", "auth-runtime/ci.py", "prepare-sso"],
                    environment=environment, timeout=180)
    else:
        environment.pop("AUTH_TEST_FIXTURE", None)
    nodes = definition["nodes"]
    manifest, report = EVIDENCE / (name + "-nodes.json"), EVIDENCE / (name + "-result.json")
    private_json(manifest, {"nodes": nodes})
    arguments = [sys.executable, "-B", "-m", "pytest", "-p", "required", "-p", "no:cacheprovider", "-q", "--tb=short",
                 "--rootdir=" + str(ROOT), "--required-nodes=" + str(manifest), "--required-report=" + str(report), "--basetemp=" + str(EVIDENCE / (name + "-tmp"))]
    if definition["runtime"] and not any(node.startswith("tests/") for node in nodes):
        arguments += ["-p", "tests.conftest"]
    if definition.get("suite") == "manual-security":
        arguments += ["--include-manual-security"]
    arguments += nodes
    if definition.get("coordinated_restore"):
        control = EVIDENCE / (name + "-recovery-" + secrets.token_hex(8))
        execute("prepare-" + name, [sys.executable, "-B", "auth-runtime/native_recovery.py", "prepare",
                "--owner-state", str(STATE), "--control", str(control)], environment=environment, timeout=15)
        environment["AUTH32_NATIVE_RECOVERY_PLAN"] = str(control / "plan.json")
        descriptor = os.open(EVIDENCE / (name + "-operator.log"), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as log:
            operator = subprocess.Popen([sys.executable, "-B", "auth-runtime/native_recovery.py", "serve", "--plan",
                str(control / "plan.json"), "--timeout", str(definition["timeout_seconds"])], cwd=ROOT, env=environment,
                stdout=log, stderr=subprocess.STDOUT)
            active = EVIDENCE / "active-native-recovery.json"
            try:
                private_json(active, {"pid": operator.pid, "plan": str(control / "plan.json")})
                execute(name, arguments, environment=environment, timeout=definition["timeout_seconds"])
                if operator.wait(timeout=600):
                    raise RuntimeError("Required Native recovery operator did not restore and verify the original services")
            finally:
                if operator.poll() is None:
                    operator.terminate()
                    try:
                        operator.wait(timeout=600)
                    except subprocess.TimeoutExpired:
                        raise RuntimeError("Native recovery operator is still restoring; retain private state and verify original services before cleanup") from None
                if operator.poll() is not None:
                    active.unlink(missing_ok=True)
    elif not definition.get("outages"):
        execute(name, arguments, environment=environment, timeout=definition["timeout_seconds"])
    else:
        control = EVIDENCE / "outage-control.json"
        control.unlink(missing_ok=True)
        environment["BACKLOT29_OUTAGE_CONTROL"] = str(control)
        descriptor = os.open(EVIDENCE / (name + ".log"), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as log:
            process = subprocess.Popen(arguments, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
            paused = None
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if time.monotonic() - started > definition["timeout_seconds"]:
                        raise RuntimeError("Required identity outage phase timed out")
                    value = json.loads(control.read_text()) if control.is_file() else {}
                    if value.get("phase") == "ready" and not paused:
                        if not re.fullmatch(r"[a-f0-9]{32}", value.get("scenario", "")) or value.get("service") not in {"logto", "postgres"}:
                            raise RuntimeError("Invalid private outage request")
                        paused = outage_container(value["service"])
                        execute("outage-pause", ["docker", "pause", paused], timeout=15)
                        private_json(control, {**value, "phase": "paused"})
                    elif value.get("phase") == "finished" and paused:
                        execute("outage-resume", ["docker", "unpause", paused], timeout=15)
                        paused = None
                        private_json(control, {**value, "phase": "resumed"})
                    time.sleep(0.05)
            finally:
                if paused:
                    execute("outage-final-resume", ["docker", "unpause", paused], timeout=15)
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=20)
            if process.returncode:
                raise RuntimeError("Required identity operation failed: " + name)
    result = json.loads(report.read_text())
    if not result.get("passed"):
        raise RuntimeError("Required identity result guard rejected: " + name)
    print(name + ": every reviewed node passed", flush=True)


def cleanup():
    active = EVIDENCE / "active-native-recovery.json"
    if active.is_file():
        value = json.loads(active.read_text())
        if type(value.get("pid")) is not int or value["pid"] <= 0 or not isinstance(value.get("plan"), str):
            raise RuntimeError("Invalid retained Native recovery operator record; verify original recovery before cleanup")
        observed = subprocess.run(["ps", "-p", str(value["pid"]), "-o", "args="], capture_output=True, text=True, timeout=5)
        if observed.returncode == 0 and "auth-runtime/native_recovery.py serve" in observed.stdout and value["plan"] in observed.stdout:
            raise RuntimeError("Native recovery operator is still active; verify original recovery before cleanup")
    import manage
    manage.assert_ownership()
    for service in ("logto", "identity-postgres", "keycloak", "mailpit"):
        value = manage.compose("ps", "--quiet", service).strip()
        if value and subprocess.check_output(["docker", "inspect", "--format", "{{.State.Paused}}", value], text=True).strip() == "true":
            execute("cleanup-unpause-" + service, ["docker", "unpause", value], timeout=15)
    execute("cleanup", [sys.executable, "-B", "auth-runtime/manage.py", "down"], timeout=120)


def sso_subject_proof(principal, memberships):
    return {"native_subject": principal["native_subject"], "connector_id": principal["connector_id"],
            "identity_id": principal["sso_identity_id"], "issuer": principal["sso_issuer"], "memberships": memberships}


def prepare_sso():
    paths = (STATE / "credentials.json", STATE / "fixture-auth30.json")
    if not STATE.is_dir() or STATE.resolve() != STATE or STATE.stat().st_mode & 0o077 or any(
        not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077 for path in paths):
        raise RuntimeError("SSO fixture setup requires this checkout's private lifecycle-owner state")
    import manage
    manage.assert_ownership()
    manage.check_sso_runtime()
    sys.path.insert(0, str(ROOT))
    import pytest
    from tests.fixtures.auth30 import sso_browser
    with pytest.MonkeyPatch.context() as monkeypatch, sso_browser(monkeypatch) as sso:
        saved = sso.fixture._data.setdefault("sso_subjects", {})
        for protocol in ("oidc", "saml"):
            context, _ = sso.sign_in(protocol)
            principal = sso.record_principal(context, protocol)
            if principal["email"] != principal["fixture_idp_email"] or principal["native_subject"] in {user.id for user in sso.fixture.users.values()}:
                raise RuntimeError("Fresh enterprise fixture identity did not bind its actual standard IdP subject")
            proof = sso_subject_proof(principal, {})
            previous = saved.get(protocol)
            if previous and any(previous[key] != proof[key] for key in ("connector_id", "native_subject", "identity_id", "issuer")):
                raise RuntimeError("Frozen enterprise fixture principal changed")
            saved[protocol] = proof
            context.close()
        if saved["oidc"]["native_subject"] == saved["saml"]["native_subject"]:
            raise RuntimeError("Standard fixture protocols require distinct actual enterprise identities")
        for protocol, organization, role in (("oidc", "A", "editor"), ("saml", "A", "viewer"), ("saml", "B", "editor")):
            organization_id, subject = sso.fixture.organizations[organization], saved[protocol]["native_subject"]
            members = sso.fixture.api.items("/api/organizations/" + organization_id + "/users")
            if subject not in {member["id"] for member in members}:
                sso.fixture.api.request("POST", "/api/organizations/" + organization_id + "/users", {"userIds": [subject]})
            sso.fixture.api.request("PUT", "/api/organizations/" + organization_id + "/users/" + subject + "/roles",
                                   {"organizationRoleIds": [sso.fixture._data["roles"][role]]})
            context, _ = sso.sign_in(protocol, organization=organization)
            current = sso.me(context)
            if current["role"] != role or current["workspace"]["key"] != "organization:" + organization_id:
                raise RuntimeError("Actual enterprise membership did not reach its explicitly granted BFF role")
            saved[protocol]["memberships"][organization] = [role]
            if role == "editor":
                task = sso.create(context)
                if context.request.get("http://127.0.0.1:4765/api/studio/tasks/" + task["task_id"]).status != 200:
                    raise RuntimeError("Enterprise fixture member cannot read its actual owned resource")
            context.close()
        private_json(sso.fixture.path, sso.fixture._data)


def publish():
    """Publish only reviewed node outcomes; raw assertions and private state stay on the runner."""
    summary = json.loads((EVIDENCE / "summary.json").read_text()) if (EVIDENCE / "summary.json").is_file() else {}
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    public = {}
    passed = {}
    manual_passed_nodes = set()
    for suite in ("automated", "manual-security"):
        plan = validation_plan(suite)
        receipt_path = EVIDENCE / ("summary-" + suite + ".json")
        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else summary if summary.get("suite", "automated") == suite else {}
        public[suite] = {}
        if receipt.get("candidate") == sha and receipt.get("run_id"):
            for name, definition in plan["phases"].items():
                path = EVIDENCE / (name + ("-manual-security" if suite == "manual-security" else "") + "-result.json")
                if not path.is_file():
                    continue
                result = json.loads(path.read_text())
                if result.get("run_id") != receipt["run_id"]:
                    continue
                records = {}
                for node in definition["nodes"]:
                    phases = result.get("nodes", {}).get(node, {})
                    records[node] = {}
                    for stage in ("setup", "call", "teardown"):
                        values = phases.get(stage, [])
                        if any(value.get("outcome") not in {"passed", "failed", "skipped"} or type(value.get("xfail")) is not bool for value in values):
                            raise RuntimeError("Unsafe identity outcome artifact")
                        records[node][stage] = [{"outcome": value["outcome"], "xfail": value["xfail"]} for value in values]
                    if suite == "manual-security" and all(values and all(value == {"outcome": "passed", "xfail": False} for value in values) for values in records[node].values()):
                        manual_passed_nodes.add(node)
                complete_nodes = set(result.get("nodes", {})) == set(definition["nodes"]) and all(
                    values and all(value == {"outcome": "passed", "xfail": False} for value in values)
                    for stages in records.values() for values in stages.values())
                public[suite][name] = {"passed": result.get("passed") is True and complete_nodes, "nodes": records}
        passed[suite] = receipt.get("passed") is True and receipt.get("candidate") == sha and bool(plan["phases"]) and set(public[suite]) == set(plan["phases"]) and all(value["passed"] for value in public[suite].values())
    pending_owners = plan["acceptance_pending_owners"]
    manual_receipt_path = EVIDENCE / "summary-manual-security.json"
    manual_receipt = json.loads(manual_receipt_path.read_text()) if manual_receipt_path.is_file() else {}
    manual_operations = [] if (manual_receipt.get("candidate") == sha and manual_receipt.get("dependency_failure_calibration_passed") is True) else ["dependency-failure-calibration"]
    complete = all(passed.values()) and not pending_owners and not manual_operations
    report = {"candidate": sha, "run_id": summary.get("run_id"), "completed": complete,
              "automated_passed": passed["automated"], "manual_security_passed": passed["manual-security"],
              "manual_pending": sorted(set(plan["manual_pending"]) - manual_passed_nodes),
              "manual_pending_operations": manual_operations,
              "acceptance_pending_owners": pending_owners, "acceptance_status": "PASSED" if complete else "PENDING", "phases": public}
    serialized = json.dumps(report, ensure_ascii=True, indent=2)
    secret_fields = {"password", "app_secret", "bootstrap_app_secret", "pg_bootstrap", "logto_db", "keycloak_db", "keycloak_admin", "STUDIO_TEST_DATABASE_URL", "STUDIO_TEST_SESSION_KEY",
                     "bootstrap_database_url", "bff_database_url", "worker_database_url", "session_encryption_key",
                     "target_bootstrap_database_url", "target_bff_database_url", "target_worker_database_url"}

    def scan(value, field=""):
        if isinstance(value, dict):
            for key, item in value.items():
                scan(item, key)
        elif isinstance(value, list):
            for item in value:
                scan(item, field)
        elif field in secret_fields and isinstance(value, str) and len(value) >= 16 and value in serialized:
            raise RuntimeError("Identity artifact secret scan failed")

    for path in [STATE / "credentials.json", STATE / "ci-business.json", STATE / "ci-runtime-roles.json", STATE / "ci-native-runtime-roles.json", *STATE.glob("fixture*.json"), *STATE.glob("idp*.json")]:
        if path.is_file():
            scan(json.loads(path.read_text()))
    private_json(EVIDENCE / "public/outcomes.json", report)
    print("Sanitized required-node outcomes prepared; private diagnostics excluded")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dependencies", "plan", "prepare-sso", "run", "cleanup", "publish"))
    parser.add_argument("--dependency", choices=DEPENDENCIES + tuple("library:" + name for name in PACKAGES))
    parser.add_argument("--candidate", help="Exact immutable candidate Git SHA required for acceptance")
    parser.add_argument("--suite", choices=("automated", "manual-security"), default="automated",
                        help="Normal verification by default; security scenarios require explicit human selection")
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "plan":
        print(json.dumps(validation_plan(args.suite), indent=2))
    elif args.command == "prepare-sso":
        prepare_sso()
        print("Actual standard SSO fixture principals and explicit member roles prepared")
    elif args.command == "dependencies":
        dependencies([args.dependency] if args.dependency else [*("library:" + package for package in PACKAGES), *DEPENDENCIES])
        print("Required dependency checks passed")
    elif args.command == "cleanup":
        cleanup()
        print("Owned identity containers stopped; volumes and private recovery state retained")
    elif args.command == "publish":
        publish()
    else:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        os.environ["AUTH32_VALIDATION_RUN_ID"] = secrets.token_hex(16)
        receipt = {"candidate": sha, "passed": False, "suite": args.suite, "run_id": os.environ["AUTH32_VALIDATION_RUN_ID"], "started_at": datetime.now(timezone.utc).isoformat()}
        private_json(EVIDENCE / "summary.json", receipt)
        private_json(EVIDENCE / ("summary-" + args.suite + ".json"), receipt)
        if sha != args.candidate or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT):
            raise RuntimeError("Required identity acceptance needs the exact clean candidate")
        plan = validation_plan(args.suite)
        if not plan["phases"]:
            raise RuntimeError("Selected identity verification suite has no required nodes")
        import manage
        manage.assert_ownership()
        business_account()
        dependencies([*("library:" + package for package in PACKAGES), *DEPENDENCIES])
        execute("native-runtime-provenance", [sys.executable, "-B", "auth-runtime/manage.py", "check"], timeout=60)
        private_json(EVIDENCE / "versions.json", {"python": platform.python_version(), "host": platform.system(), "architecture": platform.machine(),
                     "packages": {package: version(package) for package in PACKAGES},
                     "images": json.loads((STATE / "images/loaded.json").read_text())})
        if args.suite == "manual-security":
            dependency_failures()
            receipt["dependency_failure_calibration_passed"] = True
        for name, definition in plan["phases"].items():
            phase(name + ("-manual-security" if args.suite == "manual-security" else ""), definition)
        receipt.update(passed=True, completed_at=datetime.now(timezone.utc).isoformat(), phases=list(plan["phases"]))
        private_json(EVIDENCE / "summary.json", receipt)
        private_json(EVIDENCE / ("summary-" + args.suite + ".json"), receipt)
        print("Required " + args.suite + " verification passed for " + sha + "; full acceptance remains PENDING until both suites and owner signoffs are recorded")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(str(error) if isinstance(error, RuntimeError) else "Required identity validation failed; inspect private diagnostics") from None
