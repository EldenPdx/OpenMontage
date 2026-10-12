"""Locale behavior through real browser pages, navigation and form state."""

import json
from pathlib import Path

from playwright.sync_api import expect
import pytest

from lib.checkpoint import init_project
from tests.browser.test_studio_board_controls import pending_gate
from tests.browser.test_studio_submission import studio_browser
from tests.contracts.test_phase0_contracts import sample_artifact
from tests.fixtures.studio.repository import test_repository as isolated_repository


def test_unprefixed_library_defaults_to_chinese(tmp_path):
    with isolated_repository() as repo, studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
        response = page.goto(base_url + "/")
        assert response.status == 200
        expect(page.locator("html")).to_have_attribute("lang", "zh", timeout=1500)
        expect(page.get_by_role("tab", name="中文", exact=True)).to_have_attribute("aria-selected", "true")
        expect(page.get_by_role("heading", name="项目库", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="制作工作室", exact=True)).to_be_visible()
        expect(page.locator("#count")).to_have_text("0 个项目")
        expect(page.locator("#empty")).to_be_visible()
        expect(page.locator("#empty")).to_contain_text("制作工作室")
        expect(page.locator("#empty")).not_to_contain_text("No projects yet")


def test_locale_tab_preserves_studio_route_query_hash_and_form_draft(tmp_path):
    with isolated_repository() as repo, studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
        response = page.goto(base_url + "/studio?source=locale%20test&mode=preview#draft")
        assert response.status == 200
        tabs = page.get_by_role("tablist")
        expect(tabs).to_be_visible(timeout=1500)
        expect(tabs.get_by_role("tab")).to_have_count(3)
        expect(tabs.get_by_role("tab", name="中文", exact=True)).to_have_attribute("aria-selected", "true")
        page.locator("#brief").fill("An unsent lighthouse draft 灯塔")
        page.locator("#profile").select_option("local")
        page.locator("#duration").fill("42")
        page.locator("#aspect-ratio").select_option("9:16")
        page.locator("#budget").fill("12.34")
        page.locator("#narration").check()

        tabs.get_by_role("tab", name="English", exact=True).click()

        expect(page).to_have_url(base_url + "/en/studio?source=locale%20test&mode=preview#draft")
        expect(page.locator("html")).to_have_attribute("lang", "en")
        expect(tabs.get_by_role("tab", name="English", exact=True)).to_have_attribute("aria-selected", "true")
        expect(tabs.get_by_role("tab", name="中文", exact=True)).to_have_attribute("aria-selected", "false")
        expect(page.locator("#brief")).to_have_value("An unsent lighthouse draft 灯塔")
        expect(page.locator("#profile")).to_have_value("local")
        expect(page.locator("#duration")).to_have_value("42")
        expect(page.locator("#aspect-ratio")).to_have_value("9:16")
        expect(page.locator("#budget")).to_have_value("12.34")
        expect(page.locator("#narration")).to_be_checked()
        page.go_back()
        expect(page).to_have_url(base_url + "/zh/studio?source=locale%20test&mode=preview#draft")
        expect(page.locator("#brief")).to_have_value("An unsent lighthouse draft 灯塔")
        page.go_forward()
        expect(page).to_have_url(base_url + "/en/studio?source=locale%20test&mode=preview#draft")
        expect(page.locator("#brief")).to_have_value("An unsent lighthouse draft 灯塔")


