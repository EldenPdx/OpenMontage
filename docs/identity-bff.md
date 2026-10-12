# Browser identity boundary

The BFF uses the official pinned Logto Python SDK, encrypted PostgreSQL sessions and live Management API reads. It verifies the configured issuer, signature/JWKS, audience/authorized party, mandatory dates and subject, login nonce, one-time state/PKCE interaction and matching UserInfo subject. Every SDK client receives a fresh private storage snapshot. Tokens and management credentials remain in the backend.

## Trusted configuration

`OPENMONTAGE_AUTH_CONFIG_FILE` selects a protected JSON file (regular file, no symlink, mode 0600). `OPENMONTAGE_AUTH_MODE` can explicitly select `authenticated` or `local`; invalid or incomplete authenticated configuration yields safe dependency errors. The BFF does not read authentication trust from creative projects or accept it in browser requests. Keep the existing `config.yaml` and production profiles separate.

An authenticated configuration requires these fields:

| Fields | Meaning |
| --- | --- |
| `mode: authenticated`, `origin` | Exact public browser origin; HTTPS in production |
| `endpoint`, `issuer` | Configured Logto base and its exact `/oidc` issuer |
| `app_id`, `app_secret` | Traditional-web application credentials |
| `redirect_uri`, `post_logout_redirect_uri` | Exact `origin/api/auth/callback` and registered localized library URI |
| `management_endpoint`, `management_resource`, `management_app_id`, `management_app_secret` | Private Management API configuration; the fixed read adapter cannot proxy arbitrary browser paths |
| `database_url`, `database_schema` | Already migrated control-plane database/schema; BFF credentials must have the private identity-store privileges |
| `encryption_keys`, `active_key_id` | Key-id → base64url 32-byte protected key; retain keys needed to decrypt existing sessions |

Optional `absolute_seconds` and `idle_seconds` can shorten the 24-hour absolute and 8-hour idle limits. They cannot disable expiry. `http_development: true` allows explicitly configured loopback HTTP URLs for isolated testing. `trusted_proxy_ips` contains exact administrator-approved physical proxy IP addresses, never client-provided addresses.

Run Uvicorn with **`proxy_headers=False`**. The Backlot CLI already does this and binds `127.0.0.1`. A reverse proxy must preserve the configured Host and provide the matching `X-Forwarded-Proto`/`X-Forwarded-Host`; the BFF checks the original socket peer before trusting these headers. Direct requests must use the configured origin scheme; an HTTP backend serving an HTTPS origin therefore requires the trusted proxy evidence. Arbitrary forwarding and non-loopback anonymous clients are rejected. Access query strings are redacted, and the CLI disables access logging.

## Root protection and local compatibility

Genuine file-only legacy use remains available without an identity database. A configured local root has trusted `root_provenance` containing `canonical_root`, `classification`, and, for adopted/restored roots, `expected_binding` retained from the operator configuration or backup receipt. Classifications are `file_only_legacy`, `adopted_or_restored`, and `authenticated_or_unknown`.

Authenticated activation and ownership import persist and fsync an independent witness in each protected directory's parent before its inner `.openmontage-authenticated` marker. The private `.openmontage-auth-root-records/<directory basename>` namespace uses the filesystem's own case and Unicode name equivalence, so aliases of the same directory retain protection. Either entry, including malformed or unsafe namespace state, denies anonymous business access. Parent-directory write/fsync access is required. The witnesses remain after inner-marker loss, configuration rollback and an unavailable or replacement database; they are never automatically deleted.

Historical `.openmontage-auth-root-<hash>.json` receipts remain protective. Activation upgrades only matching retained classifications to the native namespace; an unmatched old hash conservatively refuses anonymous sibling-root access and cannot mint a new binding. Retain these receipts and their parent directories during upgrades and supported restores.

Adopted/restored roots require the preserved binding and the authoritative registry to confirm the same database and no owned task/project anywhere in the schema. Missing classification/registry, unavailable or replacement database, unknown/authenticated classification and retained authenticated configuration deny anonymous business requests. A newly empty database is never its own proof of legacy classification. Keep external protection/classification receipts when copying or restoring files to a new path; follow the [migration runbook](identity-migration.md). Offline CLI canonical files are unaffected.

## HTTP and consumer interfaces

The frozen [HTTP contracts](identity-contracts.md) define `/api/auth/login`, `/callback`, `/me`, POST `/logout` and POST `/workspace`. `/me` exposes only the safe user/workspace/permission projection, CSRF token, context id and absolute deadline. Workspace selection verifies current membership and rotates cookie/CSRF/context atomically. Successful callback redirects to a validated localized return; failed browser callbacks preserve the error HTTP status and use a nonce-only CSP script to clear protocol parameters from history. JSON clients retain the safe ErrorDTO envelope.

Logout validates the stored CSRF/context and revokes the exact session revision before any remote operation. It then uses the SDK with a fresh private snapshot. SDK 0.2.1 emits `client_id` and `post_logout_redirect_uri`, with no `id_token_hint`. `logout_url: null` means local revocation succeeded but remote sign-out is unavailable. A database failure never reports successful revocation. Account suspension rejects access; re-enabling an account does not resurrect credentials the provider has revoked.

`production.api.security.require_context(request, write=True|False)` verifies the session and current authority without deciding a resource capability. Consumers first perform the scoped ownership lookup (foreign/unassigned/absent → 404), then call `require_permission(context, permission)` (owned but insufficient → 403). `revalidate_context(request, context_id)` rechecks revocation/authority and is required before sensitive stream frames and at each idle heartbeat, no more than 15 seconds apart. The entire revalidation has a 15-second deadline; proof timestamps denote verification start. A changed storage revision invalidates in-flight permission evidence and returns 409 instead of reusing old permissions.

Central registration uses `app.state.auth_policies` for the Studio, projects and organizations modules. Only trusted source code registers these policies. Until each consumer's resource authorization is integrated, authenticated business endpoints fail closed; signing in does not enable the older global handlers. Health is constant and public, UI assets contain no business state, and authenticated OpenAPI is disabled.

## Required verification

The existing generic `tests/` suite covers local compatibility, transport/CSRF, CLI and PostgreSQL behavior. The required real identity suite lives under `auth-runtime/tests/`; it is run explicitly and fails for missing dependencies or private fixture inputs. Generic CI success does not claim real identity acceptance. #32 connects the complete identity and cross-module suite to mandatory CI.

With the isolated fixture, database and encryption key provided securely in the process environment, run:

```sh
python -m pytest -p tests.conftest auth-runtime/tests/test_bff.py auth-runtime/tests/test_bff_oidc_faults.py auth-runtime/tests/test_bff_authority.py -q
python -m pytest tests/integration/test_auth_boundary.py tests/integration/test_auth_cli_boundary.py tests/integration/test_auth_root_classification.py tests/integration/test_auth_store_boundaries.py tests/backlot/test_server.py -q
make lint
```

Set `AUTH_TEST_FIXTURE`, `STUDIO_TEST_DATABASE_URL`, `STUDIO_TEST_SESSION_KEY` and `STUDIO_REQUIRE_INTEGRATION=1` without printing their values. Migration/restore verification also requires the matching isolated `STUDIO_TEST_POSTGRES_CONTAINER`. Real tests temporarily change only the dedicated fixture accounts/roles and pause/unpause its Logto container from the actual stack-owner checkout; restore and verify the baseline afterward. They do not deploy, call paid providers or change customer identity systems.
