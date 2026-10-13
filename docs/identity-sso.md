# Enterprise single sign-on

Use the standard Logto enterprise OIDC or SAML connector with the existing traditional-web application and persistent BFF. The IdP authenticates the employee; current Logto membership and recognized organization roles authorize the workspace. A connector or email domain does not grant organization membership. Without a verified organization hint, a new BFF session selects personal space.

Platform operators configure IdP credentials, discovery, metadata, certificates and exact redirects. Organization administrators receive the existing organization API capabilities; they do not receive an arbitrary connector, Management API or SQL gateway. Read the [identity contracts](identity-contracts.md), [BFF configuration](identity-bff.md) and [local identity environment](identity-environment.md) before onboarding.

## Configure and verify the identity source

1. Register exact Logto connector callback/ACS values in the IdP. Register the exact application BFF callback and post-logout URLs in Logto. Preserve HTTPS and the configured trusted public origins in deployment.
2. Use immutable OIDC subject identifiers. For the pinned Keycloak 26.8.0 SAML fixture, set `saml_name_id_format=persistent` and `saml_force_name_id_format=true`. The dotted variants do not configure these settings. Confirm the actual signed NameID format and compare two fresh credential-authenticated sessions. Keycloak stores the stable value under `saml.persistent.name.id.for.<exact SP clientId>`; its Admin user DTO may hide this internal attribute, so an absent DTO field is not evidence that persistence failed.
3. Keep connector `syncProfile` and `enableTokenStorage` disabled for the validation baseline; set OIDC `trustUnverifiedEmail=false`. These settings do not disable the vendor's enterprise email account linking. The local policy below disables that implicit fallback in both current Experience and legacy enterprise paths. Existing verified identity bindings continue to work. A previously unlinked identity whose email already belongs to another account is rejected; it must not inherit that account's session or organization roles.
4. Keep organization SSO/email-domain JIT relations empty. Establish the native immutable principal through a genuine personal BFF login, confirm its IdP identity binding, then provision membership and fixed admin/editor/viewer roles explicitly. Independently read the authoritative member/role inventory before testing an organization hint. Do not infer the principal or membership from email.

## Prepare the pinned local policy

The policy reuses Logto OSS 1.44.0, samlify 2.13.1 and its existing tenant-scoped PostgreSQL `oidc_model_instances` interface. It adds native SAML request/consumption model records in that table; it adds no service, database relation, credential, browser token store or public BFF API.

The preparer accepts only the pinned image descriptor and original vendor bundle SHA-256 `0f214fae0a87a74b6b361d2c66803e0c2df4114668cdf97b2414ee0466d7483c`. It verifies every attachment point, records separate generated/policy/preparer hashes and removes the original source-map reference from modified output. Release, content or attachment drift fails preparation. Keep the untouched vendor input separately; the running entry file becomes modified after mounting and cannot be used as original input.

With the verified image's `packages/core/package.json` and `packages/core/build/main-YCQ7ZWUE.js` under a protected staged vendor root:

```sh
python -B auth-runtime/sso/overlay.py prepare \
  --state "$AUTH_STATE_DIR" --vendor-root "$AUTH_SSO_VENDOR_ROOT" \
  --platform linux/amd64 --pin-file auth-runtime/images.lock.json
python -B auth-runtime/sso/overlay.py check \
  --state "$AUTH_STATE_DIR" --vendor-root "$AUTH_SSO_VENDOR_ROOT" \
  --platform linux/amd64 --pin-file auth-runtime/images.lock.json
```

Use `linux/arm64` for the verified ARM64 image. The Python integration accepts `prepare(state, vendor_root, PINS["logto"], ("linux", arch))` and the corresponding `check`. The input pin is the complete version/digest dictionary.

The result names two mode-0600 read-only mounts: generated `main-YCQ7ZWUE.js` at `/etc/logto/packages/core/build/main-YCQ7ZWUE.js`, and `openmontage-sso.mjs` at `/etc/logto/packages/core/build/openmontage-sso.mjs`. The modified entry imports the policy in the original Logto process. Missing policy or an unsupported samlify version prevents startup. Only the stack lifecycle owner changes these mounts and recreates the service, after other native users are safe. Do not edit files in a running container. Preserve the original image provenance, protected state, identity data and other services.

Preparation/check is explicitly structural. It does not certify live activation or successful SSO. The owner must verify actual mounts, loaded file hashes, current process/startup health and real public behavior. Preserve the source/receipt/result hashes with the exact code commit. No credentials belong in command arguments or public output.

## One-time native processing

The native SAML request is recorded before its IdP URL is issued. Samlify validates the response signature, issuer and supported time checks; its signature-verified Assertion shortcut supplies the assertion identifier, Audience and bearer confirmation. The local policy requires the bearer method, an unexpired bearer timestamp, and the expected recipient. A PostgreSQL statement locks an unexpired pending request, compares the signed Audience/Recipient/request correlation and the response headers with the recorded SP/ACS/request values, checks expiry against database time, inserts a tenant-scoped unique assertion identifier hash and conditionally marks the request consumed. Response headers constrain the original request; they do not establish identity. Concurrent delivery has one winner. A consumed response is not reopened after downstream failure or an unknown result. Database failures deny authentication. The outer BFF independently retains its existing one-time state/nonce/PKCE, private SDK storage and authoritative workspace checks.

The supported local SP profile has one Audience value and one bearer SubjectConfirmation. Additional SAML profiles require explicit configuration acceptance. IdP-initiated SSO is outside this implementation.