def test_library_language_tabs_support_keyboard_focus_and_browser_history(tmp_path):
    with isolated_repository() as repo, studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
        suffix = "?static=1&source=locale%20test#library"
        page.goto(base_url + "/zh" + suffix)
        tabs = page.get_by_role("tablist", name="语言", exact=True)
        expect(tabs.get_by_role("tab")).to_have_text(["中文", "日本語", "English"])
        chinese = page.get_by_role("tab", name="中文", exact=True)
        japanese = page.get_by_role("tab", name="日本語", exact=True)
        english = page.get_by_role("tab", name="English", exact=True)
        expect(chinese).to_have_attribute("tabindex", "0")
        expect(japanese).to_have_attribute("tabindex", "-1")
        expect(english).to_have_attribute("tabindex", "-1")
        page.locator("#studio-link").focus()
        page.keyboard.press("Tab")
        expect(chinese).to_be_focused()

        page.keyboard.press("ArrowRight")
        expect(page).to_have_url(base_url + "/ja" + suffix)
        expect(japanese).to_be_focused()
        expect(japanese).to_have_attribute("aria-selected", "true")
        expect(page.get_by_role("heading", name="プロジェクト一覧", exact=True)).to_be_visible()
        page.keyboard.press("ArrowRight")
        expect(page).to_have_url(base_url + "/en" + suffix)
        expect(english).to_be_focused()
        expect(english).to_have_attribute("aria-selected", "true")
        expect(page.get_by_role("heading", name="Library", exact=True)).to_be_visible()

        page.go_back()
        expect(page).to_have_url(base_url + "/ja" + suffix)
        expect(japanese).to_have_attribute("aria-selected", "true")
        expect(page.get_by_role("heading", name="プロジェクト一覧", exact=True)).to_be_visible()
        page.go_forward()
        expect(page).to_have_url(base_url + "/en" + suffix)
        expect(english).to_have_attribute("aria-selected", "true")
        expect(page.get_by_role("heading", name="Library", exact=True)).to_be_visible()

        english.focus()
        page.keyboard.press("Home")
        expect(chinese).to_be_focused()
        expect(chinese).to_have_attribute("aria-selected", "true")
        page.keyboard.press("End")
        expect(english).to_be_focused()
        expect(english).to_have_attribute("aria-selected", "true")
        page.keyboard.press("ArrowRight")
        expect(chinese).to_be_focused()
        page.keyboard.press("ArrowLeft")
        expect(english).to_be_focused()
        page.keyboard.press("Tab")
        expect(page.locator(".theme-toggle")).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(english).to_be_focused()


def test_missing_project_board_keeps_language_tabs_and_localized_error(tmp_path):
    with isolated_repository() as repo, studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
        suffix = "/p/missing-project?static=1&source=library#review"
        response = page.goto(base_url + "/en" + suffix)
        assert response.status == 200
        expect(page.locator(".empty .big")).to_have_text("PROJECT NOT FOUND")
        expect(page.get_by_role("tablist")).to_be_visible(timeout=1500)
        page.get_by_role("tab", name="日本語", exact=True).click()
        expect(page).to_have_url(base_url + "/ja" + suffix)
        expect(page.locator("html")).to_have_attribute("lang", "ja")
        expect(page.locator(".empty .big")).to_contain_text("プロジェクト")
        expect(page.locator(".empty .big")).not_to_have_text("PROJECT NOT FOUND")
        expect(page.get_by_role("tab", name="日本語", exact=True)).to_have_attribute("aria-selected", "true")
        page.reload()
        expect(page.locator(".empty .big")).to_contain_text("プロジェクト")
        expect(page.get_by_role("tab", name="日本語", exact=True)).to_have_attribute("aria-selected", "true")


def test_studio_validation_messages_follow_locale_and_clear_when_values_are_corrected(tmp_path):
    with isolated_repository() as repo, studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
        page.goto(base_url + "/zh/studio")
        brief = page.locator("#brief")
        duration = page.locator("#duration")
        brief.fill("")
        duration.fill("0")
        page.locator("#create-task").click()
        assert brief.evaluate("field => field.validationMessage") == "请填写此项。"
        assert duration.evaluate("field => field.validationMessage") == "请输入不小于 1 的数值。"

        page.get_by_role("tab", name="日本語", exact=True).click()
        expect(brief).to_have_value("")
        expect(duration).to_have_value("0")
        assert brief.evaluate("field => field.validationMessage") == "この項目を入力してください。"
        assert duration.evaluate("field => field.validationMessage") == "1 以上の値を入力してください。"
        page.get_by_role("tab", name="English", exact=True).click()
        expect(brief).to_have_value("")
        expect(duration).to_have_value("0")
        assert brief.evaluate("field => field.validationMessage") == "Complete this field."
        assert duration.evaluate("field => field.validationMessage") == "Enter a value of at least 1."
        page.get_by_role("tab", name="中文", exact=True).click()
        assert brief.evaluate("field => field.validationMessage") == "请填写此项。"
        assert duration.evaluate("field => field.validationMessage") == "请输入不小于 1 的数值。"

        brief.fill("A valid unsent lighthouse draft")
        duration.fill("1")
        page.get_by_role("tab", name="日本語", exact=True).click()
        expect(brief).to_have_value("A valid unsent lighthouse draft")
        expect(duration).to_have_value("1")
        assert brief.evaluate("field => field.validationMessage") == ""
        assert duration.evaluate("field => field.validationMessage") == ""
        assert page.locator("#studio-form").evaluate("form => form.checkValidity()")


