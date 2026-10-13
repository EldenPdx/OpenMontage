# Coordinated Native identity recovery acceptance

This is the dedicated #32 validation drill. The portable lifecycle helper is
`auth-runtime/native_recovery.py`; real execution remains **PENDING** until its
exact candidate report passes. All #30 and #31
Native consumers must first report safe. The Orchestrator exclusively owns the
helper, stop/recreate operations and private environment changes. The test does
not run Docker lifecycle commands or create Native databases.

The test uses the fixed `auth30` application on `http://127.0.0.1:4765`, its
already proved standard OIDC and signed persistent SAML subjects, and their
explicit frozen memberships. It does not seed JIT identities or grant members.
Its source business schema is a fresh `auth32_test_<32 hex>` schema. BFF and
Worker each start with their actual restricted principal; migration credentials
are never substituted into either runtime.

The source control produces an actual two second video through official Pi,
the local controlled model/media boundary and FFmpeg using the OIDC editor in
organization A. The SAML viewer in A must independently sign in and read the
same authorized task and exact media bytes. Both real session cookies remain
private in test memory. The real BFF, browser, Worker, model listener and media
fixture manager exit before the snapshot request.

## Private fixed helper contract

Create a new 0700 control directory and a 0600 `plan.json`. Pass its absolute
path as `AUTH32_NATIVE_RECOVERY_PLAN`. Retain any earlier failed directory and
choose a new run ID; a previous request, acknowledgment, manifest or receipt
causes failure before service work. The plan has exactly these fields:

```json
{"schema_version":1,"run_id":"<32 lowercase hex>","owner_state_dir":"<absolute private lifecycle-owner .state>"}
```

The CI owner first runs `native_recovery.py prepare --owner-state <owned .state>
--control <new private directory>`. It starts `native_recovery.py serve --plan
<plan.json> --timeout <bounded seconds>` alongside the required browser test.
The helper validates the actual Compose working-directory labels before stopping
writers. Its interruption handler restores original environments and services;
the parent waits for that recovery before cleanup.

`AUTH_TEST_FIXTURE` must point to that owner's unchanged `fixture-auth30.json`.
`AUTH32_DATABASE_CONFIG_FILE` supplies the private source/target migration, BFF,
Worker and original session-key inputs described in `identity-enable.md`. The
target business database must be a separate, owned, empty test database. The
test retains source and target schemas and files for diagnosis.

The test writes `manifest.json` containing only this drill's schema, source and
target roots, private operator-config references, explicit ownership mapping,
business archive destination, Native source/clone database names, actual pinned
container/image/seal observations, public signing-key/metadata hashes, private
ledger/env hashes and source business table/sequence/file digests. The helper
must verify every reference belongs to the fresh control directory and the
fixed source/target configuration, check the exact manifest SHA-256, and refuse
foreign/reused databases, schemas, symlinks, paths, identities or namespaces.

The two Native clone names are fixed by the test:
`auth32_restore_logto_<run_id>` and `auth32_restore_keycloak_<run_id>`. Require
that they do not exist before creation. Use the already pinned Logto 1.44.0,
Keycloak 26.8.0, PostgreSQL 17.11 images and the same two read-only SSO policy
mounts. Keep the original databases, image provenance, private keys, service
network namespace and all other fixture ledgers. Never drop an original
database, re-seed a realm, change shared login policy, replace signing keys or
deploy production.

The pinned original database names are `auth_logto` and `auth_keycloak`.
`logto` and `keycloak` in evidence label services rather than alternate source
databases. Business-only and coordinated Native drills use separate empty target
databases; neither drill clears the other's restored data.

`request.json` is test-owned; `ack.json` is helper-owned. Write atomically with
0600 permissions and fsync. Each has exactly:

```json
{"schema_version":1,"run_id":"<same run>","phase":"<fixed phase>","manifest_sha256":"<exact manifest SHA-256>"}
```

| Request | Required helper action and acknowledgment |
| --- | --- |
| `source_quiesced` | Verify all Native consumers are safe; stop Logto and Keycloak writers without stopping identity PostgreSQL or Mailpit; acknowledge `native_quiesced`. |
| `snapshot_allowed` | Only after the test has observed stopped Native writers and unchanged source data, take coordinated whole Logto + Keycloak custom-format dumps and the whole business schema/file/Pi-session archive. Restore into fresh Native clones and the separate empty business target using the same schema/AAD binding, original key and real runtime grants. Point only the private Native env at the clones, recreate only the required Native services, verify health; acknowledge `restored_clones_ready`. |
| `restore_original` | Stop cloned Native writers, compare the frozen original Native inventories, restore the exact original private env, recreate services against original databases and verify real health; acknowledge `original_ready`. |

