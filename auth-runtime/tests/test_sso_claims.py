"""Signed SSO claims must fail at Native before a BFF session can be issued."""

from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from tests.fixtures.auth30 import ORIGIN, sso_browser
from tests.fixtures.auth30_claims import (
    SAML_CLAIM_CASES,
    corrupt_saml_response_signature,
    require_saml_claim_helper,
    saml_claim_form,
)


def _record(case, values):
    directory = os.environ.get("AUTH30_EVIDENCE_DIR")
    if directory:
        path = Path(directory) / ("native-" + case + ".json")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
            json.dump(values, output, indent=2)


def _accepted_saml_editor_control(sso, helper, case):
    control = {}
    member, page = sso.sign_in("saml", organization="B", hold_saml_post=control)
    form = saml_claim_form(helper, sso.fixture, control["body"], "control") if helper else control["body"]
    accepted = member.request.post(sso.fixture.idp["saml"]["acs_url"], data=form, max_redirects=0,
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert accepted.status == 302, "The same trusted signing fixture must reach Native acceptance"
    page.goto(accepted.headers["location"])
    page.wait_for_url(ORIGIN + "/en")
    assert sso.callback_status == 303
    me = sso.me(member)
    assert me["workspace"]["key"] == "organization:" + sso.fixture.organizations["B"]
    assert me["role"] == "editor" and "production:create" in me["permissions"]
    principal = sso.record_principal(member, "saml")
    assert principal["xml_signature_count"] == 2
    assert principal["native_subject"] == sso.fixture._data["sso_subjects"]["saml"]["native_subject"]
    task = sso.create(member)
    resource = member.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"])
    assert resource.status == 200
    assert resource.json()["ownership"]["workspace"] == {
        "kind": "organization", "owner_id": sso.fixture.organizations["B"]}
    _record(case + "-control", {"native_status": accepted.status, "callback_status": sso.callback_status,
        "role": me["role"], "resource_status": resource.status, "known_subject_verified": True,
        "source_form_sha256": sha256(control["body"].encode()).hexdigest(),
        "submitted_form_sha256": sha256(form.encode()).hexdigest()})
    member.close()
    return task


def _assert_native_rejection(sso, context, form, task, case):
    denied = context.request.post(sso.fixture.idp["saml"]["acs_url"], data=form, max_redirects=0,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    _record(case, {"native_status": denied.status, "callback_status": sso.callback_status,
        "submitted_form_sha256": sha256(form.encode()).hexdigest()})
    assert denied.status in {400, 401, 403, 404, 409}, f"Native must reject fixed {case} before BFF continuation"
    assert sso.callback_status is None
    assert context.request.get(ORIGIN + "/api/auth/me").status == 401
    assert context.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status == 401
    assert not any(cookie["name"] == "openmontage_session" for cookie in context.cookies(ORIGIN))
    context.close()


def test_trusted_signed_saml_wrong_audience_is_rejected_before_native_continuation(monkeypatch):
    helper = require_saml_claim_helper()
    with sso_browser(monkeypatch) as sso:
        task = _accepted_saml_editor_control(sso, helper, "wrong_audience")

        # A fresh pending request prevents the consumed control from masking an Audience gap.
        negative = {}
        signed_out, _ = sso.sign_in("saml", organization="B", hold_saml_post=negative)
        form = saml_claim_form(helper, sso.fixture, negative["body"], "wrong_audience")
        _assert_native_rejection(sso, signed_out, form, task, "wrong_audience")


@pytest.mark.parametrize("case", SAML_CLAIM_CASES)
def test_trusted_signed_saml_fixed_claims_are_rejected_before_native_continuation(monkeypatch, case):
    helper = require_saml_claim_helper()
    with sso_browser(monkeypatch) as sso:
        task = _accepted_saml_editor_control(sso, helper, case)
        negative = {}
        signed_out, _ = sso.sign_in("saml", organization="B", hold_saml_post=negative)
        form = saml_claim_form(helper, sso.fixture, negative["body"], case)
        _assert_native_rejection(sso, signed_out, form, task, case)


def test_saml_corrupt_outer_response_signature_is_rejected_before_native_continuation(monkeypatch):
    with sso_browser(monkeypatch) as sso:
        task = _accepted_saml_editor_control(sso, None, "wrong_response_signature")
        negative = {}
        signed_out, _ = sso.sign_in("saml", organization="B", hold_saml_post=negative)
        form = corrupt_saml_response_signature(negative["body"])
        _assert_native_rejection(sso, signed_out, form, task, "wrong_response_signature")
