"""Drive the real pinned Logto screens; no cookies or authenticated flags are fabricated."""

import re
from playwright.sync_api import Error


def _fill_verification(page, fixture, email, before):
    page.locator('input[name="passcode_0"]').wait_for()
    message = fixture.wait_for_mail(email, after=before)
    match = re.search(r"\b(\d{6})\b", message["Text"])
    if not match:
        raise RuntimeError("Native verification email did not contain the expected code")
    for index, digit in enumerate(match[1]):
        page.locator(f'input[name="passcode_{index}"]').fill(digit)
    page.locator('input[name="passcode_5"]').press("Enter")


def logto_sign_in(page, credentials):
    try:
        page.locator('input[name="identifier"]').fill(credentials.username or credentials.email)
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.locator('input[name="password"]').fill(credentials.password)
        page.locator('input[name="password"]').press("Enter")
    except Error:
        raise RuntimeError("Real Logto sign-in screen could not complete") from None


def logto_register(page, fixture, credentials):
    try:
        page.get_by_role("link", name="Create account", exact=True).click()
        page.locator('input[name="identifier"]').fill(credentials.email)
        before = fixture.mail_ids()
        page.locator('input[name="identifier"]').press("Enter")
        _fill_verification(page, fixture, credentials.email, before)
        page.locator('input[name="newPassword"]').fill(credentials.password)
        page.locator('input[name="newPassword"]').press("Enter")
    except Error:
        raise RuntimeError("Real Logto email registration could not complete") from None


def logto_reset_password(page, fixture, credentials):
    try:
        page.locator('input[name="identifier"]').fill(credentials.email)
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.get_by_text("Forgot your password?", exact=True).click()
        page.locator('input[name="identifier"]').fill(credentials.email)
        before = fixture.mail_ids()
        page.locator('input[name="identifier"]').press("Enter")
        _fill_verification(page, fixture, credentials.email, before)
        page.locator('input[name="newPassword"]').fill(credentials.password)
        page.locator('input[name="newPassword"]').press("Enter")
        page.locator('input[name="identifier"]').wait_for()
    except Error:
        raise RuntimeError("Real Logto password reset could not complete") from None


def logto_sso_sign_in(page, credentials):
    try:
        page.locator('input[name="identifier"]').fill(credentials.email)
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.get_by_role("button", name="Single Sign-On", exact=True).click()
        page.locator("#username").fill(credentials.username)
        page.locator("#password").fill(credentials.password)
        page.locator("#kc-login").click()
    except Error:
        raise RuntimeError("Real Keycloak enterprise sign-in could not complete") from None
