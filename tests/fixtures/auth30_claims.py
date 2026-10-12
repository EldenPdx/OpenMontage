"""Fixed auth30 audience cases; Root supplies private signing and verification."""

import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from urllib.parse import parse_qs
import xml.etree.ElementTree as ET

import pytest

SAML = "{urn:oasis:names:tc:SAML:2.0:assertion}"
SAMLP = "{urn:oasis:names:tc:SAML:2.0:protocol}"
DS = "{http://www.w3.org/2000/09/xmldsig#}"
WRONG_AUDIENCE = "urn:openmontage:auth30:wrong-audience"


def require_saml_audience_helper():
    value = os.environ.get("AUTH30_SAML_AUDIENCE_HELPER")
    if not value:
        pytest.fail("PENDING: Root must supply the private fixed auth30 SAML audience helper")
    path = Path(value)
    if not path.is_absolute():
        pytest.fail("PENDING: auth30 SAML audience helper must be an absolute private path")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                pytest.fail("PENDING: auth30 SAML audience helper must be a private regular file")
    except OSError:
        pytest.fail("PENDING: auth30 SAML audience helper is unavailable")
    return path


def _signed_form(form):
    if not isinstance(form, str) or len(form) > 1_048_576:
        raise ValueError("Invalid fixed SAML form")
    values = parse_qs(form, keep_blank_values=True, strict_parsing=True)
    if set(values) != {"SAMLResponse", "RelayState"} or any(len(value) != 1 for value in values.values()):
        raise ValueError("Invalid fixed SAML form")
    xml = base64.b64decode(values["SAMLResponse"][0], validate=True)
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ValueError("Invalid fixed SAML form")
    response = ET.fromstring(xml)
    assertions = response.findall(SAML + "Assertion")
    if response.tag != SAMLP + "Response" or len(assertions) != 1:
        raise ValueError("Invalid fixed SAML form")
    assertion = assertions[0]
    identifiers = [element.get("ID") for element in response.iter() if element.get("ID") is not None]
    if not response.get("ID") or not assertion.get("ID") or len(identifiers) != len(set(identifiers)):
        raise ValueError("Invalid fixed SAML form")
    if len(response.findall(".//" + DS + "Signature")) != 2:
        raise ValueError("Unexpected fixed SAML signature coverage")
    for element in (response, assertion):
        signatures = element.findall(DS + "Signature")
        if len(signatures) != 1:
            raise ValueError("Missing fixed SAML signature coverage")
        references = signatures[0].findall(DS + "SignedInfo/" + DS + "Reference")
        if len(references) != 1 or references[0].get("URI") != "#" + element.get("ID"):
            raise ValueError("Unexpected fixed SAML signature coverage")
    return values, response, assertion


def _claims(element):
    text = element.text if element.text and element.text.strip() else None
    tail = element.tail if element.tail and element.tail.strip() else None
    return (element.tag, sorted(element.attrib.items()), text, tail,
            [_claims(child) for child in element if child.tag != DS + "Signature"])


def saml_audience_form(helper, fixture, source_form, case):
    """Accept only a control or one changed signed Audience, with Root's receipt."""
    if fixture._data["namespace"] != "auth30" or case not in {"control", "wrong_audience"}:
        pytest.fail("Unsupported fixed auth30 SAML audience case")
    provider = fixture.idp["saml"]
    try:
        source_values, source_response, source_assertion = _signed_form(source_form)
        source_hash = sha256(source_form.encode()).hexdigest()
        request = {"operation": "saml_audience_pair", "namespace": "auth30", "case": case,
                   "source_form": source_form, "source_form_sha256": source_hash,
                   "connector_id": provider["connector_id"], "sp_entity_id": provider["sp_entity_id"],
                   "acs_url": provider["acs_url"], "certificate_sha256": provider["certificate_sha256"]}
        result = subprocess.run([sys.executable, str(helper)], input=json.dumps(request),
                                capture_output=True, text=True, timeout=30, check=True)
        prepared = json.loads(result.stdout)
        if (prepared["namespace"], prepared["case"], prepared["source_form_sha256"]) != ("auth30", case, source_hash):
            raise ValueError("Unbound fixed SAML material")
        values, response, assertion = _signed_form(prepared["form"])
        if values["RelayState"] != source_values["RelayState"]:
            raise ValueError("Changed fixed SAML interaction")
        receipt = prepared["signature_verification"]
        if receipt["certificate_sha256"] != provider["certificate_sha256"] or not receipt["verifier"]:
            raise ValueError("Untrusted fixed SAML verification receipt")
        for name, element in (("response", response), ("assertion", assertion)):
            if receipt[name] != {"verified": True, "reference_uri": "#" + element.get("ID")}:
                raise ValueError("Unverified fixed SAML coverage")
        path = SAML + "Conditions/" + SAML + "AudienceRestriction/" + SAML + "Audience"
        source_audiences, audiences = source_assertion.findall(path), assertion.findall(path)
        expected = provider["sp_entity_id"] if case == "control" else WRONG_AUDIENCE
        if len(source_audiences) != 1 or len(audiences) != 1 or source_audiences[0].text != provider["sp_entity_id"] or audiences[0].text != expected:
            raise ValueError("Unexpected fixed SAML audience")
        audiences[0].text = source_audiences[0].text
        if _claims(response) != _claims(source_response):
            raise ValueError("Changed unrelated fixed SAML claims")
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError):
        pytest.fail("PENDING: fixed auth30 SAML material or its trusted signature receipt is unavailable or invalid", pytrace=False)
    return prepared["form"]
