"""Controlled signed-claim calibration; native Logto remains the membership authority."""

import base64
from contextlib import contextmanager
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import jwt

from lib.config_model import PiPrice, PiProfile, StudioConfig
from tests.browser.test_studio_submission import serving
from tests.fixtures.auth26 import ORIGIN, auth_browser


class ProtocolCalibration:
    def __init__(self, fixture, configuration, variant):
        self.fixture, self.configuration, self.variant = fixture, configuration, variant
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwk.update(kid="calibration-key", alg="RS256", use="sig")
        self.issuer, self.nonce, self.challenge = None, None, None
        self.fault = False
        self.received = {"userinfo": 0, "management_token": 0, "native_read": 0}
        self.app = FastAPI()
        self.app.get("/oidc/.well-known/openid-configuration")(self.discovery)
        self.app.get("/oidc/jwks")(self.keys)
        self.app.post("/oidc/token")(self.token)
        self.app.get("/oidc/me")(self.userinfo)
        self.app.get("/api/{path:path}")(self.authority_read)

    def discovery(self):
        return {"issuer": self.issuer, "authorization_endpoint": self.issuer + "/auth", "token_endpoint": self.issuer + "/token",
                "userinfo_endpoint": self.issuer + "/me", "jwks_uri": self.issuer + "/jwks", "end_session_endpoint": self.issuer + "/session/end",
                "response_types_supported": ["code"], "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"]}

    def keys(self):
        return {"keys": [self.jwk, self.jwk] if self.fault and self.variant == "ambiguous_kid" else [self.jwk]}

    async def token(self, request: Request):
        values = parse_qs((await request.body()).decode())
        if values.get("grant_type") == ["client_credentials"]:
            self.received["management_token"] += 1
            expected = {"grant_type": "client_credentials", "client_id": self.configuration["management_app_id"],
                        "client_secret": self.configuration["management_app_secret"], "resource": self.configuration["management_resource"], "scope": "all"}
            if values != {key: [value] for key, value in expected.items()}:
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                response = await client.post("http://127.0.0.1:3010/oidc/token", data=expected)
            return JSONResponse(response.json(), status_code=response.status_code)
        verifier = values.get("code_verifier", [""])[0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if values.get("grant_type") != ["authorization_code"] or values.get("code") != ["calibration-code"] or not verifier or challenge != self.challenge:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": self.fixture.users["u1"].id, "aud": self.configuration["app_id"],
                  "iat": now, "exp": now + 600, "nonce": self.nonce}
        if self.variant in {"multiple_audience_without_azp", "wrong_azp"}:
            claims.update(aud=[claims["aud"], "other-calibration-app"], azp=self.configuration["app_id"])
        if self.fault:
            if self.variant.startswith("missing_") and self.variant != "missing_id_token":
                claims.pop(self.variant.removeprefix("missing_"))
            elif self.variant == "multiple_audience_without_azp":
                claims.pop("azp")
            elif self.variant in {"wrong_nonce", "wrong_issuer", "wrong_audience", "wrong_azp"}:
                field = {"wrong_issuer": "iss", "wrong_audience": "aud"}.get(self.variant, self.variant.removeprefix("wrong_"))
                claims[field] = "https://foreign.openmontage.test/oidc" if field == "iss" else "different-calibration-value"
            elif self.variant == "expired":
                claims["exp"] = now - 60
            elif self.variant == "future_iat":
                claims["iat"] = now + 60
            elif self.variant.startswith("nonfinite_"):
                claims[self.variant.removeprefix("nonfinite_")] = float("inf")
            elif self.variant.startswith("boolean_"):
                claims[self.variant.removeprefix("boolean_")] = True
            elif self.variant.startswith("string_"):
                field = self.variant.removeprefix("string_")
                claims[field] = str(claims[field])
            elif self.variant == "empty_sub":
                claims["sub"] = ""
        signer = rsa.generate_private_key(public_exponent=65537, key_size=2048) if self.fault and self.variant == "wrong_signature" else self.key
        algorithm = "HS256" if self.fault and self.variant == "untrusted_algorithm" else "RS256"
        if algorithm == "HS256":
            signer = "controlled-untrusted-algorithm-only"
        kid = "unknown-calibration-key" if self.fault and self.variant == "unknown_kid" else "calibration-key"
        result = {"access_token": "calibration-access", "token_type": "Bearer", "expires_in": 3600}
        if not (self.fault and self.variant == "missing_id_token"):
            result["id_token"] = jwt.encode(claims, signer, algorithm=algorithm, headers={"kid": kid})
        return result

    def userinfo(self):
        self.received["userinfo"] += 1
        return {"sub": "different-calibration-subject" if self.fault and self.variant == "userinfo_sub_mismatch" else self.fixture.users["u1"].id,
                "name": "Controlled claim calibration"}

    async def authority_read(self, path: str, request: Request):
        subject = self.fixture.users["u1"].id
        known = {"users/" + subject, "users/" + subject + "/organizations"}
        known.update("organizations/" + organization + "/users/" + subject + "/roles" for organization in self.fixture.organizations.values())
        known.update("organization-roles/" + role + "/scopes" for role in self.fixture._data["roles"].values())
        if path not in known or not request.headers.get("Authorization", "").startswith("Bearer "):
            return JSONResponse({"error": "not_found"}, status_code=404)
        query = dict(request.query_params)
        if query and (path != "users/" + subject + "/organizations" or set(query) != {"page", "page_size"}
                      or query["page"] not in {str(number) for number in range(1, 11)} or query["page_size"] != "100"):
            return JSONResponse({"error": "invalid_query"}, status_code=400)
        self.received["native_read"] += 1
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.get("http://127.0.0.1:3010/api/" + path, params=query,
                                        headers={"Authorization": request.headers["Authorization"]})
        return JSONResponse(response.json(), status_code=response.status_code)

    def callback(self, context, *, fault=False):
        self.fault = False
        response = context.request.get(ORIGIN + "/api/auth/login", max_redirects=0)
        assert response.status == 302
        query = parse_qs(urlsplit(response.headers["location"]).query)
        self.nonce, self.challenge = query["nonce"][0], query["code_challenge"][0]
        self.fault = fault
        return context.request.get(ORIGIN + "/api/auth/callback", params={"code": "calibration-code", "state": query["state"][0]}, max_redirects=0)


@contextmanager
def protocol_calibration(environment, variant=None):
    fixture, repository, path = environment
    original = path.read_text()
    configuration = json.loads(original)
    provider = ProtocolCalibration(fixture, configuration, variant)
    profile = PiProfile(provider="controlled", base_url="http://127.0.0.1:9999/v1", model="local-model",
                        credential_env="AUTH32_MODEL_KEY", reasoning=False, thinking_level="off", price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
    studio = StudioConfig(enabled=True, default_profile="calibration", profiles={"calibration": profile})
    try:
        with serving(provider.app) as origin:
            provider.issuer = origin + "/oidc"
            configuration.update(endpoint=origin, issuer=provider.issuer, management_endpoint=origin)
            path.write_text(json.dumps(configuration))
            with auth_browser(studio_repository=repository, studio_config=studio, studio_environment={"AUTH32_MODEL_KEY": "local-calibration-only"}) as browser:
                yield browser, provider, fixture
    finally:
        path.write_text(original)
