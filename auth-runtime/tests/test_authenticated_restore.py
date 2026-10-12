"""Whole-schema operator restore retains real login, media, keys and source data."""

import asyncio
import base64
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from uuid import uuid4

from playwright.sync_api import sync_playwright
import psycopg

from backlot.server import create_app
from production.auth.config import AuthConfig
from production.auth.routes import SESSION_COOKIE
from production.identity_store import PostgresIdentityStore
from production.repository import PostgresRepository
from production.worker import Worker
from tests.browser.test_studio_submission import serving
from tests.fixtures.auth28 import ORIGIN, complete_production
from tests.fixtures.auth32_roles import grant_runtime_schema, runtime_role_production
from tests.fixtures.identity import load_fixture

ROOT = Path(__file__).resolve().parents[2]


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)


@contextmanager
def restored_browser(configuration, repository, studio_configuration, monkeypatch, keys, active):
    fixture = load_fixture()
    values = fixture.private_app_configuration(ORIGIN)
    ledger = json.loads(fixture.path.read_text())
    values.update(mode="authenticated", origin=ORIGIN, http_development=True, management_endpoint="http://127.0.0.1:3010",
                  management_resource=ledger["management"]["resource"], management_app_id=ledger["management"]["app_id"],
                  management_app_secret=ledger["management"]["app_secret"], database_url=configuration["bff_database_url"], database_schema=repository.schema,
                  encryption_keys=keys, active_key_id=active)
    from backlot import projects_api, state
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", repository.projects_dir)
    monkeypatch.setattr(state, "PROJECTS_DIR", repository.projects_dir)
    app = create_app(studio_repository=repository, studio_config=studio_configuration,
                     studio_environment={"AUTH28_MODEL_KEY": "private-auth32-controlled-model"}, auth_config=AuthConfig.model_validate(values))
    with serving(app, port=4762), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


def test_operator_restore_keeps_original_cookie_key_media_and_runtime_roles_without_changing_source(monkeypatch):
    run = ROOT / "auth-runtime/.state/validation" / ("recovery-" + uuid4().hex)
    run.mkdir(parents=True, mode=0o700)
    source, target = run / "source", run / "target"
    source.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    with runtime_role_production(source, monkeypatch, retain_source=True) as (studio, configuration):
        context = studio.sign_in()
        studio.switch(context, "A")
        result = complete_production(studio, context)
        cookies = [cookie for cookie in context.cookies() if cookie["name"] == SESSION_COOKIE]
        assert len(cookies) == 1
        expected = context.request.get(ORIGIN + result["result"]["download_url"]).body()
        schema, projects = studio.repository.schema, studio.repository.projects_dir
        studio_configuration = studio.app.state.studio_config
        binding = studio.repository.database_binding()
    # The actual API, browser, model and Worker are stopped before the exported snapshot.
    target_configuration = {**configuration, **{name + "_database_url": configuration["target_" + name + "_database_url"]
                                               for name in ("bootstrap", "bff", "worker")}}
    source_config, target_config, mapping = run / "source-operator.json", run / "target-operator.json", run / "mapping.json"
    private_json(source_config, {"database_url": configuration["bootstrap_database_url"], "postgres_container": configuration["postgres_container"]})
    private_json(target_config, {"database_url": target_configuration["bootstrap_database_url"], "postgres_container": configuration["postgres_container"]})
    private_json(mapping, [{"project_id": result["project_id"], "task_id": result["task_id"], "workspace": result["ownership"]["workspace"],
                            "creator_user_id": result["ownership"]["creator"]["user_id"]}])
    backup = run / "backup"
    source_hashes = {str(path.relative_to(source)): sha256(path.read_bytes()).hexdigest() for path in source.rglob("*") if path.is_file()}
    for command, private_config, roots in (("backup", source_config, source), ("restore", target_config, target)):
        arguments = [sys.executable, "-B", "-m", "production.identity_migrate", command, "--private-config", str(private_config),
                     "--schema", schema, "--projects-root", str(roots / "projects"), "--runtime-root", str(roots / "worker-runtime"), "--backup", str(backup)]
        if command == "backup":
            arguments += ["--mapping", str(mapping)]
        with (run / (command + ".log")).open("w") as output:
            completed = subprocess.run(arguments, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, timeout=90)
        assert completed.returncode == 0, "Private operator " + command + " failed"
    grant_runtime_schema(target_configuration, schema)
    bff = PostgresRepository(target_configuration["bff_database_url"], schema=schema, projects_dir=target / "projects")
    worker = PostgresRepository(target_configuration["worker_database_url"], schema=schema, projects_dir=target / "projects")
    assert bff.database_binding() == binding
    assert bff.get_task(result["task_id"]).public_dict()["result"]["video"]["sha256"] == result["result"]["video"]["sha256"]
    original_key = configuration["session_encryption_key"]
    with restored_browser(target_configuration, bff, studio_configuration, monkeypatch, {"auth32": original_key}, "auth32") as browser:
        restored = browser.new_context()
        restored.add_cookies(cookies)
        assert restored.request.get(ORIGIN + "/api/auth/me").status == 200
        downloaded = restored.request.get(ORIGIN + result["result"]["download_url"])
        assert downloaded.status == 200 and downloaded.body() == expected
    resumed = Worker(worker, studio_configuration, target / "worker-runtime", worker.projects_dir,
                     environment={"AUTH28_MODEL_KEY": "private-auth32-controlled-model"})
    before_worker = bff.get_task(result["task_id"]).public_dict()
    asyncio.run(resumed.run_once())
    assert bff.get_task(result["task_id"]).public_dict() == before_worker
    replacement = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    with restored_browser(target_configuration, bff, studio_configuration, monkeypatch, {"bad": replacement}, "bad") as browser:
        rejected = browser.new_context()
        rejected.add_cookies(cookies)
        assert rejected.request.get(ORIGIN + "/api/auth/me").status == 503
        assert rejected.request.get(ORIGIN + result["result"]["download_url"]).status == 503
    store = PostgresIdentityStore(target_configuration["bff_database_url"], schema=schema,
        encryption_keys={"auth32": base64.urlsafe_b64decode(original_key), "rotated": base64.urlsafe_b64decode(replacement)}, active_key_id="rotated")
    session = store.get_session(cookies[0]["value"])
    store.update_session(cookies[0]["value"], expected_revision=session.metadata.revision, sdk_storage=session.sdk_storage)
    with restored_browser(target_configuration, bff, studio_configuration, monkeypatch, {"auth32": original_key}, "auth32") as browser:
        rejected = browser.new_context()
        rejected.add_cookies(cookies)
        assert rejected.request.get(ORIGIN + "/api/auth/me").status == 503
    with restored_browser(target_configuration, bff, studio_configuration, monkeypatch, {"auth32": original_key, "rotated": replacement}, "rotated") as browser:
        restored = browser.new_context()
        restored.add_cookies(cookies)
        assert restored.request.get(ORIGIN + "/api/auth/me").status == 200
        assert restored.request.get(ORIGIN + result["result"]["download_url"]).body() == expected
    assert {str(path.relative_to(source)): sha256(path.read_bytes()).hexdigest() for path in source.rglob("*") if path.is_file()} == source_hashes
    with psycopg.connect(configuration["bootstrap_database_url"]) as connection:
        assert connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone() == (1,)
    private_json(run / "receipt.json", {"schema": schema, "source_preserved": True, "original_cookie_restored": True,
                                       "rotated_key_recovery": True, "video_sha256": sha256(expected).hexdigest()})
