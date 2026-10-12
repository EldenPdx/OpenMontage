"""Actual remote acceptance, response loss and public-factory restart reconciliation."""

import pytest

from tests.fixtures.auth27 import (
    ORIGIN, headers, organization_browser, organization_environment, owned_organization,
    signed_in, unique_name, create_organization, select_organization, invitation_ticket, invite, accept, me, short_invitation,
    organization_items,
)
from tests.fixtures.auth27_faults import AfterResponse, ChangedTemplate, LoseResponse, UnavailableRead, UnknownThrottle


def test_invitation_response_loss_restarts_without_another_email_or_invitation(organization_environment):
    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    email = fixture.users[alias].email
    with organization_browser(organization_environment) as (browser, app):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        org = organization["organization_id"]
        select_organization(admin, org)
        baseline = fixture.mail_ids()
        fault = LoseResponse("POST", "/api/organization-invitations")
        app.state.organization_service.management.transport = fault
        path = ORIGIN + "/api/organizations/" + org + "/invitations"
        first = admin.request.post(path, data={"email": email, "role": "editor"}, headers=headers(admin, "lost-invitation-001"))
        assert first.status == 503 and first.json()["error"]["code"] == "outcome_unknown"
        assert fault.statuses == [201], "Response loss must follow real native invitation acceptance"
        ticket = invitation_ticket(fixture, email, after=baseline)
        sent = fixture.mail_ids() - baseline
        assert len(sent) == 1 and fault.deliveries == 1
        state = admin.storage_state()
        admin.close()
    with organization_browser(organization_environment) as (browser, app):
        admin = browser.new_context(storage_state=state)
        guard = LoseResponse("POST", "/api/organization-invitations")
        app.state.organization_service.management.transport = guard
        recovered = admin.request.post(path, data={"email": email, "role": "editor"}, headers=headers(admin, "lost-invitation-001"))
        assert recovered.status == 201
        assert guard.deliveries == 0 and fixture.mail_ids() - baseline == sent
        assert len(admin.request.get(path).json()["items"]) == 1
        assert ticket not in recovered.text()
        admin.close()


@pytest.mark.parametrize("phase", ["create", "membership", "admin"])
def test_organization_creation_recovers_each_native_stage_without_repeating_it(organization_environment, phase):
    fixture, _, _ = organization_environment
    method, path = {"create": ("POST", "/api/organizations"),
        "membership": ("POST", lambda path: path.startswith("/api/organizations/") and path.endswith("/users")),
        "admin": ("PUT", lambda path: path.startswith("/api/organizations/") and path.endswith("/roles"))}[phase]
    body = {"name": unique_name()}
    with organization_browser(organization_environment) as (browser, app):
        context = signed_in(browser, fixture)
        fault = LoseResponse(method, path)
        app.state.organization_service.management.transport = fault
        first = context.request.post(ORIGIN + "/api/organizations", data=body, headers=headers(context, "lost-create-stage-001"))
        assert first.status == 503 and first.json()["error"]["code"] == "outcome_unknown"
        assert fault.deliveries == 1 and fault.statuses[0] in {201, 204}
        state = context.storage_state()
        context.close()
    with organization_browser(organization_environment) as (browser, app):
        context = browser.new_context(storage_state=state)
        guard = LoseResponse(method, path)
        app.state.organization_service.management.transport = guard
        recovered = context.request.post(ORIGIN + "/api/organizations", data=body, headers=headers(context, "lost-create-stage-001"))
        assert recovered.status == 201 and recovered.json()["roles"] == ["admin"]
        assert guard.deliveries == 0
        owned_organization(fixture, recovered)
        assert len([item for item in organization_items(context) if item["name"] == body["name"]]) == 1
        context.close()


