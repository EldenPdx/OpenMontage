"""Real public account controls; no fabricated identity or authorization responses."""

import threading
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from playwright.sync_api import expect

from tests.fixtures.auth31 import ORIGIN, ui_environment, ui_browser, ui_context, sign_in, write_headers
from tests.fixtures.auth31 import invitation_ticket, mailbox_user, identity_outage
from tests.fixtures.identity.browser import logto_sign_in


def test_signed_in_account_identifies_the_current_user_and_personal_space(ui_environment):
    fixture, repository = ui_environment
    with ui_browser(repository) as (browser, _):
        context = ui_context(browser)
        page = sign_in(context, fixture)
        expect(page.locator("#account-control")).to_contain_text(fixture.users["u1"].email, timeout=3000)
        expect(page.get_by_label("Workspace", exact=True)).to_have_value("personal")
        expect(page.get_by_role("button", name="Sign out", exact=True)).to_be_visible()
        context.close()


def test_keyboard_signout_revokes_the_account_and_shows_safe_login_links(ui_environment):
    fixture, repository = ui_environment
    with ui_browser(repository) as (browser, _):
        context = ui_context(browser)
        page = sign_in(context, fixture)
        page.get_by_role("button", name="Sign out", exact=True).focus()
        with page.expect_response(ORIGIN + "/api/auth/logout", timeout=2500) as logout:
            page.keyboard.press("Enter")
        assert logout.value.status == 200
        expect(page.get_by_role("link", name="Sign in", exact=True)).to_be_visible()
        assert context.request.get(ORIGIN + "/api/auth/me").status == 401
        link = page.get_by_role("link", name="Create account", exact=True)
        assert "screen_hint=sign_up" in link.get_attribute("href")
        assert "return_to=%2Fen" in link.get_attribute("href")
        context.close()


def test_lost_personal_task_response_keeps_the_same_context_and_key_after_reload(ui_environment):
    fixture, repository = ui_environment
    with ui_browser(repository) as (browser, _):
        context = ui_context(browser, viewport={"width": 390, "height": 844})
        page = sign_in(context, fixture, return_to="/en/studio")
        me = context.request.get(ORIGIN + "/api/auth/me").json()
        requests = []
        def lose_response(route):
            if route.request.method != "POST":
                route.continue_()
                return
            requests.append(route.request.headers)
            response = route.fetch()
            if len(requests) == 1:
                assert response.status == 202
                route.abort()
            else:
                route.fulfill(response=response)
        page.route("**/api/studio/tasks", lose_response)
        page.get_by_label("Video brief", exact=True).fill("Personal lighthouse production")
        page.get_by_role("button", name="Create video task", exact=True).click()
        expect(page.locator("#studio-status")).to_contain_text("request key is saved")
        page.reload()
        expect(page.get_by_label("Video brief", exact=True)).to_have_value("Personal lighthouse production")
        page.get_by_role("button", name="Create video task", exact=True).click()
        expect(page.locator("#current-task")).to_contain_text("Personal lighthouse production")
        assert len(requests) == 2
        assert requests[0]["idempotency-key"] == requests[1]["idempotency-key"]
        assert requests[0].get("x-auth-context") == requests[1].get("x-auth-context") == me["context_id"]
        tasks = context.request.get(ORIGIN + "/api/studio/tasks").json()
        assert len(tasks) == 1
        assert tasks[0]["ownership"]["workspace"] == {"kind": "personal", "owner_id": me["user"]["user_id"]}
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        context.close()


def test_real_permission_403_never_retries_the_write_and_updates_viewer_controls(ui_environment):
    fixture, repository = ui_environment
    try:
        with ui_browser(repository) as (browser, _):
            context = ui_context(browser)
            page = sign_in(context, fixture, "cross")
            me = context.request.get(ORIGIN + "/api/auth/me").json()
            selected = context.request.post(ORIGIN + "/api/auth/workspace", headers=write_headers(me),
                data={"kind": "organization", "owner_id": fixture.organizations["A"]})
            assert selected.status == 200
            page.goto(ORIGIN + "/en/studio")
            page.get_by_label("Video brief", exact=True).fill("A denied organization production")
            writes = []
            page.on("request", lambda request: writes.append(request) if request.method == "POST"
                    and request.url == ORIGIN + "/api/studio/tasks" else None)
            fixture.set_roles("cross", "A", ["viewer"])
            with page.expect_response(lambda response: response.url == ORIGIN + "/api/studio/tasks"
                                      and response.request.method == "POST") as rejected:
                page.get_by_role("button", name="Create video task", exact=True).click()
            assert rejected.value.status == 403 and rejected.value.json()["error"]["code"] == "forbidden"
            expect(page.locator("#studio-status")).to_contain_text("You do not have permission")
            expect(page.locator("#create-task")).to_be_disabled()
            assert len(writes) == 1
            assert context.request.get(ORIGIN + "/api/studio/tasks").json() == []
            context.close()
    finally:
        fixture.set_roles("cross", "A", ["editor"])


