"""Studio submission through real Pi, browser review and verified local delivery."""

import asyncio
from contextlib import contextmanager
import json
import subprocess
import threading

from playwright.sync_api import expect, sync_playwright
import pytest

import backlot.server
import backlot.state
from backlot.server import create_app
from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.worker import Worker
from tests.browser.test_studio_submission import serving
from tests.fixtures.studio.model_server import model_server
from tests.fixtures.studio.production_flow import ProductionFlow, production_registry
from tests.fixtures.studio.repository import require_pi, test_repository as isolated_repository


@contextmanager
def running_worker(worker):
    loop, stop = asyncio.new_event_loop(), asyncio.Event()
    finished = asyncio.run_coroutine_threadsafe(worker.run_forever(stop), loop)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        loop.call_soon_threadsafe(stop.set)
        try:
            finished.result(timeout=45)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            assert not thread.is_alive(), "Browser production worker did not stop"
            loop.close()


def test_browser_submits_revises_approves_plays_and_downloads_real_local_video(tmp_path, monkeypatch, request):
    require_pi()
    projects, runtime = tmp_path / "projects", tmp_path / "runtime"
    monkeypatch.setattr(backlot.server, "PROJECTS_DIR", projects)
    monkeypatch.setattr(backlot.state, "PROJECTS_DIR", projects)
    with isolated_repository() as repo:
        flow = ProductionFlow(repo, projects)
        with model_server(flow) as (url, model_requests):
            profile = PiProfile(provider="controlled", base_url=url, model="local-model", credential_env="E2E_MODEL_KEY",
                                reasoning=False, thinking_level="off", context_window=200000, max_output_tokens=8192,
                                price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
            config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
            environment = {"E2E_MODEL_KEY": "browser-e2e-private-credential"}
            registry, submissions = production_registry()
            request.addfinalizer(registry.fixture_manager.shutdown)
            worker = Worker(repo, config, runtime, projects, environment=environment, registry=registry)
            app = create_app(studio_repository=repo, studio_config=config, studio_environment=environment)
            app.state.studio_projects_dir = projects
            with serving(app) as base_url, sync_playwright() as playwright, running_worker(worker):
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.set_default_timeout(30000)
                page_errors = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base_url) else route.abort())
                try:
                    page.goto(base_url + "/studio")
                    page.locator("#brief").fill("A two second animation of a blue lighthouse")
                    page.locator("#profile").select_option("local")
                    page.locator("#duration").fill("2")
                    expect(page.locator("#narration")).not_to_be_checked()
                    with page.expect_response(lambda response: response.url == base_url + "/api/studio/tasks"
                                              and response.request.method == "POST") as created:
                        page.locator("#create-task").click()
                    assert created.value.status == 202
                    task = created.value.json()
                    expect(page.locator("#current-task")).to_contain_text(task["task_id"])
                    page.locator("#current-task a[href^='/p/']").click()

                    gates = []
                    for stage, decision in (("proposal", "approve"), ("script", "revise"),
                                            ("script", "approve"), ("scene_plan", "approve"), ("assets", "approve")):
                        review = page.locator(f'.approval-review[data-stage="{stage}"]')
                        expect(review).to_be_visible(timeout=30000)
                        expect(page.locator("#studio-controls")).to_contain_text(stage.replace("_", " ").capitalize() + " review")
                        gate_id = page.locator("#studio-controls").get_attribute("data-gate-id")
                        assert gate_id and gate_id not in gates
                        gates.append(gate_id)
                        if stage in {"proposal", "script", "scene_plan"}:
                            assert list(submissions) == []
                        if decision == "revise":
                            expect(review).to_contain_text("A lighthouse guides ships.")
                            page.locator("#revision-comment").fill("Make the lighthouse blue and the script simpler.")
                        elif stage == "script":
                            expect(review).to_contain_text("A blue lighthouse helps ships find the shore.")
                        with page.expect_response(lambda response: "/approvals/" in response.url
                                                  and response.request.method == "POST") as accepted:
                            page.locator("#revise-gate" if decision == "revise" else "#approve-gate").click()
                        assert accepted.value.status == 202, accepted.value.text()
                        expect(page.locator("#studio-controls")).not_to_have_attribute("data-gate-id", gate_id)

                    expect(page.locator("#studio-control-heading")).to_have_text("Succeeded", timeout=30000)
                    video = page.locator("#studio-final-video")
                    expect(video).to_be_visible()
                    page.wait_for_function("document.getElementById('studio-final-video')?.readyState >= 2")
                    assert video.evaluate("video => video.duration") == pytest.approx(2, abs=0.1)
                    video.evaluate("video => video.play()")
                    page.wait_for_function("document.getElementById('studio-final-video')?.currentTime > 0.1")
                    assert video.evaluate("video => video.error") is None
                    with page.expect_download() as downloaded:
                        page.locator("#download-video").click()
                    output = tmp_path / "downloaded-lighthouse.mp4"
                    downloaded.value.save_as(output)
                    assert downloaded.value.failure() is None
                    assert output.stat().st_size > 1000
                    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
                                           capture_output=True, text=True, check=True, timeout=10)
                    media = json.loads(probe.stdout)
                    stream = next(stream for stream in media["streams"] if stream["codec_type"] == "video")
                    assert (stream["width"], stream["height"]) == (1280, 720)
                    assert float(media["format"]["duration"]) == pytest.approx(2, abs=0.1)
                    assert list(submissions) == [("newapi_image", "Images2.5-Flare"), ("newapi_video", "dreamina-seedance-2-5-260628")]
                    assert len(model_requests) >= 20
                    assert flow.errors == []
                    assert page_errors == []
                    assert environment["E2E_MODEL_KEY"] not in page.content()
                    assert environment["E2E_MODEL_KEY"] not in page.evaluate("JSON.stringify({...localStorage})")
                finally:
                    browser.close()
