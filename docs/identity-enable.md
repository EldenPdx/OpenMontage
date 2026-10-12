# Identity activation and recovery

This is the operator runbook for #32. Production activation remains a manual operator action. The final acceptance record must identify the exact application commit, both required CI jobs, independently reproduced scenarios, and the verified backup before these steps are used. Current #30/#31 integration and #32 operator rehearsals remain pending; this document is not a deployment signoff.

Read [the contracts](identity-contracts.md), [BFF configuration](identity-bff.md), [historical ownership operations](identity-migration.md), [the isolated environment](identity-environment.md), and [the validation matrix](identity-validation-matrix.md). Use their existing interfaces and preserve canonical projects, approvals, cost journals, leases, fences, and exact Pi session references.

## Prepare the maintenance window

Record the previous and proposed commits, database schema version/binding, canonical project and runtime roots, root-protection receipts, active session-key IDs, and the authority issuer. Keep that record and all private configuration outside served roots and Git, in a directory with mode 0700 and regular files with mode 0600. Retain the parent `.openmontage-auth-root-records` directories and any historical hashed receipts when backing up or moving a root. An inner marker alone is not the complete protection record.

Stop accepting production and membership writes. Quiesce the BFF, old unguarded Backlot/CLI processes, workers and active media/SSE streams. Confirm actual Pi process exit before recovery or lease changes; an expired lease does not authorize a second writer. Resolve pending/claimed commands and unknown paid-call outcomes through the existing recovery flow, without submitting replacement calls. Historical ownership mapping refuses active or unreconciled work.

Export only the selected private operator configuration into the current process. Do not pass passwords, tokens or encryption keys on command lines. Do not source a project-supplied environment file. The migration runner, BFF and Worker use distinct database credentials; the migration runner owns DDL. Runtime roles have no superuser, database/role creation, replication or BYPASSRLS rights. The Worker does not receive BFF/Logto client credentials or the session-key ring.

## Database privileges and execution credentials

Migrate with the existing `python -m production migrate` entry point under the migration credential, or the trusted Repository port with an explicit nondefault schema. `STUDIO_DATABASE_URL` is backend-only. The CLI uses the `studio` schema; select another schema through the existing trusted backend construction, not a browser argument. Runtime BFF and Worker startup does not replace this maintenance step.

For the selected schema, revoke PUBLIC access, grant each runtime role only schema USAGE and the tables/sequences it uses. The BFF needs business operations, backend identity registration, `auth_sessions`, `login_interactions`, `known_organizations`, organization intents/operation owners/invitations and the identity audit sequence. The Worker needs the existing production tables (`tasks`, `runs`, `requests`, `commands`, `events`, `gates`, `calls`, `file_intents`), their command/event sequences, and read-only ownership/binding evidence. It must be denied every private session/interaction/organization-intent/invitation table and schema DDL. Do not use `GRANT ALL ON ALL TABLES` for the Worker. The final rehearsal must show an accepted real production progressing with this grant set, alongside explicit denied private-table reads.

Native Logto 1.44.0 OSS exposes a coarse `all` Management API scope. Do not describe it as a granular provider grant. Use separate operator bootstrap and application M2M credentials, fixed trusted issuer/endpoint/resource settings, and the existing fixed-path/capability-checked application adapter. End-user roles never authorize arbitrary Management API paths, trusted provider changes or platform credential changes. Keep bootstrap secrets out of the long-running authority container, application workers, browser bundles and production records. Coordinate Console membership administration with the same last-admin invariant used by application writes.

## Back up before migration

Take coordinated, quiescent backups of the identity database, Keycloak database/configuration, business schema and canonical files/Pi sessions. Preserve the authority signing material, application/Management/IdP credentials, SSO policy overlay/version, exact issuer and private session-key ring separately. The OpenMontage archive intentionally excludes raw encryption keys; retaining only the archive cannot decrypt private sessions after restore. Record hashes and recovery order in the private operator ledger. Protect database dumps and raw mail/assertion/session evidence as secrets.

The dedicated [Native recovery drill](identity-native-recovery.md) defines the required owner-only maintenance handshake, fresh empty clone targets, independent source-retention observations and actual OIDC/SAML/old-session/media checks. Its lifecycle helper and real execution are still pending; the earlier business-only restore rehearsal does not satisfy coordinated Native recovery.

Use the released commands with absolute private path references:

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

Resolve every plan conflict before applying. Mappings explicitly name actual verified identities/organizations; email/domain matching is not ownership evidence. Apply independently verifies native authority existence and preserves task state/version/timestamps, approvals, budget journals, commands, fences and file/session bytes. Unknown historical actor stays unknown. Check every database-referenced project/output/session, including unmapped projects, in the complete backup inventory. A successful selective mapping is not proof that a partial backup is complete.