def test_real_csrf_rejection_refreshes_only_the_same_identity_space_and_key(ui_environment):
    fixture, repository = ui_environment
    with ui_browser(repository) as (browser, _):
        context = ui_context(browser)
        page = sign_in(context, fixture, return_to="/en/studio")
        keys, statuses = [], []
        def corrupt_first_csrf(route):
            if route.request.method != "POST":
                route.continue_()
                return
            keys.append(route.request.headers["idempotency-key"])
            headers = route.request.headers
            if len(keys) == 1:
                headers["x-csrf-token"] = "deliberately-expired-csrf"
            response = route.fetch(headers=headers)
            statuses.append(response.status)
            route.fulfill(response=response)
        page.route("**/api/studio/tasks", corrupt_first_csrf)
        page.get_by_label("Video brief", exact=True).fill("A real CSRF recovery production")
        page.locator("#create-task").click()
        expect(page.locator("#current-task")).to_contain_text("A real CSRF recovery production")
        assert statuses == [403, 202]
        assert len(keys) == 2 and keys[0] == keys[1]
        assert len(context.request.get(ORIGIN + "/api/studio/tasks").json()) == 1
        context.close()


def test_a_lost_task_event_connection_reconnects_with_the_verified_current_space(ui_environment):
    fixture, repository = ui_environment
    with ui_browser(repository) as (browser, _):
        context = ui_context(browser)
        page = sign_in(context, fixture, return_to="/en/studio")
        attempts = []
        def drop_first_stream(route):
            attempts.append(route.request.url)
            if len(attempts) == 1:
                route.abort()
            else:
                route.continue_()
        page.route("**/api/studio/tasks/*/events?**", drop_first_stream)
        page.locator("#brief").fill("A production with a recovering event stream")
        with page.expect_response(lambda response: response.url == ORIGIN + "/api/studio/tasks"
                                  and response.request.method == "POST") as created:
            page.locator("#create-task").click()
        task = created.value.json()
        expect(page.locator("#current-task")).to_contain_text("Queued")
        page.wait_for_timeout(1500)
        me = context.request.get(ORIGIN + "/api/auth/me").json()
        cancelled = context.request.post(ORIGIN + f"/api/studio/tasks/{task['task_id']}/cancel",
            headers={**write_headers(me), "Idempotency-Key": "auth31-stream-cancel"},
            data={"expected_version": task["version"]})
        assert cancelled.status == 202
        expect(page.locator("#current-task")).to_contain_text("Cancel requested", timeout=5000)
        assert len(attempts) >= 2
        context.close()


def test_a_slow_old_workspace_read_cannot_restore_private_tasks_or_drafts(ui_environment):
    fixture, repository = ui_environment
    delay = {"armed": False, "entered": threading.Event(), "release": threading.Event()}
    with ui_browser(repository, delayed_read=delay) as (browser, _):
        context = ui_context(browser)
        page = sign_in(context, fixture)
        old = context.request.get(ORIGIN + "/api/auth/me").json()
        created = context.request.post(ORIGIN + "/api/studio/tasks", headers={**write_headers(old),
            "Idempotency-Key": "auth31-slow-read-old-task"}, data={"brief": "A private old personal task", "profile_id": "local"})
        assert created.status == 202
        delay["armed"] = True
        page.goto(ORIGIN + "/en/studio", wait_until="domcontentloaded")
        page.locator("#brief").fill("An old personal draft")
        assert delay["entered"].wait(timeout=5)
        selected = context.request.post(ORIGIN + "/api/auth/workspace", headers=write_headers(old),
            data={"kind": "organization", "owner_id": fixture.organizations["A"]})
        assert selected.status == 200
        second = context.new_page()
        second.goto(ORIGIN + "/en")
        page.bring_to_front()
        expect(page.locator("#brief")).to_have_value("", timeout=20000)
        delay["release"].set()
        expect(page.get_by_label("Workspace", exact=True)).to_have_value(fixture.organizations["A"])
        expect(page.locator("#brief")).to_be_enabled()
        expect(page.locator("#create-task")).to_be_enabled()
        expect(page.locator("#task-list")).not_to_contain_text("A private old personal task")
        assert context.request.get(ORIGIN + "/api/studio/tasks").json() == []
        context.close()


