"""Lifecycle of the dedicated, disposable AUTH-24 identity environment."""

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import httpx
import psycopg
from psycopg import sql

from images import PINS, local_tag, stage_images

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "auth-runtime/.state"
PROJECT = "openmontage-auth24"


def private_write(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)
    Path(path).chmod(0o600)


def prepare():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    path = STATE / "credentials.json"
    if not path.exists():
        private_write(path, json.dumps({
            "pg_bootstrap": secrets.token_urlsafe(32), "logto_db": secrets.token_urlsafe(32),
            "keycloak_db": secrets.token_urlsafe(32), "keycloak_admin": secrets.token_urlsafe(32),
            "bootstrap_app_id": secrets.token_hex(9), "bootstrap_app_secret": secrets.token_urlsafe(32)}, indent=2))
    credentials = json.loads(path.read_text())
    for value in credentials.values():
        if not value or not all(c.isalnum() or c in "_-" for c in value):
            raise RuntimeError("Invalid private managed credential file")
    private_write(STATE / "pg-bootstrap.password", credentials["pg_bootstrap"])
    private_write(STATE / "logto.env", "\n".join((
        "DB_URL=postgresql://auth_logto:" + credentials["logto_db"] + "@identity-postgres:5432/auth_logto",
        "ENDPOINT=http://127.0.0.1:3010", "ADMIN_ENDPOINT=http://127.0.0.1:3011", "TRUST_PROXY_HEADER=false",
        "SSRF_ALLOWED_ADDRESSES=127.0.0.1")) + "\n")
    private_write(STATE / "logto-bootstrap.env", "DB_URL=postgresql://auth_bootstrap:" + credentials["pg_bootstrap"]
                  + "@identity-postgres:5432/auth_logto\n")
    private_write(STATE / "keycloak.env", "\n".join((
        "KC_DB=postgres", "KC_DB_URL=jdbc:postgresql://identity-postgres:5432/auth_keycloak",
        "KC_DB_USERNAME=auth_keycloak", "KC_DB_PASSWORD=" + credentials["keycloak_db"],
        "KC_BOOTSTRAP_ADMIN_USERNAME=auth24-admin", "KC_BOOTSTRAP_ADMIN_PASSWORD=" + credentials["keycloak_admin"])) + "\n")
    return credentials


def run(arguments, *, timeout=240):
    environment = {**os.environ, "AUTH_STATE_DIR": str(STATE)}
    result = subprocess.run(arguments, env=environment, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    descriptor = os.open(STATE / "commands.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError("Identity command failed; diagnostics are in the private state directory")
    return result.stdout


def compose(*arguments, timeout=240):
    return run(["docker", "compose", "--project-name", PROJECT, "--file", str(ROOT / "compose.auth.yaml"), *arguments], timeout=timeout)


def assert_ownership():
    result = subprocess.check_output(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT], text=True)
    if not result.strip():
        return
    containers = json.loads(subprocess.check_output(["docker", "inspect", *result.split()], text=True))
    for container in containers:
        labels = container["Config"].get("Labels", {})
        if labels.get("org.openmontage.auth-runtime") != "24-v1" or labels.get("com.docker.compose.project.working_dir") != str(ROOT):
            raise RuntimeError("Refusing to operate on an identity project owned by another checkout")


def database_url(credentials, role="auth_bootstrap", database="postgres"):
    key = {"auth_bootstrap": "pg_bootstrap", "auth_logto": "logto_db", "auth_keycloak": "keycloak_db"}[role]
    return f"postgresql://{role}:{credentials[key]}@127.0.0.1:55433/{database}"


def initialize_databases(credentials):
    with psycopg.connect(database_url(credentials), autocommit=True) as connection:
        for role, key in (("auth_logto", "logto_db"), ("auth_keycloak", "keycloak_db")):
            if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS").format(
                    sql.Identifier(role), sql.Literal(credentials[key])))
            if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (role,)).fetchone():
                owner = "auth_bootstrap" if role == "auth_logto" else role
                connection.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(role), sql.Identifier(owner)))
            connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(role)))
            connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(role), sql.Identifier(role)))
        connection.execute("REVOKE CONNECT ON DATABASE postgres FROM PUBLIC")


def bootstrap_management(credentials):
    from psycopg.types.json import Jsonb
    with psycopg.connect(database_url(credentials, database="auth_logto")) as connection:
        base_role = "logto_tenant_auth_logto"
        if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (base_role,)).fetchone():
            raise RuntimeError("Pinned Logto base database role was not created")
        connection.execute(sql.SQL("GRANT {} TO auth_logto").format(sql.Identifier(base_role)))
        # The authority owns only its tables, so RLS bootstrap reads need no superuser or BYPASSRLS.
        for (table,) in connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall():
            connection.execute(sql.SQL("ALTER TABLE public.{} OWNER TO auth_logto").format(sql.Identifier(table)))
        for (role,) in connection.execute("SELECT db_user FROM tenants").fetchall():
            if not role.startswith("logto_tenant_auth_logto"):
                raise RuntimeError("Unexpected tenant database role in the isolated Logto database")
            connection.execute(sql.SQL("GRANT CONNECT ON DATABASE auth_logto TO {}").format(sql.Identifier(role)))
        connection.execute("INSERT INTO applications (tenant_id,id,name,secret,type,oidc_client_metadata,custom_data) VALUES ('default',%s,'AUTH24 bootstrap',%s,'MachineToMachine',%s,%s) ON CONFLICT (id) DO NOTHING",
                           (credentials['bootstrap_app_id'], credentials['bootstrap_app_secret'], Jsonb({"redirectUris": [], "postLogoutRedirectUris": []}), Jsonb({"auth24": "bootstrap"})))
        connection.execute("INSERT INTO application_secrets (tenant_id,application_id,name,value) VALUES ('default',%s,'auth24-bootstrap',%s) ON CONFLICT DO NOTHING",
                           (credentials['bootstrap_app_id'], credentials['bootstrap_app_secret']))
        connection.execute("INSERT INTO applications_roles (tenant_id,id,application_id,role_id) SELECT 'default',%s,%s,id FROM roles WHERE tenant_id='default' AND name='Logto Management API access' ON CONFLICT DO NOTHING",
                           (credentials['bootstrap_app_id'], credentials['bootstrap_app_id']))


