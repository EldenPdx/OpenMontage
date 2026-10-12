"""Prepare or check a sealed local security policy for the pinned Logto vendor files."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

VERSION = "1.44.0"
DIGEST = "sha256:847cdd2759ad60f61b9733af0ea352183c805681dff5c9f8140a80f52d8e1340"
BUNDLE = "main-YCQ7ZWUE.js"
ORIGINAL_SHA256 = "0f214fae0a87a74b6b361d2c66803e0c2df4114668cdf97b2414ee0466d7483c"
POLICY = Path(__file__).with_name("policy.mjs")

PATCHES = (
    ('''        if (error instanceof RequestError && error.code === "user.sso_identity_not_exist") {
          const user = await verificationRecord.identifyRelatedUser();
          const syncedProfile = {
            ...verificationRecord.toUserProfile(),
            ...await verificationRecord.toSyncedProfile(),
            enterpriseSsoConnectorTokenSetSecret: verificationRecord.getTokenSetSecret()
          };
          return { user, syncedProfile };
        }
''', ""),
    ('''  const user = userInfo.email && await usersQueries.findUserByEmail(userInfo.email);
  if (user) {
    return signInAndLinkWithSsoAuthentication(ctx, tenant, {
      connectorData,
      user,
      ssoAuthentication
    });
  }
''', ""),
    ('''    await injectSamlSigningCredential(connectorInstance, connectorData, queries, log);''',
     '''    connectorInstance.openmontageSsoQueries = queries.oidcModelInstances;
    await injectSamlSigningCredential(connectorInstance, connectorData, queries, log);'''),
    ('''      const loginRequest = serviceProvider.createLoginRequest(identityProvider, "redirect");
      return loginRequest.context;''',
     '''      const loginRequest = serviceProvider.createLoginRequest(identityProvider, "redirect");
      await openmontageRegisterSamlRequest(this.openmontageSsoQueries, relayState, loginRequest.id, this.serviceProviderMetadata);
      return loginRequest.context;'''),
    ('''    return ssoSamlAssertionContentGuard.parse(assertionResult.extract);''',
     '''    return openmontageVerifiedSamlProfile(ssoSamlAssertionContentGuard.parse(assertionResult.extract), assertionResult.extract);'''),
    ('''  return {
    upsertInstance,
    findPayloadById,''',
     '''  return {
    openmontageSamlConsume: openmontageCreateSamlConsumption(pool, sql42, table19, fields19),
    upsertInstance,
    findPayloadById,'''),
    ('''      const assertionContent = await connectorInstance.parseSamlAssertionContent(body);
      const userInfo = connectorInstance.getUserInfoFromSamlAssertion(assertionContent);''',
     '''      const assertionContent = await connectorInstance.parseSamlAssertionContent(body);
      try {
        await openmontageConsumeSamlResponse(queries.oidcModelInstances, jti, assertionContent);
      } catch {
        throw new RequestError({ code: "session.connector_validation_session_not_found", status: 400 });
      }
      const userInfo = connectorInstance.getUserInfoFromSamlAssertion(assertionContent);'''),
)


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def read_regular(path, *, private=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16_777_216 or private and info.st_mode & 0o077:
            raise RuntimeError("Unsafe SSO policy input")
        return source.read()


def expected(state, vendor_root, image_pin, platform):
    if platform not in {("linux", "amd64"), ("linux", "arm64")} or image_pin.get("version") != VERSION or image_pin.get("digest") != DIGEST:
        raise RuntimeError("Unsupported pinned SSO vendor or platform")
    core = Path(vendor_root) / "packages/core"
    if json.loads(read_regular(core / "package.json"))["version"] != VERSION:
        raise RuntimeError("SSO vendor version drifted")
    original = read_regular(core / "build" / BUNDLE)
    if sha256(original) != ORIGINAL_SHA256:
        raise RuntimeError("SSO vendor content drifted")
    text = original.decode("utf-8")
    for before, after in PATCHES:
        if text.count(before) != 1:
            raise RuntimeError("Pinned SSO policy attachment changed")
        text = text.replace(before, after)
    trailer = "//# sourceMappingURL=" + BUNDLE + ".map"
    if not text.endswith(trailer):
        raise RuntimeError("Pinned SSO vendor source map changed")
    text = text[:-len(trailer)]
    imports = '''import { registerSamlRequest as openmontageRegisterSamlRequest,
  verifiedSamlProfile as openmontageVerifiedSamlProfile,
  createSamlConsumption as openmontageCreateSamlConsumption,
  consumeSamlResponse as openmontageConsumeSamlResponse } from "./openmontage-sso.mjs";
'''
    generated = (imports + text).encode("utf-8")
    policy = read_regular(POLICY)
    directory = Path(state).absolute() / "sso"
    manifest = {"vendor_version": VERSION, "vendor_image_digest": DIGEST, "platform": "/".join(platform),
        "original_sha256": ORIGINAL_SHA256, "generated_sha256": sha256(generated), "policy_sha256": sha256(policy),
        "preparer_sha256": sha256(read_regular(Path(__file__))), "patch_count": len(PATCHES), "structural_only": True,
        "mounts": [{"source": str(directory / BUNDLE), "target": "/etc/logto/packages/core/build/" + BUNDLE, "read_only": True},
                   {"source": str(directory / "openmontage-sso.mjs"), "target": "/etc/logto/packages/core/build/openmontage-sso.mjs", "read_only": True}]}
    return directory, generated, policy, manifest


def private_directory(directory):
    if directory.is_symlink():
        raise RuntimeError("Unsafe SSO policy directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise RuntimeError("Unsafe SSO policy directory")


def publish(path, value):
    if path.is_symlink():
        raise RuntimeError("Unsafe SSO policy output")
    descriptor, pending = tempfile.mkstemp(prefix=".sso-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
    finally:
        Path(pending).unlink(missing_ok=True)


def prepare(state, vendor_root, image_pin, platform):
    directory, generated, policy, manifest = expected(state, vendor_root, image_pin, platform)
    private_directory(directory)
    for name, value in ((BUNDLE, generated), ("openmontage-sso.mjs", policy),
                        ("manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())):
        path = directory / name
        if path.exists() and read_regular(path, private=True) == value:
            continue
        publish(path, value)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return check(state, vendor_root, image_pin, platform)


def check(state, vendor_root, image_pin, platform):
    directory, generated, policy, manifest = expected(state, vendor_root, image_pin, platform)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise RuntimeError("Unsafe SSO policy directory")
    actual = json.loads(read_regular(directory / "manifest.json", private=True))
    if actual != manifest or read_regular(directory / BUNDLE, private=True) != generated or read_regular(directory / "openmontage-sso.mjs", private=True) != policy:
        raise RuntimeError("SSO policy is missing, partial or drifted")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check"))
    parser.add_argument("--state", required=True)
    parser.add_argument("--vendor-root", required=True)
    parser.add_argument("--platform", required=True, choices=("linux/amd64", "linux/arm64"))
    parser.add_argument("--pin-file", required=True)
    arguments = parser.parse_args()
    pin = json.loads(read_regular(arguments.pin_file))["logto"]
    function = prepare if arguments.command == "prepare" else check
    try:
        result = function(arguments.state, arguments.vendor_root, pin, tuple(arguments.platform.split("/")))
    except (OSError, ValueError, KeyError, RuntimeError):
        raise SystemExit("SSO policy preparation/check failed; inspect private operator diagnostics") from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