@pytest.mark.parametrize("locale,workspace,manage,send,invalid", [
    ("zh", "工作空间", "管理企业", "发送邀请", "请输入有效的邮箱地址。"),
    ("ja", "ワークスペース", "組織を管理", "招待を送信", "有効なメールアドレスを入力してください。"),
    ("en", "Workspace", "Manage organization", "Send invitation", "Enter a valid email address."),
])
def test_account_controls_have_keyboard_labels_localized_errors_and_mobile_layout(ui_environment, locale, workspace, manage, send, invalid):
    fixture, repository = ui_environment
    with ui_browser(repository) as (browser, _):
        context = ui_context(browser, viewport={"width": 390, "height": 844})
        page = sign_in(context, fixture, "u2", return_to=f"/{locale}")
        selector = page.get_by_label(workspace, exact=True)
        expect(selector.locator("option[value='" + fixture.organizations["B"] + "']")).to_have_count(1)
        selector.select_option(fixture.organizations["B"])
        expect(page.get_by_label(workspace, exact=True)).to_have_value(fixture.organizations["B"])
        summary = page.get_by_text(manage, exact=True)
        summary.focus(); page.keyboard.press("Enter")
        page.locator("#invitation-email").fill("invalid-email")
        page.get_by_role("button", name=send, exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text(invalid)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        context.close()


def test_real_invitation_is_memory_only_and_login_accept_role_change_remove_use_the_ui(ui_environment):
    fixture, repository = ui_environment
    invitation_id = None
    organization = fixture.organizations["B"]
    try:
        with mailbox_user(fixture) as (_, target), ui_browser(repository) as (browser, _):
            admin = ui_context(browser)
            page = sign_in(admin, fixture, "u2")
            page.get_by_label("Workspace", exact=True).select_option(organization)
            expect(page.get_by_label("Workspace", exact=True)).to_have_value(organization)
            page.get_by_text("Manage organization", exact=True).click()
            page.locator("#invitation-email").fill(target.email)
            page.locator("#invitation-role").select_option("viewer")
            before = fixture.mail_ids()
            with page.expect_response(lambda response: response.url == ORIGIN + f"/api/organizations/{organization}/invitations"
                                      and response.request.method == "POST") as invited:
                page.get_by_role("button", name="Send invitation", exact=True).click()
            assert invited.value.status == 201
            invitation_id = invited.value.json()["invitation_id"]
            ticket = invitation_ticket(fixture, target.email, after=before)
            recipient = ui_context(browser, viewport={"width": 390, "height": 844})
            landing = recipient.new_page()
            requests = []
            landing.on("request", lambda request: requests.append(request))
            response = landing.goto(ORIGIN + "/en/invitations/accept#ticket=" + ticket)
            assert response.status == 200
            assert response.headers["referrer-policy"] == "no-referrer"
            expect(landing.get_by_role("button", name="I have signed in", exact=True)).to_be_visible()
            clean = landing.url + landing.content() + landing.evaluate("JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")
            assert ticket not in clean, "Invitation tickets must not remain in history, DOM or persistent browser state"
            assert all(ticket not in request.url and urlsplit(request.url).hostname == "127.0.0.1" for request in requests)
            with recipient.expect_page() as popup:
                landing.get_by_role("link", name="Sign in in a new window", exact=True).click()
            login = popup.value
            logto_sign_in(login, target)
            login.wait_for_url(ORIGIN + "/en")
            landing.bring_to_front()
            if landing.get_by_role("button", name="I have signed in", exact=True).count():
                landing.get_by_role("button", name="I have signed in", exact=True).click()
            with landing.expect_response(ORIGIN + "/api/invitations/accept") as accepted:
                landing.get_by_role("button", name="Accept invitation", exact=True).click()
            assert accepted.value.status == 200
            expect(landing.locator("#invitation-status")).to_contain_text("Invitation accepted")
            page.reload()
            page.get_by_text("Manage organization", exact=True).click()
            role = page.get_by_label(f"Role for {target.email}", exact=True)
            expect(role).to_have_value("viewer")
            role.select_option("editor")
            with page.expect_response(lambda response: "/members/" in response.url and response.request.method == "PATCH") as changed:
                page.get_by_role("button", name=f"Save role for {target.email}", exact=True).click()
            assert changed.value.status == 200
            expect(page.locator("#account-control")).to_have_attribute("aria-busy", "false")
            expect(page.get_by_label(f"Role for {target.email}", exact=True)).to_have_value("editor")
            with page.expect_response(lambda response: "/members/" in response.url and response.request.method == "DELETE") as removed:
                page.get_by_role("button", name=f"Remove {target.email}", exact=True).click()
            assert removed.value.status == 204
            expect(page.get_by_label(f"Role for {target.email}", exact=True)).to_have_count(0)
            recipient.close(); admin.close()
    finally:
        if invitation_id:
            fixture.api.request("DELETE", "/api/organization-invitations/" + invitation_id, missing_ok=True)


def test_user_creates_an_organization_cancels_its_invitation_and_cannot_remove_its_last_admin(ui_environment):
    fixture, repository = ui_environment
    organization_id = invitation_id = None
    name = "Auth31 UI " + uuid4().hex
    try:
        with mailbox_user(fixture) as (_, target), ui_browser(repository) as (browser, _):
            context = ui_context(browser)
            page = sign_in(context, fixture, "u2")
            page.locator(".account-management summary").filter(has_text="Create organization").click()
            page.get_by_label("Organization name", exact=True).fill(name)
            with page.expect_response(lambda response: response.url == ORIGIN + "/api/organizations"
                                      and response.request.method == "POST") as created:
                page.locator(".account-management form button").click()
            assert created.value.status == 201
            organization_id = created.value.json()["organization_id"]
            page.get_by_label("Workspace", exact=True).select_option(organization_id)
            expect(page.get_by_label("Workspace", exact=True)).to_have_value(organization_id)
            page.get_by_text("Manage organization", exact=True).click()
            own_email = fixture.users["u2"].email
            page.get_by_label(f"Role for {own_email}", exact=True).select_option("viewer")
            with page.expect_response(lambda response: "/members/" in response.url and response.request.method == "PATCH") as protected:
                page.get_by_role("button", name=f"Save role for {own_email}", exact=True).click()
            assert protected.value.status == 409 and protected.value.json()["error"]["code"] == "last_admin"
            expect(page.locator("#account-status")).to_contain_text("at least one administrator")
            page.locator("#invitation-email").fill(target.email)
            with page.expect_response(lambda response: response.url.endswith("/invitations") and response.request.method == "POST") as invited:
                page.get_by_role("button", name="Send invitation", exact=True).click()
            assert invited.value.status == 201
            invitation_id = invited.value.json()["invitation_id"]
            with page.expect_response(lambda response: "/invitations/" in response.url and response.request.method == "DELETE") as cancelled:
                page.get_by_role("button", name=f"Cancel invitation for {target.email}", exact=True).click()
            assert cancelled.value.status == 200 and cancelled.value.json()["status"] == "cancelled"
            expect(page.get_by_role("button", name=f"Cancel invitation for {target.email}", exact=True)).to_have_count(0)
            assert context.request.get(ORIGIN + "/api/auth/me").json()["role"] == "admin"
            context.close()
    finally:
        if invitation_id:
            fixture.api.request("DELETE", "/api/organization-invitations/" + invitation_id, missing_ok=True)
        if organization_id:
            record = fixture.api.request("GET", "/api/organizations/" + organization_id)
            assert record["name"] == name, "Only this UI scenario's own temporary organization may be removed"
            fixture.api.request("DELETE", "/api/organizations/" + organization_id)


def test_multitab_space_logout_and_new_account_do_not_transfer_pending_tasks_cursors_or_old_urls(ui_environment):
    fixture, repository = ui_environment
    with ui_browser(repository) as (browser, _):
        context = ui_context(browser)
        first = sign_in(context, fixture, return_to="/en/studio")
        first.locator("#brief").fill("Original personal production")
        with first.expect_response(lambda response: response.url == ORIGIN + "/api/studio/tasks"
                                   and response.request.method == "POST") as original:
            first.locator("#create-task").click()
        personal = original.value.json()
        expect(first.locator("#current-task")).to_contain_text("Original personal production")
        first.wait_for_function("Object.keys(sessionStorage).some(k => k.startsWith('backlot.studio.cursor:'))")
        writes = []
        def lose_personal_response(route):
            if route.request.method != "POST":
                route.continue_(); return
            writes.append((route.request.headers["idempotency-key"], route.request.post_data_json))
            response = route.fetch()
            assert response.status == 202
            route.abort()
        first.route("**/api/studio/tasks", lose_personal_response)
        first.locator("#brief").fill("An unconfirmed private personal draft")
        first.locator("#create-task").click()
        expect(first.locator("#studio-status")).to_contain_text("request key is saved")
        second = context.new_page()
        second.goto(ORIGIN + "/en/studio")
        second.get_by_label("Workspace", exact=True).select_option(fixture.organizations["A"])
        expect(second.get_by_label("Workspace", exact=True)).to_have_value(fixture.organizations["A"])
        expect(second.locator("#brief")).to_have_value("")
        expect(first.get_by_label("Workspace", exact=True)).to_have_value(fixture.organizations["A"])
        stored = second.evaluate("JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")
        assert all(value not in stored for value in ["An unconfirmed private personal draft", personal["task_id"]])
        second.locator("#brief").fill("A clean organization production")
        with second.expect_response(lambda response: response.url == ORIGIN + "/api/studio/tasks"
                                    and response.request.method == "POST") as created:
            second.locator("#create-task").click()
        organization_task = created.value.json()
        assert created.value.status == 202
        assert created.value.request.headers["idempotency-key"] != writes[0][0]
        assert organization_task["ownership"]["workspace"] == {"kind": "organization", "owner_id": fixture.organizations["A"]}
        first.goto(ORIGIN + "/en/studio?task=" + personal["task_id"])
        expect(first.locator("#task-list")).to_contain_text("A clean organization production")
        expect(first.locator("#current-task")).not_to_contain_text("Original personal production")
        second.get_by_role("button", name="Sign out", exact=True).click()
        expect(second.get_by_role("link", name="Sign in", exact=True)).to_be_visible()
        expect(first.get_by_role("link", name="Sign in", exact=True)).to_be_visible()
        first.get_by_role("link", name="Sign in", exact=True).click()
        logto_sign_in(first, fixture.login_credentials("u2"))
        first.wait_for_url(ORIGIN + "/en")
        expect(first.locator("#account-control")).to_contain_text(fixture.users["u2"].email)
        first.goto(ORIGIN + "/en/studio?task=" + personal["task_id"])
        expect(first.locator("#account-control")).to_contain_text(fixture.users["u2"].email)
        expect(first.locator("#task-list")).not_to_contain_text("Original personal production")
        assert context.request.get(ORIGIN + "/api/studio/tasks").json() == []
        assert len(writes) == 1
        stored = first.evaluate("JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")
        assert all(value not in stored for value in [personal["task_id"], organization_task["task_id"], "An unconfirmed private personal draft"])
        context.close()


def test_a_member_without_recognized_roles_is_not_displayed_as_a_viewer(ui_environment):
    fixture, repository = ui_environment
    try:
        fixture.set_roles("cross", "B", [])
        with ui_browser(repository) as (browser, _):
            context = ui_context(browser)
            page = sign_in(context, fixture, "u2")
            page.get_by_label("Workspace", exact=True).select_option(fixture.organizations["B"])
            expect(page.get_by_label("Workspace", exact=True)).to_have_value(fixture.organizations["B"])
            page.get_by_text("Manage organization", exact=True).click()
            target = fixture.users["cross"].email
            expect(page.get_by_label(f"Role for {target}", exact=True)).to_have_value("")
            expect(page.get_by_role("button", name=f"Save role for {target}", exact=True)).to_be_disabled()
            context.close()
    finally:
        fixture.set_roles("cross", "B", ["viewer"])


def test_active_account_can_revoke_its_session_when_identity_reads_are_unavailable(ui_environment):
    fixture, repository = ui_environment
    with ui_browser(repository) as (browser, app):
        context = ui_context(browser)
        page = sign_in(context, fixture)
        expect(page.get_by_role("button", name="Sign out", exact=True)).to_be_visible()
        with identity_outage(app):
            expect(page.get_by_role("link", name="Sign in", exact=True)).to_be_visible(timeout=20000)
            expect(page.get_by_role("button", name="Sign out", exact=True)).to_be_visible()
            with page.expect_response(ORIGIN + "/api/auth/logout") as revoked:
                page.get_by_role("button", name="Sign out", exact=True).click()
            assert revoked.value.status == 200
            assert context.request.get(ORIGIN + "/api/auth/me").status == 401
        context.close()


def test_back_navigation_preserves_only_an_unconfirmed_write_in_the_same_context(ui_environment):
    fixture, repository = ui_environment
    with ui_browser(repository) as (browser, _):
        context = ui_context(browser)
        page = sign_in(context, fixture, return_to="/en/studio")
        keys = []
        def lose_once(route):
            if route.request.method != "POST":
                route.continue_(); return
            keys.append(route.request.headers["idempotency-key"])
            response = route.fetch()
            if len(keys) == 1:
                route.abort()
            else:
                route.fulfill(response=response)
        page.route("**/api/studio/tasks", lose_once)
        page.locator("#brief").fill("An unconfirmed draft before browser history navigation")
        page.locator("#create-task").click()
        expect(page.locator("#studio-status")).to_contain_text("request key is saved")
        page.goto(ORIGIN + "/en")
        page.go_back()
        expect(page.locator("#brief")).to_have_value("An unconfirmed draft before browser history navigation")
        # Exercise the public restored-page lifecycle even when Chromium evicts a no-store page.
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
        expect(page.locator("#brief")).to_have_value("An unconfirmed draft before browser history navigation")
        page.locator("#create-task").click()
        expect(page.locator("#current-task")).to_contain_text("An unconfirmed draft before browser history navigation")
        assert len(keys) == 2 and keys[0] == keys[1]
        assert len(context.request.get(ORIGIN + "/api/studio/tasks").json()) == 1
        context.close()


def test_focus_without_broadcast_restores_current_workspace_and_studio(ui_environment):
    fixture, repository = ui_environment
    with ui_browser(repository) as (browser, _):
        context = ui_context(browser)
        context.add_init_script("window.BroadcastChannel = undefined")
        first = sign_in(context, fixture, return_to="/en/studio")
        expect(first.locator("#create-task")).to_be_enabled()
        first.locator("#brief").fill("An old personal draft which must be cleared")
        second = context.new_page()
        second.goto(ORIGIN + "/en/studio")
        second.get_by_label("Workspace", exact=True).select_option(fixture.organizations["A"])
        expect(second.get_by_label("Workspace", exact=True)).to_have_value(fixture.organizations["A"])
        first.bring_to_front()
        first.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(first.get_by_label("Workspace", exact=True)).to_have_value(fixture.organizations["A"])
        expect(first.locator("#brief")).to_have_value("")
        expect(first.locator("#brief")).to_be_enabled()
        expect(first.locator("#create-task")).to_be_enabled()
        context.close()


def test_authority_recovery_focus_restores_studio_in_the_verified_context(ui_environment):
    fixture, repository = ui_environment
    with ui_browser(repository) as (browser, app):
        context = ui_context(browser)
        page = sign_in(context, fixture, return_to="/en/studio")
        expect(page.locator("#create-task")).to_be_enabled()
        page.locator("#brief").fill("An unsubmitted draft before the account outage")
        with identity_outage(app):
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(page.get_by_role("link", name="Sign in", exact=True)).to_be_visible(timeout=20000)
            expect(page.locator("#brief")).to_be_disabled()
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(page.locator("#account-control")).to_contain_text(fixture.users["u1"].email)
        expect(page.get_by_label("Workspace", exact=True).locator("option[value='" + fixture.organizations["A"] + "']")).to_have_count(1)
        expect(page.locator("#brief")).to_have_value("")
        expect(page.locator("#brief")).to_be_enabled()
        expect(page.locator("#create-task")).to_be_enabled()
        context.close()
