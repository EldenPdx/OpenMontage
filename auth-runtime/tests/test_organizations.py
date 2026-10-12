"""Organization business behavior through the real public application factory."""

from tests.fixtures.auth27 import (
    ORIGIN, headers, organization_browser, organization_environment, owned_organization,
    signed_in, unique_name, create_organization, select_organization, invitation_ticket, me, invite, accept,
    organization_items,
)
from tests.fixtures.identity.environment import username_only_sign_up


def test_personal_user_creates_one_organization_and_becomes_its_admin(organization_environment):
    fixture, _, _ = organization_environment
    with organization_browser(organization_environment) as (browser, _):
        context = signed_in(browser, fixture)
        body = {"name": unique_name()}
        response = context.request.post(ORIGIN + "/api/organizations", data=body, headers=headers(context, "organization-create-001"))
        assert response.status == 201
        organization = owned_organization(fixture, response)
        assert organization == {"organization_id": organization["organization_id"], "name": body["name"], "roles": ["admin"]}
        repeated = context.request.post(ORIGIN + "/api/organizations", data=body, headers=headers(context, "organization-create-001"))
        assert repeated.status == 201 and repeated.json() == organization
        assert [item for item in organization_items(context) if item["name"] == body["name"]] == [organization]
        other = signed_in(browser, fixture, "u2")
        assert organization not in organization_items(other)
        separate = other.request.post(ORIGIN + "/api/organizations", data=body, headers=headers(other, "organization-create-001"))
        assert separate.status == 201 and separate.json()["organization_id"] != organization["organization_id"]
        owned_organization(fixture, separate)
        assert response.headers["cache-control"] == "private, no-store"
        other.close()
        context.close()


def test_admin_invites_verified_recipient_through_real_mail_and_membership(organization_environment):
    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, _):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        organization_id = organization["organization_id"]
        select_organization(admin, organization_id)
        baseline = fixture.mail_ids()
        invitation = admin.request.post(ORIGIN + "/api/organizations/" + organization_id + "/invitations",
            data={"email": fixture.users[alias].email, "role": "editor"}, headers=headers(admin, "invitation-create-001"))
        assert invitation.status == 201
        safe = invitation.json()
        assert set(safe) == {"invitation_id", "organization_id", "email", "role", "status", "expires_at"}
        ticket = invitation_ticket(fixture, fixture.users[alias].email, after=baseline)
        assert ticket not in invitation.text()
        wrong = signed_in(browser, fixture, "u2")
        response = wrong.request.post(ORIGIN + "/api/invitations/accept", data={"ticket": ticket}, headers=headers(wrong, "invitation-accept-001"))
        assert response.status == 409 and response.json()["error"]["code"] == "invitation_invalid"
        recipient = signed_in(browser, fixture, alias)
        assert me(recipient)["user"]["email_verified"] is True
        accepted = recipient.request.post(ORIGIN + "/api/invitations/accept", data={"ticket": ticket}, headers=headers(recipient, "invitation-accept-001"))
        assert accepted.status == 200
        assert accepted.json() == {"organization_id": organization_id, "name": organization["name"], "roles": ["editor"]}
        select_organization(recipient, organization_id)
        assert me(recipient)["role"] == "editor"
        members = admin.request.get(ORIGIN + "/api/organizations/" + organization_id + "/members")
        assert members.status == 200
        member = next(item for item in members.json()["items"] if item["user_id"] == me(recipient)["user"]["user_id"])
        assert member["email"] == fixture.users[alias].email and member["roles"] == ["editor"]
        recipient.close()
        wrong.close()
        admin.close()


def test_organization_detail_requires_its_selected_workspace(organization_environment):
    fixture, _, _ = organization_environment
    with organization_browser(organization_environment) as (browser, _):
        context = signed_in(browser, fixture)
        organization = create_organization(context, fixture)
        path = ORIGIN + "/api/organizations/" + organization["organization_id"]
        assert context.request.get(path).status == 404
        select_organization(context, organization["organization_id"])
        response = context.request.get(path)
        assert response.status == 200 and response.json() == organization
        assert context.request.get(ORIGIN + "/api/organizations/absent").status == 404
        select_organization(context, fixture.organizations["A"])
        assert context.request.get(path).status == 404
        other = signed_in(browser, fixture, "u2")
        select_organization(other, fixture.organizations["B"])
        assert other.request.get(path).status == 404
        other.close()
        context.close()


