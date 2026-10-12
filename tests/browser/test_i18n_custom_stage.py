"""Custom checkpoint stage names remain literal in every board language."""

import json

import pytest
from playwright.sync_api import expect

from lib.checkpoint import init_project
from tests.browser.test_studio_submission import studio_browser
from tests.fixtures.studio.repository import test_repository as isolated_repository


@pytest.mark.parametrize("stage", ["constructor", "toString", "__proto__"])
def test_board_keeps_custom_checkpoint_stage_names_literal(tmp_path, stage):
    project = init_project(
        "custom-stage", title="Custom stage fixture", pipeline_type="cinematic", pipeline_dir=tmp_path
    )
    (project / f"checkpoint_{stage}.json").write_text(json.dumps({
        "version": "1.0", "project_id": "custom-stage", "pipeline_type": "cinematic",
        "stage": stage, "status": "completed", "timestamp": "2026-10-12T00:00:00Z", "artifacts": {},
    }), encoding="utf-8")

    with isolated_repository() as repo, studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        with page.expect_response(base_url + "/api/project/custom-stage/state") as loaded:
            page.goto(base_url + "/zh/p/custom-stage?static=1")
        assert loaded.value.status == 200
        assert any(item["name"] == stage for item in loaded.value.json()["stages"])
        expect(page.locator(".slate h1")).to_have_text("Custom stage fixture")
        assert page_errors == [], page_errors
        label = page.locator(".stage.undeclared .name")
        expect(label).to_have_text(stage)
        for language in ["日本語", "English"]:
            page.get_by_role("tab", name=language, exact=True).click()
            expect(label).to_have_text(stage)
