"""Fixed Management API reads; no caller-controlled gateway or authority cache."""

import asyncio
from datetime import datetime, timezone
from urllib.parse import quote

from production.auth.oidc import provider_json
from production.contracts import ContractViolation

PERSONAL_PERMISSIONS = ["resource:read", "production:create", "production:approve", "production:cancel",
                        "production:resume", "organization:create", "organization:read"]
ROLE_PERMISSIONS = {
    "viewer": {"resource:read", "organization:read"},
    "editor": {"resource:read", "organization:read", "production:create", "production:approve", "production:cancel", "production:resume"},
    "admin": {"resource:read", "organization:read", "production:create", "production:approve", "production:cancel", "production:resume", "organization:manage"},
}


class LogtoAuthority:
    def __init__(self, config):
        self.config = config

    async def _read(self, path):
        token = await provider_json("POST", self.config.issuer + "/token", data={
            "grant_type": "client_credentials", "client_id": self.config.management_app_id,
            "client_secret": self.config.management_app_secret, "resource": self.config.management_resource, "scope": "all"})
        if not isinstance(token, dict) or not isinstance(token.get("access_token"), str) or not token["access_token"]:
            raise ContractViolation("Identity service is unavailable", "identity_unavailable")
        return await provider_json("GET", self.config.management_endpoint + path,
                                   headers={"Authorization": "Bearer " + token["access_token"]})

    async def verify(self, identity, workspace):
        started = datetime.now(timezone.utc)
        try:
            return await asyncio.wait_for(self._verify(identity, workspace, started), timeout=15)
        except asyncio.TimeoutError:
            raise ContractViolation("Identity service is unavailable", "identity_unavailable") from None

    async def _verify(self, identity, workspace, started):
        account = await self._read("/api/users/" + quote(identity.sub, safe=""))
        if not isinstance(account, dict) or account.get("id") != identity.sub or type(account.get("isSuspended")) is not bool:
            raise ContractViolation("Identity service is unavailable", "identity_unavailable")
        if account["isSuspended"]:
            raise ContractViolation("Authentication could not be verified", "unauthenticated")
        if workspace.kind == "personal":
            if workspace.owner_id != identity.user_id:
                raise ContractViolation("Not found", "not_found")
            return {"role": "personal", "permissions": PERSONAL_PERMISSIONS, "name": "Personal", "verified_at": started}
        organizations = []
        for page in range(1, 11):
            batch = await self._read("/api/users/" + quote(identity.sub, safe="") + f"/organizations?page={page}&page_size=100")
            if not isinstance(batch, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("name"), str) for item in batch):
                raise ContractViolation("Identity service is unavailable", "identity_unavailable")
            organizations.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ContractViolation("Identity service is unavailable", "identity_unavailable")
        organization = next((item for item in organizations if item["id"] == workspace.owner_id), None)
        if organization is None:
            raise ContractViolation("Not found", "not_found")
        roles = await self._read("/api/organizations/" + quote(workspace.owner_id, safe="") + "/users/" + quote(identity.sub, safe="") + "/roles")
        if not isinstance(roles, list) or len(roles) > 100 or any(not isinstance(role, dict) or not isinstance(role.get("id"), str) or not isinstance(role.get("name"), str) for role in roles):
            raise ContractViolation("Identity service is unavailable", "identity_unavailable")
        names, permissions = set(), set()
        recognized = [role for role in roles if role["name"] in ROLE_PERMISSIONS]
        if len(recognized) > 3 or len({role["name"] for role in recognized}) != len(recognized):
            raise ContractViolation("Identity service is unavailable", "identity_unavailable")
        for role in recognized:
            scopes = await self._read("/api/organization-roles/" + quote(role["id"], safe="") + "/scopes")
            if not isinstance(scopes, list) or len(scopes) > 100 or any(not isinstance(scope, dict) or not isinstance(scope.get("name"), str) for scope in scopes):
                raise ContractViolation("Identity service is unavailable", "identity_unavailable")
            names.add(role["name"])
            permissions.update({scope["name"] for scope in scopes} & ROLE_PERMISSIONS[role["name"]])
        return {"role": next((name for name in ("admin", "editor", "viewer") if name in names), None),
                "permissions": sorted(permissions), "name": organization["name"], "verified_at": started}
