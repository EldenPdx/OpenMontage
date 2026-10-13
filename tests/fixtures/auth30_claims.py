"""Fixed auth30 SAML cases; Root supplies private signing and verification."""

import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from urllib.parse import parse_qs, urlencode
import xml.etree.ElementTree as ET

import pytest

SAML = "{urn:oasis:names:tc:SAML:2.0:assertion}"
SAMLP = "{urn:oasis:names:tc:SAML:2.0:protocol}"
DS = "{http://www.w3.org/2000/09/xmldsig#}"
WRONG_AUDIENCE = "urn:openmontage:auth30:wrong-audience"
SAML_CLAIM_CASES = (
    "wrong_assertion_issuer", "conditions_expired", "bearer_expired", "wrong_recipient",
    "wrong_bearer_in_response_to", "wrong_response_in_response_to", "wrong_destination",
)


def require_saml_claim_helper():
    value = os.environ.get("AUTH30_SAML_AUDIENCE_HELPER")
    if not value:
        pytest.fail("PENDING: Root must supply the private fixed auth30 SAML claim helper")
    path = Path(value)
    if not path.is_absolute():
        pytest.fail("PENDING: auth30 SAML claim helper must be an absolute private path")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                pytest.fail("PENDING: auth30 SAML claim helper must be a private regular file")
    except OSError:
        pytest.fail("PENDING: auth30 SAML claim helper is unavailable")
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


def saml_claim_form(helper, fixture, source_form, case):
    """Accept only the fixed claim deltas and Root's trusted signature receipt."""
    if fixture._data["namespace"] != "auth30" or case not in {"control", "wrong_audience", *SAML_CLAIM_CASES}:
        pytest.fail("Unsupported fixed auth30 SAML claim case")
    provider = fixture.idp["saml"]
    try:
        source_values, source_response, _ = _signed_form(source_form)
        source_hash = sha256(source_form.encode()).hexdigest()
        request = {"operation": "saml_claim_pair", "namespace": "auth30", "case": case,
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
        audience = SAML + "Assertion/" + SAML + "Conditions/" + SAML + "AudienceRestriction/" + SAML + "Audience"
        source_audiences = source_response.findall(audience)
        if len(source_audiences) != 1 or source_audiences[0].text != provider["sp_entity_id"]:
            raise ValueError("Unexpected fixed SAML audience")
        conditions = SAML + "Assertion/" + SAML + "Conditions"
        bearer = (SAML + "Assertion/" + SAML + "Subject/" + SAML + "SubjectConfirmation"
                  "[@Method='urn:oasis:names:tc:SAML:2.0:cm:bearer']/" + SAML + "SubjectConfirmationData")
        changes = {
            "control": (),
            "wrong_audience": ((audience, None, WRONG_AUDIENCE),),
            "wrong_assertion_issuer": ((SAML + "Assertion/" + SAML + "Issuer", None,
                                        "urn:openmontage:auth30:wrong-issuer"),),
            "conditions_expired": ((conditions, "NotBefore", "1999-12-31T23:00:00.000Z"),
                                   (conditions, "NotOnOrAfter", "2000-01-01T00:00:00.000Z")),
            "bearer_expired": ((bearer, "NotOnOrAfter", "2000-01-01T00:00:00.000Z"),),
            "wrong_recipient": ((bearer, "Recipient", provider["acs_url"] + "/auth30-recipient-mismatch"),),
            "wrong_bearer_in_response_to": ((bearer, "InResponseTo", "_openmontage_auth30_wrong_bearer_request"),),
            "wrong_response_in_response_to": (("", "InResponseTo", "_openmontage_auth30_wrong_response_request"),),
            "wrong_destination": (("", "Destination", provider["acs_url"] + "/auth30-destination-mismatch"),),
        }
        for path, attribute, expected in changes[case]:
            originals = source_response.findall(path) if path else [source_response]
            targets = response.findall(path) if path else [response]
            if len(originals) != 1 or len(targets) != 1:
                raise ValueError("Ambiguous fixed SAML claim")
            original, target = originals[0], targets[0]
            before = original.get(attribute) if attribute else original.text
            actual = target.get(attribute) if attribute else target.text
            if before is None or before == expected or actual != expected:
                raise ValueError("Unexpected fixed SAML claim delta")
            if attribute:
                target.set(attribute, before)
            else:
                target.text = before
        if _claims(response) != _claims(source_response):
            raise ValueError("Changed unrelated fixed SAML claims")
        directory = os.environ.get("AUTH30_EVIDENCE_DIR")
        if directory:
            path = Path(directory) / ("material-" + case + "-" + source_hash + ".json")
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
                json.dump({"case": case, "source_form_sha256": source_hash,
                    "submitted_form_sha256": sha256(prepared["form"].encode()).hexdigest(),
                    "signature_verification": {key: receipt[key] for key in
                        ("verifier", "certificate_sha256", "response", "assertion")}}, output, indent=2)
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError):
        pytest.fail("PENDING: fixed auth30 SAML material or its trusted signature receipt is unavailable or invalid", pytrace=False)
    return prepared["form"]


def corrupt_saml_response_signature(source_form):
    """Flip only the outer SignatureValue; preserve the signed Assertion bytes."""
    try:
        values, response, _ = _signed_form(source_form)
        value = response.find(DS + "Signature/" + DS + "SignatureValue")
        if value is None or not value.text:
            raise ValueError("Missing fixed response signature")
        signature = bytearray(base64.b64decode("".join(value.text.split()), validate=True))
        if not signature:
            raise ValueError("Empty fixed response signature")
        xml = base64.b64decode(values["SAMLResponse"][0], validate=True)
        original = value.text.encode()
        if xml.count(original) != 1:
            raise ValueError("Ambiguous fixed response signature")
        signature[0] ^= 1
        changed = xml.replace(original, base64.b64encode(signature), 1)
        return urlencode({"SAMLResponse": base64.b64encode(changed).decode(), "RelayState": values["RelayState"][0]})
    except (ValueError, TypeError, ET.ParseError):
        pytest.fail("PENDING: the fixed original SAML response signature is unavailable or invalid", pytrace=False)
