"""Administrator-only auth configuration, separate from production artifacts."""

import base64
import ipaddress
import json
import os
from pathlib import Path
import stat
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, model_validator

from production.contracts import ContractModel, ContractViolation


def trusted_url(value, *, development=False, origin_only=False):
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or "\\" in value or any(ord(c) < 33 for c in value):
        raise ValueError("Invalid trusted URL")
    if parsed.scheme != "https":
        if parsed.scheme != "http" or not development or not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError("Trusted URL requires HTTPS")
    if origin_only and (parsed.path or value != f"{parsed.scheme}://{parsed.netloc}"):
        raise ValueError("Origin must be exact")
    return parsed


class RootProvenance(ContractModel):
    canonical_root: str
    classification: Literal["file_only_legacy", "adopted_or_restored", "authenticated_or_unknown"]
    expected_binding: str | None = None

    @model_validator(mode="after")
    def validate_binding(self):
        if not Path(self.canonical_root).is_absolute():
            raise ValueError("Root classification must use an absolute path")
        if self.expected_binding is not None:
            UUID(self.expected_binding)
        if self.classification == "file_only_legacy" and self.expected_binding is not None:
            raise ValueError("File-only legacy classification cannot assert a database")
        return self


class AuthConfig(ContractModel):
    mode: Literal["local", "authenticated"] = "local"
    origin: str = ""
    issuer: str = ""
    endpoint: str = ""
    app_id: str = ""
    app_secret: str = Field(default="", repr=False)
    redirect_uri: str = ""
    post_logout_redirect_uri: str = ""
    management_endpoint: str = ""
    management_resource: str = ""
    management_app_id: str = ""
    management_app_secret: str = Field(default="", repr=False)
    database_url: str = Field(default="", repr=False)
    database_schema: str = "studio"
    encryption_keys: dict[str, str] = Field(default_factory=dict, repr=False)
    active_key_id: str = ""
    http_development: bool = False
    trusted_proxy_ips: list[str] = Field(default_factory=list)
    absolute_seconds: int = Field(default=86400, strict=True, ge=1, le=86400)
    idle_seconds: int = Field(default=28800, strict=True, ge=1, le=28800)
    root_provenance: RootProvenance | None = None

    @model_validator(mode="after")
    def validate_trust(self):
        if self.mode == "local":
            return self
        required = (self.origin, self.issuer, self.endpoint, self.app_id, self.app_secret,
                    self.management_endpoint, self.management_resource, self.management_app_id,
                    self.management_app_secret, self.database_url, self.active_key_id)
        if not all(required) or self.active_key_id not in self.encryption_keys:
            raise ValueError("Required authentication trust is missing")
        trusted_url(self.origin, development=self.http_development, origin_only=True)
        for value in (self.issuer, self.endpoint, self.management_endpoint):
            trusted_url(value, development=self.http_development)
        if self.issuer != self.endpoint + "/oidc" or self.management_endpoint != self.endpoint:
            raise ValueError("Identity endpoints do not match configured Logto")
        if self.redirect_uri != self.origin + "/api/auth/callback" or self.post_logout_redirect_uri not in {self.origin + "/" + locale for locale in ("zh", "ja", "en")}:
            raise ValueError("Callback or logout redirect is not trusted")
        for value in self.trusted_proxy_ips:
            ipaddress.ip_address(value)
        if any(len(base64.b64decode(key, altchars=b"-_", validate=True)) != 32 for key in self.encryption_keys.values()):
            raise ValueError("Invalid private session key")
        return self

    @classmethod
    def load(cls, environment=None):
        environment = os.environ if environment is None else environment
        values = {}
        try:
            if environment.get("OPENMONTAGE_AUTH_CONFIG_FILE"):
                path = Path(environment["OPENMONTAGE_AUTH_CONFIG_FILE"])
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "r") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 32768:
                        raise ValueError("Private auth configuration is unsafe")
                    values = json.load(source)
            if "OPENMONTAGE_AUTH_MODE" in environment:
                values["mode"] = environment["OPENMONTAGE_AUTH_MODE"]
            if environment.get("OPENMONTAGE_AUTH_CONFIG_FILE") and values.get("mode", "local") == "local" and "root_provenance" not in values:
                raise ValueError("Configured local roots require trusted classification")
            return cls.model_validate(values)
        except (ValueError, TypeError, OSError):
            raise ContractViolation("Identity service is unavailable", "identity_unavailable") from None