def verify_images(*, online=False):
    path = STATE / "images/loaded.json"
    if not path.is_file():
        stage_images(STATE, online=online)
    records = json.loads(path.read_text())
    for name, pin in PINS.items():
        expected = records.get(name, {})
        actual = subprocess.check_output(["docker", "image", "inspect", local_tag(name), "--format", "{{.Id}}"], text=True).strip()
        if expected.get("digest") != pin["digest"] or actual != expected.get("image_id"):
            raise RuntimeError("Staged identity image drifted; restage verified content")


def up(*, online=False):
    credentials = prepare()
    assert_ownership()
    verify_images(online=online)
    compose("up", "--detach", "--wait", "--wait-timeout", "120", "--pull", "never", "--no-build", "identity-postgres")
    initialize_databases(credentials)
    compose("up", "--detach", "--wait", "--wait-timeout", "240", "--pull", "never", "--no-build", "keycloak", "mailpit", timeout=300)
    compose("run", "--rm", "--no-deps", "--pull", "never", "logto-seed")
    bootstrap_management(credentials)
    compose("up", "--detach", "--wait", "--wait-timeout", "180", "--pull", "never", "--no-build", "logto")
    check()
    sys.path.insert(0, str(ROOT))
    from tests.fixtures.identity import seed_fixture
    fixture = seed_fixture(STATE)
    print(f"Real identity fixture initialized: {fixture}")


def check():
    assert_ownership()
    credentials = json.loads((STATE / "credentials.json").read_text())
    from importlib.metadata import version
    for package, expected in (("logto", "0.2.1"), ("cryptography", "50.0.2"), ("httpx", "0.28.1"), ("psycopg", "3.3.6")):
        if version(package) != expected:
            raise RuntimeError("Pinned identity Python dependency version drifted")
    with psycopg.connect(database_url(credentials)) as connection:
        if not connection.execute("SHOW server_version").fetchone()[0].startswith("17.11"):
            raise RuntimeError("Identity PostgreSQL version drifted")
    with httpx.Client(trust_env=False, timeout=10) as client:
        for url, issuer in (("http://127.0.0.1:3010/oidc/.well-known/openid-configuration", "http://127.0.0.1:3010/oidc"),
                            ("http://127.0.0.1:8180/realms/master/.well-known/openid-configuration", "http://127.0.0.1:8180/realms/master")):
            response = client.get(url)
            response.raise_for_status()
            metadata = response.json()
            if metadata["issuer"] != issuer:
                raise RuntimeError("Identity issuer does not match its trusted local URL")
            keys = client.get(metadata["jwks_uri"])
            keys.raise_for_status()
            if not keys.json().get("keys"):
                raise RuntimeError("Identity JWKS has no signing keys")
        client.get("http://127.0.0.1:8025/api/v1/messages").raise_for_status()
    print("Real Logto, PostgreSQL, Keycloak and Mailpit are reachable; fixtures require their separate validation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "images", "up", "check", "seed", "cleanup", "restart", "stop", "down"))
    parser.add_argument("--namespace", default="auth24")
    parser.add_argument("--app-origin", default="http://127.0.0.1:4760")
    parser.add_argument("--online", action="store_true", help="Allow uncached image downloads; default is offline without a process proxy")
    args = parser.parse_args()
    command = args.command
    if command == "prepare":
        prepare()
        print("Private identity credentials prepared")
    elif command == "images":
        prepare()
        stage_images(STATE, online=args.online)
    elif command == "up":
        up(online=args.online)
    elif command == "check":
        check()
    elif command in ("seed", "cleanup"):
        sys.path.insert(0, str(ROOT))
        from tests.fixtures.identity import seed_fixture, cleanup_fixture, load_fixture
        assert_ownership()
        if command == "seed":
            fixture = seed_fixture(STATE, namespace=args.namespace, origin=args.app_origin)
            print(f"Real identity fixture initialized: {fixture}")
        else:
            path = STATE / ("fixture.json" if args.namespace == "auth24" else f"fixture-{args.namespace}.json")
            cleanup_fixture(load_fixture(path))
            print("Only recorded fixture resources removed; services, templates and volumes retained")
    else:
        assert_ownership()
        if command == "restart":
            compose("stop", "logto")
            compose("restart", "identity-postgres", "keycloak", "mailpit")
            up(online=args.online)
        else:
            compose(command)
            print("Owned identity services stopped; private credentials and volumes retained")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, psycopg.Error, httpx.HTTPError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error) if isinstance(error, RuntimeError) else "Identity operation failed; inspect private diagnostics")