## Required acceptance

Run the dedicated real tests with the owner-provided `AUTH_TEST_FIXTURE`, isolated `STUDIO_TEST_DATABASE_URL`, private `STUDIO_TEST_SESSION_KEY`, `STUDIO_REQUIRE_INTEGRATION=1` and pristine `AUTH_SSO_VENDOR_ROOT`. Keep diagnostic artifacts protected and inspect them before sharing. Use a short private temporary root; retained protection witnesses from unrelated temporary roots can correctly prevent authenticated startup. Reuse the pinned installed Pi runtime for the existing business resource interface.

Required observations include standard OIDC and signed SAML through actual IdP → Logto → persistent BFF → explicit membership/role → owned resource, separate personal/organization contexts, denied foreign hints, same-email identity separation, native assertion and BFF callback replay, cancellation/provider failure/logout and secret isolation. Every invalid signed claim needs an otherwise identical accepting native control, with signature coverage, issuer, audience, bearer recipient, original request correlation and expiry checked at the intended boundary. A later BFF denial cannot certify rejection upstream.

## Resumed verification and manual checks

The resumed work follows the user's instruction to leave security testing to a human. Run only the normal-flow nodes below with the private environment described above. Do not run the whole SSO test files as a substitute for this selection.

```sh
python -B -m pytest -p tests.conftest \
  auth-runtime/tests/test_sso.py::test_standard_oidc_sso_creates_a_persistent_personal_identity_and_owned_resource \
  auth-runtime/tests/test_sso.py::test_standard_oidc_sso_selects_only_its_explicit_editor_membership \
  auth-runtime/tests/test_sso.py::test_standard_signed_saml_sso_creates_its_own_persistent_personal_identity \
  auth-runtime/tests/test_sso.py::test_standard_signed_saml_sso_keeps_explicit_a_viewer_and_b_editor_separate \
  auth-runtime/tests/test_sso.py::test_user_can_leave_the_enterprise_login_screen_and_sign_in_again \
  auth-runtime/tests/test_sso.py::test_browser_provider_network_failure_leaves_the_user_signed_out_and_allows_a_fresh_login \
  auth-runtime/tests/test_sso.py::test_standard_enterprise_logout_returns_to_the_application_signed_out \
  auth-runtime/tests/test_sso_overlay.py::test_policy_reads_the_actual_signature_verified_assertion_identifier \
  auth-runtime/tests/test_sso_overlay.py::test_operator_overlay_cli_seals_real_vendor_bytes_and_refuses_drift -q
```

The loaded policy is `481b73ca3d584e7a334b1b1851f0dbdc3945234b4f881db741c5af00ca85f524`; the generated vendor bundle is `ca9382f06831480941a9b0a78e340c393fcf6f643e62a1adf8902c52702df0ed`. The lifecycle owner's protected receipt records actual read-only mounts, running file hashes, unchanged image provenance, startup health and unchanged fixture ledgers. The ordinary cases passed against this policy with Logto 1.44.0, Keycloak 26.8.0, samlify 2.13.1 and PostgreSQL 17.11 on the ARM64 local Docker stack.

| Observation | Actual result |
| --- | --- |
| OIDC personal identity and explicitly provisioned organization editor; signed SAML stable personal identity and separate A/viewer, B/editor workspaces; owned resource access | 4 passed |
| Fresh signed SAML accepted by the real samlify extractor and the current binding policy | 1 passed |
| User leaves the IdP credential page, returns to the application signed out, then completes a fresh OIDC/SAML login | 2 passed |
| This browser's connection to either IdP or Logto fails; no application session is created; fresh OIDC/SAML login succeeds after recovery | 4 passed |
| Normal OIDC/SAML logout revokes application resource access and returns through the configured Logto end-session endpoint | 2 passed |
| Operator prepare/check seals pristine vendor bytes for each supported platform descriptor | 2 passed; structural only |

These 15 observations had zero failures and zero skips. Browser network cases abort only that page's outbound connection; they do not stop either shared service or establish server-side outage behavior. Leaving the credential page verifies ordinary abandonment, not an IdP-specific cancellation callback. The two platform descriptors establish preparation support; native AMD64 host execution remains unverified.

The following security checks are assigned to the user and were not rerun or newly created in this resumed work:

- Invalid OIDC/SAML issuer, audience, recipient, destination, request correlation, timestamps, signature coverage and unsupported SAML confirmation profiles, including an accepting control for each rejection.
- Repeated or concurrent assertion, token or callback delivery, and reuse of revoked browser credentials.
- Distinct IdP identities with equal email addresses, non-member organization hints and foreign organization access.
- Protocol error and credential/assertion isolation in failure diagnostics, ordinary logs, public URLs, browser storage and business records.

IdP-specific cancellation, customer configuration errors and real IdP/Logto service outages also need operator verification; the browser recovery observations above do not certify those cases. Historical evidence at policy checkpoint `dada3164851d237c6e4ef5411fc7df87c96e4617` recorded 13 passing cases against the earlier partial policy, including identity separation and native one-time processing. Retain it as historical evidence; it does not certify the current full claim-binding policy's rejection matrix.

The signed claim matrix, manual checks and complete independent final review remain pending. Do not interpret structural output, normal login acceptance or generic CI as full security acceptance for #30. Customer-specific IdPs, production rollout, paid/Cloud procurement and forced commercial MFA require separate validation and authorization.
