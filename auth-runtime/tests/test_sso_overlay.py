"""The operator overlay CLI seals real vendor bytes and refuses drift."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import httpx
import pytest

from tests.fixtures.auth30 import sso_browser

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "auth-runtime/sso/overlay.py"


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
def test_operator_overlay_cli_seals_real_vendor_bytes_and_refuses_drift(tmp_path, platform):
    vendor = tmp_path / "vendor"
    core = vendor / "packages/core"
    core.joinpath("build").mkdir(parents=True)
    original = os.environ.get("AUTH_SSO_VENDOR_ROOT")
    assert original, "Operator checks require staged original vendor files, independent of runtime mounts"
    for name in ("package.json", "build/main-YCQ7ZWUE.js"):
        shutil.copyfile(Path(original) / "packages/core" / name, core / name)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    command = [sys.executable, "-B", str(CLI), "prepare", "--state", str(state),
        "--vendor-root", str(vendor), "--platform", platform, "--pin-file", str(ROOT / "auth-runtime/images.lock.json")]
    prepared = subprocess.run(command, capture_output=True, text=True)
    assert prepared.returncode == 0, "The operator must be able to prepare a sealed overlay for pinned vendor files"
    manifest = json.loads(prepared.stdout)
    assert manifest["vendor_version"] == "1.44.0"
    assert manifest["platform"] == platform
    assert manifest["structural_only"] is True
    generated = Path(manifest["mounts"][0]["source"])
    assert generated.is_file() and generated.stat().st_mode & 0o777 == 0o600
    assert manifest["original_sha256"] != manifest["generated_sha256"]
    original_output = generated.read_bytes()
    repeated = subprocess.run(command, capture_output=True, text=True)
    assert repeated.returncode == 0 and generated.read_bytes() == original_output
    vendor_bundle = core / "build/main-YCQ7ZWUE.js"
    with vendor_bundle.open("a") as output:
        output.write("\n")
    command[3] = "check"
    drift = subprocess.run(command, capture_output=True, text=True)
    assert drift.returncode != 0
    assert generated.read_bytes() == original_output


def test_policy_reads_the_actual_signature_verified_assertion_identifier(monkeypatch):
    with sso_browser(monkeypatch) as sso:
        context, _ = sso.sign_in("saml")
        assert sso.me(context)["role"] == "personal"
        metadata = httpx.get(sso.fixture.idp["saml"]["metadata_url"], trust_env=False, timeout=10)
        assert metadata.status_code == 200
        payload = {"form": sso.saml_post, "metadata": metadata.text,
            "entityId": sso.fixture.idp["saml"]["sp_entity_id"], "acsUrl": sso.fixture.idp["saml"]["acs_url"],
            "policy": ROOT.joinpath("auth-runtime/sso/policy.mjs").read_text()}
        script = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const { createRequire } = require("node:module");
const load = createRequire("/etc/logto/packages/core/package.json");
const saml = load("samlify");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const raw = Buffer.from(new URLSearchParams(input.form).get("SAMLResponse"), "base64").toString("utf8");
const idp = saml.IdentityProvider({ metadata: input.metadata });
const [valid, shortcut] = saml.SamlLib.verifySignature(raw,
  { metadata: idp.entityMeta, signatureAlgorithm: idp.entitySetting.requestSignatureAlgorithm });
assert.equal(valid, true);
assert.equal(typeof shortcut, "string");
let extractionShape = {};
(async () => {
  const url = "file:///etc/logto/packages/core/build/openmontage-sso.mjs";
  const policy = new vm.SourceTextModule(input.policy, { identifier: url,
    initializeImportMeta(meta) { meta.url = url; } });
  await policy.link(async (name) => {
    const values = name === "samlify" ? { default: saml } : await import(name);
    const names = Object.keys(values);
    return new vm.SyntheticModule(names, function () {
      for (const key of names) this.setExport(key, values[key]);
    });
  });
  await policy.evaluate();
  const extracted = saml.Extractor.extract(raw, saml.Extractor.loginResponseFields(shortcut));
  extractionShape = { assertion: typeof extracted.openmontageAssertion, audience: typeof extracted.audience,
    confirmation: typeof extracted.openmontageConfirmation,
    bearerMethodMatches: extracted.openmontageMethod === "urn:oasis:names:tc:SAML:2.0:cm:bearer",
    bearerKeys: Object.keys(extracted.openmontageBearer ?? {}), responseKeys: Object.keys(extracted.response ?? {}),
    bearerExpiryFinite: Number.isFinite(Date.parse(extracted.openmontageBearer?.notOnOrAfter)),
    bearerExpiryFuture: Date.parse(extracted.openmontageBearer?.notOnOrAfter) > Date.now() };
  assert.equal(typeof extracted.openmontageAssertion, "string");
  assert.ok(extracted.openmontageAssertion.length > 0);
  const profile = policy.namespace.verifiedSamlProfile({}, extracted);
  assert.match(profile.openmontageAssertionHash, /^[a-f0-9]{64}$/);
  assert.equal(profile.openmontageBindings.audience, input.entityId);
  assert.equal(profile.openmontageBindings.recipient, input.acsUrl);
  assert.equal(profile.openmontageBindings.destination, input.acsUrl);
  assert.equal(profile.openmontageBindings.bearerRequestId, profile.openmontageBindings.responseRequestId);
  assert.ok(profile.openmontageBindings.expiresAt > Date.now());
  process.stdout.write(JSON.stringify({ signatureVerified: true, identifierIsString: true,
    policyAccepted: true, bindingsMatchNativeMetadata: true }));
})().catch(() => { process.stderr.write(JSON.stringify(extractionShape)); process.exitCode = 1; });
'''
        result = subprocess.run(["docker", "exec", "-i", "openmontage-auth24-logto-1", "node",
            "--experimental-vm-modules", "-e", script], input=json.dumps(payload), text=True,
            capture_output=True, timeout=20)
        if result.returncode != 0 and os.environ.get("AUTH30_EVIDENCE_DIR"):
            path = Path(os.environ["AUTH30_EVIDENCE_DIR"]) / "extractor-shape.txt"
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
                output.write(result.stderr)
        assert result.returncode == 0, "Actual signed native extraction must reach the local policy"
        assert json.loads(result.stdout) == {"signatureVerified": True, "identifierIsString": True,
            "policyAccepted": True, "bindingsMatchNativeMetadata": True}
