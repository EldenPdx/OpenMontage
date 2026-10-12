"""Signed protocol faults supplement, never replace, the real successful Logto chain."""

from contextlib import contextmanager
import json
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import jwt
import pytest

from tests.browser.test_studio_submission import serving
from tests.fixtures.auth26 import ORIGIN, auth_browser, identity_environment


@contextmanager
def signed_protocol_fault(config_path, mutate, *, bad_signature=False, missing_id_token=False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="fault-key", alg="RS256", use="sig")
    issuer = None
    nonce = None
    config = json.loads(config_path.read_text())
    provider = FastAPI()

    @provider.get("/oidc/.well-known/openid-configuration")
    def discovery():
        return {"issuer": issuer, "authorization_endpoint": issuer + "/auth", "token_endpoint": issuer + "/token",
                "userinfo_endpoint": issuer + "/me", "jwks_uri": issuer + "/jwks", "end_session_endpoint": issuer + "/session/end",
                "response_types_supported": ["code"], "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"]}

    @provider.get("/oidc/jwks")
    def keys():
        return {"keys": [jwk]}

    @provider.post("/oidc/token")
    async def token(request: Request):
        values = parse_qs((await request.body()).decode())
        if values.get("code") != ["fault-code"] or "code_verifier" not in values:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        now = int(time.time())
        claims = {"iss": issuer, "sub": "fault-subject", "aud": config["app_id"], "iat": now, "exp": now + 600, "nonce": nonce}
        mutate(claims)
        signer = rsa.generate_private_key(public_exponent=65537, key_size=2048) if bad_signature else key
        result = {"access_token": "fault-access", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "fault-refresh"}
        if not missing_id_token:
            result["id_token"] = jwt.encode(claims, signer, algorithm="RS256", headers={"kid": "fault-key"})
        return result

    @provider.get("/oidc/me")
    def userinfo():
        return {"sub": "different-subject"}

    with serving(provider) as origin:
        issuer = origin + "/oidc"
        config.update(issuer=issuer, endpoint=origin, management_endpoint=origin)
        config_path.write_text(json.dumps(config))
        with auth_browser() as browser:
            context = browser.new_context()
            response = context.request.get(ORIGIN + "/api/auth/login", max_redirects=0)
            assert response.status == 302
            query = parse_qs(urlsplit(response.headers["location"]).query)
            nonce = query["nonce"][0]
            try:
                yield context, query["state"][0]
            finally:
                context.close()


def test_nonfinite_id_token_date_is_rejected_as_unverified_identity(identity_environment):
    _, _, config_path = identity_environment
    with signed_protocol_fault(config_path, lambda claims: claims.update(exp=float("inf"))) as (context, state):
        response = context.request.get(ORIGIN + "/api/auth/callback", params={"code": "fault-code", "state": state})
        assert response.status == 401
        assert response.json()["error"]["code"] == "unauthenticated"
        assert context.request.get(ORIGIN + "/api/auth/me").status == 401


@pytest.mark.parametrize("fault", ["missing_nonce", "wrong_nonce", "wrong_issuer", "wrong_audience", "multiple_audience_without_azp",
                                  "wrong_azp", "expired", "missing_iss", "missing_sub", "missing_aud", "missing_exp", "missing_iat",
                                  "future_iat", "boolean_iat", "wrong_signature", "missing_id_token", "userinfo_sub_mismatch"])
def test_signed_protocol_fault_cannot_leave_partial_authenticated_session(identity_environment, fault):
    _, _, config_path = identity_environment

    def mutate(claims):
        if fault.startswith("missing_") and fault != "missing_id_token":
            claims.pop(fault.removeprefix("missing_"))
        elif fault == "wrong_nonce":
            claims["nonce"] = "wrong"
        elif fault == "wrong_issuer":
            claims["iss"] = "https://foreign.example/oidc"
        elif fault == "wrong_audience":
            claims["aud"] = "foreign-app"
        elif fault in {"multiple_audience_without_azp", "wrong_azp"}:
            claims["aud"] = [claims["aud"], "foreign-app"]
            if fault == "wrong_azp":
                claims["azp"] = "foreign-app"
        elif fault == "expired":
            claims["exp"] = int(time.time()) - 60
        elif fault == "future_iat":
            claims["iat"] = int(time.time()) + 60
        elif fault == "boolean_iat":
            claims["iat"] = True

    with signed_protocol_fault(config_path, mutate, bad_signature=fault == "wrong_signature", missing_id_token=fault == "missing_id_token") as (context, state):
        response = context.request.get(ORIGIN + "/api/auth/callback", params={"code": "fault-code", "state": state})
        status = response.status
        assert status == 401
        assert response.json()["error"]["code"] == "unauthenticated"
        assert context.request.get(ORIGIN + "/api/auth/me").status == 401
        replay = context.request.get(ORIGIN + "/api/auth/callback", params={"code": "fault-code", "state": state})
        assert replay.status == 401
