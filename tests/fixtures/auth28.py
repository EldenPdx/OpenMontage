"""Real browser identity and isolated PostgreSQL for Studio authorization."""

from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading

import pytest
from playwright.sync_api import sync_playwright

from backlot.server import create_app
from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.auth.config import AuthConfig
from tests.browser.test_studio_submission import serving
from tests.fixtures.identity import load_fixture
from tests.fixtures.identity.browser import logto_sign_in
from tests.fixtures.studio.repository import require_pi, test_repository as isolated_repository

ORIGIN = "http://127.0.0.1:4762"


class SlowResultTransport:
    """Hold actual HTTP result chunks long enough to revoke a live session."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def slow_send(message):
            if scope.get("path", "").endswith("/result") and message["type"] == "http.response.body" and message.get("body"):
                await asyncio.sleep(0.5)
            await send(message)
        await self.app(scope, receive, slow_send)


class BackpressuredEventTransport:
    def __init__(self, app, ready):
        self.app, self.ready = app, ready

    async def __call__(self, scope, receive, send):
        async def held_send(message):
            if scope.get("path", "").startswith("/api/studio/tasks/") and scope["path"].endswith("/events") and message["type"] == "http.response.body" and message.get("body"):
                self.ready.set()
                await asyncio.sleep(45)
            await send(message)
        await self.app(scope, receive, held_send)


class StudioBrowser:
    def __init__(self, browser, fixture, repository, app):
        self.browser, self.fixture, self.repository, self.app = browser, fixture, repository, app

    def sign_in(self, alias="u1"):
        context = self.browser.new_context()
        page = context.new_page()
        page.goto(ORIGIN + "/api/auth/login?return_to=/en")
        logto_sign_in(page, self.fixture.login_credentials(alias))
        page.wait_for_url(ORIGIN + "/en")
        assert context.request.get(ORIGIN + "/api/auth/me").status == 200
        return context

    def me(self, context):
        response = context.request.get(ORIGIN + "/api/auth/me")
        assert response.status == 200
        return response.json()

    def headers(self, context, key):
        me = self.me(context)
        return {"Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"],
                "X-Auth-Context": me["context_id"], "Idempotency-Key": key}

    def switch(self, context, organization):
        response = context.request.post(ORIGIN + "/api/auth/workspace", headers=self.headers(context, "workspace-selection"),
            data={"kind": "organization", "owner_id": self.fixture.organizations[organization]})
        assert response.status == 200
        return response.json()

    def create(self, context, *, key="studio-auth28-create", **values):
        response = context.request.post(ORIGIN + "/api/studio/tasks", headers=self.headers(context, key),
            data={"brief": "A two second lighthouse animation", "profile_id": "local", "duration_seconds": 2, **values})
        assert response.status == 202, response.text()
        return response.json()

    def run_worker(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(self.worker.run_once())).result(timeout=90)

    def committed_run_actor(self, task_id):
        import psycopg
        from psycopg import sql
        with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
            record = connection.execute(sql.SQL("SELECT actor FROM {}.runs WHERE task_id=%s").format(
                sql.Identifier(self.repository.schema)), (task_id,)).fetchone()
            return record[0]

    def accepted_production_counts(self):
        import psycopg
        from psycopg import sql
        with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as connection:
            return {table: connection.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(self.repository.schema), sql.Identifier(table))).fetchone()[0]
                for table in ("tasks", "commands", "requests", "gates", "events", "calls", "identity_audit")}

    def record_evidence(self, name, evidence):
        directory = os.environ.get("AUTH28_EVIDENCE_DIR")
        if directory:
            path = Path(directory) / ("studio28-" + name + ".json")
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
                json.dump(evidence, output, indent=2)
                output.write("\n")
            path.chmod(0o600)


@contextmanager
def studio_browser(monkeypatch, *, config=None, environment=None, slow_results=False, absolute_seconds=86400, backpressure=False):
    require_pi()
    if not os.environ.get("STUDIO_TEST_DATABASE_URL") or not os.environ.get("STUDIO_TEST_SESSION_KEY"):
        pytest.fail("Studio identity acceptance requires a real isolated PostgreSQL database and session key")
    monkeypatch.setenv("STUDIO_REQUIRE_INTEGRATION", "1")
    fixture = load_fixture()
    ledger = json.loads(fixture.path.read_text())
    with isolated_repository() as repository:
        values = fixture.private_app_configuration(ORIGIN)
        values.update(mode="authenticated", origin=ORIGIN, http_development=True, absolute_seconds=absolute_seconds,
            management_endpoint=values["endpoint"], management_resource=ledger["management"]["resource"],
            management_app_id=ledger["management"]["app_id"], management_app_secret=ledger["management"]["app_secret"],
            database_url=os.environ["STUDIO_TEST_DATABASE_URL"], database_schema=repository.schema,
            encryption_keys={"test": os.environ["STUDIO_TEST_SESSION_KEY"]}, active_key_id="test")
        from backlot import projects_api, state
        monkeypatch.setattr(projects_api, "PROJECTS_DIR", repository.projects_dir)
        monkeypatch.setattr(state, "PROJECTS_DIR", repository.projects_dir)
        profile = PiProfile(provider="controlled", base_url="http://127.0.0.1:9999/v1", model="local-model",
            credential_env="AUTH28_MODEL_KEY", reasoning=False, thinking_level="off",
            price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        app = create_app(studio_repository=repository,
            studio_config=config or StudioConfig(enabled=True, default_profile="local", profiles={"local": profile}),
            studio_environment=environment or {"AUTH28_MODEL_KEY": "private-auth28-model-credential"},
            auth_config=AuthConfig.model_validate(values))
        if slow_results:
            app.add_middleware(SlowResultTransport)
        if backpressure:
            app.state.transport_ready = threading.Event()
            app.add_middleware(BackpressuredEventTransport, ready=app.state.transport_ready)
        with serving(app, port=4762), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                yield StudioBrowser(browser, fixture, repository, app)
            finally:
                browser.close()


@pytest.fixture
def studio_identity(monkeypatch):
    with studio_browser(monkeypatch) as studio:
        yield studio


@contextmanager
def production_studio(monkeypatch, *, media_http=False, slow_results=False, unquoted_model=False, media_quote=False):
    from production.worker import Worker
    from tests.fixtures.studio.model_server import model_server
    from tests.fixtures.studio.production_flow import ProductionFlow, production_registry
    flow = None
    with model_server(lambda body: flow(body)) as (url, requests):
        profile = PiProfile(provider="controlled", base_url=url, model="local-model", credential_env="AUTH28_MODEL_KEY",
            reasoning=False, thinking_level="off", context_window=200000, max_output_tokens=8192,
            price=None if unquoted_model else PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
        from tests.fixtures.auth28_media import LoopbackImage, LoopbackVideo, loopback_media
        with studio_browser(monkeypatch, config=config, slow_results=slow_results) as studio, TemporaryDirectory(prefix="r-") as runtime, \
                loopback_media() if media_http else nullcontext(None) as media:
            flow = ProductionFlow(studio.repository, studio.repository.projects_dir)
            registry, submissions = production_registry()
            if media is not None:
                address, studio.media_requests = media
                registry.register(LoopbackImage(address, submissions))
                registry.register(LoopbackVideo(address, submissions))
            quotes = {("newapi_image", "Images2.5-Flare"): 1_000_000} if media_quote else None
            studio.worker = Worker(studio.repository, config, runtime, studio.repository.projects_dir,
                environment={"AUTH28_MODEL_KEY": "private-auth28-model-credential"}, registry=registry, tool_quotes=quotes)
            studio.model_requests, studio.media_submissions, studio.flow = requests, submissions, flow
            try:
                yield studio
            finally:
                registry.fixture_manager.shutdown()


def complete_production(studio, context):
    task = studio.create(context)
    for turn in range(8):
        studio.run_worker()
        task = context.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).json()
        if task["state"] == "succeeded":
            return task
        assert task["state"] == "awaiting_approval", task.get("error")
        binding = task["approval"]["binding"]
        accepted = context.request.post(f"{ORIGIN}/api/studio/tasks/{task['task_id']}/approvals/{binding['gate_id']}/decision",
            headers=studio.headers(context, f"complete-production-{turn:04d}"),
            data={"binding": binding, "expected_version": task["version"], "decision": "approve"})
        assert accepted.status == 202, accepted.text()
    pytest.fail("Real owned production failed to deliver a verified video")


@contextmanager
def authority_outage(studio):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    fault = FastAPI()

    @fault.get("/{path:path}")
    async def unavailable(path: str):
        return JSONResponse({"error": "controlled authority outage"}, status_code=503)

    with serving(fault) as address:
        authority = studio.app.state.auth_runtime.authority
        original = authority.config
        authority.config = original.model_copy(update={"management_endpoint": address})
        try:
            yield
        finally:
            authority.config = original
