# Identity persistence and historical ownership operations

The persistence implementation is AUTH-03 / #25. Read the frozen identity contracts and browser-production ADR first. This module does not implement login, current membership/role authorization, routers, or anonymous-mode startup. Those owners must integrate the scoped ports and guard before enabling authenticated business access.

## Consumer handoff

- Construct `PostgresRepository(dsn, schema=..., projects_dir=<trusted root>)` for owned creation. Both workspace and actor are mandatory together; omitted values are only for unassigned trusted local operations. Owned writes reject omission. Worker reads/leases/fences retain their trusted methods.
- Use `get_task_for_workspace`, `list_tasks_for_workspace`, `project_ownership` and `project_ids_for_workspace` after current identity/workspace checks. Ownership lookup precedes capability/state/replay/file work. A foreign/unassigned resource or foreign task cursor returns the same not_found result.
- `TaskRecord` is strict internal persistence. `TaskRecord.public_dict()` validates through the separate `PublicTaskRecord` response model/schema and omits issuer/sub. #28 adds existing board/result URL links to that projection. Keep not-yet-authorized legacy HTTP paths closed in authenticated mode.
- Resource creator/ownership never change through ordinary operations. Task actor is the latest accepted operation; command actor is immutable; run actor belongs to the command actually claimed. Historical mapping preserves an unknown historical task actor as null. Worker/session bind/recovery never substitute creator or a later canceller.
- `IdentityStore` and `PersistentStorage` protocols are in `production/contracts.py`. Instantiate `PostgresIdentityStore` with trusted DSN/schema and an external 32-byte encryption-key ring plus active key ID. It never loads operator environments or calls Logto. Each `storage(cookie)` instance is request/session specific and implements synchronous get/set/delete.
- Login initiation persists a private interaction; consume it atomically before external protocol checks. The consumed payload is private and request specific. #26 performs every mandatory SDK/token/nonce/issuer/audience/signature/expiry/return-path check, using a request-local adapter for its consumed SDK data. Only `complete_login` creates authenticated state. An exception after consumption burns the interaction; no authenticated session exists for its binding cookie.
- Organization mutations persist a scoped intent and durable organization owner. Mark submitted before remote work, advance with the current revision and stable non-secret references, and reconcile unknown outcomes. A pending duplicate authorizes no POST. All 503/explicit unknown completion results retain recovery/serialization. `reject_prepared_organization_intent` only handles known pre-submit denials; it cannot release submitted work. Completion derives mandatory invitation transition and affected member-session invalidation; optional explicit values must match. #27 owns current authority, last-admin checks, mail, and remote reconciliation.

## Guard and classification

`production/ownership_guard.py` imports only the standard library. A genuine explicitly trusted file-only legacy root can check `has_guard` without PostgreSQL, Logto, cryptography or Pydantic installed.

The fixed marker is `.openmontage-authenticated`. **Any entry** at that name, including malformed, unreadable, directory or dangling symlink, blocks anonymous classification. `mark_authenticated` writes/fsyncs before protection is used and verifies existing classification. There is no removal/bypass function. Metadata contains the durable database binding and, for an owned project, its workspace. The marker is noncanonical and never rewrites project.json, checkpoints/artifacts/render/media, Studio writer/fence markers or Pi JSONL.

The migration adds a single persistent database binding. Backups retain it; repeat migrations and proper restores preserve it. An empty/replacement database gets a different binding and cannot silently adopt an old marker. Missing guard in a partial authenticated-files restore is not proof of legacy classification. #26 startup/HTTP, #28 owned initialization and #29 private marker/media handling must consume the same policy. Exclude the fixed marker and its temporary `.pending-` prefix from served files. Never roll back to an old binary that ignores the guard.

Owned task acceptance and import protect root and project before the owned DB commit. A failed SQL/guard/import can leave protection without ownership; this is conservative and explicit. Do not remove markers to retry. Resolve the trusted classification/backup/ownership mismatch through the operator recovery process. Quiesce older unguarded servers, streams, CLI writers and workers before activation/import. Existing writer locks are checked without creating/replacing them.

## Private operator input

The CLI is `python -m production.identity_migrate`. It is operator-only and has no HTTP claim route. Use a regular configuration file outside served/project roots, mode 0600. Required field is `database_url`; the isolated test handoff's `STUDIO_TEST_DATABASE_URL` is also accepted explicitly. Do not pass a DSN/password/token on argv. `postgres_container` (or the isolated handoff's `container`) selects the trusted cached PostgreSQL container when native pg_dump/pg_restore are unavailable.

Migration integration tests optionally use `STUDIO_TEST_POSTGRES_CONTAINER` for the isolated database container's PostgreSQL clients; when unset or empty, compatible native `pg_dump` and `pg_restore` must be on `PATH`.

Apply additionally requires private `authority_issuer`, `management_endpoint` (the fixed Logto Management API base) and a narrowly authorized `management_access_token`. The CLI makes only GET user/organization existence requests and compares actual returned IDs and the configured issuer. It has no offline-authority/skip switch. The catalog is an existence anchor, never membership permission. Account email/domain is never an identity key. Requests use the operator process's normal network environment; no project proxy configuration is written.

Each mapping entry contains `project_id`, optional matching `task_id`, `workspace:{kind,owner_id}` and `creator_user_id`. The creator must already exist as a complete backend identity. Personal owner must equal that creator; an organization must be registered from a trusted authority result and currently verified before apply. A CLI-only project stays file-only: mapping it creates no task/run and changes no file layout.

