"""Signed protocol faults supplement, never replace, the real successful Logto chain."""

from contextlib import contextmanager
import json
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import jwt

from tests.browser.test_studio_submission import serving
from tests.integration.test_auth_bff import ORIGIN, auth_browser, identity_environment


@contextmanager
def signed_protocol_fault(config_path, mutate):
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
        return {"access_token": "fault-access", "token_type": "Bearer", "expires_in": 3600,
                "id_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fault-key"}), "refresh_token": "fault-refresh"}

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
