"""Logto's public Management API and private fixture ledger for local acceptance."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STATE = ROOT / "auth-runtime/.state"
CAPABILITIES = {
    "viewer": ["resource:read", "organization:read"],
    "editor": ["resource:read", "production:create", "production:approve", "production:cancel", "production:resume", "organization:read"],
    "admin": ["resource:read", "production:create", "production:approve", "production:cancel", "production:resume", "organization:read", "organization:manage"],
}


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending = path.with_suffix(".pending")
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    path.chmod(0o600)


class ManagementApi:
    def __init__(self, app_id, app_secret):
        self._id, self._secret = app_id, app_secret

    def __repr__(self):
        return "ManagementApi(private local credentials)"

    def request(self, method, path, payload=None, *, missing_ok=False):
        if not path.startswith("/api/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("Fixture requests must use a local Management API path")
        with httpx.Client(trust_env=False, timeout=15) as client:
            token = client.post("http://127.0.0.1:3010/oidc/token", data={
                "grant_type": "client_credentials", "client_id": self._id, "client_secret": self._secret,
                "resource": "https://default.logto.app/api", "scope": "all"})
            if token.status_code != 200:
                raise RuntimeError(f"Local management authentication failed ({token.status_code})")
            response = client.request(method, "http://127.0.0.1:3010" + path,
                                      headers={"Authorization": "Bearer " + token.json()["access_token"]}, json=payload)
            if response.status_code == 404 and missing_ok:
                return None
            if not 200 <= response.status_code < 300:
                body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                code = body.get("code", "unknown")
                code = code if re.fullmatch(r"[a-zA-Z0-9_.]+", str(code)) else "unknown"
                raise RuntimeError(f"Local identity request failed: {method} {path} ({response.status_code}, {code})")
            return response.json() if response.content else None

    def items(self, path):
        # These dedicated fixtures stay small; Logto returns all when pagination is omitted.
        result = self.request("GET", path)
        if not isinstance(result, list):
            raise RuntimeError("Unexpected identity list response")
        return result


@dataclass(frozen=True)
class TestUser:
    id: str
    username: str
    email: str


@dataclass(frozen=True)
class LoginCredentials:
    username: str
    email: str
    password: str = field(repr=False)


@dataclass(repr=False)
class IdentityFixture:
    path: Path
    _data: dict

    def __repr__(self):
        return f"IdentityFixture(namespace={self._data['namespace']!r}, private credentials)"

    @property
    def users(self):
        return {alias: TestUser(value["id"], value["username"], value["email"]) for alias, value in self._data["users"].items()}

    def login_credentials(self, user):
        value = self._data["users"][user]
        return LoginCredentials(value["username"], value["email"], value["password"])

    @property
    def organizations(self):
        return dict(self._data["organizations"])

    @property
    def application(self):
        return {key: value for key, value in self._data["application"].items() if key != "app_secret"}

    def app_for_origin(self, origin):
        """Idempotent exact callback registration; secrets remain in the private ledger."""
        parsed = urlparse(origin)
        if not parsed.port or origin != f"http://127.0.0.1:{parsed.port}":
            raise ValueError("Fixture origin must be an exact trusted loopback URL")
        name = self._data["namespace"] + ":web:" + str(parsed.port)
        api = self.api
        apps = [app for app in api.items("/api/applications") if app["name"] == name]
        if len(apps) > 1:
            raise RuntimeError("Duplicate origin-specific fixture applications")
        callbacks = {"redirectUris": [origin + "/api/auth/callback"], "postLogoutRedirectUris": [origin + "/en"]}
        app = apps[0] if apps else api.request("POST", "/api/applications", {
            "name": name, "type": "Traditional", "oidcClientMetadata": callbacks, "customData": {"auth24": self._data["namespace"]}})
        api.request("PATCH", "/api/applications/" + app["id"], {"oidcClientMetadata": callbacks})
        existing = self._data.setdefault("origin_apps", {}).get(origin)
        if not existing:
            secret = api.request("POST", f"/api/applications/{app['id']}/secrets", {"name": "auth24-origin"})["value"]
            self._data["origin_apps"][origin] = {"app_id": app["id"], "app_secret": secret, "endpoint": "http://127.0.0.1:3010",
                "issuer": self._data["issuer"], "redirect_uri": callbacks["redirectUris"][0], "post_logout_redirect_uri": callbacks["postLogoutRedirectUris"][0]}
            self._data["resources"].append({"path": "/api/applications/" + app["id"], "key": "name", "name": name})
            private_json(self.path, self._data)
        return {key: value for key, value in self._data["origin_apps"][origin].items() if key != "app_secret"}

    def private_app_configuration(self, origin=None):
        """Use only in a trusted SDK/BFF test backend, never serialize to browser DTOs."""
        return dict(self._data["origin_apps"][origin] if origin else self._data["application"])

    @property
    def idp(self):
        return dict(self._data.get("idp", {}))

    def idp_login_credentials(self, protocol):
        data = json.loads(Path(self._data["idp_private_file"]).read_text())["users"][protocol]
        return LoginCredentials(data["username"], data["email"], data["password"])

    @property
    def api(self):
        config = self._data["management"]
        return ManagementApi(config["app_id"], config["app_secret"])

    def organization_roles(self, user, organization):
        roles = self.api.request("GET", f"/api/organizations/{self.organizations[organization]}/users/{self.users[user].id}/roles")
        return sorted(role["name"] for role in roles)

    def set_roles(self, user, organization, roles):
        if any(role not in CAPABILITIES for role in roles):
            raise ValueError("Only the frozen admin/editor/viewer roles may be assigned")
        self.api.request("PUT", f"/api/organizations/{self.organizations[organization]}/users/{self.users[user].id}/roles",
                         {"organizationRoleIds": [self._data["roles"][role] for role in roles]})

    def add_member(self, user, organization, roles):
        self.api.request("POST", f"/api/organizations/{self.organizations[organization]}/users", {"userIds": [self.users[user].id]})
        self.set_roles(user, organization, roles)

    def remove_member(self, user, organization):
        self.api.request("DELETE", f"/api/organizations/{self.organizations[organization]}/users/{self.users[user].id}")

    def suspend(self, user, suspended=True):
        self.api.request("PATCH", f"/api/users/{self.users[user].id}", {"isSuspended": suspended})

    def send_invitation(self, recipient, organization, role):
        if role not in CAPABILITIES or not recipient.endswith("@openmontage.test") or not recipient.startswith(self._data["namespace"]):
            raise ValueError("Invitation must target this fixture's test mailbox")
        invitation = self.api.request("POST", "/api/organization-invitations", {
            "inviterId": self.users["u1" if organization == "A" else "u2"].id, "invitee": recipient,
            "organizationId": self.organizations[organization], "expiresAt": int(time.time() * 1000) + 86400000,
            "organizationRoleIds": [self._data["roles"][role]], "messagePayload": {"link": "http://127.0.0.1:4760/en"}})
        self._data["resources"].append({"path": "/api/organization-invitations/" + invitation["id"], "key": "invitee", "name": recipient})
        private_json(self.path, self._data)
        return {key: invitation[key] for key in ("id", "organizationId", "invitee", "status")}

    def cancel_invitation(self, invitation_id):
        path = "/api/organization-invitations/" + invitation_id
        if path not in [r["path"] for r in self._data["resources"]]:
            raise ValueError("Invitation does not belong to this fixture")
        self.api.request("DELETE", path, missing_ok=True)

    def mail_ids(self):
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.get("http://127.0.0.1:8025/api/v1/messages", params={"limit": 1000})
            response.raise_for_status()
            return {message["ID"] for message in response.json()["messages"]}

    def wait_for_mail(self, recipient, *, after=(), timeout=15):
        deadline = time.monotonic() + timeout
        with httpx.Client(trust_env=False, timeout=5) as client:
            while time.monotonic() < deadline:
                response = client.get("http://127.0.0.1:8025/api/v1/messages", params={"limit": 1000})
                response.raise_for_status()
                for message in response.json()["messages"]:
                    if message["ID"] not in after and any(to["Address"] == recipient for to in message["To"]):
                        result = client.get("http://127.0.0.1:8025/api/v1/message/" + message["ID"])
                        result.raise_for_status()
                        self._data.setdefault("mail_ids_owned", [])
                        if message["ID"] not in self._data["mail_ids_owned"]:
                            self._data["mail_ids_owned"].append(message["ID"])
                            private_json(self.path, self._data)
                        return result.json()
                time.sleep(0.1)
        raise RuntimeError("Expected local test email was not received")


def load_fixture(path=None):
    path = Path(path or os.environ.get("AUTH_TEST_FIXTURE", DEFAULT_STATE / "fixture.json")).resolve()
    if not path.is_file():
        raise RuntimeError("Real identity fixture is missing; run auth-runtime/manage.py seed")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Identity fixture must be private (mode 0600)")
    value = json.loads(path.read_text())
    if value.get("schema_version") != 1 or value.get("issuer") != "http://127.0.0.1:3010/oidc":
        raise RuntimeError("Untrusted identity fixture configuration")
    return IdentityFixture(path, value)


def seed_fixture(state=DEFAULT_STATE, *, namespace="auth24", origin="http://127.0.0.1:4760"):
    state = Path(state)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", namespace):
        raise ValueError("Invalid fixture namespace")
    parsed = urlparse(origin)
    if not parsed.port or origin != f"http://127.0.0.1:{parsed.port}":
        raise ValueError("Fixture application origin must be an exact trusted loopback URL")
    path = state / ("fixture.json" if namespace == "auth24" else f"fixture-{namespace}.json")
    credentials = json.loads((state / "credentials.json").read_text())
    api = ManagementApi(credentials["bootstrap_app_id"], credentials["bootstrap_app_secret"])
    data = json.loads(path.read_text()) if path.exists() else {
        "schema_version": 1, "namespace": namespace, "issuer": "http://127.0.0.1:3010/oidc",
        "users": {}, "organizations": {}, "roles": {}, "resources": []}

    def persist():
        private_json(path, data)

    def reconcile(endpoint, name, payload, *, key="name"):
        matches = [item for item in api.items(endpoint) if item.get(key) == name]
        if len(matches) > 1:
            raise RuntimeError("Duplicate fixture identity resources require operator inspection")
        if matches:
            return matches[0]
        result = api.request("POST", endpoint, payload)
        data["resources"].append({"path": endpoint + "/" + result["id"], "name": name, "key": key})
        persist()
        return result

    scope_ids = {}
    for name in sorted(set(sum(CAPABILITIES.values(), []))):
        scope = reconcile("/api/organization-scopes", name, {"name": name, "description": "OpenMontage frozen capability"})
        scope_ids[name] = scope["id"]
    for name, capabilities in CAPABILITIES.items():
        role = reconcile("/api/organization-roles", name, {"name": name, "description": "OpenMontage " + name,
            "organizationScopeIds": [scope_ids[capability] for capability in capabilities]})
        api.request("PUT", "/api/organization-roles/" + role["id"] + "/scopes", {"organizationScopeIds": [scope_ids[c] for c in capabilities]})
        data["roles"][name] = role["id"]
    for alias in ("u1", "u2", "cross", "viewer"):
        username = namespace.replace("-", "_") + "_" + alias
        password = data["users"].get(alias, {}).get("password") or "Aa9-" + secrets.token_urlsafe(24)
        email = namespace + "." + alias + "@openmontage.test"
        data["users"][alias] = {"id": data["users"].get(alias, {}).get("id"), "username": username, "email": email, "password": password}
        persist()
        user = reconcile("/api/users", username, {"username": username, "primaryEmail": email, "password": password,
            "name": namespace + " " + alias, "customData": {"auth24": namespace}}, key="username")
        data["users"][alias] = {"id": user["id"], "username": username, "email": email, "password": password}
        persist()
    for alias in ("A", "B"):
        name = namespace + ":" + alias
        organization = reconcile("/api/organizations", name, {"name": name, "customData": {"auth24": namespace}})
        data["organizations"][alias] = organization["id"]
        persist()
    app = reconcile("/api/applications", namespace + ":web", {"name": namespace + ":web", "type": "Traditional",
        "oidcClientMetadata": {"redirectUris": [origin + "/api/auth/callback"], "postLogoutRedirectUris": [origin + "/en"]}, "customData": {"auth24": namespace}})
    secret = data.get("application", {}).get("app_secret")
    if not secret:
        secret = api.request("POST", f"/api/applications/{app['id']}/secrets", {"name": "auth24-web"})["value"]
    api.request("PATCH", f"/api/applications/{app['id']}", {"oidcClientMetadata": {
        "redirectUris": [origin + "/api/auth/callback"], "postLogoutRedirectUris": [origin + "/en"]}})
    data["application"] = {"app_id": app["id"], "app_secret": secret, "endpoint": "http://127.0.0.1:3010",
        "issuer": data["issuer"], "redirect_uri": origin + "/api/auth/callback", "post_logout_redirect_uri": origin + "/en"}
    management = reconcile("/api/applications", namespace + ":management", {"name": namespace + ":management", "type": "MachineToMachine", "customData": {"auth24": namespace}})
    secret = data.get("management", {}).get("app_secret")
    if not secret:
        secret = api.request("POST", f"/api/applications/{management['id']}/secrets", {"name": "auth24-management"})["value"]
    roles = [role for role in api.items("/api/roles") if role["name"] == "Logto Management API access"]
    api.request("POST", f"/api/applications/{management['id']}/roles", {"roleIds": [roles[0]["id"]]})
    data["management"] = {"app_id": management["id"], "app_secret": secret, "resource": "https://default.logto.app/api", "scope": "all"}
    persist()
    fixture = load_fixture(path)
    for user, organization, roles in (("u1", "A", ["admin"]), ("u2", "B", ["admin"]),
                                      ("cross", "A", ["editor"]), ("cross", "B", ["viewer"]), ("viewer", "A", ["viewer"])):
        fixture.add_member(user, organization, roles)
    providers = api.items("/api/connector-factories")
    smtp = [provider for provider in providers if provider["id"] == "smtp" or provider.get("name", {}).get("en") == "SMTP"]
    if not smtp:
        raise RuntimeError("Pinned Logto image has no SMTP connector factory")
    existing = [connector for connector in api.items("/api/connectors") if connector.get("connectorId") == smtp[0]["id"]]
    config = {"host": "mailpit", "port": 1025, "auth": {"type": "login"}, "fromEmail": "auth24@openmontage.test", "secure": False,
        "ignoreTLS": True, "disableFileAccess": True, "disableUrlAccess": True, "logger": False, "debug": False,
        "templates": [{"usageType": usage, "contentType": "text/plain", "subject": "OpenMontage " + usage,
            "content": "Your OpenMontage verification code is {{code}}. {{link}}"} for usage in
            ("Register", "SignIn", "ForgotPassword", "Generic", "OrganizationInvitation")]}
    if existing:
        connector = api.request("PATCH", "/api/connectors/" + existing[0]["id"], {"config": config})
    else:
        connector = api.request("POST", "/api/connectors", {"connectorId": smtp[0]["id"], "config": config})
    data["mail_connector_id"] = connector["id"]
    api.request("PATCH", "/api/sign-in-exp", {
        "signIn": {"methods": [
            {"identifier": "email", "password": True, "verificationCode": True, "isPasswordPrimary": True},
            {"identifier": "username", "password": True, "verificationCode": False, "isPasswordPrimary": True}]},
        "signUp": {"identifiers": ["email"], "password": True, "verify": True},
        "forgotPasswordMethods": ["EmailVerificationCode"]})
    persist()
    from .idp import seed_keycloak
    seed_keycloak(state, load_fixture(path))
    return load_fixture(path)


def cleanup_fixture(fixture):
    data = fixture._data
    credentials = json.loads((fixture.path.parent / "credentials.json").read_text())
    api = ManagementApi(credentials["bootstrap_app_id"], credentials["bootstrap_app_secret"])
    namespace = data["namespace"]
    for resource in reversed(data["resources"]):
        if resource["path"].startswith(("/api/organization-roles/", "/api/organization-scopes/")):
            continue  # Shared frozen templates outlive each disposable dataset.
        value = api.request("GET", resource["path"], missing_ok=True)
        if value is None:
            continue
        if resource["key"] == "username":
            prefix = namespace.replace("-", "_") + "_"
        elif resource["key"] in {"name", "connectorName"}:
            prefix = namespace + ":"
        else:
            prefix = (namespace + ".", namespace + "@")
        if value.get(resource["key"]) != resource["name"] or not resource["name"].startswith(prefix):
            raise RuntimeError("Refusing to clean an identity resource outside its recorded namespace")
        api.request("DELETE", resource["path"])
    with httpx.Client(trust_env=False, timeout=10) as client:
        for message_id in data.get("mail_ids_owned", []):
            response = client.get("http://127.0.0.1:8025/api/v1/message/" + message_id)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            if not all(address["Address"].startswith((namespace + ".", namespace + "@")) for address in response.json()["To"]):
                raise RuntimeError("Refusing to delete mail outside the fixture ledger")
            deleted = client.request("DELETE", "http://127.0.0.1:8025/api/v1/messages", json={"IDs": [message_id]})
            deleted.raise_for_status()
    from .idp import cleanup_keycloak
    cleanup_keycloak(fixture.path.parent, fixture)
    fixture.path.unlink(missing_ok=True)
