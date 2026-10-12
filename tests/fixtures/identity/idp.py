"""Provision the pinned standard Keycloak IdP; keys remain in its private database."""

import base64
import json
from pathlib import Path
import secrets
import xml.etree.ElementTree as ET

from cryptography import x509
from cryptography.hazmat.primitives import hashes
import httpx

from .environment import private_json


def seed_keycloak(state, fixture):
    state = Path(state)
    namespace = fixture._data["namespace"]
    credentials = json.loads((state / "credentials.json").read_text())
    path = state / ("idp.json" if namespace == "auth24" else f"idp-{namespace}.json")
    private = json.loads(path.read_text()) if path.exists() else {"users": {}, "oidc_secret": secrets.token_urlsafe(32)}
    base = "http://127.0.0.1:8180"
    issuer = base + "/realms/" + namespace
    api = fixture.api
    with httpx.Client(trust_env=False, timeout=20) as client:
        token = client.post(base + "/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli", "username": "auth24-admin", "password": credentials["keycloak_admin"]})
        if token.status_code != 200:
            raise RuntimeError("Private Keycloak administration authentication failed")
        headers = {"Authorization": "Bearer " + token.json()["access_token"]}

        def request(method, endpoint, payload=None, *, missing_ok=False):
            response = client.request(method, base + endpoint, headers=headers, json=payload)
            if response.status_code == 404 and missing_ok:
                return None
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"Local IdP operation failed ({method}, {response.status_code})")
            return response.json() if response.content else True

        realm_path = "/admin/realms/" + namespace
        realm = request("GET", realm_path, missing_ok=True)
        if realm is not None and realm.get("attributes", {}).get("openmontageAuthFixture") != namespace:
            raise RuntimeError("IdP realm does not belong to this fixture")
        if realm is None:
            request("POST", "/admin/realms", {"realm": namespace, "enabled": True, "sslRequired": "none",
                "registrationAllowed": False, "duplicateEmailsAllowed": True, "loginWithEmailAllowed": True,
                "attributes": {"openmontageAuthFixture": namespace}})
        for protocol in ("oidc", "saml", "same-email"):
            username = namespace + "-" + protocol
            domain = protocol + ".openmontage.test" if namespace == "auth24" else namespace + "." + protocol + ".openmontage.test"
            email = fixture.users["u1"].email if protocol == "same-email" else namespace + "@" + domain
            password = private["users"].get(protocol, {}).get("password") or "Aa9-" + secrets.token_urlsafe(24)
            private["users"][protocol] = {"username": username, "email": email, "password": password}
            private_json(path, private)
            existing = request("GET", realm_path + "/users?username=" + username + "&exact=true")
            if not existing:
                request("POST", realm_path + "/users", {"username": username, "email": email, "emailVerified": True,
                    "enabled": True, "firstName": "OpenMontage", "lastName": protocol,
                    "credentials": [{"type": "password", "value": password, "temporary": False}]})
                existing = request("GET", realm_path + "/users?username=" + username + "&exact=true")
            if len(existing) != 1:
                raise RuntimeError("Ambiguous IdP fixture user")
            private["users"][protocol]["id"] = existing[0]["id"]
            private_json(path, private)
        connectors = api.items("/api/sso-connectors")
        output = {}
        for protocol, provider in (("oidc", "OIDC"), ("saml", "SAML")):
            name = namespace + ":" + protocol
            matches = [connector for connector in connectors if connector["connectorName"] == name]
            config = {"issuer": issuer, "clientId": namespace + "-logto-oidc", "clientSecret": private["oidc_secret"], "trustUnverifiedEmail": False} if protocol == "oidc" else {
                "metadataUrl": issuer + "/protocol/saml/descriptor", "signAuthnRequest": False}
            if matches:
                connector = api.request("PATCH", "/api/sso-connectors/" + matches[0]["id"], {"config": config})
            else:
                domain = protocol + ".openmontage.test" if namespace == "auth24" else namespace + "." + protocol + ".openmontage.test"
                connector = api.request("POST", "/api/sso-connectors", {"providerName": provider, "connectorName": name,
                    "domains": [domain], "config": config, "syncProfile": False, "enableTokenStorage": False})
                fixture._data["resources"].append({"path": "/api/sso-connectors/" + connector["id"], "name": name, "key": "connectorName"})
                private_json(fixture.path, fixture._data)
            full = api.request("GET", "/api/sso-connectors/" + connector["id"])
            if protocol == "oidc":
                redirect = "http://127.0.0.1:3010/callback/" + connector["id"]
                spec = {"clientId": namespace + "-logto-oidc", "protocol": "openid-connect", "enabled": True,
                    "publicClient": False, "secret": private["oidc_secret"], "standardFlowEnabled": True,
                    "directAccessGrantsEnabled": False, "redirectUris": [redirect], "webOrigins": ["http://127.0.0.1:3010"]}
                output[protocol] = {"connector_id": connector["id"], "client_id": spec["clientId"], "issuer": issuer,
                    "discovery_url": issuer + "/.well-known/openid-configuration", "redirect_uri": redirect}
            else:
                provider_details = full["providerConfig"]
                sp = provider_details["serviceProvider"]
                spec = {"clientId": sp["entityId"], "protocol": "saml", "enabled": True,
                    "redirectUris": [sp["assertionConsumerServiceUrl"]], "attributes": {
                        "saml_assertion_consumer_url_post": sp["assertionConsumerServiceUrl"], "saml.force.post.binding": "true",
                        "saml.server.signature": "true", "saml.assertion.signature": "true", "saml.client.signature": "false",
                        "saml.signature.algorithm": "RSA_SHA256", "saml.name_id.format": "persistent", "saml.force.name.id.format": "true"},
                    "protocolMappers": [{"name": name, "protocol": "saml", "protocolMapper": "saml-user-property-mapper",
                        "config": {"attribute.nameformat": "Basic", "attribute.name": name, "friendly.name": name, "user.attribute": attribute}}
                        for name, attribute in (("email", "email"), ("name", "firstName"))]}
                metadata = client.get(issuer + "/protocol/saml/descriptor")
                metadata.raise_for_status()
                xml = ET.fromstring(metadata.text)
                certificate = xml.find(".//{http://www.w3.org/2000/09/xmldsig#}X509Certificate")
                cert = x509.load_der_x509_certificate(base64.b64decode(certificate.text))
                output[protocol] = {"connector_id": connector["id"], "client_id": spec["clientId"],
                    "metadata_url": issuer + "/protocol/saml/descriptor", "certificate_sha256": cert.fingerprint(hashes.SHA256()).hex(),
                    "sp_entity_id": sp["entityId"], "acs_url": sp["assertionConsumerServiceUrl"]}
            clients = request("GET", realm_path + "/clients?clientId=" + spec["clientId"])
            if len(clients) > 1:
                raise RuntimeError("Duplicate IdP clients")
            if clients:
                request("PUT", realm_path + "/clients/" + clients[0]["id"], {**spec, "id": clients[0]["id"]})
            else:
                request("POST", realm_path + "/clients", spec)
        output["users"] = {name: {k: v for k, v in user.items() if k != "password"} for name, user in private["users"].items()}
        fixture._data["idp"] = output
        fixture._data["idp_private_file"] = str(path)
        private_json(fixture.path, fixture._data)
        api.request("PATCH", "/api/sign-in-exp", {"singleSignOnEnabled": True})
    return output


def cleanup_keycloak(state, fixture):
    private_file = fixture._data.get("idp_private_file")
    if not private_file:
        return
    namespace = fixture._data["namespace"]
    credentials = json.loads((Path(state) / "credentials.json").read_text())
    base = "http://127.0.0.1:8180"
    with httpx.Client(trust_env=False, timeout=20) as client:
        token = client.post(base + "/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli", "username": "auth24-admin", "password": credentials["keycloak_admin"]})
        if token.status_code != 200:
            raise RuntimeError("Private IdP cleanup authentication failed")
        headers = {"Authorization": "Bearer " + token.json()["access_token"]}
        realm = client.get(base + "/admin/realms/" + namespace, headers=headers)
        if realm.status_code != 404:
            realm.raise_for_status()
            if realm.json().get("attributes", {}).get("openmontageAuthFixture") != namespace:
                raise RuntimeError("Refusing to delete an IdP realm outside the fixture ledger")
            response = client.delete(base + "/admin/realms/" + namespace, headers=headers)
            response.raise_for_status()
    Path(private_file).unlink(missing_ok=True)
