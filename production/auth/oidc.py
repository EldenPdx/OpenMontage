"""Official Logto flow with private snapshots and the SDK's missing validation."""

import hmac
import math
from urllib.parse import urlsplit

import httpx
import jwt
from logto import LogtoClient, LogtoConfig
from logto.OidcCore import OidcCore
from logto.Storage import Storage
from logto.models.oidc import OidcProviderMetadata
from logto.models.response import TokenResponse, UserInfoResponse

from production.contracts import ContractViolation

ALGORITHMS = {"RS256", "PS256", "ES256", "ES384", "ES512"}


class PrivateStorage(Storage):
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


async def provider_json(method, url, *, data=None, headers=None, token_request=False):
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.request(method, url, data=data, headers=headers)
        if token_request and response.status_code in {400, 401}:
            raise ContractViolation("Authentication could not be verified", "unauthenticated")
        if response.status_code != 200 or len(response.content) > 1_048_576:
            raise ValueError
        return response.json()
    except ContractViolation:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise ContractViolation("Identity service is unavailable", "identity_unavailable") from None


class VerifiedCore(OidcCore):
    def __init__(self, config, metadata, keys, nonce):
        self.config, self.metadata, self.keys, self.nonce = config, metadata, keys, nonce
        self.claims = None

    def verifyIdToken(self, idToken, clientId):
        try:
            header = jwt.get_unverified_header(idToken)
            algorithm = header["alg"]
            if algorithm not in ALGORITHMS or not isinstance(header.get("kid"), str):
                raise ValueError
            matches = [key for key in self.keys if key.get("kid") == header["kid"] and key.get("use", "sig") == "sig"]
            if len(matches) != 1 or matches[0].get("alg", algorithm) != algorithm:
                raise ValueError
            key = jwt.PyJWK.from_dict(matches[0], algorithm=algorithm)
            claims = jwt.decode(idToken, key.key, algorithms=[algorithm], issuer=self.config.issuer,
                                audience=clientId, options={"require": ["iss", "sub", "aud", "iat", "exp"]})
            if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 256:
                raise ValueError
            if any(type(claims[name]) not in (int, float) or not math.isfinite(claims[name]) for name in ("iat", "exp")) or claims["exp"] <= claims["iat"]:
                raise ValueError
            audience = claims["aud"]
            if not isinstance(audience, (str, list)) or not audience or isinstance(audience, list) and any(not isinstance(item, str) for item in audience):
                raise ValueError
            if (isinstance(audience, list) and len(audience) > 1 or "azp" in claims) and claims.get("azp") != clientId:
                raise ValueError
            if self.nonce is not None and (not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"].encode(), self.nonce.encode())):
                raise ValueError
            self.claims = claims
        except (ValueError, TypeError, KeyError, OverflowError, jwt.PyJWTError):
            raise ContractViolation("Authentication could not be verified", "unauthenticated") from None

    async def fetchTokenByCode(self, clientId, clientSecret, redirectUri, code, codeVerifier):
        return TokenResponse.model_validate(await provider_json("POST", self.metadata.token_endpoint, token_request=True,
            data={"grant_type": "authorization_code", "client_id": clientId, "client_secret": clientSecret,
                  "redirect_uri": redirectUri, "code": code, "code_verifier": codeVerifier}))

    async def fetchTokenByRefreshToken(self, clientId, clientSecret, refreshToken, resource=""):
        return TokenResponse.model_validate(await provider_json("POST", self.metadata.token_endpoint, token_request=True,
            data={"grant_type": "refresh_token", "client_id": clientId, "client_secret": clientSecret,
                  "refresh_token": refreshToken, **({"resource": resource} if resource else {})}))

    async def fetchUserInfo(self, accessToken):
        return UserInfoResponse.model_validate(await provider_json("GET", self.metadata.userinfo_endpoint,
            headers={"Authorization": "Bearer " + accessToken}, token_request=True))


class PrivateLogtoClient(LogtoClient):
    def __init__(self, config, storage, *, nonce=None):
        super().__init__(LogtoConfig(endpoint=config.endpoint, appId=config.app_id, appSecret=config.app_secret,
                                   scopes=["email"]), storage=storage)
        self.trust, self.nonce = config, nonce

    async def getOidcCore(self):
        if self._oidcCore is None:
            try:
                data = await provider_json("GET", self.trust.issuer + "/.well-known/openid-configuration")
                if data.get("issuer") != self.trust.issuer:
                    raise ValueError
                base = urlsplit(self.trust.issuer)
                for name in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint", "jwks_uri", "end_session_endpoint"):
                    parsed = urlsplit(data[name])
                    if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc) or not parsed.path.startswith(base.path + "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
                        raise ValueError
                metadata = OidcProviderMetadata.model_validate(data)
                keys = (await provider_json("GET", metadata.jwks_uri))["keys"]
                if not isinstance(keys, list) or not keys or len(keys) > 100:
                    raise ValueError
                self._oidcCore = VerifiedCore(self.trust, metadata, keys, self.nonce)
            except ContractViolation:
                raise
            except (ValueError, TypeError, KeyError):
                raise ContractViolation("Identity service is unavailable", "identity_unavailable") from None
        return self._oidcCore


def safe_return_path(value):
    import re
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value) or any(c in value for c in ("%", "\\", "?", "#")):
        raise ContractViolation("Invalid return path")
    if not re.fullmatch(r"/(?:zh|ja|en)(?:/studio|/p/[a-z0-9][a-z0-9_-]{0,79}|/invitations(?:/accept)?)?", value):
        raise ContractViolation("Invalid return path")
    return value
