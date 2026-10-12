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

The native SAML request is recorded before its IdP URL is issued. Samlify validates the response signature, issuer and supported time checks; its signature-verified Assertion shortcut supplies the stable assertion identifier. A PostgreSQL statement locks an unexpired pending request, inserts a tenant-scoped unique assertion identifier hash and conditionally marks the request consumed. Concurrent delivery has one winner. A consumed response is not reopened after downstream failure or an unknown result. Database failures deny authentication. The outer BFF independently retains its existing one-time state/nonce/PKCE, private SDK storage and authoritative workspace checks.

## Required acceptance

Run the dedicated real tests with the owner-provided `AUTH_TEST_FIXTURE`, isolated `STUDIO_TEST_DATABASE_URL`, private `STUDIO_TEST_SESSION_KEY`, `STUDIO_REQUIRE_INTEGRATION=1` and pristine `AUTH_SSO_VENDOR_ROOT`. Keep diagnostic artifacts protected and inspect them before sharing. Use a short private temporary root; retained protection witnesses from unrelated temporary roots can correctly prevent authenticated startup. Reuse the pinned installed Pi runtime for the existing business resource interface.

Required observations include standard OIDC and signed SAML through actual IdP → Logto → persistent BFF → explicit membership/role → owned resource, separate personal/organization contexts, denied foreign hints, same-email identity separation, native assertion and BFF callback replay, cancellation/provider failure/logout and secret isolation. Every invalid signed claim needs an otherwise identical accepting native control, with signature coverage, issuer, audience, bearer recipient, original request correlation and expiry checked at the intended boundary. A later BFF denial cannot certify rejection upstream.

Current local policy verification covers identity separation and one-time native processing; the remaining signed claim matrix and complete independent final review are pending. Do not interpret structural output, a base identity test or generic CI as full #30 acceptance. Customer-specific IdPs, production rollout, paid/Cloud procurement and forced commercial MFA require separate validation and authorization.
