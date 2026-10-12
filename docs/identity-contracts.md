# Identity contracts v1

Authority: [#22](https://github.com/EldenPdx/OpenMontage/issues/22) and [#23](https://github.com/EldenPdx/OpenMontage/issues/23). Read with [the identity ADR](adr/0001-personal-organization-identity.md), [production contracts](browser-production-contracts.md), and [browser-production ADR](adr/browser-production.md). This is a frozen implementation specification, not evidence that login already works. Contract changes update #23 and the affected Issues before implementation.

## Identity, ownership and trusted boundaries

| Contract | Required fields and meaning | Public visibility |
| --- | --- | --- |
| UserIdentity | `user_id`, `issuer`, `sub`; unique immutable `(issuer, sub)` from a verified token; email/name are display attributes, not identity or membership keys | `/me` returns `user_id`, safe display name and verified email; issuer/sub stay in backend evidence |
| WorkspaceContext | `kind: personal\|organization`, `owner_id`; personal owner is local user_id, organization owner is authoritative Logto organization_id; canonical key is `personal:<user_id>` or `organization:<organization_id>` | Selected workspace key/kind/id/name |
| ResourceOwnership | `workspace`, `creator: UserIdentity`; immutable for accepted tasks/projects; null means unassigned historical resource | Only within authorized resource DTOs |
| Actor | `identity: UserIdentity`, `workspace: WorkspaceContext`; captured on acceptance, never supplied in a request body | Safe actor user_id/workspace in authorized operation evidence |
| AuthContext | `identity`, `workspace`, `role: admin\|editor\|viewer\|personal`, `permissions`, `context_id`, `session_ref`, authority verification timestamp | Only safe identity/workspace/role/permissions/context_id projection |
| AuthSession | hash of random session identifier; authenticated identity, selected workspace, context_id, CSRF hash, absolute/idle expiration, revoked state; encrypted SDK storage and one-time interactions | Opaque cookie and CSRF/context values only |

Use one selected workspace per session. Membership in another organization does not authorize its resources while a different workspace is selected. Workspace selection verifies membership with the authority before committing. A fresh login selects personal space unless an explicitly allowed organization hint is verified; no first-organization or email-domain default. Organization permissions are resolved from the authoritative organization template, never from browser roles or stale token claims alone.

#25 implements DTOs in `production/contracts.py` and updates checked-in JSON schemas/fixtures together. All request DTOs retain `extra=forbid` and secret-safe validation; TaskCreate remains a creative request and does not acquire `owner`, `actor`, `issuer`, `sub`, or `organization_id` fields. Internal TaskRecord, RunContext and TaskCommand gain persisted ownership/actor without adding credentials. Public task serialization never exposes session_ref, encrypted storage, access/refresh tokens, raw identity assertions or M2M secrets.

The synchronous Repository keeps existing trusted worker methods. Add explicit scoped user reads (`get_task_for_workspace`, `list_tasks_for_workspace`, `project_ownership`) rather than an `auth_bypass` switch. Accepted task creation/commands receive backend-only `workspace` and `actor` arguments; both must be present together, or both absent in explicit local legacy mode. Worker claim/recovery derives them exclusively from committed task/command records. Existing get_task/list_tasks remain internal trusted interfaces and are never called directly by authenticated user routes.

Session storage provides create/get/update/revoke/atomic one-time interaction consumption; get refuses expired/revoked sessions. Each LogtoClient and Storage instance is request/session specific, never shared mutable user state. Ciphertext includes integrity protection and a version/key identifier; missing/wrong keys fail closed. Hash cookie identifiers at rest; cryptographic secrets and SDK state reside only in a private session store with least database privileges. Rotation invalidates the previous identifier atomically. Backup/rotation instructions preserve decryptability without printing keys.

## Roles and capability matrix

Capabilities use these names in `/me` and organization templates. Personal users receive their own business capabilities and may create an organization; organization management requires current admin membership. Unknown or empty roles grant nothing; multiple recognized roles grant their explicit capability union. A user with admin and viewer is still admin, not accidentally downgraded by ordering.

| Capability | Personal owner | admin | editor | viewer |
| --- | --- | --- | --- | --- |
| `resource:read` (tasks, project, events, media/results) | yes | yes | yes | yes |
| `production:create` | yes | yes | yes | no |
| `production:approve` (ordinary and cost gate, exact binding still required) | yes | yes | yes | no |
| `production:cancel` / `production:resume` | yes | yes | yes | no |
| `organization:create` (from personal space) | yes | no | no | no |
| `organization:read` (own organization details) | member list via own memberships | yes | yes | yes |
| `organization:manage` (member list, invite/cancel invite, remove/change role) | no | yes | no | no |
| Change platform credentials/endpoints/providers/execution configuration | no | no | no | no |

Creation may choose an already configured production profile as allowed by the existing contract. Roles do not grant provider/model changes outside the displayed approved scope, eliminate gates, reconcile unknown fees automatically, or permit arbitrary Management API calls.

## Session, CSRF, origin and workspace generation

Cookie `openmontage_session`: at least 256 random bits, opaque, HttpOnly, `Path=/`, `SameSite=Lax`, Secure on HTTPS; no Domain attribute. Local HTTP is allowed only in explicitly trusted loopback development mode. Default authenticated lifetime is 24 hours absolute and 8 hours idle; one-time login interactions expire after 10 minutes. Operators may shorten these values, never disable expiry. Login success, workspace switch and sensitive identity changes rotate cookie, CSRF and `context_id`; old identifiers are revoked. Failed callbacks never leave a partially authenticated session.

`GET /api/auth/me` returns a CSRF token and unpredictable `context_id` bound to the current identity/workspace. Every protected JSON write requires both `X-CSRF-Token` and `X-Auth-Context`, plus same-origin Origin and trusted Host. Authenticated writes with an old context receive 409 `auth_context_conflict` before replay or side effects. No blind refresh/retry is allowed for permission 403 or context mismatch. This prevents an old tab/pending write from being silently submitted under a newly selected space even when its URL has no resource id. Read responses expose `X-Auth-Context` so the UI can discard stale responses. SSE/media use the same cookie and resource ownership checks; they never need tokens in URL parameters.

Only administrator-configured origins/hosts/proxy identities are trusted. Forwarded headers are not accepted from arbitrary clients; exact callback and post-logout URIs are configured, never reconstructed from untrusted Host. No permissive CORS. Existing local studio_session/CSRF behavior remains only in explicit local mode, never an authentication credential. Session/CSRF changes use constant-time comparisons where appropriate; request/error logging redacts callbacks, credentials and tickets.

OIDC uses the official Python SDK and server-side authorization-code/PKCE flow. Generate independent state and nonce; bind both, the verifier and exact return path to a one-time stored interaction. Verify returned state, the ID token nonce, issuer, signature/JWKS, audience/authorized party and expiry; add missing mandatory verification around the official SDK if its locked version does not provide it. Consume the interaction atomically so concurrent replay cannot authorize twice. Return paths must be same-origin localized library/studio/board or invitation routes; reject absolute/protocol-relative URLs, encoded escapes/backslashes/control characters and unknown routes. No email-based identity linking. OAuth codes/assertions are allowed only at the necessary protocol callbacks and are redacted from logs/history after consumption; no application credentials or tokens in ordinary URLs.

## BFF and organization HTTP interface

JSON uses the existing `{"error": ErrorDTO}` envelope. Safe DTOs below have no implicit extra fields. Success examples denote required shape; timestamps use UTC ISO-8601. All unsafe API methods require current session and CSRF/context; business writes and workspace selection additionally require current authority. Local logout revokes the session even when authority is unavailable or the account has been disabled. Organization/member/invitation mutations also require Idempotency-Key; logout/workspace selection use one-time session rotation instead of replaying across a changed session.

| Method/path | Input | Successful behavior | Authority/capability |
| --- | --- | --- | --- |
| GET `/api/auth/login` | optional validated `return_to`, `organization_id`, `screen_hint=sign_in\|sign_up` | 302 to configured Logto; stores new isolated expiring interaction | Public login initiation; organization hint never grants membership |
| GET `/api/auth/callback` | OIDC code/state or protocol error | consume once, rotate session, 303 to validated return_to | Public only for the bound interaction |
| GET `/api/auth/me` | none | `{"mode":"authenticated","user":{"user_id","name","email","email_verified"},"workspace":{"kind","owner_id","key","name"},"role","permissions":[],"context_id","csrf_token","expires_at"}` | 401 if signed out; 503 authority unavailable; local mode reports `mode:"local"` without a fabricated Logto identity |
| POST `/api/auth/logout` | `{}` | revoke local session even if remote sign-out fails; 200 `{"logout_url": <configured Logto end-session URL or null>}`; browser follows URL to clear IdP session | Session/CSRF/context; no task cancellation; unavailable remote sign-out is explicit safe status |
| POST `/api/auth/workspace` | `{"kind":"personal\|organization","owner_id":<id>}` | verify owner/membership, rotate, 200 current `/me` DTO | Personal owner must equal session user_id; org must be a current membership |
| GET `/api/organizations` | `limit=1..100`, optional opaque `after` | `{"items":[{"organization_id","name","roles":[]}],"next_cursor":null\|string}` | Only current user's memberships |
| POST `/api/organizations` | `{"name":<1..100 chars>}` | 201 organization DTO after organization+creator admin confirmed | Personal `organization:create`; Idempotency-Key |
| GET `/api/organizations/{organization_id}` | none | `{"organization_id","name","roles":[]}` | Selected matching organization; `organization:read` |
| GET `/api/organizations/{organization_id}/members` | pagination as above | `{"items":[{"user_id","name","email","roles":[]}],"next_cursor"}` | `organization:manage`; no platform identity dump |
| POST `/api/organizations/{organization_id}/invitations` | `{"email":<verified-target email>,"role":"admin\|editor\|viewer"}` | 201 `{"invitation_id","organization_id","email","role","status","expires_at"}`; sends through test/approved mail connector | `organization:manage`; no raw ticket in list/events |
| GET `/api/organizations/{organization_id}/invitations` | pagination as above | safe invitation list envelope | `organization:manage` |
| DELETE `/api/organizations/{organization_id}/invitations/{invitation_id}` | none | 200 safe cancelled invitation; subsequent acceptance denied | `organization:manage`; CSRF/context/key |
| POST `/api/invitations/accept` | `{"ticket":<one-time secret>}` | 200 safe organization DTO after verified email/identity matches and membership+role confirmed | Logged in verified intended identity; session personal space permitted; CSRF/context/key |
| PATCH `/api/organizations/{organization_id}/members/{user_id}` | `{"role":"admin\|editor\|viewer"}` | 200 safe member DTO; only recognized role assignment | `organization:manage`; protect last admin |
| DELETE `/api/organizations/{organization_id}/members/{user_id}` | none | 204 after confirmed removal | `organization:manage`; protect last admin including self-removal |

Invitation ticket is a random restricted bearer value whose hash is stored; the display landing URL may carry it only long enough to place it in memory and remove it from browser history. Use `Referrer-Policy: no-referrer`, no third-party resources, no ticket in telemetry/localStorage/events. Invitations expire within 7 days, can be cancelled, and accept once for the intended verified email/identity. Equal emails never merge user identities. Same actor/workspace/key/payload replays the committed membership result; a different identity cannot use that replay. A cancelled/expired/used ticket yields 409 `invitation_invalid` without target secrets.

All list cursors bind endpoint, actor and workspace; cross-context or invalid cursors yield 422 `invalid_cursor`, never global counts/items. Existing Studio `/tasks` retains its list response and existing slug cursor shape for compatibility; its SQL filter applies before limit/cursor and its next cursor can only name an accessible task. Cross-workspace task cursor returns 404 without existence disclosure. Existing library list shape remains compatible and filtered before counts/summaries.

## Existing public entry points and authorization order

For every protected request: validate trusted Host/Origin → validate session/context → verify authority and selected workspace/capability → verify resource ownership → perform scoped replay/version/state work → read/commit/send. Invalid or unauthenticated requests never execute business work. A valid member without a capability gets 403; a non-owned/foreign/unassigned resource gets uniform 404 before task/gate state, file existence, replay conflict, content length, ETag or Range are exposed.

| Entry points (all aliases share policy) | Requirement | Acceptance scenarios / owner |
| --- | --- | --- |
| GET `/api/studio/config` | authenticated selected workspace; sanitized readiness/profiles; current CSRF/context | U1/U2 cannot see secrets; viewer reads configured options but cannot change them / #28 |
| POST `/api/studio/tasks` | `production:create`; inject owner+creator+actor | Same key in U1/U2/A/B produces separate tasks; spoofed actor/owner rejected; pending old tab fails / #28 |
| GET `/api/studio/tasks`, `/api/studio/tasks/{task_id}` | `resource:read`; scoped query before cursor/limit/details | U2/B gets no U1/A task id, gate, cost or pending-call details / #28 |
| POST `/api/studio/tasks/{task_id}/approvals/{gate_id}/decision` | owned task + `production:approve`; exact existing gate/version/hash/scope | viewer denied; actor is session user; stale binding conflict only after ownership / #28 |
| POST `/api/studio/tasks/{task_id}/cancel`, `/resume` | owned task + matching capability; trusted command actor | Existing accepted command stays in original workspace after logout/switch; old-key foreign replay denied / #28 |
| GET `/api/studio/tasks/{task_id}/events` | owned task + `resource:read`; authorize subscribe/replay and every send | Foreign Last-Event-ID cannot disclose events; revoked stream closes within 30s / #28 |
| GET `/api/studio/tasks/{task_id}/result` (`download=1`, Range, conditional GET) | owned task + `resource:read` before verified file or headers | Authenticated owner gets playable verified bytes; foreign request no bytes/304/metadata / #28 |
| GET `/api/library` | `resource:read`; owner filter before summaries/counts | U1 personal and org A lists separate; historical unassigned hidden / #29 |
| GET `/api/project/{project_id}/state` | owned project + `resource:read` before files/state | Same guessed path under foreign session 404 / #29 |
| GET `/api/library/events`, `/api/project/{project_id}/events` | scoped stream; reauthorize on heartbeat and before sending | No foreign project ids or notifications; removal/logout/outage terminates / #29 |
| GET `/media/{project_id}/{file_path:path}`, `/thumb/{project_id}/{file_path:path}` | owned project + `resource:read` before thumbnail generation/file read/cache/Range | Private cache, range playback, cross-user conditional requests rejected; symlink and sandbox rules retained / #29 |
| GET `/`, `/studio`, `/p/{project_path:path}` (legacy redirect), `/zh`, `/ja`, `/en`, `/{locale}/studio`, `/{locale}/p/{project_path:path}` | library/studio shell may show signed-out sign-in shell with no business data; project-specific page requires ownership, or signed-out redirect to safe login | No title/artifact inline leakage; signed-in foreign board 404; localized aliases identical / #29 + #31 |
| GET `/ui/{path}` | public static assets only; never fixture/private/runtime/project files | No secrets in bundled files / #26 registration + #31 |
| GET health/readiness if provided | public constant liveness; readiness safe 503, no identity/config/database details | No business/identity disclosure / #26 |

Private authenticated responses, HTML containing identity, media/thumb/results and SSE use `Cache-Control: private, no-store`, `Vary: Cookie`, and `X-Content-Type-Options: nosniff`; no shared authorized response cache. Authorization precedes conditional response evaluation, including 304, HEAD if enabled, multipart/single Range and 416. Media retains existing MIME, resolved-root/symlink protection and HTML/SVG sandbox CSP. Public static assets may retain existing no-cache behavior. Identity-bearing shell data is fetched after authorization and discarded on context changes.

## Errors, replay, revocation and recovery

| HTTP/code | Observable rule |
| --- | --- |
| 401 `unauthenticated` | Missing, expired, revoked or disabled identity; safe login recovery only |
| 403 `forbidden` | Current identity lacks capability; no automatic retry |
| 403 `csrf_invalid` | Missing/stale CSRF, invalid Origin or trusted boundary failure; client may refresh only while identity and workspace remain identical |
| 404 `not_found` | Foreign, unassigned or absent resource/organization/member; no distinguishing details |
| 409 `auth_context_conflict` | Workspace/session generation changed; discard pending write; never resend into new context |
| 409 `invitation_invalid` / `last_admin` | Cannot accept invitation or remove/downgrade final admin; no mutation |
| 409 existing conflicts | Version, gate, fingerprint, fence etc.; only after authorization |
| 422 `invalid_input` / `invalid_cursor` | Strict safe input error; never echo secrets |
| 503 `identity_unavailable` / existing `dependency_unavailable` | Authority/SDK/session DB unavailable; fail closed, no anonymous fallback |

Idempotency scope is `(issuer, sub, workspace kind, owner_id, operation, resource_id, key)` with canonical non-secret request fingerprint. It is authenticated and authorized before replay lookup. Same scope/key/payload returns accepted evidence; changed payload conflicts; a different actor or workspace cannot read that evidence. Accepted command ownership cannot change through retry/resume. This adds no new meaning to existing production version/gate/budget rules.

Every sensitive write verifies current authority before acceptance, even on replay. Local logout revokes immediately. Business member/role mutations invalidate affected local sessions/authority cache immediately; external Logto Console changes and account suspension are reflected within 30 seconds. Readers/streams revalidate at least every 15 seconds (absolute maximum accepted cache age 30 seconds), and streams authorize immediately before each sensitive frame. An authority query failure does not use an old successful cache as fallback. Long media responses recheck at bounded intervals before further bytes; data already delivered before revocation cannot be recalled. No new stream/frame/chunk is accepted after the revocation window. Queued notifications are filtered before emitting, and a revoked stream ends rather than publishing sensitive error details.

Organization operations journal an intent keyed by authorized scope before Management API work, with stable operation id, target, requested role, actor and stage/result. A per-organization DB lock/serialization protects the final admin under concurrent writes; hold the logical serialized operation until remote outcome is known/reconciled, not a long open DB transaction during HTTP. Responses lost after remote acceptance are reconciled from authoritative resources with stable intent markers before retry. Uncertain outcome stays explicit 503/recovery state without another create/invite/grant. The M2M adapter exposes only required user-status/membership/role/organization/invitation operations, accepts validated DTOs, and cannot proxy arbitrary paths/roles/tenant ids. #24 pins and proves available least privileges; any vendor coarse manage scope is documented and compensated by the fixed adapter and inaccessible credentials.

Audit entries record backend actor identity/workspace, operation/target, acceptance/result and UTC timestamp; credentials, tickets, raw assertions and unnecessary foreign profile fields are forbidden. Organization operation storage is implemented by #25 so #27 does not mutate Repository concurrently. Audits are scoped internally, not an unrestricted public browse endpoint.

## Historical data and compatibility

Existing rows acquire nullable ownership through additive reentrant SQL migration. Historical unassigned resources are invisible in authenticated mode, including direct project/media paths, and are not assigned to the first login. CLI remains offline and file-compatible; explicit loopback local mode reads legacy resources as before but never impersonates a Logto user.

Trusted migration entry point is an operator-only CLI, never a browser claim API. It reads an explicit resource→workspace+creator mapping, validates known identities/organizations and project references, dry-runs conflicts, requires a backup before apply, locks affected records and applies atomically with audit. Repeat identical mappings is a no-op; incompatible ownership, missing resources, active lease/run/queued work or conflicts fail without partial reassignment. Ownership of accepted work is immutable. Preserve file bytes, project layout, exact hashes/revisions, journals, session JSONL references and command/fence evidence. Restore rehearsals use isolated databases/files; rollback never exposes authenticated resources to anonymous non-loopback clients.

Authenticated mode refuses missing issuer/app/client secret/session encryption key, untrusted callbacks/hosts or unavailable dependencies. Local anonymous mode is explicitly enabled, bound to loopback and checks actual incoming peer/Host; non-loopback/wildcard bind or untrusted proxy exposure fails startup/access. Local mode is not an outage fallback. Documentation never embeds operator credentials or local network proxy settings.

## Module ownership and actual dependencies

Each implementation owner uses its own `codex/auth-<issue>-<purpose>` branch/worktree and records absolute cwd, writable paths and evidence. Reviewer is a different agent on an exact commit and independently reproduces public behavior. Orchestrator is the sole merger/registrar. No other owner writes public contracts, Repository, shared fixtures or CI without explicit reassignment serialized with the designated owner.

| Issue | Owned paths / output | Start only after closed evidence | Final acceptance |
| --- | --- | --- | --- |
| #23 | this document, identity ADR, glossary; all contract changes coordinated here | none; alone | Independent endpoint/design review, docs checks and merged CI; no login claims |
| #24 | `auth-runtime/`, `compose.auth.yaml`, `requirements-auth.txt`, `tests/fixtures/identity/`, environment docs | #23 | Real pinned Logto/PG/mail/standard OIDC+SAML IdP, two init passes/restart/safe fixture cleanup; existing Pi/PG retained |
| #25 | `production/contracts.py`, `production/repository.py`, additive migrations, `production/identity_store.py`, schemas/studio, dedicated Repository/identity tests and existing shared studio fixture sync | #23 | Real DB upgrade/restart/concurrency/scoped replay/explicit mapping and encrypted-session isolation; no API implementation |
| #26 | `production/auth/`, `production/api/security.py`, runtime auth config; `backlot/server.py` app factory/registration; lock/dependency integration | #24 + #25 | Real two-user OIDC/CSRF/negative callbacks/authority outage/revocation and CLI/local compatibility |
| #27 | `production/organizations.py`, `production/api/organizations.py`, dedicated org API tests | #24 + #25 + #26 | Real create/invite/mail/accept/roles/remove/recovery/last-admin matrix; uses #25 store, no factory edits |
| #28 | `production/api/tasks.py`, approvals/events routers, production approvals/task-service/worker identity consumption; dedicated Studio auth tests | #25 + #26 | Real identities/PG/Pi, scopes/gates/commands/SSE/result/Range/replay; no contracts/Repository edits |
| #29 | `backlot/access.py` and extracted project/media/event handlers in `backlot/projects_api.py`; dedicated Backlot auth tests | #25 + #26 | Every legacy/localized/direct/cache/media/SSE entry; no app-factory edits; Orchestrator moves/mounts existing handlers serially |
| #30 | `auth-runtime/sso/`, dedicated SSO protocol/browser tests, SSO onboarding docs; no shared bootstrap edits in parallel | #24 + #26 + #27 | Real application-initiated OIDC and SAML with standard IdP, negative assertions/linking/membership; no customer provisioning |
| #31 | `backlot/ui/` helpers/account controls/i18n and dedicated identity browser tests | #26 | Wait #27–#30 true integrated behavior: two users/two orgs/multi-tab/pending-write isolation, real invite+SSO, 390px/keyboard/zh-ja-en |
| #32 | final cross-module validation, CI, activation/migration/backup/rollback docs | #23 for matrix preparation only | #24–#31 closed; real full chain and target main CI; gaps fail explicitly |
| #22 | final dependency/PR/evidence index | all children closed | Last manual closure, final main CI and clear unverified production/customer/paid items |

Existing factory contains project routes; extraction/mounting is a shared-entry edit reserved for #26/Orchestrator, never concurrent #29 edits to server.py. #28 requests any needed DTO/Repository extension from #25, and #27 uses the predeclared session/organization intent store. Shared identity fixture belongs only to #24; new scenario-specific tests belong to their Issue. CI belongs only to #32. Dependency manifests have a single owner during integration. Review findings go back to the original owner, followed by verification on the new SHA.

## Behavior matrix and evidence gate

Test seams already approved in #22/#23: real browser and public HTTP APIs; limited real PostgreSQL upgrade/transaction/storage seams where persistence itself is the public contract. TDD adds one failing observable scenario and minimal implementation per cycle. Fault fixtures may inject outages, invalid signed protocol data or response loss; they do not replace real successful login/SSO/production validation.

| Dataset/scenario | Required observations | Primary Issues |
| --- | --- | --- |
| Signed out; independent personal U1/U2; organizations A/B; one user in A+B | identity/workspace explicitly verified; 401/403/404 distinctions; all endpoint/list/metadata isolation | #26–#32 |
| A admin/editor/viewer and B admin; removed/disabled member | role matrix, spoofed owner/actor/organization rejected, last-admin serialized protection; no gateway/command/cost side effects on denial | #25–#32 |
| Real registration/login/reset/logout/refresh/restart; concurrent U1/U2 | verified issuer/sub; one-time state/nonce/PKCE checks, cookie rotation, encrypted persistent isolated SDK state | #24–#26, #31–#32 |
| Wrong/replayed state/nonce/issuer/audience/signature/expiry, return URL/Host/Origin/CSRF | request rejected through public callback/API; no partial authenticated session or leaked secrets | #26, #30, #32 |
| Create organization → invite → local mail → intended verified identity accepts → role change → remove | real authoritative memberships/roles; wrong email/expired/cancelled/replayed ticket denied; duplicate key and lost-response restart cause no duplicates | #24, #27, #31–#32 |
| Real standard IdP OIDC and SAML → Logto → BFF | correct organization only after verified membership; same email different identities separate; denial/cancel/outage and invalid/replayed assertions fail | #30–#32 |
| Real Pi/PG/controlled model+media/FFmpeg/Chromium full production | personal+organization creation, bound ordinary/cost approvals, original command owner, real advancing playback/download/hash/ffprobe; logout does not cancel accepted work | #28, #31–#32 |
| Download/result/media/thumb, Range/HEAD if supported/ETag/304/416, sandbox/symlink | authorize before bytes/cache metadata and thumbnail work; valid playback; foreign and revoked clients receive no protected bytes | #28–#29, #32 |
| Task/project/library SSE, foreign cursors, queued foreign changes, logout/remove/downgrade/disable/outage midstream | scope before replay/frame; no foreign ids; close within 30 seconds; reconnect reauthorizes; no new sensitive frames/chunks afterward | #26, #28–#29, #32 |
| Same key across users/spaces; lost response; multi-tab login/switch/back/refresh | authorized scoped replay only; stale context/pending requests not transferred; valid same-context retry retains key and exact binding | #25, #28, #31–#32 |
| Old real PG + canonical files, explicit mapping dry-run/apply/reapply/conflict/backup restore | legacy default hidden, atomic audited import, active tasks refused; files/gates/cost/queue/fence unchanged; rollback cannot reopen private data | #25, #32 |
| Missing/dead Logto/PG/IdP/Pi/FFmpeg/browser; secrets scan | required validation fails, never skip/mock pass; no secrets in DTOs/events/errors/logs/URLs/browser storage/git | #24–#32 |

Per-Issue evidence includes exact commit SHA, actual dependency/runtime versions, actual commands with secrets removed, externally observable results and a named independent reviewer with reproduced scenarios. Focused PR uses `Refs #N`, never auto-close wording. User-authorized Orchestrator integrates, checks required CI and integrated regression, posts evidence/reviewer sign-off and then manually closes that Issue. `ready-for-agent` is specification readiness only. The historical planning-stage sentence in issue bodies does not override the user's later implementation/merge authorization.

No paid provider calls, Cloud procurement, production deployment/cutover or real customer IdP setup are part of these checks. Final report lists these separately as unverified, alongside every genuine incomplete validation rather than silently relaxing the acceptance gate.

## Primary documentation reviewed

The official documentation was fetched through Context7 for SDK storage and organization flows; #24/#26 verify the locked releases themselves before relying on behavior.

- [Python server SDK](https://docs.logto.io/quick-starts/python): implement request-specific persistent Storage.
- [Organization template](https://docs.logto.io/authorization/organization-template): roles/permissions are shared authoritative templates.
- [Organization invitation](https://docs.logto.io/end-user-flows/organization-experience/invite-organization-members): management invitation, mail and acceptance are distinct operations.
- [Sign-out](https://docs.logto.io/end-user-flows/sign-out): revoke local state and clear the Logto browser session.
