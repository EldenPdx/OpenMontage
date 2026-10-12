"""The language tabs expose and focus their active content through browser APIs."""

import pytest
from playwright.sync_api import expect

from lib.checkpoint import init_project
from tests.browser.test_studio_submission import studio_browser
from tests.fixtures.studio.repository import test_repository as isolated_repository


@pytest.mark.parametrize("path", ["/zh", "/zh/studio", "/zh/p/locale-panel"])
def test_language_tabs_label_one_focusable_panel_outside_the_header(tmp_path, path):
    init_project("locale-panel", title="Locale panel", pipeline_type="cinematic", pipeline_dir=tmp_path)
    with isolated_repository() as repo, studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
        page.goto(base_url + path)
        chinese = page.get_by_role("tab", name="中文", exact=True)
        expect(chinese).to_be_visible()
        assert chinese.get_attribute("aria-controls"), "Language tabs must identify their content panel"
        panel = page.get_by_role("tabpanel")
        expect(panel).to_have_count(1)
        expect(panel).to_have_attribute("tabindex", "0")
        expect(panel).to_have_attribute("aria-labelledby", chinese.get_attribute("id"))
        for tab in page.get_by_role("tab").all():
            expect(tab).to_have_attribute("aria-controls", panel.get_attribute("id"))
        assert panel.locator(".slate").count() == 0
        english = page.get_by_role("tab", name="English", exact=True)
        english.click()
        expect(panel).to_have_attribute("aria-labelledby", english.get_attribute("id"))
        panel.focus()
        expect(panel).to_be_focused()


def test_history_selects_the_focused_language_without_moving_focus_from_a_form(tmp_path):
    with isolated_repository() as repo, studio_browser(repo, projects_dir=tmp_path) as (page, base_url):
        page.goto(base_url + "/zh/studio")
        japanese = page.get_by_role("tab", name="日本語", exact=True)
        english = page.get_by_role("tab", name="English", exact=True)
        japanese.click()
        english.click()
        page.go_back()
        expect(japanese).to_have_attribute("aria-selected", "true")
        expect(japanese).to_be_focused()
        page.keyboard.press("ArrowRight")
        expect(page).to_have_url(base_url + "/en/studio")
        expect(english).to_be_focused()
        brief = page.locator("#brief")
        brief.fill("An unsent draft")
        brief.focus()
        page.go_back()
        expect(page).to_have_url(base_url + "/ja/studio")
        expect(brief).to_be_focused()
        expect(brief).to_have_value("An unsent draft")