Private plan/backup files include backend identity and session-reference evidence. Keep them mode 0600 in a mode 0700 directory, outside served roots and Git. Retain protected encryption keys separately; the archive never includes raw encryption keys. Do not print or upload either private configuration or archive.

## Dry-run, backup and apply

First stop/quiesce all relevant writers and old unguarded serving processes. Set these task-specific shell variables privately to absolute local paths; their values are paths, not credentials:

```sh
python -m production.identity_migrate plan \
  --private-config "$IDENTITY_OPERATOR_CONFIG" --schema "$IDENTITY_SCHEMA" \
  --projects-root "$IDENTITY_PROJECTS_ROOT" --runtime-root "$IDENTITY_RUNTIME_ROOT" \
  --mapping "$IDENTITY_MAPPING" --output "$IDENTITY_PLAN"

python -m production.identity_migrate backup \
  --private-config "$IDENTITY_OPERATOR_CONFIG" --schema "$IDENTITY_SCHEMA" \
  --projects-root "$IDENTITY_PROJECTS_ROOT" --runtime-root "$IDENTITY_RUNTIME_ROOT" \
  --mapping "$IDENTITY_MAPPING" --backup "$IDENTITY_NEW_BACKUP_DIRECTORY"

python -m production.identity_migrate apply \
  --private-config "$IDENTITY_OPERATOR_CONFIG" --schema "$IDENTITY_SCHEMA" \
  --projects-root "$IDENTITY_PROJECTS_ROOT" --runtime-root "$IDENTITY_RUNTIME_ROOT" \
  --mapping "$IDENTITY_MAPPING" --backup "$IDENTITY_NEW_BACKUP_DIRECTORY"
```

Plan reports duplicate/missing/aliased projects, inconsistent task/project/Pi references, unknown identities/organizations, conflicting owners/creators and active/leased/pending/claimed work. Repeat identical ownership is a no-op. A live or expired-but-unreconciled run is not reassigned. Resolve every conflict before backup/apply; never override it with a flag.

Backup uses real PostgreSQL custom-format pg_dump for the entire selected schema. Ownership mappings remain selective; the archive covers every database-referenced project tree and exact run/Pi session, including unmapped historical productions, plus mapped file-only projects. All schema work must be quiescent. File inventory and pg_dump share an exported PostgreSQL snapshot, with existing project writer locks held through copying. Missing/aliased source references fail backup. The manifest binds database identity, mapping/snapshot and exact archive/file hashes. Apply verifies these bytes, rechecks the snapshot and locks affected DB records deterministically; a conflict aborts the entire batch. It modifies ownership metadata only, without changing task version/state/timestamps, gate/decision/budget/call/file journals, command order/status, lease/fence or session references. Accepted actions receive verified user actors; import audit separately records actual OS uid/euid/name and command provenance.

After apply, verify scoped reads, canonical/Pi file hashes, audit and both root/project protection before enabling authenticated traffic. The ordinary HTTP/role/SSE/media/browser acceptance remains with #26–#32. CLI exit success or store tests alone do not prove login or integrated rollout.

## Restore and key rotation

Rehearse into an empty **isolated** target schema/database and private file roots. The restore command refuses an existing schema and conflicting file bytes/aliases. It establishes root protection before restoring data, uses single-transaction pg_restore, checks the restored binding and requires every restored project/FileReference/session path in the archive inventory. It then marks owned projects, restores only missing files and verifies referenced file hashes before reporting success. Older subset archives with schema-wide rows and missing referenced files fail explicitly; restored rows and guards remain protected, never silently deleted to fit the archive. Matching existing bytes and markers remain unchanged. Partial failure retains protection and reports incomplete work; never serve it anonymously.

```sh
python -m production.identity_migrate restore \
  --private-config "$IDENTITY_ISOLATED_RESTORE_CONFIG" --schema "$IDENTITY_SCHEMA" \
  --projects-root "$IDENTITY_ISOLATED_PROJECTS_ROOT" --runtime-root "$IDENTITY_ISOLATED_RUNTIME_ROOT" \
  --backup "$IDENTITY_VERIFIED_BACKUP_DIRECTORY"
```

Do not drop/reset a live schema to satisfy the empty-target check. Test cleanup is limited to its freshly generated schema. Retain existing Studio/media/Pi volumes and all unrelated projects. No `down -v` or automatic marker removal. #32 owns final activation/rollback rehearsal.

AES-GCM envelopes bind schema, purpose, row identity, format version and key ID. Wrong/missing keys, tag failures and row swaps fail closed. To rotate, retain old decryption keys, select the new active key for writes and rotate/re-encrypt sessions through the store. Do not remove an old key while any retained ciphertext or backup requires it. Preserve source schema names for decryption on restore. Verify restored encrypted state with the retained key ring, expiry/revocation and actual least database privileges before exposure.

Migration runner, session/BFF and business-worker credentials need distinct least privileges at deployment. PUBLIC receives no private session/interaction/organization-intent/invitation access; grant workers only the existing business tables plus non-secret ownership/binding evidence they need. Do not supply the session key or private session table grants to workers. The isolated test account's NOSUPERUSER/NOCREATEDB/NOCREATEROLE boundary is tested separately from deployment role separation.
