# Local identity environment (AUTH-02 / #24)

Developer/operator runbook for the free Logto OSS baseline in [#24](https://github.com/EldenPdx/OpenMontage/issues/24). Read the [identity contracts](identity-contracts.md), [identity ADR](adr/0001-personal-organization-identity.md) and [browser-production ADR](adr/browser-production.md) first. This environment supplies real identities and standard protocol fixtures; application login, persistent BFF sessions and tenant authorization remain subsequent modules' work.

**Status, 2026-10-12:** commands below describe the current owner implementation, not an accepted release. The dated environment audit is inventory evidence; the progress snapshot records partial real checks followed by a PostgreSQL reachability failure after a network restart. Turing is correcting lifecycle behavior. Current health, signed SAML completion and final OIDC proof remain pending the owner's exact-commit report and independent reproduction. No network topology is accepted by this runbook.

## Versions and prerequisites

`auth-runtime/images.lock.json` is the image authority. Its current pins are:

| Component | Version | Source tag | SHA-256 descriptor |
| --- | --- | --- | --- |
| Logto OSS | 1.44.0 | `ghcr.io/logto-io/logto:1.44.0` | `847cdd2759ad60f61b9733af0ea352183c805681dff5c9f8140a80f52d8e1340` |
| PostgreSQL | 17.11 | `public.ecr.aws/docker/library/postgres:17-alpine` | `b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24` |
| Keycloak | 26.8.0 | `quay.io/keycloak/keycloak:26.8.0` | `27b3230fbabb8c9ff3c66c864cf09dcd8a41aca82e5100679d109ef0eeb05741` |
| Mailpit | 1.31.5 | `ghcr.io/axllent/mailpit:v1.31.5` | `94f6b70c1436defae6799d9ac7ff0eeff9540465b842026d5cdb018f1c0234ea` |

Descriptors have the `sha256:` prefix. Tags alone are insufficient: `auth-runtime/images.py` verifies registry descriptors, platform/configuration and layer hashes, then imports Docker archives under `openmontage-auth/<component>:<version>-<digest-prefix>`. `up` checks the recorded image ID and digest before starting with `--pull never --no-build`. Restage on drift; do not replace an image with an unverified tag. Version changes require coordinated contract/evidence updates and matching lock, Compose and dependency changes before acceptance.

The current stager selects **Linux ARM64** only. Require a compatible Docker engine and Compose, `uv`, an existing project `.venv/bin/python`, and the project's installed test stack (pytest, FastAPI/uvicorn and Chromium for Playwright). The bootstrap inherits host packages into a separate `auth-runtime/.state/venv` and installs the exact packages in `requirements-auth.txt`; it does not provision the host test stack or browser binary. Relevant pins include Logto Python SDK **0.2.1**, psycopg/psycopg-binary **3.3.6**, httpx **0.28.1**, Playwright **1.63.0** and cryptography **50.0.2**. Host-package inheritance is a reproducibility limit: record the interpreter, test-runner and browser versions in final evidence.

The dated audit observed Python 3.10.22, Docker Engine 29.8.2 and Compose 5.5.1; these are observations, not enforced version locks or proof on another host. Uncached dependency/image retrieval requires the operator's approved current-process network environment; otherwise dependency installation uses the offline cache and image staging fails on absent content. Keep operator network settings out of repository files. Missing tools, cache entries, images or browser binaries are failures, never a skipped acceptance pass.

## Prepare, start and repeat initialization

Run from the repository root of the checkout that owns the identity services. One checkout operates this stack at a time: project `openmontage-auth24` has fixed loopback ports, and the helper rejects existing containers whose ownership label or checkout path differs. Do not bypass that check with a raw Compose invocation.

```sh
set -eu
umask 077
.venv/bin/python -B auth-runtime/bootstrap.py prepare
auth-runtime/.state/venv/bin/python -B auth-runtime/manage.py --help
.venv/bin/python -B auth-runtime/bootstrap.py images
.venv/bin/python -B auth-runtime/bootstrap.py up
auth-runtime/.state/venv/bin/python -B auth-runtime/manage.py check
auth-runtime/.state/venv/bin/python -B auth-runtime/manage.py seed --namespace auth24 --app-origin http://127.0.0.1:4760
auth-runtime/.state/venv/bin/python -B auth-runtime/manage.py seed --namespace auth24 --app-origin http://127.0.0.1:4760
```

With no arguments, `bootstrap.py` runs `up`. `up` also prepares credentials, initializes the isolated databases, runs the disposable Logto database seed, bootstraps management access and seeds the default fixture. It stages images if their local ledger is absent. The two explicit `seed` calls exercise repeat initialization; command completion alone does not prove unchanged resource counts. Use the real test below for that assertion.

The exact `manage.py` interface is `COMMAND [--namespace NAMESPACE] [--app-origin APP_ORIGIN]`:

| Command | Current behavior |
| --- | --- |
| `prepare` | Create/reuse private credentials and write service configuration; no service startup. |
| `images` | Verify/cache/import all pinned images. |
| `up` | Start and initialize the owned stack; seed `auth24` at `http://127.0.0.1:4760`. |
| `check` | Verify four pinned Python dependency versions, PostgreSQL version, actual Logto and Keycloak master discovery/issuer/JWKS, and Mailpit HTTP API. |
| `seed` | Reconcile users, organizations, templates, apps, SMTP settings and the namespace's Keycloak realm/connectors. |
| `cleanup` | Remove only resources recorded by the selected fixture, with ownership checks described below. |
| `restart` | Restart owned services through the lifecycle helper and run initialization again; persistence proof is pending. |
| `stop` | Stop the owned services; retain private state and volumes. |
| `down` | Remove the owned containers/network; retain private state and named volumes. |

Defaults are namespace `auth24` and app origin `http://127.0.0.1:4760`. Only `seed` consumes `--app-origin`; `--namespace` affects `seed` and `cleanup`. Passing those options to `up` or `restart` does not change their default fixture. Namespaces must match `[a-z][a-z0-9-]{0,30}`. `check` is reachability evidence, not SMTP delivery, fixture correctness or completed login/SSO evidence.

The current advertised local interfaces are:

| Service | Interface |
| --- | --- |
| Logto | endpoint `http://127.0.0.1:3010`; issuer `http://127.0.0.1:3010/oidc`; discovery at `/oidc/.well-known/openid-configuration` |
| Logto Console | `http://127.0.0.1:3011`; privileged operator administration only, never the customer organization UI |
| Identity PostgreSQL | `127.0.0.1:55433`; dedicated identity service, separate from Studio |
| Keycloak | `http://127.0.0.1:8180`; fixture realm `/realms/auth24`; standard OIDC discovery/JWKS and `/protocol/saml/descriptor` |
| Mailpit | HTTP/UI `http://127.0.0.1:8025`; SMTP `127.0.0.1:1025`; connector target `mailpit:1025` |

Ports and issuer strings must agree with the actual code and registrations. Reachability after restart must be reverified by Turing; changing a port in this table alone does not configure the environment.

## Private configuration and privilege boundaries

`auth-runtime/.state` is ignored private state with mode **0700**. Generated `credentials.json`, service `.env` files, `pg-bootstrap.password`, fixture and IdP ledgers use **0600**. `load_fixture()` rejects group/world-accessible fixture files. Default ledgers are `fixture.json` and `idp.json`; other namespaces use `fixture-<namespace>.json` and `idp-<namespace>.json`. Keep these outside project media, frontend bundles, Issues and public artifacts. Directory protection also covers inherited packages, caches and diagnostic logs.

Identity and business storage stay separate. The helper creates databases `auth_logto` and `auth_keycloak`, with distinct runtime login roles of the same names and separate credentials. It sets `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, revokes public database access and grants each role its own connection. These roles must not reuse a Studio/Pi database account. The privileged `auth_bootstrap` account initializes this dedicated instance and is not an application credential.

Compose supplies the PostgreSQL bootstrap password only to PostgreSQL and the Logto bootstrap DB secret only to disposable `logto-seed`. The long-running Logto service receives the restricted `auth_logto` configuration and declares no bootstrap secret mount. Final evidence must inspect the actual runtime mounts, role flags and rejected cross-database connections. Keycloak uses its separate restricted DB account; its private administration password remains an operator capability.

The fixture M2M client currently receives Logto's `Logto Management API access` role and requests resource `https://default.logto.app/api`, scope **`all`**. The public fixture helper is a trusted test administration client, not a constrained application adapter. Reading memberships with that client does not prove a read-only credential. The test asserting the vendor's coarse scope and denial of ungranted clients must pass before this privilege limitation is accepted; subsequent BFF/organization adapters must enforce the fixed operations and tenant checks required by the contracts.

## Fixture and protocol interfaces

Import `load_fixture`, `seed_fixture` and `cleanup_fixture` from `tests.fixtures.identity`. `load_fixture()` uses the default private ledger or `AUTH_TEST_FIXTURE`; the override selects a ledger, not alternate ports or a parallel-safe stack. The current AUTH-24 suite expects the default `auth24` dataset.

| Interface | Use and privilege |
| --- | --- |
| `fixture.users`, `.organizations`, `.application`, `.idp` | Views of identifiers/configuration; application and IdP views omit passwords/app secrets. Do not publish whole ledgers. |
| `.organization_roles(user, organization)` | Read authoritative roles using the same privileged management credential. |
| `.login_credentials(user)`, `.idp_login_credentials(protocol)`, `.private_app_configuration(origin=None)` | Credentials for trusted test backends/browser drivers only; never serialize into DTOs or logs. |
| `.api`, `.set_roles`, `.add_member`, `.remove_member`, `.suspend` | Privileged test mutations; bypass application authorization and cannot establish its correctness. |
| `.app_for_origin(origin)` | Idempotent exact Traditional-web callback registration; returns configuration without the secret. |
| `.send_invitation`, `.cancel_invitation`, `.mail_ids`, `.wait_for_mail` | Real Logto invitation and local mailbox flow; received message bodies contain verification codes/links and remain private. |

Four real Logto users provide distinct identities and personal-space test subjects; personal space itself does not require an organization:

| Alias | Authoritative organization grants |
| --- | --- |
| `u1` | A admin |
| `u2` | B admin |
| `cross` | A editor, B viewer |
| `viewer` | A viewer |

The shared templates grant viewer `resource:read` and `organization:read`; editor adds `production:create`, `production:approve`, `production:cancel` and `production:resume`; admin adds `organization:manage`. The exact sets are `CAPABILITIES` in `tests/fixtures/identity/environment.py`; BFF enforcement remains governed by the identity contracts.

Keycloak provisioning is implemented by `tests/fixtures/identity/idp.py`: one dedicated marked realm, confidential OIDC client and standard signed-SAML client, with `oidc`, `saml` and `same-email` accounts. The same-email account is a future identity-linking negative test subject, not evidence of safe application linking. `fixture.idp["oidc"]` exposes discovery/issuer/registered redirect data; `fixture.idp["saml"]` exposes metadata URL, public certificate SHA-256, SP entity ID and ACS URL. Signing private keys remain in Keycloak's database. Keycloak provisioning/cleanup requires private administration access, not read-only protocol access.

The native browser fixture uses the locked Python SDK, real Chromium Logto/Keycloak screens, a temporary callback server and SDK callback handling to obtain a verified Logto subject. Its in-memory SDK storage and returned subject prove a protocol seam only: they do not prove production BFF cookies, persistent session isolation, workspace membership, tenant authorization or negative SSO assertions. Configured metadata/connectors alone do not prove either browser protocol round trip.

### Register exact callback origins

The default Traditional-web app registers exactly `http://127.0.0.1:4760/api/auth/callback` and post-logout `http://127.0.0.1:4760/en`. For a trusted backend test on another assigned port, call `fixture.app_for_origin(origin)` before SDK sign-in, then retrieve the matching secret with `fixture.private_app_configuration(origin)` privately. Origins must be `http://127.0.0.1:<port>` with no path, credentials, query or fragment; no wildcard callbacks or untrusted Host-derived registration.

For upstream OIDC, Keycloak registers Logto's exact `/callback/<connector_id>` URL. For SAML, provisioning uses Logto's actual provider-reported SP entity ID and ACS URL, then records the certificate fingerprint from real Keycloak metadata. Do not guess those values or insert example IDs/certificates. The local Logto configuration narrowly permits the loopback IdP address for metadata retrieval; it does not disable SSRF checks globally.

### Local mail only

Logto's SMTP connector uses sender `auth24@openmontage.test` and the Mailpit sink. Relay/forwarding is unconfigured; keep it that way. Fixture recipients use reserved `openmontage.test` addresses, and invitation helpers restrict recipients to their namespace. Never configure a real third-party mailbox, production sender or delivery service. Actual receipt and assertions for invitation, registration and reset are required; a listening SMTP port or healthy Mailpit API is insufficient. The fixture invitation link is currently fixed at `http://127.0.0.1:4760/en`; changing a web origin does not change that mail link or prove BFF invitation acceptance.

## Restart, rebuild and scoped cleanup

```sh
set -eu
auth-runtime/.state/venv/bin/python -B auth-runtime/manage.py restart
auth-runtime/.state/venv/bin/python -B auth-runtime/manage.py check
auth-runtime/.state/venv/bin/python -B auth-runtime/manage.py stop
auth-runtime/.state/venv/bin/python -B auth-runtime/manage.py up
```

There is no `rebuild`/`reset` command. For owned-container recreation, use `down` then `up`; credentials and named volumes remain. For a disposable dataset rebuild, use the namespace ledger:

```sh
set -eu
auth-runtime/.state/venv/bin/python -B auth-runtime/manage.py seed --namespace auth24-docs --app-origin http://127.0.0.1:4760
auth-runtime/.state/venv/bin/python -B auth-runtime/manage.py cleanup --namespace auth24-docs
auth-runtime/.state/venv/bin/python -B auth-runtime/manage.py seed --namespace auth24-docs --app-origin http://127.0.0.1:4760
```

Cleanup checks recorded Logto IDs plus exact names/namespace prefixes, removes the owned marked Keycloak realm and deletes only observed, ledger-recorded mail whose recipients match the namespace. It retains shared organization roles/scopes, SMTP/sign-in settings, bootstrap resources and volumes; it is not a global identity reset. It removes the successful fixture/IdP ledgers. A subsequent CLI cleanup fails on a missing fixture; after a partial failure, retain the ledger for inspection/retry. Do not broaden cleanup to discover resources by prefix alone.

Never use volume deletion, global Docker pruning, database-wide deletion or deletion of private credentials to fix this stack. Existing Studio/PostgreSQL volumes, Pi runtime/session files, canonical projects and media are outside this project's scope. Do not run `compose.studio.yaml` lifecycle commands or migrate business data as part of AUTH-24. Preserve them and require the owner's existing Pi/PostgreSQL/CLI regression evidence separately.

## Real validation and safe failure output

After the lifecycle correction, run the actual suite from the repository root. It mutates/restarts the dedicated identity environment, so serialize it with other fixture users:

```sh
set -eu
umask 077
auth-runtime/.state/venv/bin/python -B -m pytest -q --tb=no -p no:cacheprovider \
  auth-runtime/tests/test_environment.py >auth-runtime/.state/pytest.log 2>&1
```

This command must return zero with no required skips. The suite exercises real discovery/JWKS, memberships, native SDK login, local invitation/registration/reset mail, standard IdP metadata, OIDC and signed SAML browser flows, restricted database accounts/no bootstrap mount, coarse management scope/denials, repeat initialization with stable resource IDs, cleanup with a foreign sentinel, exact origins, bootstrap reuse and restart/certificate persistence. Presence of these tests is not passing evidence. The restart and protocol tests must be rerun on the final owner SHA; existing Pi/Studio/CLI preservation checks and independent reproduction are also required.

Lifecycle subprocess diagnostics go to private `commands.log`; bootstrap installation diagnostics go to private `bootstrap.log`. Keep `pytest.log` private too. Public failures should report a sanitized operation/status only. Driver exceptions, callback URLs, assertions, mail bodies, tokens and subprocess environments can expose credentials even when normal helper representations omit them; share only reviewed, sanitized scenario results. Missing dependencies, invalid/private-state permissions, wrong issuers, unreachable services or ambiguous resources stop validation. Do not substitute mock health/login responses or anonymous mode.

The dated progress snapshot reported a generated test DB credential exposed by a failure traceback and subsequently rotated; final output-safety proof is still pending. On any exposure, stop sharing output, have the owner rotate/revoke the affected DB/app/IdP credential and reconcile its protected configuration/ledger, then repeat validation. `prepare` reuses credentials and is not a rotation command; deleting state does not update passwords in retained databases. There is no generic rotation CLI in this implementation.

Final acceptance requires the owner report's exact code SHA, actual versions/commands, both completed browser protocol flows, privilege denials, two init passes, restart persistence, narrowly scoped cleanup, preservation regressions, private-output review and a named independent reviewer. The earlier audit/progress snapshot cannot satisfy that gate. Production deployment, paid/Cloud provisioning, customer IdP onboarding and final CI integration are outside this runbook.