@pytest.mark.parametrize("locale, library_title, studio_title, tab_label, library_link_label, studio_document_title", [
    ("zh", "项目库", "制作工作室", "中文", "项目库", "Backlot — 制作工作室"),
    ("ja", "プロジェクト一覧", "制作スタジオ", "日本語", "プロジェクト一覧", "Backlot — 制作スタジオ"),
    ("en", "Library", "Production Studio", "English", "Project library", "Backlot — Production Studio"),
], ids=["zh", "ja", "en"])
def test_localized_internal_links_and_reload_keep_locale_and_project_title(
    tmp_path, locale, library_title, studio_title, tab_label, library_link_label, studio_document_title
):
    init_project("locale-board", title="Lighthouse 用户 brief", pipeline_type="cinematic", pipeline_dir=tmp_path)
    with isolated_repository() as repo, studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
        page.goto(base_url + f"/{locale}")
        expect(page.get_by_role("heading", name=library_title, exact=True)).to_be_visible()
        expect(page).to_have_title("Backlot — " + library_title)
        studio_link = page.locator("#studio-link")
        expect(studio_link).to_have_attribute("href", f"/{locale}/studio")
        studio_link.click()
        expect(page).to_have_url(base_url + f"/{locale}/studio")
        expect(page.get_by_role("heading", name=studio_title, exact=True)).to_be_visible()
        expect(page.locator("html")).to_have_attribute("lang", locale)
        page.reload()
        expect(page).to_have_title(studio_document_title)
        library_link = page.get_by_role("link", name=library_link_label, exact=True).first
        expect(library_link).to_have_attribute("href", f"/{locale}")
        library_link.click()
        expect(page).to_have_url(base_url + f"/{locale}")
        board_link = page.locator(".lib-card")
        expect(board_link).to_have_attribute("href", f"/{locale}/p/locale-board")
        board_link.click()
        expect(page).to_have_url(base_url + f"/{locale}/p/locale-board")
        expect(page.locator(".slate h1")).to_have_text("Lighthouse 用户 brief")
        expect(page.get_by_role("tab", name=tab_label, exact=True)).to_have_attribute("aria-selected", "true")
        page.reload()
        expect(page.locator("html")).to_have_attribute("lang", locale)
        expect(page.locator(".slate h1")).to_have_text("Lighthouse 用户 brief")
        library_link = page.locator("a.wordmark")
        expect(library_link).to_have_attribute("href", f"/{locale}")
        library_link.click()
        expect(page.get_by_role("heading", name=library_title, exact=True)).to_be_visible()


def test_queued_studio_task_stays_selected_on_language_change_and_history_navigation(tmp_path):
    with isolated_repository() as repo, studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
        page.goto(base_url + "/en/studio?source=task#selection")
        page.locator("#brief").fill("A queued lighthouse 用户 draft")
        with page.expect_response(lambda response: response.url == base_url + "/api/studio/tasks"
                                  and response.request.method == "POST") as created:
            page.locator("#create-task").click()
        assert created.value.status == 202
        task = created.value.json()
        current = page.locator("#current-task .studio-task")
        expect(current).to_have_attribute("data-task-id", task["task_id"])
        page.get_by_role("tab", name="日本語", exact=True).click()
        suffix = f"/studio?source=task&task={task['task_id']}#selection"
        expect(page).to_have_url(base_url + "/ja" + suffix)
        expect(current).to_have_attribute("data-task-id", task["task_id"])
        expect(current.locator(".chip")).to_have_text("待機中")
        expect(current.get_by_role("heading")).to_have_text("A queued lighthouse 用户 draft")
        expect(current.get_by_role("link")).to_have_attribute("href", f"/ja/p/{task['project_id']}")
        expect(page.locator("#task-list .studio-task")).to_have_count(0)
        page.go_back()
        expect(page).to_have_url(base_url + "/en" + suffix)
        expect(current).to_have_attribute("data-task-id", task["task_id"])
        expect(current.locator(".chip")).to_have_text("Queued")
        page.go_forward()
        expect(page).to_have_url(base_url + "/ja" + suffix)
        expect(current).to_have_attribute("data-task-id", task["task_id"])
        expect(current.locator(".chip")).to_have_text("待機中")


