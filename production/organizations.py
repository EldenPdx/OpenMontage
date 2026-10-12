"""Fixed organization Management operations and durable business reconciliation."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import secrets
from urllib.parse import quote

import httpx

from production.auth.authority import ROLE_PERMISSIONS
from production.contracts import (
    Actor, ContractViolation, ErrorDTO, InvitationCreationMaterial, InvitationDTO, InvitationTransition, OperationScope, OrganizationDTO, MemberDTO,
    OrganizationOperationResult, OrganizationOperationTarget, SafeRemoteRefs, canonical_sha256,
)


def unavailable():
    return ContractViolation("Identity service is unavailable", "identity_unavailable")


class RemoteFailure(Exception):
    def __init__(self, status, code=None):
        self.status, self.code = status, code


class OrganizationManagement:
    """Paths and request payloads are fixed here; callers cannot proxy the M2M identity."""

    def __init__(self, config, *, transport=None):
        self.config, self.transport = config, transport

    async def _request(self, method, path, *, body=None, params=None):
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False, transport=self.transport) as client:
                token = await client.post(self.config.issuer + "/token", data={
                    "grant_type": "client_credentials", "client_id": self.config.management_app_id,
                    "client_secret": self.config.management_app_secret, "resource": self.config.management_resource, "scope": "all"})
                if token.status_code != 200 or not isinstance(token.json().get("access_token"), str):
                    raise unavailable()
                response = await client.request(method, self.config.management_endpoint + path,
                    headers={"Authorization": "Bearer " + token.json()["access_token"]}, json=body, params=params)
                if len(response.content) > 2_000_000:
                    raise unavailable()
                if not 200 <= response.status_code < 300:
                    failure = response.json() if response.status_code == 429 else {}
                    raise RemoteFailure(response.status_code, failure.get("code") if isinstance(failure, dict) else None)
                return response.json() if response.content else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise unavailable() from None

    async def _list(self, path, *, params=None):
        result = []
        for page in range(1, 11):
            items = await self._request("GET", path, params={**(params or {}), "page": page, "page_size": 100})
            if not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in items):
                raise unavailable()
            result.extend(items)
            if len(items) < 100:
                return result
        raise unavailable()

    async def memberships(self, identity):
        return await self._list("/api/users/" + quote(identity.sub, safe="") + "/organizations")

    async def roles(self, organization_id, sub):
        roles = await self._request("GET", "/api/organizations/" + quote(organization_id, safe="") + "/users/" + quote(sub, safe="") + "/roles")
        if not isinstance(roles, list) or len(roles) > 100 or any(not isinstance(role, dict) or not isinstance(role.get("id"), str) or not isinstance(role.get("name"), str) for role in roles):
            raise unavailable()
        return roles

    async def template_role(self, name):
        matches = [role for role in await self._list("/api/organization-roles") if role.get("name") == name and role.get("type", "User") == "User"]
        if len(matches) != 1:
            raise unavailable()
        scopes = await self._request("GET", "/api/organization-roles/" + quote(matches[0]["id"], safe="") + "/scopes")
        if not isinstance(scopes, list) or {scope.get("name") for scope in scopes} != ROLE_PERMISSIONS[name]:
            raise unavailable()
        return matches[0]["id"]

    async def create(self, intent):
        return await self._request("POST", "/api/organizations", body={"name": intent.target.name,
            "customData": {"openmontageOperationId": intent.operation_id}})

    async def created_organization(self, intent):
        matches = [item for item in await self._list("/api/organizations", params={"q": intent.target.name})
                   if item.get("name") == intent.target.name and item.get("customData", {}).get("openmontageOperationId") == intent.operation_id]
        return matches[0] if len(matches) == 1 else None

    async def add_creator(self, organization_id, identity):
        return await self._request("POST", "/api/organizations/" + quote(organization_id, safe="") + "/users", body={"userIds": [identity.sub]})

    async def replace_role(self, organization_id, sub, role_id):
        return await self._request("PUT", "/api/organizations/" + quote(organization_id, safe="") + "/users/" + quote(sub, safe="") + "/roles",
                                   body={"organizationRoleIds": [role_id]})

    async def members(self, organization_id, *, sub=None, role_id=None):
        return await self._list("/api/organizations/" + quote(organization_id, safe="") + "/users",
            params={**({"q": sub} if sub else {}), **({"organizationRoleId": role_id} if role_id else {})})

    async def remove_member(self, organization_id, sub):
        return await self._request("DELETE", "/api/organizations/" + quote(organization_id, safe="") + "/users/" + quote(sub, safe=""))

    async def invitations(self, organization_id, *, email, inviter):
        items = await self._request("GET", "/api/organization-invitations", params={
            "organizationId": organization_id, "invitee": email, "inviterId": inviter})
        if not isinstance(items, list) or len(items) > 1000:
            raise unavailable()
        return items

    async def create_invitation(self, context, target, *, role_id, expires_at, ticket):
        return await self._request("POST", "/api/organization-invitations", body={
            "organizationId": target.organization_id, "inviterId": context.identity.sub,
            "invitee": target.email, "organizationRoleIds": [role_id],
            "expiresAt": int(expires_at.timestamp() * 1000),
            "messagePayload": {"link": self.config.origin + "/en/invitations/accept#ticket=" + ticket}})

    async def invitation(self, invitation_id):
        return await self._request("GET", "/api/organization-invitations/" + quote(invitation_id, safe=""))

    async def accept_invitation(self, invitation_id, identity):
        return await self._request("PUT", "/api/organization-invitations/" + quote(invitation_id, safe="") + "/status",
                                   body={"status": "Accepted", "acceptedUserId": identity.sub})

    async def cancel_invitation(self, invitation_id):
        return await self._request("PUT", "/api/organization-invitations/" + quote(invitation_id, safe="") + "/status", body={"status": "Revoked"})


class OrganizationService:
    def __init__(self, runtime, *, management=None):
        self.runtime, self.store = runtime, runtime.store
        self.management = management or OrganizationManagement(runtime.config)

    async def stored(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)

    @staticmethod
    def scope(context, operation, resource, key):
        return OperationScope(actor=Actor(identity=context.identity, workspace=context.workspace), operation=operation,
                              resource_id=resource, idempotency_key=key)

    def page(self, items, context, endpoint, *, limit, after):
        binding = canonical_sha256({"endpoint": endpoint, "actor": context.identity.user_id,
            "workspace": context.workspace.key, "context": context.context_id})
        key = base64.urlsafe_b64decode(self.runtime.config.encryption_keys[self.runtime.config.active_key_id])
        start = None
        if after:
            try:
                encoded, signature = after.split(".")
                if not hmac.compare_digest(hmac.new(key, encoded.encode(), sha256).hexdigest(), signature):
                    raise ValueError
                value = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
                if value["binding"] != binding or not isinstance(value["after"], str):
                    raise ValueError
                start = value["after"]
            except (ValueError, KeyError, TypeError):
                raise ContractViolation("Invalid cursor", "invalid_cursor") from None
        entries = sorted(items, key=lambda item: item[0])
        entries = [item for item in entries if start is None or item[0] > start]
        selected = entries[:limit]
        cursor = None
        if len(entries) > limit:
            encoded = base64.urlsafe_b64encode(json.dumps({"binding": binding, "after": selected[-1][0]}, separators=(",", ":")).encode()).rstrip(b"=").decode()
            cursor = encoded + "." + hmac.new(key, encoded.encode(), sha256).hexdigest()
        return {"items": [item[1] for item in selected], "next_cursor": cursor}

    async def list_organizations(self, context, **pagination):
        items = []
        for organization in await self.management.memberships(context.identity):
            roles = await self.management.roles(organization["id"], context.identity.sub)
            dto = OrganizationDTO(organization_id=organization["id"], name=organization["name"],
                roles=sorted({role["name"] for role in roles} & ROLE_PERMISSIONS.keys()))
            items.append((dto.organization_id, dto.model_dump(mode="json")))
        return self.page(items, context, "organizations", **pagination)

    async def organization_detail(self, context, name):
        roles = await self.management.roles(context.workspace.owner_id, context.identity.sub)
        return OrganizationDTO(organization_id=context.workspace.owner_id, name=name,
            roles=sorted({role["name"] for role in roles} & ROLE_PERMISSIONS.keys()))

    async def member_dto(self, organization_id, account):
        identity = await self.stored("resolve_user", self.runtime.config.issuer, account["id"])
        roles = await self.management.roles(organization_id, account["id"])
        return MemberDTO(user_id=identity.user_id, name=account.get("name"), email=account.get("primaryEmail"),
                         roles=sorted({role["name"] for role in roles} & ROLE_PERMISSIONS.keys()))

    async def list_members(self, context, **pagination):
        items = []
        for account in await self.management.members(context.workspace.owner_id):
            member = await self.member_dto(context.workspace.owner_id, account)
            items.append((member.user_id, member.model_dump(mode="json")))
        return self.page(items, context, "members", **pagination)

    async def list_invitations(self, context, **pagination):
        actor = Actor(identity=context.identity, workspace=context.workspace)
        invitations = await self.stored("list_invitations", actor, limit=1001)
        if len(invitations) > 1000:
            raise unavailable()
        return self.page([(invitation.invitation_id, invitation.public_dict()) for invitation in invitations], context, "invitations", **pagination)

    async def cancel_invitation(self, context, invitation_id, key):
        actor = Actor(identity=context.identity, workspace=context.workspace)
        invitation = await self.stored("get_invitation", actor, invitation_id)
        scope = self.scope(context, "invitation:cancel", invitation_id, key)
        await self.reconcile_active_member(context, scope)
        target = OrganizationOperationTarget(organization_id=context.workspace.owner_id, invitation_id=invitation_id)
        accepted = await self.stored("begin_organization_intent", scope, request_sha256=canonical_sha256(target), target=target)
        intent = accepted.intent
        if accepted.disposition == "replay":
            return intent.result
        try:
            if intent.stage == "prepared":
                await self.fresh(context, "organization:manage")
                native = await self.management.invitation(invitation_id)
                if invitation.status != "pending" or native.get("status") != "Pending" or native.get("organizationId") != context.workspace.owner_id:
                    return (await self.stored("reject_prepared_organization_intent", scope, expected_revision=intent.revision,
                        result=OrganizationOperationResult(status_code=409, body=ErrorDTO(code="invitation_invalid", message="Invitation unavailable")))).result
                intent = await self.advance(intent, "invite-cancel")
                await self.management.cancel_invitation(invitation_id)
            native = await self.management.invitation(invitation_id)
            if native.get("organizationId") != context.workspace.owner_id or native.get("status") != "Revoked":
                return await self.unknown(intent)
            result = OrganizationOperationResult(status_code=200, body=InvitationDTO(**{**invitation.public_dict(), "status": "cancelled"}))
            return (await self.stored("complete_organization_intent", scope, expected_revision=intent.revision, result=result)).result
        except (RemoteFailure, ContractViolation) as error:
            return await self.failed(intent, error)

    async def fresh(self, context, permission):
        authority = await self.runtime.authority.verify(context.identity, context.workspace)
        if permission not in authority["permissions"]:
            raise ContractViolation("Permission is required", "forbidden")

    async def scoped_member(self, organization_id, user_id):
        identity = await self.stored("get_user", user_id)
        if identity.issuer != self.runtime.config.issuer:
            raise ContractViolation("Not found", "not_found")
        account = next((account for account in await self.management.members(organization_id, sub=identity.sub) if account["id"] == identity.sub), None)
        return identity, account

    async def authorize_member(self, context, organization_id, user_id, operation, key):
        identity, account = await self.scoped_member(organization_id, user_id)
        if account is None:
            try:
                scope = self.scope(context, operation, user_id, key)
            except ValueError:
                raise ContractViolation("Not found", "not_found") from None
            intent = await self.stored("get_organization_intent", scope)
            if intent is None or intent.target.organization_id != organization_id or intent.target.user_id != user_id or intent.status == "prepared":
                raise ContractViolation("Not found", "not_found")
        return identity

    async def reconcile_active_member(self, context, requested_scope):
        active = await self.stored("active_organization_intent", Actor(identity=context.identity, workspace=context.workspace))
        if active is None or active.scope == requested_scope or active.status not in {"submitted", "recovery_required"}:
            return
        if active.stage not in {"member-remove", "member-role"}:
            return
        await self.fresh(context, "organization:manage")
        identity, account = await self.scoped_member(active.organization_id, active.target.user_id)
        if active.stage == "member-remove" and account is None:
            result = OrganizationOperationResult(status_code=204)
        elif active.stage == "member-role" and account is not None:
            role_id = await self.management.template_role(active.target.role)
            if {role["id"] for role in await self.management.roles(active.organization_id, identity.sub)} != {role_id}:
                return
            result = OrganizationOperationResult(status_code=200, body=await self.member_dto(active.organization_id, account))
        else:
            return
        await self.stored("complete_organization_intent", active.scope, expected_revision=active.revision, result=result)

    async def change_member(self, context, user_id, role, key):
        operation = "member:role" if role is not None else "member:remove"
        target = OrganizationOperationTarget(organization_id=context.workspace.owner_id, user_id=user_id, role=role)
        scope = self.scope(context, operation, user_id, key)
        await self.reconcile_active_member(context, scope)
        accepted = await self.stored("begin_organization_intent", scope, request_sha256=canonical_sha256(target), target=target)
        intent = accepted.intent
        if accepted.disposition == "replay":
            return intent.result
        try:
            identity, account = await self.scoped_member(target.organization_id, user_id)
            requested = await self.management.template_role(role) if role else None
            if intent.stage == "prepared":
                await self.fresh(context, "organization:manage")
                if account is None:
                    raise ContractViolation("Not found", "not_found")
                current = await self.management.roles(target.organization_id, identity.sub)
                admin = await self.management.template_role("admin")
                if admin in {item["id"] for item in current} and requested != admin:
                    admins = await self.management.members(target.organization_id, role_id=admin)
                    if len({item["id"] for item in admins}) < 2:
                        return (await self.stored("reject_prepared_organization_intent", scope, expected_revision=intent.revision,
                            result=OrganizationOperationResult(status_code=409, body=ErrorDTO(code="last_admin", message="The final administrator cannot be removed or downgraded")))).result
                if requested not in {item["id"] for item in current} or role is None:
                    await self.stored("invalidate_authority", [user_id], context.workspace)
                intent = await self.advance(intent, "member-role" if role else "member-remove")
                if role:
                    await self.management.replace_role(target.organization_id, identity.sub, requested)
                else:
                    await self.management.remove_member(target.organization_id, identity.sub)
                _, account = await self.scoped_member(target.organization_id, user_id)
            if role:
                if account is None or {item["id"] for item in await self.management.roles(target.organization_id, identity.sub)} != {requested}:
                    return await self.unknown(intent)
                result = OrganizationOperationResult(status_code=200, body=await self.member_dto(target.organization_id, account))
            else:
                if account is not None:
                    return await self.unknown(intent)
                result = OrganizationOperationResult(status_code=204)
            return (await self.stored("complete_organization_intent", scope, expected_revision=intent.revision, result=result)).result
        except (RemoteFailure, ContractViolation) as error:
            return await self.failed(intent, error)

    async def create_invitation(self, context, email, role, key):
        target = OrganizationOperationTarget(organization_id=context.workspace.owner_id, email=email, role=role)
        scope = self.scope(context, "invitation:create", context.workspace.owner_id, key)
        await self.reconcile_active_member(context, scope)
        accepted = await self.stored("begin_organization_intent", scope, request_sha256=canonical_sha256(target), target=target)
        intent = accepted.intent
        if accepted.disposition == "replay":
            return intent.result
        try:
            role_id = await self.management.template_role(role)
            material = await self.stored("invitation_creation_material", scope)
            if intent.status == "prepared" or intent.stage == "invite-throttled":
                if material is None:
                    previous = await self.management.invitations(target.organization_id, email=email, inviter=context.identity.sub)
                    if any(item.get("status") == "Pending" and item.get("expiresAt", 0) > datetime.now(timezone.utc).timestamp() * 1000 for item in previous):
                        return (await self.stored("reject_prepared_organization_intent", scope, expected_revision=intent.revision,
                            result=OrganizationOperationResult(status_code=409, body=ErrorDTO(code="invitation_invalid", message="Invitation unavailable")))).result
                    expires = datetime.fromtimestamp(int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp() * 1000) / 1000, timezone.utc)
                    material = InvitationCreationMaterial(ticket=secrets.token_urlsafe(32), expires_at=expires,
                        organization_role_id=role_id, prior_native_invitation_ids=tuple(sorted(item["id"] for item in previous)))
                    intent = await self.stored("prepare_invitation_creation", scope, expected_revision=intent.revision, material=material)
                if intent.stage == "invite-throttled":
                    previous = await self.management.invitations(target.organization_id, email=email, inviter=context.identity.sub)
                    if any(item["id"] not in material.prior_native_invitation_ids for item in previous):
                        return await self.unknown(intent)
                    if material.expires_at <= datetime.now(timezone.utc):
                        return (await self.stored("complete_organization_intent", scope, expected_revision=intent.revision,
                            result=OrganizationOperationResult(status_code=409, body=ErrorDTO(code="invitation_invalid", message="Invitation unavailable")))).result
                if material.organization_role_id != role_id:
                    raise unavailable()
                await self.fresh(context, "organization:manage")
                intent = await self.advance(intent, "invite-create")
                try:
                    await self.management.create_invitation(context, target, role_id=role_id, expires_at=material.expires_at, ticket=material.ticket)
                except RemoteFailure as error:
                    if error.status != 429 or error.code != "request.message_rate_limited":
                        raise
                    # Logto's message guard rejects before mail and rolls back this invitation transaction.
                    await self.stored("advance_organization_intent", scope, expected_revision=intent.revision,
                        stage="invite-throttled", status="recovery_required")
                    return OrganizationOperationResult(status_code=503, body=ErrorDTO(code="identity_unavailable",
                        message="Invitation mail is temporarily limited", retryable=True, recovery_actions=["reconcile"]))
            if material is None:
                return await self.unknown(intent)
            candidates = [item for item in await self.management.invitations(target.organization_id, email=email, inviter=context.identity.sub)
                if item.get("id") not in material.prior_native_invitation_ids and item.get("organizationId") == target.organization_id
                and item.get("inviterId") == context.identity.sub and item.get("invitee", "").casefold() == email
                and item.get("expiresAt") == int(material.expires_at.timestamp() * 1000)
                and {item["id"] for item in item.get("organizationRoles", [])} == {material.organization_role_id}]
            if len(candidates) != 1 or candidates[0].get("status") != "Pending":
                return await self.unknown(intent)
            native = candidates[0]
            intent = await self.advance(intent, "invite-confirmed", refs=SafeRemoteRefs(organization_id=target.organization_id, invitation_id=native["id"]))
            invitation = InvitationDTO(invitation_id=native["id"], organization_id=target.organization_id, email=email,
                                       role=role, expires_at=material.expires_at)
            await self.stored("store_invitation", scope, expected_revision=intent.revision, invitation=invitation, ticket=material.ticket)
            return (await self.stored("complete_organization_intent", scope, expected_revision=intent.revision,
                                     result=OrganizationOperationResult(status_code=201, body=invitation))).result
        except (RemoteFailure, ContractViolation) as error:
            return await self.failed(intent, error)

    async def accept_invitation(self, context, user, ticket, key):
        if user["email_verified"] is not True or not user["email"]:
            raise ContractViolation("Invitation unavailable", "invitation_invalid")
        scope = self.scope(context, "invitation:accept", "invitations", key)
        invitation = await self.stored("invitation_for_acceptance", scope, ticket=ticket, verified_email=user["email"])
        target = OrganizationOperationTarget(organization_id=invitation.organization_id, invitation_id=invitation.invitation_id,
                                              user_id=context.identity.user_id)
        accepted = await self.stored("begin_organization_intent", scope, request_sha256=canonical_sha256(target), target=target)
        intent = accepted.intent
        if accepted.disposition == "replay":
            return intent.result
        try:
            role_id = await self.management.template_role(invitation.role)
            if intent.stage == "prepared":
                native = await self.management.invitation(invitation.invitation_id)
                if native.get("status") != "Pending" or native.get("organizationId") != invitation.organization_id:
                    raise ContractViolation("Invitation unavailable", "invitation_invalid")
                await self.runtime.authority.verify(context.identity, context.workspace)
                intent = await self.advance(intent, "invite-accept")
                await self.management.accept_invitation(invitation.invitation_id, context.identity)
            native = await self.management.invitation(invitation.invitation_id)
            memberships = await self.management.memberships(context.identity)
            organization = next((item for item in memberships if item["id"] == invitation.organization_id), None)
            roles = await self.management.roles(invitation.organization_id, context.identity.sub) if organization else []
            if native.get("status") != "Accepted" or native.get("acceptedUserId") != context.identity.sub or organization is None or {role["id"] for role in roles} != {role_id}:
                return await self.unknown(intent)
            if native.get("organizationId") != invitation.organization_id or native.get("expiresAt") != int(invitation.expires_at.timestamp() * 1000) or not isinstance(native.get("updatedAt"), (int, float)):
                return await self.unknown(intent)
            result = OrganizationOperationResult(status_code=200, body=OrganizationDTO(organization_id=invitation.organization_id,
                name=organization["name"], roles=[invitation.role]))
            transition = InvitationTransition(invitation_id=invitation.invitation_id, status="accepted",
                accepted_at=datetime.fromtimestamp(native["updatedAt"] / 1000, timezone.utc), accepted_user_id=native["acceptedUserId"])
            return (await self.stored("complete_organization_intent", scope, expected_revision=intent.revision, result=result,
                invitation_transition=transition)).result
        except (RemoteFailure, ContractViolation) as error:
            return await self.failed(intent, error)

    async def failed(self, intent, error):
        if isinstance(error, ContractViolation) and error.code in {"state_conflict", "idempotency_conflict"}:
            raise error
        if intent.status != "prepared":
            return await self.unknown(intent)
        codes = {"unauthenticated": 401, "forbidden": 403, "not_found": 404, "invitation_invalid": 409}
        if isinstance(error, ContractViolation) and error.code in codes:
            result = OrganizationOperationResult(status_code=codes[error.code], body=ErrorDTO(code=error.code, message=str(error)))
            return (await self.stored("reject_prepared_organization_intent", intent.scope,
                expected_revision=intent.revision, result=result)).result
        raise unavailable() from None

    async def advance(self, intent, stage, *, refs=None):
        return await self.stored("advance_organization_intent", intent.scope, expected_revision=intent.revision,
                                 stage=stage, status="submitted", remote_refs=refs)

    async def unknown(self, intent):
        await self.stored("advance_organization_intent", intent.scope, expected_revision=intent.revision,
                          stage=intent.stage, status="recovery_required")
        return OrganizationOperationResult(status_code=503, body=ErrorDTO(code="outcome_unknown",
            message="Organization operation requires reconciliation", recovery_actions=["reconcile"]))

    async def create_organization(self, context, name, key):
        scope = self.scope(context, "organization:create", "organizations", key)
        target = OrganizationOperationTarget(name=name)
        accepted = await self.stored("begin_organization_intent", scope, request_sha256=canonical_sha256(target), target=target)
        intent = accepted.intent
        if accepted.disposition == "replay":
            return intent.result
        try:
            admin = await self.management.template_role("admin")
            if intent.stage == "prepared":
                await self.runtime.authority.verify(context.identity, context.workspace)
                intent = await self.advance(intent, "create")
                await self.management.create(intent)
            if intent.stage == "create":
                organization = await self.management.created_organization(intent)
                if organization is None:
                    return await self.unknown(intent)
                await self.stored("remember_verified_organization", organization["id"], organization["name"])
                intent = await self.advance(intent, "membership-ready", refs=SafeRemoteRefs(organization_id=organization["id"]))
            if intent.stage == "membership-ready":
                await self.runtime.authority.verify(context.identity, context.workspace)
                intent = await self.advance(intent, "creator-membership")
                await self.management.add_creator(intent.organization_id, context.identity)
            if intent.stage == "creator-membership":
                memberships = await self.management.memberships(context.identity)
                if not any(item["id"] == intent.organization_id for item in memberships):
                    return await self.unknown(intent)
                intent = await self.advance(intent, "admin-ready")
            if intent.stage == "admin-ready":
                await self.runtime.authority.verify(context.identity, context.workspace)
                intent = await self.advance(intent, "creator-admin")
                await self.management.replace_role(intent.organization_id, context.identity.sub, admin)
            if intent.stage == "creator-admin":
                roles = await self.management.roles(intent.organization_id, context.identity.sub)
                if {role["id"] for role in roles} != {admin}:
                    return await self.unknown(intent)
                result = OrganizationOperationResult(status_code=201, body=OrganizationDTO(
                    organization_id=intent.organization_id, name=name, roles=["admin"]))
                return (await self.stored("complete_organization_intent", scope, expected_revision=intent.revision, result=result)).result
        except (RemoteFailure, ContractViolation) as error:
            return await self.failed(intent, error)
        return await self.unknown(intent)
