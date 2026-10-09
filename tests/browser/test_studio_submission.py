"""Browser-facing Studio behavior backed by real PostgreSQL task APIs."""

from contextlib import contextmanager
import socket
import threading
import time

import pytest
from playwright.sync_api import sync_playwright, expect
import uvicorn

from backlot.server import create_app
from lib.config_model import PiProfile, StudioConfig
from tests.fixtures.studio.repository import test_repository as isolated_repository


@contextmanager
def serving(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "Browser fixture API did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "Browser fixture API did not stop"


@contextmanager
def studio_browser(repo, *, viewport=None, projects_dir=None):
    profiles = {name: PiProfile(provider=name, base_url="http://127.0.0.1:9999/v1", model=name + "-model",
                               credential_env="BROWSER_MODEL_KEY", reasoning=False, thinking_level="off")
                for name in ("xvan", "local")}
    config = StudioConfig(enabled=True, profiles=profiles)
    patch = pytest.MonkeyPatch()
    if projects_dir is not None:
        import backlot.server
        import backlot.state
        patch.setattr(backlot.server, "PROJECTS_DIR", projects_dir)
        patch.setattr(backlot.state, "PROJECTS_DIR", projects_dir)
    app = create_app(studio_repository=repo, studio_config=config,
                     studio_environment={"BROWSER_MODEL_KEY": "private-browser-credential"})
    if projects_dir is not None:
        app.state.studio_projects_dir = projects_dir
    with serving(app) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport=viewport or {"width": 1280, "height": 900})
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base_url) else route.abort())
        try:
            yield page, base_url
        finally:
            browser.close()
            patch.undo()


def test_browser_creates_a_durable_task_once_and_refresh_does_not_submit_again():
    with isolated_repository() as repo, studio_browser(repo) as (page, base_url):
        page.goto(base_url + "/studio")
        expect(page.locator("#studio-form")).to_be_visible(timeout=1500)
        page.locator("#brief").fill("A short animation about a lighthouse")
        page.locator("#profile").select_option("local")
        assert not page.locator("#narration").is_checked()
        page.locator("#create-task").dblclick()
        expect(page.locator("#current-task")).to_contain_text("Queued")
        tasks = repo.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].request.profile_id == "local"
        assert tasks[0].request.narration is False
        page.reload()
        expect(page.locator("#current-task")).to_contain_text(tasks[0].task_id)
        assert len(repo.list_tasks()) == 1
        assert "private-browser-credential" not in page.content()
        storage = page.evaluate("JSON.stringify({...localStorage})")
        assert "private-browser-credential" not in storage
        expect(page.locator("#current-task a[href^='/p/']")).to_be_visible()


def test_lost_create_response_reuses_the_saved_key_after_reload_and_mobile_layout_fits():
    with isolated_repository() as repo, studio_browser(repo, viewport={"width": 390, "height": 844}) as (page, base_url):
        keys = []
        def lose_first_response(route):
            if route.request.method != "POST":
                route.continue_()
                return
            keys.append(route.request.headers["idempotency-key"])
            if len(keys) == 1:
                response = route.fetch()
                assert response.status == 202
                route.abort()
            else:
                route.continue_()
        page.route("**/api/studio/tasks", lose_first_response)
        page.goto(base_url + "/studio")
        page.locator("#brief").fill("A lighthouse with a blue light")
        page.locator("#create-task").click()
        expect(page.locator("#studio-status")).to_contain_text("request key is saved")
        assert len(repo.list_tasks()) == 1
        page.reload()
        expect(page.locator("#brief")).to_have_value("A lighthouse with a blue light")
        page.locator("#create-task").click()
        expect(page.locator("#current-task")).to_contain_text("Queued")
        assert keys[0] == keys[1]
        assert len(repo.list_tasks()) == 1
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_task_state_stream_updates_and_cancel_remains_a_request():
    from production.contracts import TaskState

    with isolated_repository() as repo, studio_browser(repo) as (page, base_url):
        page.goto(base_url + "/studio")
        page.locator("#brief").fill("A short local lighthouse video")
        page.locator("#create-task").click()
        expect(page.locator("#current-task")).to_contain_text("Queued")
        task = repo.list_tasks()[0]
        claim = repo.claim_command("browser-worker")
        repo.transition(task.task_id, TaskState.RUNNING, expected_version=task.version, fence=claim.context.fence)
        expect(page.locator("#current-task")).to_contain_text("Running")
        page.get_by_role("button", name="Cancel task", exact=True).click()
        expect(page.locator("#current-task")).to_contain_text("Cancel requested")
        expect(page.locator("#studio-status")).to_contain_text("Remote work may still finish and charge")
        assert repo.get_task(task.task_id).state == TaskState.CANCEL_REQUESTED
        page.reload()
        expect(page.locator("#current-task")).to_contain_text("Cancel requested")
        assert len(repo.list_tasks()) == 1


def test_expired_csrf_session_is_refreshed_without_changing_submission_key():
    with isolated_repository() as repo, studio_browser(repo) as (page,base_url):
        keys=[]
        def expire_once(route):
            if route.request.method!="POST":
                route.continue_();return
            keys.append(route.request.headers["idempotency-key"])
            if len(keys)==1:
                route.fulfill(status=403,json={"error":{"code":"forbidden","message":"Expired session"}})
            else:
                route.continue_()
        page.route("**/api/studio/tasks",expire_once)
        page.goto(base_url+"/studio")
        page.locator("#brief").fill("A lighthouse after a service restart")
        page.locator("#create-task").click()
        expect(page.locator("#current-task")).to_contain_text("Queued")
        assert keys[0]==keys[1]
        assert len(repo.list_tasks())==1