def test_remaining_admin_reconciles_an_unknown_self_removal_without_granting_as_old_actor(organization_environment):
    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, app):
        first = signed_in(browser, fixture)
        organization = create_organization(first, fixture)
        org = organization["organization_id"]
        select_organization(first, org)
        _, ticket = invite(first, fixture, org, alias, "admin")
        second = signed_in(browser, fixture, alias)
        assert accept(second, ticket).status == 200
        select_organization(second, org)
        first_id, second_id = me(first)["user"]["user_id"], me(second)["user"]["user_id"]
        native_path = "/api/organizations/" + org + "/users/" + fixture.users["u1"].id
        fault = LoseResponse("DELETE", native_path)
        app.state.organization_service.management.transport = fault
        path = ORIGIN + "/api/organizations/" + org + "/members/"
        removed = first.request.delete(path + first_id, headers=headers(first, "lost-self-remove-001"))
        assert removed.status == 503 and fault.statuses == [204]
        assert first.request.get(ORIGIN + "/api/auth/me").status == 404
        state = second.storage_state()
        first.close()
        second.close()
    with organization_browser(organization_environment) as (browser, app):
        remaining = browser.new_context(storage_state=state)
        guard = LoseResponse("DELETE", native_path)
        app.state.organization_service.management.transport = guard
        resumed = remaining.request.patch(path + second_id, data={"role": "admin"}, headers=headers(remaining, "after-self-remove-001"))
        assert resumed.status == 200 and resumed.json()["roles"] == ["admin"]
        assert guard.deliveries == 0
        assert len(remaining.request.get(ORIGIN + "/api/organizations/" + org + "/members").json()["items"]) == 1
        remaining.close()


def test_native_acceptance_before_expiry_can_reconcile_after_expiry(organization_environment):
    from datetime import datetime, timezone
    import time

    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, app):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        org = organization["organization_id"]
        select_organization(admin, org)
        invitation, ticket, expiry = short_invitation(admin, app, fixture, org, alias, seconds=12, key="short-creation-001")
        recipient = signed_in(browser, fixture, alias)
        native_path = "/api/organization-invitations/" + invitation["invitation_id"] + "/status"
        fault = LoseResponse("PUT", native_path)
        app.state.organization_service.management.transport = fault
        response = accept(recipient, ticket, key="short-acceptance-001")
        assert response.status == 503 and fault.statuses == [200]
        assert datetime.now(timezone.utc) < expiry
        state = recipient.storage_state()
        recipient.close()
        admin.close()
    time.sleep(max(0, (expiry - datetime.now(timezone.utc)).total_seconds()) + 0.2)
    with organization_browser(organization_environment) as (browser, app):
        recipient = browser.new_context(storage_state=state)
        guard = LoseResponse("PUT", native_path)
        app.state.organization_service.management.transport = guard
        recovered = accept(recipient, ticket, key="short-acceptance-001")
        assert recovered.status == 200 and recovered.json()["roles"] == ["editor"]
        assert guard.deliveries == 0
        assert accept(recipient, ticket, key="short-new-key-001").status == 409
        recipient.close()


