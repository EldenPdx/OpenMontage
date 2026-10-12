"""Real native UI, Logto, PostgreSQL, official Pi and FFmpeg delivery."""

import hashlib
import json
import subprocess

import pytest
from playwright.sync_api import expect

from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.worker import Worker
from tests.browser.test_studio_end_to_end import running_worker
from tests.fixtures.auth31 import ORIGIN, ui_environment, ui_browser, ui_context, sign_in
from tests.fixtures.studio.model_server import model_server
from tests.fixtures.studio.production_flow import ProductionFlow, production_registry
from tests.fixtures.studio.repository import require_pi


@pytest.mark.parametrize("workspace", ["personal", "organization"])
def test_ui_submits_revises_approves_and_plays_real_owned_production(ui_environment, tmp_path, workspace):
    fixture, repository = ui_environment
    require_pi()
    flow = ProductionFlow(repository, repository.projects_dir)
    with model_server(flow) as (address, requests):
        profile = PiProfile(provider="controlled", base_url=address, model="local-model", credential_env="AUTH31_MODEL_KEY",
            reasoning=False, thinking_level="off", context_window=200000, max_output_tokens=8192,
            price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
        environment = {"AUTH31_MODEL_KEY": "private-auth31-controlled-model"}
        registry, submissions = production_registry()
        try:
            worker = Worker(repository, config, tmp_path / "runtime", repository.projects_dir,
                            environment=environment, registry=registry)
            with ui_browser(repository, studio_config=config, studio_environment=environment) as (browser, _), running_worker(worker):
                context = ui_context(browser)
                page = sign_in(context, fixture, return_to="/en/studio")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                if workspace == "organization":
                    page.get_by_label("Workspace", exact=True).select_option(fixture.organizations["A"])
                    expect(page.get_by_label("Workspace", exact=True)).to_have_value(fixture.organizations["A"])
                me = context.request.get(ORIGIN + "/api/auth/me").json()
                page.locator("#brief").fill("A two second blue lighthouse in the " + workspace + " workspace")
                page.locator("#duration").fill("2")
                with page.expect_response(lambda response: response.url == ORIGIN + "/api/studio/tasks"
                                          and response.request.method == "POST") as created:
                    page.locator("#create-task").click()
                assert created.value.status == 202
                task = created.value.json()
                assert task["ownership"]["workspace"] == {"kind": me["workspace"]["kind"], "owner_id": me["workspace"]["owner_id"]}
                expect(page.locator("#current-task")).to_contain_text(task["task_id"])
                page.locator("#current-task a[href^='/en/p/']").click()
                gates = []
                for stage, decision in [("proposal", "approve"), ("script", "revise"), ("script", "approve"),
                                        ("scene_plan", "approve"), ("assets", "approve")]:
                    expect(page.locator(f'.approval-review[data-stage="{stage}"]')).to_be_visible(timeout=30000)
                    expect(page.locator("#studio-controls")).to_contain_text(stage.replace("_", " ").capitalize() + " review")
                    gate = page.locator("#studio-controls").get_attribute("data-gate-id")
                    assert gate and gate not in gates
                    gates.append(gate)
                    if decision == "revise":
                        page.locator("#revision-comment").fill("Make the lighthouse blue and simplify the script.")
                    with page.expect_response(lambda response: "/approvals/" in response.url and response.request.method == "POST") as approved:
                        page.locator("#revise-gate" if decision == "revise" else "#approve-gate").click()
                    assert approved.value.status == 202
                    expect(page.locator("#studio-controls")).not_to_have_attribute("data-gate-id", gate)
                expect(page.locator("#studio-control-heading")).to_have_text("Succeeded", timeout=30000)
                if workspace == "organization":
                    viewer = ui_context(browser)
                    read_only = sign_in(viewer, fixture, "viewer")
                    read_only.get_by_label("Workspace", exact=True).select_option(fixture.organizations["A"])
                    expect(read_only.get_by_label("Workspace", exact=True)).to_have_value(fixture.organizations["A"])
                    read_only.goto(ORIGIN + "/en/p/" + task["project_id"])
                    expect(read_only.locator("#account-control")).to_contain_text("viewer")
                    page = read_only
                video = page.locator("#studio-final-video")
                expect(video).to_be_visible()
                page.wait_for_function("document.getElementById('studio-final-video')?.readyState >= 2")
                video.evaluate("video => video.play()")
                page.wait_for_function("document.getElementById('studio-final-video')?.currentTime > 0.1")
                assert video.evaluate("video => video.error") is None
                with page.expect_download() as saved:
                    page.locator("#download-video").click()
                output = tmp_path / (workspace + ".mp4")
                saved.value.save_as(output)
                final = context.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).json()
                assert hashlib.sha256(output.read_bytes()).hexdigest() == final["result"]["video"]["sha256"]
                probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
                    capture_output=True, text=True, check=True, timeout=10)
                media = json.loads(probe.stdout)
                stream = next(item for item in media["streams"] if item["codec_type"] == "video")
                assert (stream["width"], stream["height"]) == (1280, 720)
                assert float(media["format"]["duration"]) == pytest.approx(2, abs=0.1)
                assert len(requests) >= 20 and len(submissions) == 2
                assert flow.errors == [] and errors == []
                state = page.content() + page.url + page.evaluate("JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")
                assert environment["AUTH31_MODEL_KEY"] not in state
                context.close()
        finally:
            registry.fixture_manager.shutdown()
