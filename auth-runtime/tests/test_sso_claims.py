"""Signed SSO claims must fail at Native before a BFF session can be issued."""

from tests.fixtures.auth30 import ORIGIN, sso_browser
from tests.fixtures.auth30_claims import require_saml_audience_helper, saml_audience_form


def test_trusted_signed_saml_wrong_audience_is_rejected_before_native_continuation(monkeypatch):
    helper = require_saml_audience_helper()
    with sso_browser(monkeypatch) as sso:
        control = {}
        member, page = sso.sign_in("saml", organization="B", hold_saml_post=control)
        acs = sso.fixture.idp["saml"]["acs_url"]
        valid_form = saml_audience_form(helper, sso.fixture, control["body"], "control")
        accepted = member.request.post(acs, data=valid_form, max_redirects=0,
                                       headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert accepted.status == 302, "The same trusted signing fixture must reach Native acceptance"
        page.goto(accepted.headers["location"])
        page.wait_for_url(ORIGIN + "/en")
        assert sso.callback_status == 303
        me = sso.me(member)
        assert me["workspace"]["key"] == "organization:" + sso.fixture.organizations["B"]
        assert me["role"] == "editor" and "production:create" in me["permissions"]
        principal = sso.record_principal(member, "saml")
        assert principal["native_subject"] == sso.fixture._data["sso_subjects"]["saml"]["native_subject"]
        task = sso.create(member)
        resource = member.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"])
        assert resource.status == 200
        assert resource.json()["ownership"]["workspace"] == {
            "kind": "organization", "owner_id": sso.fixture.organizations["B"]}
        member.close()

        # A fresh pending request prevents the consumed control from masking an Audience gap.
        negative = {}
        signed_out, _ = sso.sign_in("saml", organization="B", hold_saml_post=negative)
        wrong_form = saml_audience_form(helper, sso.fixture, negative["body"], "wrong_audience")
        denied = signed_out.request.post(acs, data=wrong_form, max_redirects=0,
                                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert denied.status in {400, 403, 404, 409}, "A later BFF denial cannot substitute for Native claim rejection"
        assert sso.callback_status is None
        assert signed_out.request.get(ORIGIN + "/api/auth/me").status == 401
        assert signed_out.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status == 401
        assert not any(cookie["name"] == "openmontage_session" for cookie in signed_out.cookies(ORIGIN))
        signed_out.close()
