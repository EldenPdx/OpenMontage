"""Distinct real migration, BFF and Worker principals in one freshly owned schema."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
import pytest

from production.repository import PostgresRepository


def private_role_configuration():
    name = os.environ.get("AUTH32_DATABASE_CONFIG_FILE")
    if not name:
        pytest.fail("Required role validation needs AUTH32_DATABASE_CONFIG_FILE", pytrace=False)
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            pytest.fail("Role validation configuration must be a private regular file", pytrace=False)
        value = json.load(stream)
    fields = ("bootstrap_database_url", "bff_database_url", "worker_database_url", "session_encryption_key")
    if any(not isinstance(value.get(field), str) or not value[field] for field in fields):
        pytest.fail("Required role validation configuration is incomplete", pytrace=False)
    return value


def grant_runtime_schema(configuration, schema):
    if not re.fullmatch(r"auth32_test_[a-f0-9]{32}", schema):
        pytest.fail("Runtime grants require this test's unique schema", pytrace=False)
    accounts = [conninfo_to_dict(configuration[field]) for field in ("bff_database_url", "worker_database_url")]
    with psycopg.connect(configuration["bootstrap_database_url"]) as connection:
        connection.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        for account in accounts:
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(account["user"])))
        bff_role, worker_role = sql.Identifier(accounts[0]["user"]), sql.Identifier(accounts[1]["user"])
        connection.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(schema), bff_role))
        connection.execute(sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sql.Identifier(schema), bff_role))
        for table in ("tasks", "runs", "requests", "commands", "events", "gates", "calls", "file_intents"):
            connection.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON {}.{} TO {}").format(sql.Identifier(schema), sql.Identifier(table), worker_role))
        for table in ("identity_binding", "project_owners"):
            connection.execute(sql.SQL("GRANT SELECT ON {}.{} TO {}").format(sql.Identifier(schema), sql.Identifier(table), worker_role))
        for sequence in ("commands_sequence_seq", "events_event_id_seq"):
            connection.execute(sql.SQL("GRANT USAGE,SELECT ON SEQUENCE {}.{} TO {}").format(sql.Identifier(schema), sql.Identifier(sequence), worker_role))


@contextmanager
def isolated_runtime_roles(tmp_path, *, retain_source=False):
    configuration = private_role_configuration()
    dsns = [configuration[field] for field in ("bootstrap_database_url", "bff_database_url", "worker_database_url")]
    accounts = [conninfo_to_dict(dsn) for dsn in dsns]
    if len({account["user"] for account in accounts}) != 3 or len({tuple(account.get(field) for field in ("host", "port", "dbname")) for account in accounts}) != 1:
        pytest.fail("Role validation needs three distinct principals in the same isolated database", pytrace=False)
    schema = "auth32_test_" + uuid4().hex
    projects = tmp_path / "projects"
    projects.mkdir()
    owner, bff, worker = [PostgresRepository(dsn, schema=schema, projects_dir=projects) for dsn in dsns]
    owner.migrate()
    try:
        grant_runtime_schema(configuration, schema)
        yield configuration, owner, bff, worker
    finally:
        if not retain_source:
            with psycopg.connect(dsns[0]) as connection:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@contextmanager
def runtime_role_production(tmp_path, monkeypatch, *, retain_source=False):
    from playwright.sync_api import sync_playwright
    from backlot.server import create_app
    from lib.config_model import PiPrice, PiProfile, StudioConfig
    from production.auth.config import AuthConfig
    from production.worker import Worker
    from tests.browser.test_studio_submission import serving
    from tests.fixtures.auth28 import ORIGIN, StudioBrowser
    from tests.fixtures.identity import load_fixture
    from tests.fixtures.identity.browser import logto_sign_in
    from tests.fixtures.studio.model_server import model_server
    from tests.fixtures.studio.production_flow import ProductionFlow, production_registry
    from tests.fixtures.studio.repository import require_pi

    require_pi()
    fixture = load_fixture()

    class RoleBrowser(StudioBrowser):
        def sign_in(self, alias="u1"):
            context = self.browser.new_context()
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith((ORIGIN + "/",
                "http://127.0.0.1:3010/", "http://127.0.0.1:8180/")) else route.abort())
            page = context.new_page()
            page.goto(ORIGIN + "/api/auth/login?return_to=/en")
            logto_sign_in(page, self.fixture.login_credentials(alias))
            page.wait_for_url(ORIGIN + "/en")
            assert context.request.get(ORIGIN + "/api/auth/me").status == 200
            return context

    flow = None
    with isolated_runtime_roles(tmp_path, retain_source=retain_source) as (configuration, migration, bff, worker), model_server(lambda body: flow(body)) as (url, requests):
        profile = PiProfile(provider="controlled", base_url=url, model="local-model", credential_env="AUTH28_MODEL_KEY",
                            reasoning=False, thinking_level="off", price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        studio_configuration = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
        values = fixture.private_app_configuration(ORIGIN)
        ledger = json.loads(fixture.path.read_text())
        values.update(mode="authenticated", origin=ORIGIN, http_development=True, management_endpoint="http://127.0.0.1:3010",
                      management_resource=ledger["management"]["resource"], management_app_id=ledger["management"]["app_id"],
                      management_app_secret=ledger["management"]["app_secret"], database_url=configuration["bff_database_url"], database_schema=bff.schema,
                      encryption_keys={"auth32": configuration["session_encryption_key"]}, active_key_id="auth32")
        from backlot import projects_api, state
        monkeypatch.setattr(projects_api, "PROJECTS_DIR", bff.projects_dir)
        monkeypatch.setattr(state, "PROJECTS_DIR", bff.projects_dir)
        app = create_app(studio_repository=bff, studio_config=studio_configuration,
                         studio_environment={"AUTH28_MODEL_KEY": "private-auth32-controlled-model"}, auth_config=AuthConfig.model_validate(values))
        flow = ProductionFlow(bff, bff.projects_dir)
        registry, submissions = production_registry()
        with serving(app, port=4762), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            studio = RoleBrowser(browser, fixture, bff, app)
            studio.worker = Worker(worker, studio_configuration, tmp_path / "worker-runtime", worker.projects_dir,
                                   environment={"AUTH28_MODEL_KEY": "private-auth32-controlled-model"}, registry=registry)
            studio.model_requests, studio.media_submissions, studio.flow = requests, submissions, flow
            try:
                yield studio, configuration
            finally:
                browser.close()
                registry.fixture_manager.shutdown()
