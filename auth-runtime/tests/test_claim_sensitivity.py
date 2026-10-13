"""Each controlled invalid claim is calibrated against real native membership and an owned HTTP result."""

from tests.fixtures.auth26 import ORIGIN, identity_environment, write_headers
from tests.fixtures.auth32_protocol import protocol_calibration
import pytest


def authorized_resource(browser, provider, fixture):
    context = browser.new_context()
    accepted = provider.callback(context)
    assert accepted.status == 303
    identity = context.request.get(ORIGIN + "/api/auth/me")
    assert identity.status == 200
    me = identity.json()
    selected = context.request.post(ORIGIN + "/api/auth/workspace", data={"kind": "organization", "owner_id": fixture.organizations["A"]}, headers=write_headers(me))
    assert selected.status == 200
    me = selected.json()
    assert me["role"] == "admin"
    assert me["workspace"]["owner_id"] == fixture.organizations["A"]
    created = context.request.post(ORIGIN + "/api/studio/tasks", data={"brief": "Controlled claim authorization", "profile_id": "calibration"},
                                  headers={**write_headers(me), "Idempotency-Key": "calibration-control-001"})
    assert created.status == 202
    task = created.json()
    resource = context.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"])
    assert resource.status == 200
    assert resource.json()["ownership"]["workspace"] == {"kind": "organization", "owner_id": fixture.organizations["A"]}
    return context, task["task_id"]


def test_controlled_signed_protocol_reaches_real_member_role_and_owned_resource(identity_environment):
    with protocol_calibration(identity_environment) as (browser, provider, fixture):
        context, _ = authorized_resource(browser, provider, fixture)
        context.close()


@pytest.mark.parametrize("fault", ["missing_nonce", "wrong_nonce", "wrong_issuer", "wrong_audience", "multiple_audience_without_azp", "wrong_azp",
    "expired", "missing_iss", "missing_sub", "missing_aud", "missing_exp", "missing_iat", "future_iat", "boolean_iat", "wrong_signature",
    "missing_id_token", "userinfo_sub_mismatch", "nonfinite_exp", "unknown_kid", "ambiguous_kid", "untrusted_algorithm", "nonfinite_iat",
    "boolean_exp", "string_exp", "string_iat", "empty_sub"])
def test_each_invalid_claim_has_an_authorized_control_before_rejection(identity_environment, fault):
    with protocol_calibration(identity_environment, fault) as (browser, provider, fixture):
        authorized, task_id = authorized_resource(browser, provider, fixture)
        before = authorized.request.get(ORIGIN + "/api/studio/tasks").json()
        peer_before = dict(provider.received)
        denied = browser.new_context()
        response = provider.callback(denied, fault=True)
        assert response.status == 401
        assert response.json()["error"]["code"] == "unauthenticated"
        assert provider.received["management_token"] == peer_before["management_token"]
        assert provider.received["native_read"] == peer_before["native_read"]
        assert provider.received["userinfo"] == peer_before["userinfo"] + (1 if fault == "userinfo_sub_mismatch" else 0)
        assert denied.request.get(ORIGIN + "/api/auth/me").status == 401
        assert denied.request.get(ORIGIN + "/api/studio/tasks/" + task_id).status == 401
        provider.fault = False
        assert authorized.request.get(ORIGIN + "/api/studio/tasks/" + task_id).status == 200
        assert authorized.request.get(ORIGIN + "/api/studio/tasks").json() == before
        denied.close()
        authorized.close()