def test_invitation_replays_only_for_its_verified_actor_and_original_key(organization_environment):
    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, _):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        select_organization(admin, organization["organization_id"])
        _, ticket = invite(admin, fixture, organization["organization_id"], alias)
        recipient = signed_in(browser, fixture, alias)
        first = accept(recipient, ticket)
        assert first.status == 200
        repeated = accept(recipient, ticket)
        assert repeated.status == 200 and repeated.json() == first.json()
        used = accept(recipient, ticket, key="invitation-new-key-001")
        assert used.status == 409 and used.json()["error"]["code"] == "invitation_invalid"
        wrong = signed_in(browser, fixture, "u2")
        assert accept(wrong, ticket).status == 409
        wrong.close()
        recipient.close()
        admin.close()


def test_organization_writes_reject_spoofed_fields_and_missing_operation_key(organization_environment):
    fixture, _, _ = organization_environment
    with organization_browser(organization_environment) as (browser, _):
        context = signed_in(browser, fixture)
        response = context.request.post(ORIGIN + "/api/organizations", data={"name": unique_name()}, headers=headers(context))
        assert response.status == 422 and response.json()["error"]["code"] == "invalid_input"
        response = context.request.post(ORIGIN + "/api/organizations", data={"name": unique_name(), "actor": {"user_id": "foreign"}}, headers=headers(context, "spoofed-creation-001"))
        assert response.status == 422
        private_ticket = "Q" * 43
        response = context.request.post(ORIGIN + "/api/invitations/accept", data={"ticket": private_ticket, "sub": "foreign"}, headers=headers(context, "spoofed-accept-001"))
        assert response.status == 422 and private_ticket not in response.text()
        assert context.request.post(ORIGIN + "/api/invitations/accept", data={"ticket": "bad"}, headers=headers(context, "bad-ticket-accept-001")).status == 422
        context.close()


def test_admin_changes_and_removes_members_and_final_admin_stays(organization_environment):
    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, _):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        org = organization["organization_id"]
        select_organization(admin, org)
        _, ticket = invite(admin, fixture, org, alias)
        recipient = signed_in(browser, fixture, alias)
        assert accept(recipient, ticket).status == 200
        select_organization(recipient, org)
        member = me(recipient)["user"]["user_id"]
        admin_id = me(admin)["user"]["user_id"]
        path = ORIGIN + "/api/organizations/" + org + "/members/"
        promoted = admin.request.patch(path + member, data={"role": "admin"}, headers=headers(admin, "member-promote-001"))
        assert promoted.status == 200 and promoted.json()["roles"] == ["admin"]
        assert me(recipient)["role"] == "admin"
        demoted = admin.request.patch(path + member, data={"role": "viewer"}, headers=headers(admin, "member-demote-001"))
        assert demoted.status == 200 and demoted.json()["roles"] == ["viewer"]
        assert me(recipient)["role"] == "viewer"
        assert recipient.request.patch(path + admin_id, data={"role": "viewer"}, headers=headers(recipient, "viewer-denied-001")).status == 403
        assert recipient.request.patch(path + "foreign-user", data={"role": "viewer"}, headers=headers(recipient, "viewer-denied-002")).status == 404
        last = admin.request.delete(path + admin_id, headers=headers(admin, "last-admin-remove-001"))
        assert last.status == 409 and last.json()["error"]["code"] == "last_admin"
        removed = admin.request.delete(path + member, headers=headers(admin, "member-remove-001"))
        assert removed.status == 204 and removed.body() == b""
        assert recipient.request.get(ORIGIN + "/api/auth/me").status == 404
        assert admin.request.get(ORIGIN + "/api/organizations/" + org + "/members").json()["items"][0]["user_id"] == admin_id
        recipient.close()
        admin.close()


def test_admin_lists_and_cancels_invitation_without_exposing_ticket(organization_environment):
    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, _):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        org = organization["organization_id"]
        select_organization(admin, org)
        invitation, ticket = invite(admin, fixture, org, alias)
        path = ORIGIN + "/api/organizations/" + org + "/invitations"
        listed = admin.request.get(path)
        assert listed.status == 200 and listed.json()["items"] == [invitation]
        assert ticket not in listed.text() and "ticket_hash" not in listed.text()
        repeated = admin.request.post(path, data={"email": fixture.users[alias].email, "role": "editor"}, headers=headers(admin, "invitation-create-001"))
        assert repeated.status == 201 and repeated.json() == invitation
        cancelled = admin.request.delete(path + "/" + invitation["invitation_id"], headers=headers(admin, "invitation-cancel-001"))
        assert cancelled.status == 200 and cancelled.json()["status"] == "cancelled"
        assert admin.request.get(path).json()["items"] == [cancelled.json()]
        assert admin.request.delete(path + "/absent", headers=headers(admin, "invitation-cancel-absent")).status == 404
        assert admin.request.delete(path + "/" + invitation["invitation_id"], headers=headers(admin, "invitation-cancel-001")).json() == cancelled.json()
        recipient = signed_in(browser, fixture, alias)
        assert accept(recipient, ticket).status == 409
        recipient.close()
        admin.close()