def test_board_language_change_preserves_gate_revision_draft_choices_and_navigation(tmp_path):
    with isolated_repository() as repo:
        task = pending_gate(repo, tmp_path, stage="script", summary="Review the user's lighthouse script")
        with studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
            suffix = f"/p/{task.project_id}?task={task.task_id}#review"
            page.goto(base_url + "/en" + suffix)
            panel = page.locator("#studio-controls")
            expect(panel).to_be_visible()
            gate_id = panel.get_attribute("data-gate-id")
            feedback = "Keep this unsent revision 用户 <b>lighthouse</b>"
            page.locator("#revision-comment").fill(feedback)
            page.get_by_role("checkbox", name="Use HyperFrames", exact=True).check()
            page.get_by_role("tab", name="日本語", exact=True).click()
            expect(page).to_have_url(base_url + "/ja" + suffix)
            expect(panel).to_have_attribute("data-gate-id", gate_id)
            expect(panel.get_by_role("heading", name="承認待ち", exact=True)).to_be_visible()
            expect(panel.get_by_role("heading", name="脚本 のレビュー", exact=True)).to_be_visible()
            expect(page.locator("#revision-comment")).to_have_value(feedback)
            expect(page.get_by_role("checkbox", name="Use HyperFrames", exact=True)).to_be_checked()
            expect(panel.get_by_role("button", name="このバージョンを承認", exact=True)).to_be_disabled()
            expect(panel.get_by_role("link", name="Studio でタスクを表示", exact=True)).to_have_attribute(
                "href", f"/ja/studio?task={task.task_id}"
            )
            page.get_by_role("tab", name="日本語", exact=True).press("ArrowRight")
            expect(page).to_have_url(base_url + "/en" + suffix)
            expect(page.get_by_role("tab", name="English", exact=True)).to_be_focused()
            expect(page.locator("#revision-comment")).to_have_value(feedback)
            expect(page.get_by_role("checkbox", name="Use HyperFrames", exact=True)).to_be_checked()
            page.go_back()
            expect(page).to_have_url(base_url + "/ja" + suffix)
            expect(page.locator("#revision-comment")).to_have_value(feedback)
            expect(page.get_by_role("checkbox", name="Use HyperFrames", exact=True)).to_be_checked()


def test_board_failed_image_fallback_and_open_script_modal_follow_locale(tmp_path):
    project = init_project("image-fallback", title="Image fallback fixture", pipeline_type="cinematic", pipeline_dir=tmp_path)
    plan = sample_artifact("scene_plan")
    plan["scenes"][0].update(description="", script_section_id="s1")
    manifest = sample_artifact("asset_manifest")
    manifest["assets"][0].update(type="image", path="assets/images/good.png")
    manifest["assets"].append({**manifest["assets"][0], "id": "asset-2", "path": "assets/images/unreadable.png"})
    sample = Path(__file__).resolve().parents[1] / "fixtures/newapi/image_sample.png"
    (project / "assets/images/good.png").write_bytes(sample.read_bytes())
    (project / "assets/images/unreadable.png").write_bytes(b"This fixture image cannot be decoded.")
    for name, artifact in [("script", sample_artifact("script")), ("scene_plan", plan), ("asset_manifest", manifest)]:
        (project / "artifacts" / f"{name}.json").write_text(json.dumps(artifact), encoding="utf-8")

    with isolated_repository() as repo, studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
        page.goto(base_url + "/en/p/image-fallback?static=1")
        fallback = page.locator(".scene-card .thumb.spec .spec-desc")
        expect(fallback).to_have_text("asset unavailable")
        expect(page.locator(".scene-card .takes .tk-label")).to_have_text("2 TAKES")
        page.locator(".script-preview").click()
        modal = page.locator("#modal")
        expect(modal).to_be_visible()
        expect(modal).to_contain_text("Hello world")
        expect(modal.locator(".modal-close")).to_have_text("ESC · CLOSE")
        page.get_by_role("tab", name="English", exact=True).press("Home")
        expect(page).to_have_url(base_url + "/zh/p/image-fallback?static=1")
        expect(fallback).to_have_text("素材不可用")
        expect(modal).to_be_visible()
        expect(modal).to_contain_text("Hello world")
        expect(modal.locator(".modal-close")).to_have_text("ESC · 关闭")
        expect(modal.locator(".sp-fade")).to_have_text("结束")
        page.keyboard.press("Escape")
        expect(modal).not_to_be_visible()
