"""UI entry points through real native email registration and password reset."""

import re
import secrets
from uuid import uuid4

from playwright.sync_api import expect

from tests.fixtures.auth31 import ORIGIN, ui_environment, ui_browser, ui_context, read_mail, delete_mail
from tests.fixtures.identity.browser import logto_sign_in
from tests.fixtures.identity.environment import LoginCredentials


def verification(page, fixture, recipient, messages):
    page.locator('input[name="passcode_0"]').wait_for()
    mail = read_mail(recipient, after=messages)
    messages.add(mail["ID"])
    code = re.search(r"\b(\d{6})\b", mail["Text"])
    assert code, "The real native email must contain an OTP"
    for index, digit in enumerate(code[1]):
        page.locator(f'input[name="passcode_{index}"]').fill(digit)
    page.locator('input[name="passcode_5"]').press("Enter")


def test_ui_registration_reset_and_logout_keep_the_verified_identity(ui_environment):
    fixture, repository = ui_environment
    email = "auth31.ui.signup." + uuid4().hex + "@openmontage.test"
    first = LoginCredentials("", email, "Aa9-" + secrets.token_urlsafe(24))
    replacement = LoginCredentials("", email, "Bb9-" + secrets.token_urlsafe(24))
    original_mail = fixture.mail_ids()
    messages = set(original_mail)
    try:
        with ui_browser(repository) as (browser, _):
            context = ui_context(browser)
            page = context.new_page()
            page.goto(ORIGIN + "/en")
            page.get_by_role("link", name="Create account", exact=True).click()
            if page.get_by_role("link", name="Create account", exact=True).count():
                page.get_by_role("link", name="Create account", exact=True).click()
            page.locator('input[name="identifier"]').fill(email)
            page.locator('input[name="identifier"]').press("Enter")
            verification(page, fixture, email, messages)
            page.locator('input[name="newPassword"]').fill(first.password)
            page.locator('input[name="newPassword"]').press("Enter")
            page.wait_for_url(ORIGIN + "/en")
            expect(page.locator("#account-control")).to_contain_text(email)
            me = context.request.get(ORIGIN + "/api/auth/me").json()
            assert me["user"]["email"] == email and me["user"]["email_verified"] is True
            old_cookie = next(cookie for cookie in context.cookies() if cookie["name"] == "openmontage_session")
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_role("link", name="Sign in", exact=True)).to_be_visible()
            rejected = ui_context(browser)
            rejected.add_cookies([old_cookie])
            assert rejected.request.get(ORIGIN + "/api/auth/me").status == 401
            rejected.close()
            page.get_by_role("link", name="Reset password or enterprise SSO", exact=True).click()
            page.locator('input[name="identifier"]').fill(email)
            page.get_by_role("button", name="Sign in", exact=True).click()
            page.get_by_text("Forgot your password?", exact=True).click()
            page.locator('input[name="identifier"]').fill(email)
            page.locator('input[name="identifier"]').press("Enter")
            verification(page, fixture, email, messages)
            page.locator('input[name="newPassword"]').fill(replacement.password)
            page.locator('input[name="newPassword"]').press("Enter")
            page.locator('input[name="identifier"]').wait_for()
            logto_sign_in(page, replacement)
            page.wait_for_url(ORIGIN + "/en")
            returned = context.request.get(ORIGIN + "/api/auth/me").json()
            assert returned["user"]["user_id"] == me["user"]["user_id"]
            assert returned["user"]["email_verified"] is True and len(messages - original_mail) == 2
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_role("link", name="Sign in", exact=True)).to_be_visible()
            context.close()
    finally:
        users = [user for user in fixture.api.items("/api/users") if user.get("primaryEmail") == email]
        assert len(users) <= 1, "Cleanup only targets the exact unique registration email"
        for user in users:
            fixture.api.request("DELETE", "/api/users/" + user["id"])
        for message_id in messages - original_mail:
            delete_mail(message_id, email)