def test_unverified_email_cannot_accept_a_real_invitation(organization_environment):
    fixture, _, _ = organization_environment
    before = fixture.api.request("GET", "/api/sign-in-exp")
    with organization_browser(organization_environment) as (browser, _):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        select_organization(admin, organization["organization_id"])
        _, ticket = invite(admin, fixture, organization["organization_id"], "viewer")
        with username_only_sign_up(fixture):
            unverified = signed_in(browser, fixture, "unverified")
            assert me(unverified)["user"]["email_verified"] is False
        assert fixture.api.request("GET", "/api/sign-in-exp") == before
        rejected = accept(unverified, ticket)
        assert rejected.status == 409 and rejected.json()["error"]["code"] == "invitation_invalid"
        members = admin.request.get(ORIGIN + "/api/organizations/" + organization["organization_id"] + "/members").json()["items"]
        assert me(unverified)["user"]["user_id"] not in {member["user_id"] for member in members}
        unverified.close()
        admin.close()


def test_concurrent_admin_self_removal_keeps_one_authoritative_admin(organization_environment):
    from concurrent.futures import ThreadPoolExecutor
    import httpx

    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, _):
        first = signed_in(browser, fixture)
        organization = create_organization(first, fixture)
        org = organization["organization_id"]
        select_organization(first, org)
        _, ticket = invite(first, fixture, org, alias, "admin")
        second = signed_in(browser, fixture, alias)
        assert accept(second, ticket).status == 200
        select_organization(second, org)
        subjects = [(me(context)["user"]["user_id"], headers(context, "concurrent-self-remove-001"),
            {cookie["name"]: cookie["value"] for cookie in context.cookies()}) for context in (first, second)]
        def leave(subject):
            user_id, boundary, cookies = subject
            with httpx.Client(trust_env=False, cookies=cookies, timeout=30) as client:
                return client.delete(ORIGIN + "/api/organizations/" + org + "/members/" + user_id, headers=boundary)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(leave, subjects))
        assert sorted(response.status_code for response in results) == [204, 409]
        survivor = first if results[0].status_code == 409 else second
        assert me(survivor)["role"] == "admin"
        members = survivor.request.get(ORIGIN + "/api/organizations/" + org + "/members").json()["items"]
        assert len(members) == 1 and members[0]["roles"] == ["admin"]
        first.close()
        second.close()


def test_member_scope_and_capability_precede_operation_key_validation(organization_environment):
    fixture, _, _ = organization_environment
    with organization_browser(organization_environment) as (browser, _):
        admin = signed_in(browser, fixture)
        admin_id = me(admin)["user"]["user_id"]
        viewer = signed_in(browser, fixture, "viewer")
        org, foreign = fixture.organizations["A"], fixture.organizations["B"]
        select_organization(viewer, org)
        path = ORIGIN + "/api/organizations/" + org + "/members/"
        denied = viewer.request.patch(path + admin_id, data={"role": "viewer"}, headers=headers(viewer))
        assert denied.status == 403 and denied.json()["error"]["code"] == "forbidden"
        foreign_request = viewer.request.patch(ORIGIN + "/api/organizations/" + foreign + "/members/" + admin_id,
            data={"role": "viewer"}, headers=headers(viewer))
        assert foreign_request.status == 404 and foreign_request.json()["error"]["code"] == "not_found"
        invalid = {**headers(viewer), "Idempotency-Key": "@invalid"}
        absent = viewer.request.delete(path + "absent-user", headers=invalid)
        assert absent.status == 404 and absent.json()["error"]["code"] == "not_found"
        select_organization(admin, org)
        missing = admin.request.patch(path + admin_id, data={"role": "admin"}, headers=headers(admin))
        assert missing.status == 422
        assert me(admin)["role"] == "admin"
        viewer.close()
        admin.close()


def test_expired_invitation_cannot_add_a_member_and_lists_its_expiry(organization_environment):
    from datetime import datetime, timezone
    import time
    from tests.fixtures.auth27 import short_invitation

    fixture, _, _ = organization_environment
    alias = fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, app):
        admin = signed_in(browser, fixture)
        organization = create_organization(admin, fixture)
        org = organization["organization_id"]
        select_organization(admin, org)
        invitation, ticket, expiry = short_invitation(admin, app, fixture, org, alias, seconds=6, key="expire-before-accept-001")
        recipient = signed_in(browser, fixture, alias)
        time.sleep(max(0, (expiry - datetime.now(timezone.utc)).total_seconds()) + 0.3)
        rejected = accept(recipient, ticket)
        assert rejected.status == 409 and rejected.json()["error"]["code"] == "invitation_invalid"
        assert recipient.request.get(ORIGIN + "/api/organizations").json()["items"] == []
        listed = admin.request.get(ORIGIN + "/api/organizations/" + org + "/invitations").json()["items"]
        assert listed == [{**invitation, "status": "expired"}] and ticket not in str(listed)
        assert len(admin.request.get(ORIGIN + "/api/organizations/" + org + "/members").json()["items"]) == 1
        recipient.close()
        admin.close()