Rehearse the restore into an empty isolated target, retaining the original schema name for ciphertext binding and using separate trusted roots:

```sh
python -m production.identity_migrate restore \
  --private-config "$IDENTITY_ISOLATED_RESTORE_CONFIG" --schema "$IDENTITY_SCHEMA" \
  --projects-root "$IDENTITY_ISOLATED_PROJECTS_ROOT" --runtime-root "$IDENTITY_ISOLATED_RUNTIME_ROOT" \
  --backup "$IDENTITY_VERIFIED_BACKUP_DIRECTORY"
```

The command protects the destination before restoring rows/files. On partial failure, retain rows, receipts, files and private diagnostics; keep traffic disabled. Never drop a live schema, remove a guard, prune a volume or adopt an empty replacement database to make restore appear successful. Verify binding, all referenced bytes, exact Pi paths, preserved business journals and session decryption before accepting traffic.

## Enable the authenticated BFF

Use a protected `OPENMONTAGE_AUTH_CONFIG_FILE` with the fields in the BFF runbook. Select `mode: authenticated`, an exact HTTPS browser origin, exact Logto issuer/base, registered callback `origin/api/auth/callback`, registered localized logout destination, private application/M2M credentials, migrated database/schema and the protected 32-byte key ring. `http_development` is reserved for explicit isolated loopback tests. Missing trust or unavailable dependencies must yield a closed boundary, never implicit anonymous mode.

Retain Uvicorn `proxy_headers=False`; the Backlot CLI already supplies it. The reverse proxy must preserve the configured Host and supply the expected forwarded scheme/host from an explicitly trusted physical peer. Set exact `trusted_proxy_ips`; do not trust browser-provided forwarding headers. Verify actual HTTPS cookie Secure/HttpOnly/Path=/ /SameSite=Lax with no Domain, safe callbacks/history, CSRF/context rotation and immediate old-session denial. Do not log query strings, cookies, bearer tokens or raw protocol bodies.

Start the reviewed Worker separately with its business-only DSN and trusted Studio model/media configuration. The official pinned Pi retains its current isolated model/bridge environment; fixed media children use their existing restricted environment and disable `.env` loading. `python-dotenv>=1.2.0` is required for that disable flag. Verify actual child key names and private-table denials without printing credential values.

Before releasing the maintenance window, exercise two personal users, two organizations, cross-membership and all three roles through actual login, exact approvals, moving-video playback/download and both standard SSO protocols. Test foreign/unknown/historical 404, own viewer 403, stale-context 409, logout/role/removal/suspension/expiry and real dependency failures during each long stream. Check no duplicate command/email/submission after response loss or restart. Preserve the independent review and exact required main CI links.

## Keys, expiry and incident recovery

Retain old decryption keys when introducing a new active key; use the IdentityStore's existing key-rotation operation and verify old-session invalidation and new writes. Do not remove a key while retained sessions or backups still require it. AES-GCM schema/purpose/row/version/key bindings and ciphertext integrity must remain intact. Absolute session age is at most 24 hours, idle age at most 8 hours, and login interaction age at most 10 minutes; shortening them for labeled tests does not disable expiry. Logout revokes locally before remote sign-out. Re-enabling a native suspended user does not revive revoked provider grants.

On credential exposure, stop publication, rotate/revoke the affected authority/application/DB/IdP/session credential through its owner, reconcile protected files and ledgers, and repeat the affected positive and denial checks. Re-running `prepare` reuses credentials and is not a rotation command. Publish only sanitized status/location evidence; retain private raw diagnostics separately.

## Roll back without publicizing owned resources

Keep traffic disabled while restoring the compatible application/authority policy, coordinated databases, key ring, roots and external receipts. Use a reviewed guard-aware version compatible with the restored schema. Never start an older anonymous binary that ignores authenticated root protection, and never remove receipts/markers to permit anonymous access. Known owned roots remain protected even if the inner marker, identity configuration or database is missing/replaced.

Explicit trusted local mode is limited to proven file-only legacy roots. Adopted/restored roots require retained provenance and the original trusted database binding; an empty DB, a copied directory or an absent marker proves nothing. Health may remain a safe constant while business access is unavailable. Confirm every authenticated consumer still rejects the protected root and that untouched genuine legacy roots retain their supported local behavior.

The final #32 record must distinguish performed isolated rehearsals from production/customer/paid/Cloud operations. This task does not perform production cutover, real customer IdP onboarding, Cloud purchases or paid provider generation.