The helper may acknowledge `operator_failed` during setup to make failure
explicit. It must still handle `restore_original`. Test assertions, timeouts
and failures always request original recovery in `finally`; an absent matching
`original_ready` acknowledgment fails the drill. Each wait is bounded to 180
seconds. The helper must have its own unconditional recovery `finally` for test
process interruption, failure, stale input or timeout. New clone databases and
private recovery files stay retained; cleanup is a separate owner decision.

## Complete archive and source-retention evidence

The helper writes 0600 `logto.dump`, `keycloak.dump`, and `native-archives.json`.
Do not copy only fixture users or export an application realm in place of the
full Native databases. Include signing/encryption/private keys, users, SSO
bindings, organizations, role grants, clients, connector configuration and
native interaction state. Inventory every public source table while Native
writers are stopped. The test independently observes the original `logto` and
`keycloak` databases through the unchanged owner PostgreSQL container, using
only fixed `psql -X -qAt -U auth_bootstrap` SELECTs with
`PGOPTIONS=-c default_transaction_read_only=on`. For each table, SHA-256 hash the
exact UTF-8 stdout of `SELECT to_jsonb(t)::text ... ORDER BY to_jsonb(t)::text`
(one newline after each row). Hash every public sequence's `last_value,is_called`
and ordered `pg_largeobject` `loid,pageno,encode(data,'hex')` output similarly.
The helper must use that same byte format. Missing Unix authentication or a
different pinned schema layout fails explicitly; do not change authentication
or skip inventory checks. Compare the same original databases after cloned
writers stop and before original writers restart. The original Native relation,
sequence, large-object inventory and every digest must remain equal to the
test's independently observed frozen snapshot.

```json
{
  "schema_version":1,
  "run_id":"<same run>",
  "manifest_sha256":"<exact manifest SHA-256>",
  "databases":{
    "logto":{
      "source_database":"<original Logto database>",
      "clone_database":"auth32_restore_logto_<run_id>",
      "dump_sha256":"<actual whole custom archive SHA-256>",
      "source_frozen":{"public.<table>":"<64 lowercase hex>"},
      "source_after_clone":{"public.<table>":"<identical digest>"},
      "source_sequences_frozen":{"public.<sequence>":"<64 lowercase hex>"},
      "source_sequences_after_clone":{"public.<sequence>":"<identical digest>"},
      "source_large_objects_frozen":"<64 lowercase hex>",
      "source_large_objects_after_clone":"<identical digest>"
    },
    "keycloak":{
      "source_database":"<original Keycloak database>",
      "clone_database":"auth32_restore_keycloak_<run_id>",
      "dump_sha256":"<actual whole custom archive SHA-256>",
      "source_frozen":{"public.<table>":"<64 lowercase hex>"},
      "source_after_clone":{"public.<table>":"<identical digest>"},
      "source_sequences_frozen":{"public.<sequence>":"<64 lowercase hex>"},
      "source_sequences_after_clone":{"public.<sequence>":"<identical digest>"},
      "source_large_objects_frozen":"<64 lowercase hex>",
      "source_large_objects_after_clone":"<identical digest>"
    }
  }
}
```

The test checks actual custom archive headers/hashes and `pg_restore --list`
using the unchanged identity PostgreSQL container; TABLE DATA entries must
equal the full frozen table inventory. The helper separately retains exact
private SQL/dump/restore transcripts, clone-creation proof, complete relation,
sequence and large-object inventory, consistent-point timing and final health
diagnostics. Do not publish dumps, Native env, cookies, DSNs, assertion XML,
keys, browser failure HTML or raw transcripts to CI artifacts.

## Required external acceptance

The test verifies actual cloned `DB_URL` and `KC_DB_URL` targets with Docker
inspect, unchanged pinned image IDs and actual mounted policy bytes. Matching
public JWKS and SAML metadata prove signing-key continuity. Each original BFF
cookie with the original key must return `me` 200 with its original subject and
role and the exact authorized media bytes. Fresh real standard OIDC and SAML
sign-ins must again bind the same Native subject, IdP identity, issuer, explicit
membership and role, then read the original resource. The restored Worker starts
with its real restricted DSN and preserves the completed task. The separate
manual test `test_coordinated_native_restore_rejects_a_wrong_session_key`
retains the same full drill and also requires an otherwise identical BFF with a
wrong key to deny both `me` and media with 503. Automatic positive restore does
not execute that key-tampering case or report it as passed.

After `original_ready`, the test independently verifies actual original database
targets, Native health, pinned images, seal bytes, JWKS/metadata, exact private
env and all fixture-ledger hashes, plus every retained source business
table/sequence/file digest. It writes a private `receipt.json` only after all
checks pass. These observations do not replace the Orchestrator's independent
archive/lifecycle review. Missing helper inputs or any failed observation is a
required failure, never a skip. The cold CI phase must use the same portable
helper contract; a private handoff script is not a final CI implementation.