@pytest.mark.parametrize("denial", ["permission", "disabled", "removed"])
def test_permission_loss_before_submission_releases_the_organization_intent(organization_environment, denial):
    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, app):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        org = organization["organization_id"]
        select_organization(admin, org)
        _, ticket = invite(admin, fixture, org, alias)
        recipient = signed_in(browser, fixture, alias)
        assert accept(recipient, ticket).status == 200
        member_id = me(recipient)["user"]["user_id"]
        path = ORIGIN + "/api/organizations/" + org + "/members/" + member_id
        actor_roles = "/api/organizations/" + org + "/users/" + fixture.users["u1"].id + "/roles"
        target_roles = "/api/organizations/" + org + "/users/" + fixture.users[alias].id + "/roles"
        boundary = headers(admin, "pre-submit-denial-001")
        if denial == "permission":
            action = lambda: fixture.api.request("PUT", actor_roles, {"organizationRoleIds": [fixture._data["roles"]["viewer"]]})
        elif denial == "disabled":
            action = lambda: fixture.suspend("u1")
        else:
            action = lambda: fixture.api.request("DELETE", target_roles.removesuffix("/roles"))
        fault = AfterResponse("GET", "/api/organizations/" + org + "/users", action, after=1 if denial == "removed" else 2)
        app.state.organization_service.management.transport = fault
        try:
            denied = admin.request.patch(path, data={"role": "viewer"}, headers=boundary)
            status, code = {"permission": (403, "forbidden"), "disabled": (401, "unauthenticated"), "removed": (404, "not_found")}[denial]
            assert denied.status == status and denied.json()["error"]["code"] == code
            assert fault.deliveries == 2
            if denial != "removed":
                assert fixture.api.request("GET", target_roles)[0]["name"] == "editor"
            else:
                assert fixture.users[alias].id not in {account["id"] for account in fixture.api.items("/api/organizations/" + org + "/users")}
        finally:
            app.state.organization_service.management.transport = None
            if denial == "disabled":
                fixture.suspend("u1", False)
            elif denial == "permission":
                fixture.api.request("PUT", actor_roles, {"organizationRoleIds": [fixture._data["roles"]["admin"]]})
            else:
                fixture.api.request("POST", "/api/organizations/" + org + "/users", {"userIds": [fixture.users[alias].id]})
                fixture.api.request("PUT", target_roles, {"organizationRoleIds": [fixture._data["roles"]["editor"]]})
        if denial == "disabled":
            assert admin.request.get(ORIGIN + "/api/auth/me").status == 401
            admin.close()
            admin = signed_in(browser, fixture)
            select_organization(admin, org)
        accepted = admin.request.patch(path, data={"role": "viewer"}, headers=headers(admin, "after-pre-submit-denial-001"))
        assert accepted.status == 200 and accepted.json()["roles"] == ["viewer"]
        recipient.close()
        admin.close()


def test_real_native_mail_throttle_can_recover_after_restart_without_an_extra_invitation(organization_environment):
    import time

    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    email = fixture.users[alias].email
    with organization_browser(organization_environment) as (browser, app):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        org = organization["organization_id"]
        select_organization(admin, org)
        invitation, ticket = invite(admin, fixture, org, alias)
        native = "/api/organization-invitations/" + invitation["invitation_id"]
        for _ in range(9):
            fixture.api.request("POST", native + "/message", {"link": ORIGIN + "/en/invitations/accept#ticket=" + ticket})
        deadline = time.monotonic() + 600.5
        path = ORIGIN + "/api/organizations/" + org + "/invitations"
        assert admin.request.delete(path + "/" + invitation["invitation_id"], headers=headers(admin, "throttle-cancel-001")).status == 200
        baseline = fixture.mail_ids()
        fault = LoseResponse("POST", "/api/organization-invitations")
        app.state.organization_service.management.transport = fault
        response = admin.request.post(path, data={"email": email, "role": "editor"}, headers=headers(admin, "throttle-create-001"))
        assert fault.statuses == [429], "The rejection must be a real native mail throttle"
        assert response.status == 503 and response.json()["error"]["code"] == "identity_unavailable"
        assert response.json()["error"]["retryable"] is True
        assert len(admin.request.get(path).json()["items"]) == 1
        actor_id = me(admin)["user"]["user_id"]
        admin.close()
    while time.monotonic() < deadline:
        time.sleep(min(10, deadline - time.monotonic()))
    with organization_browser(organization_environment) as (browser, app):
        admin = signed_in(browser, fixture)
        select_organization(admin, org)
        assert me(admin)["user"]["user_id"] == actor_id
        guard = AfterResponse("POST", "/api/organization-invitations", lambda: None)
        app.state.organization_service.management.transport = guard
        recovered = admin.request.post(path, data={"email": email, "role": "editor"}, headers=headers(admin, "throttle-create-001"))
        assert recovered.status == 201 and recovered.json()["status"] == "pending"
        assert recovered.json()["invitation_id"] != invitation["invitation_id"]
        assert guard.deliveries == 1
        invitation_ticket(fixture, email, after=baseline)
        repeated = admin.request.post(path, data={"email": email, "role": "editor"}, headers=headers(admin, "throttle-create-001"))
        assert repeated.status == 201 and repeated.json() == recovered.json() and guard.deliveries == 1
        assert len(admin.request.get(path).json()["items"]) == 2
        admin.close()