def test_cursors_bind_endpoint_actor_and_workspace_and_roles_limit_management(organization_environment):
    fixture, _, _ = organization_environment
    with organization_browser(organization_environment) as (browser, _):
        admin = signed_in(browser, fixture)
        create_organization(admin, fixture)
        page = admin.request.get(ORIGIN + "/api/organizations?limit=1")
        assert page.status == 200 and len(page.json()["items"]) == 1
        cursor = page.json()["next_cursor"]
        assert cursor
        following = admin.request.get(ORIGIN + "/api/organizations", params={"limit": 1, "after": cursor})
        assert following.status == 200 and following.json()["items"] != page.json()["items"]
        other = signed_in(browser, fixture, "u2")
        crossed = other.request.get(ORIGIN + "/api/organizations", params={"after": cursor})
        assert crossed.status == 422 and crossed.json()["error"]["code"] == "invalid_cursor"
        org = fixture.organizations["A"]
        select_organization(admin, org)
        switched = admin.request.get(ORIGIN + "/api/organizations", params={"after": cursor})
        assert switched.status == 422 and switched.json()["error"]["code"] == "invalid_cursor"
        members = admin.request.get(ORIGIN + "/api/organizations/" + org + "/members?limit=1")
        member_cursor = members.json()["next_cursor"]
        assert member_cursor
        mismatch = admin.request.get(ORIGIN + "/api/organizations/" + org + "/invitations", params={"after": member_cursor})
        assert mismatch.status == 422 and mismatch.json()["error"]["code"] == "invalid_cursor"
        for alias in ("cross", "viewer"):
            member = signed_in(browser, fixture, alias)
            select_organization(member, org)
            assert member.request.get(ORIGIN + "/api/organizations/" + org).status == 200
            for collection in ("members", "invitations"):
                assert member.request.get(ORIGIN + "/api/organizations/" + org + "/" + collection).status == 403
                assert member.request.get(ORIGIN + "/api/organizations/" + fixture.organizations["B"] + "/" + collection).status == 404
            assert member.request.post(ORIGIN + "/api/organizations", data={"name": unique_name()}, headers=headers(member, "org-from-org-denied")).status == 403
            member.close()
        other.close()
        admin.close()


def test_current_membership_and_admin_capability_are_checked_before_replay(organization_environment):
    fixture, _, _ = organization_environment
    admin_alias, target_alias = fixture.fresh_mailbox_user(), fixture.fresh_mailbox_user()
    with organization_browser(organization_environment) as (browser, _):
        original = signed_in(browser, fixture)
        organization = create_organization(original, fixture)
        org = organization["organization_id"]
        select_organization(original, org)
        _, ticket = invite(original, fixture, org, admin_alias, "admin")
        current = signed_in(browser, fixture, admin_alias)
        assert accept(current, ticket).status == 200
        select_organization(current, org)
        accepted, _ = invite(original, fixture, org, target_alias, "viewer", key="authority-replay-001")
        original_id = me(original)["user"]["user_id"]
        member_path = ORIGIN + "/api/organizations/" + org + "/members/" + original_id
        changed = current.request.patch(member_path, data={"role": "viewer"}, headers=headers(current, "replay-demote-001"))
        assert changed.status == 200 and me(original)["role"] == "viewer"
        invitation_path = ORIGIN + "/api/organizations/" + org + "/invitations"
        payload = {"email": fixture.users[target_alias].email, "role": "viewer"}
        boundary = headers(original, "authority-replay-001")
        denied = original.request.post(invitation_path, data=payload, headers=boundary)
        assert denied.status == 403 and denied.json()["error"]["code"] == "forbidden"
        assert accepted["invitation_id"] not in denied.text()
        assert current.request.delete(member_path, headers=headers(current, "replay-remove-001")).status == 204
        removed = original.request.post(invitation_path, data=payload, headers=boundary)
        assert removed.status == 404 and removed.json()["error"]["code"] == "not_found"
        assert len(current.request.get(invitation_path).json()["items"]) == 2
        original.close()
        current.close()