@pytest.mark.parametrize("operation", ["role", "remove"])
def test_member_response_loss_reconciles_after_restart_and_revokes_immediately(organization_environment, operation):
    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, app):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        org = organization["organization_id"]
        select_organization(admin, org)
        _, ticket = invite(admin, fixture, org, alias)
        recipient = signed_in(browser, fixture, alias)
        assert accept(recipient, ticket).status == 200
        member_id = me(recipient)["user"]["user_id"]
        select_organization(recipient, org)
        path = ORIGIN + "/api/organizations/" + org + "/members/" + member_id
        native_path = "/api/organizations/" + org + "/users/" + fixture.users[alias].id + ("/roles" if operation == "role" else "")
        method, payload = ("PATCH", {"role": "viewer"}) if operation == "role" else ("DELETE", None)
        fault = LoseResponse("PUT" if operation == "role" else "DELETE", native_path)
        app.state.organization_service.management.transport = fault
        response = admin.request.fetch(path, method=method, data=payload, headers=headers(admin, "lost-member-change-001"))
        assert response.status == 503 and fault.statuses == [204]
        if operation == "role":
            assert me(recipient)["role"] == "viewer"
        else:
            assert recipient.request.get(ORIGIN + "/api/auth/me").status == 404
        state = admin.storage_state()
        recipient.close()
        admin.close()
    with organization_browser(organization_environment) as (browser, app):
        admin = browser.new_context(storage_state=state)
        guard = AfterResponse("PUT" if operation == "role" else "DELETE", native_path, lambda: None)
        app.state.organization_service.management.transport = guard
        recovered = admin.request.fetch(path, method=method, data=payload, headers=headers(admin, "lost-member-change-001"))
        assert recovered.status == (200 if operation == "role" else 204) and guard.deliveries == 0
        if operation == "role":
            assert recovered.json()["roles"] == ["viewer"]
        else:
            assert recovered.body() == b""
        admin.close()


def test_other_429_retains_unknown_outcome_without_another_native_post(organization_environment):
    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    payload = {"email": fixture.users[alias].email, "role": "editor"}
    with organization_browser(organization_environment) as (browser, app):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        org = organization["organization_id"]
        select_organization(admin, org)
        path = ORIGIN + "/api/organizations/" + org + "/invitations"
        app.state.organization_service.management.transport = UnknownThrottle()
        response = admin.request.post(path, data=payload, headers=headers(admin, "other-throttle-001"))
        assert response.status == 503 and response.json()["error"]["code"] == "outcome_unknown"
        state = admin.storage_state()
        admin.close()
    with organization_browser(organization_environment) as (browser, app):
        admin = browser.new_context(storage_state=state)
        guard = AfterResponse("POST", "/api/organization-invitations", lambda: None)
        app.state.organization_service.management.transport = guard
        response = admin.request.post(path, data=payload, headers=headers(admin, "other-throttle-001"))
        assert response.status == 503 and response.json()["error"]["code"] == "outcome_unknown"
        assert admin.request.post(path, data=payload, headers=headers(admin, "other-throttle-002")).status == 409
        assert admin.request.get(path).json()["items"] == [] and guard.deliveries == 0
        admin.close()


@pytest.mark.parametrize("operation", ["accept", "cancel"])
def test_invitation_terminal_response_loss_reconciles_once_after_restart(organization_environment, operation):
    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, app):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        org = organization["organization_id"]
        select_organization(admin, org)
        invitation, ticket = invite(admin, fixture, org, alias)
        recipient = signed_in(browser, fixture, alias)
        native_path = "/api/organization-invitations/" + invitation["invitation_id"] + "/status"
        path = ORIGIN + "/api/organizations/" + org + "/invitations/" + invitation["invitation_id"]
        fault = LoseResponse("PUT", native_path)
        app.state.organization_service.management.transport = fault
        response = accept(recipient, ticket, key="lost-terminal-001") if operation == "accept" else admin.request.delete(path, headers=headers(admin, "lost-terminal-001"))
        assert response.status == 503 and fault.statuses == [200]
        state = (recipient if operation == "accept" else admin).storage_state()
        recipient.close()
        admin.close()
    with organization_browser(organization_environment) as (browser, app):
        actor = browser.new_context(storage_state=state)
        guard = AfterResponse("PUT", native_path, lambda: None)
        app.state.organization_service.management.transport = guard
        recovered = accept(actor, ticket, key="lost-terminal-001") if operation == "accept" else actor.request.delete(path, headers=headers(actor, "lost-terminal-001"))
        assert recovered.status == 200 and guard.deliveries == 0
        if operation == "accept":
            assert recovered.json() == {"organization_id": org, "name": organization["name"], "roles": ["editor"]}
            assert accept(actor, ticket, key="terminal-new-key-001").status == 409
        else:
            assert recovered.json()["status"] == "cancelled"
            recipient = signed_in(browser, fixture, alias)
            assert accept(recipient, ticket).status == 409
            recipient.close()
        actor.close()


def test_management_read_outage_restarts_and_completes_one_real_invitation(organization_environment):
    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    payload = {"email": fixture.users[alias].email, "role": "editor"}
    with organization_browser(organization_environment) as (browser, app):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        org = organization["organization_id"]
        select_organization(admin, org)
        path = ORIGIN + "/api/organizations/" + org + "/invitations"
        baseline = fixture.mail_ids()
        app.state.organization_service.management.transport = UnavailableRead()
        response = admin.request.post(path, data=payload, headers=headers(admin, "read-outage-001"))
        assert response.status == 503 and response.json()["error"]["code"] == "identity_unavailable"
        assert admin.request.get(path).json()["items"] == []
        state = admin.storage_state()
        admin.close()
    with organization_browser(organization_environment) as (browser, app):
        admin = browser.new_context(storage_state=state)
        guard = AfterResponse("POST", "/api/organization-invitations", lambda: None)
        app.state.organization_service.management.transport = guard
        recovered = admin.request.post(path, data=payload, headers=headers(admin, "read-outage-001"))
        assert recovered.status == 201 and guard.deliveries == 1
        invitation_ticket(fixture, payload["email"], after=baseline)
        assert admin.request.post(path, data=payload, headers=headers(admin, "read-outage-001")).json() == recovered.json()
        assert len(admin.request.get(path).json()["items"]) == 1 and guard.deliveries == 1
        admin.close()


def test_changed_role_template_cannot_create_and_recovers_after_fixed_authority(organization_environment):
    fixture, _, _ = organization_environment
    payload = {"name": unique_name()}
    with organization_browser(organization_environment) as (browser, app):
        admin = signed_in(browser, fixture)
        fault = ChangedTemplate("POST", "/api/organizations")
        app.state.organization_service.management.transport = fault
        response = admin.request.post(ORIGIN + "/api/organizations", data=payload, headers=headers(admin, "changed-template-001"))
        assert response.status == 503 and response.json()["error"]["code"] == "identity_unavailable"
        assert fault.deliveries == 0 and "platform:manage" not in response.text()
        state = admin.storage_state()
        admin.close()
    with organization_browser(organization_environment) as (browser, app):
        admin = browser.new_context(storage_state=state)
        guard = AfterResponse("POST", "/api/organizations", lambda: None)
        app.state.organization_service.management.transport = guard
        recovered = admin.request.post(ORIGIN + "/api/organizations", data=payload, headers=headers(admin, "changed-template-001"))
        assert recovered.status == 201 and recovered.json()["roles"] == ["admin"] and guard.deliveries == 1
        owned_organization(fixture, recovered)
        admin.close()
