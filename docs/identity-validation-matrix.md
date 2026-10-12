# AUTH-10 / #32 identity validation matrix

**MATRIX PREPARATION ONLY — 2026-10-12. Every scenario below is PENDING and unexecuted.** This document specifies future acceptance observations; it supplies no working-login, SSO, authorization, migration, or media acceptance result. No identity test, fixture, CI, production code, or contract is added in this preparation.

Authority: [#32](https://github.com/EldenPdx/OpenMontage/issues/32), [#22](https://github.com/EldenPdx/OpenMontage/issues/22), and [#23](https://github.com/EldenPdx/OpenMontage/issues/23), [#24](https://github.com/EldenPdx/OpenMontage/issues/24), [#25](https://github.com/EldenPdx/OpenMontage/issues/25), [#26](https://github.com/EldenPdx/OpenMontage/issues/26), [#27](https://github.com/EldenPdx/OpenMontage/issues/27), [#28](https://github.com/EldenPdx/OpenMontage/issues/28), [#29](https://github.com/EldenPdx/OpenMontage/issues/29), [#30](https://github.com/EldenPdx/OpenMontage/issues/30), [#31](https://github.com/EldenPdx/OpenMontage/issues/31). Read with [identity contracts](identity-contracts.md), [identity ADR](adr/0001-personal-organization-identity.md), [browser-production ADR](adr/browser-production.md), [production contracts](browser-production-contracts.md), and [GLOSSARY](../GLOSSARY.md). This matrix applies those contracts; interface changes return to the contract/module owner.

Preparation owner: the assigned #32 implementation owner. Checkout: `/Users/eldenpdx/.codex/worktrees/auth-32-validation/OpenMontage`; branch: `codex/auth-32-validation`; base: `9b723673ffbdfe990b429b3e0e3641bf4a8dbe81`. Sole writable deliverable in this phase: this file. Parent owns PR creation, integration, GitHub evidence, and manual closure. Original dirty source is outside this change. Shared Serena remains on the other project; this worktree has no CodeGraph index and none is created.

## Readiness and evidence already available

Only #23 is complete. [PR #33](https://github.com/EldenPdx/OpenMontage/pull/33) and its [published acceptance](https://github.com/EldenPdx/OpenMontage/issues/23#issuecomment-6118047003) record independent reviewer **Ohm**, zero remaining design blockers, and the exact commit above. [Main CI 38205087527](https://github.com/EldenPdx/OpenMontage/actions/runs/38205087527) completed successfully on that SHA. The published baseline summary is **2479 passed, 17 skipped, 3 xfailed**; these are existing regressions, not identity acceptance. The reviewer also reproduced 28 existing contract checks. No baseline skip/xfail is silently inherited as permission to skip required identity flows.

Existing environment/route audits and the #23 reviewer report were reused as **private orchestration evidence**, without a new broad environment/source audit. They are temporary inputs, not portable authority or prerequisites for reproducing this matrix. The audits describe earlier baseline `01b573070947fbe1003df70d9e8a2d46bb081bab`; #23 subsequently changed documentation. Their environment observations are dated inventory, not current identity readiness. Reproduction uses the tracked contracts, GitHub evidence above, and operator-chosen private configuration from the repository root.

| Dependency | State at preparation handoff | Required before final #32 acceptance |
| --- | --- | --- |
| #23 contracts | CLOSED; exact accepted SHA and main CI linked above | Preserve frozen behavior; coordinate any contract correction |
| #24 real identity runtime/fixtures | Implementing in its disjoint worktree | Completed implementation, real dependency evidence, independent review, integration, required CI signoff, manual closure: PENDING |
| #25 persistence/migration | Implementing in its disjoint worktree | Real PG/storage/upgrade evidence, independent review, integration, required CI signoff, manual closure: PENDING |
| #26 BFF/session/security | Not started | Real login/security evidence, independent review, integration, required CI signoff, manual closure: PENDING |
| #27 organizations/invitations | Not started | Real lifecycle/mail/recovery evidence, independent review, integration, required CI signoff, manual closure: PENDING |
| #28 Studio/worker authorization | Not started | Real task/approval/Pi/result evidence, independent review, integration, required CI signoff, manual closure: PENDING |
| #29 Backlot/media/SSE | Not started | All entry points and measured revocation evidence, independent review, integration, required CI signoff, manual closure: PENDING |
| #30 standard OIDC and SAML SSO | Not started | Both real protocol/browser round trips, independent review, integration, required CI signoff, manual closure: PENDING |
| #31 browser identity flows | Not started | Integrated #26–30 UI behavior, independent review, integration, required CI signoff, manual closure: PENDING |
| #32 final validation/CI/operations | Matrix preparation only | Wait for every #24–31 requirement above, then execute this matrix on the integrated candidate and exact target-main SHA; PENDING |

A module's passing tests, draft PR, or self-report does not satisfy this dependency gate. Preparation does not close #32 or #22. The same #32 owner resumes final work after downstream readiness; defects return to their module owner and are rechecked on the repaired SHA.

## Execution boundary and required dataset

Successful identity cases must use real Logto, real PostgreSQL, actual SMTP receipt, a pinned standard OIDC/SAML IdP, and browser/public HTTP entry points. Real production cases also use official Pi, the worker, controlled local model/media services, FFmpeg, and Chromium. Controlled model/media responses avoid paid inference; they do not replace successful identity protocol exchange. Signed negative assertions, outages, dropped responses, and controlled time changes may be injected and must be labeled **fault injection**. A forged authenticated flag, injected AuthContext, SDK mock, or homemade successful IdP response cannot satisfy a real success row.

Use disposable, explicitly selected test databases/roles, application origins, Compose resources, projects, and private evidence directories. Never fall back to the live Studio DSN or submit tasks to existing live servers. Preserve the existing Studio/media containers, volumes, Pi installation, projects, and dirty files. External traffic uses the caller-approved current-process outbound proxy; no proxy settings are written into the project. Image staging must prove the actual Docker egress path rather than assume shell settings configure its daemon. Paid providers, Cloud procurement, customer IdP provisioning, and production deployment/cutover remain outside authorization.

| Dataset label | Required actual fixture/setup | Use |
| --- | --- | --- |
| N | No cookie; also malformed, expired, and revoked cookies | Public exceptions versus protected 401 |
| U1 / U2 | Two distinct verified Logto subjects and independent personal workspaces | Personal task/project/media/session isolation |
| A / B | Two distinct real organizations | Cross-organization isolation, including known resource URLs |
| A-admin / A-editor / A-viewer / B-admin | Authoritative role assignments to real accounts; role changes may supply successive phases with recorded timestamps | Exercise each role through every applicable endpoint |
| M | A real user in A and B, potentially U1; record different roles in each | Selecting A never authorizes B resources; switching verifies membership |
| I / E | Intended verified invitee and wrong/unverified-email identity; distinct subjects even if display emails coincide in a separate SSO case | Real mail acceptance and identity-linking defenses |
| R / D / Z | Removed member, disabled account, and empty/unknown-role member | Revocation and no invented viewer permission |
| T1 / T2 / TA / TB; P1 / P2 / PA / PB | Actual tasks and projects owned by U1, U2, A, B; record creator separately | Guessable IDs, actor audit, pagination, media, replay |
| H | Historical null-owner task and canonical project files from an isolated real pre-upgrade database/files copy | Hidden-by-default and explicit mapping/recovery |

Dataset labels are references, not assumed IDs/passwords or a new fixture schema. #24 supplies real identities; #25 supplies trusted ownership. Each scenario records actual actor, selected workspace, current authoritative roles, target owner, and fixture seed/version. Different browser profiles isolate U1/U2; additional same-profile and same-cookie-jar multi-tab cases deliberately exercise identity/context changes.

Expected role behavior (all executions PENDING): personal owner/admin/editor/viewer can read their selected workspace resources; personal/admin/editor can create, approve both gate kinds, cancel, and resume; viewer cannot write. Personal space alone permits organization creation. Admin alone manages members/invitations. Editor/viewer may read selected organization details but cannot browse its management lists. Unknown/empty roles grant nothing; recognized roles grant their union, with the highest role displayed. **No business role changes platform credentials, endpoints, providers, or trusted execution configuration.**

For valid boundary/session/context/authority requests, minimal scoped ownership lookup precedes capability, replay, state, file, and cache work: A-viewer writing TA yields **403 forbidden**; A-viewer writing TB/H/absent yields indistinguishable **404 not_found**. Missing/expired/revoked identity yields **401 unauthenticated**. A current-session write with stale context yields **409 auth_context_conflict** before replay or side effects; invalid CSRF/Origin yields **403 csrf_invalid**. Authority/session DB failure yields **503 identity_unavailable** or the existing safe dependency error. Safe denial bodies may exist; “no bytes” below means no protected file/event bytes or resource metadata.

All denied writes require evidence of unchanged business state, commands, gates, membership grants, reservations/cost journals, files, and controlled service submissions, as applicable. Compare public results and limited read-only real-PG journal evidence; private helper-call counts alone are insufficient.

## Dependency, session and boundary matrix

Each row expands into separate recorded cases for its listed variants. Every row and variant is PENDING; there is no implicit pass for a grouped row.

| ID / owner | Required exercise | Observable assertion and required evidence | Status |
| --- | --- | --- | --- |
| ENV-01 / #24,#32 | Boot the pinned isolated Logto/PG/standard IdP/mail environment; initialize twice; restart | Real discovery/JWKS/metadata, application and organization-template configuration; identical intended fixture IDs/counts after second init/restart; actual runtime versions/digests; bounded startup and exit status | PENDING |
| ENV-02 / #24,#32 | Restricted DB roles and least M2M permissions; scoped fixture cleanup | Separate identity/business DBs and accounts; prohibited operations denied; any vendor coarse management scope documented with fixed adapter compensation; cleanup changes only designated test resources and preserves existing Studio/Pi/files | PENDING |
| ENV-03 / #24,#26,#32 | Real registration/verification and password reset through Logto and SMTP sink | Received test messages with intended recipient and usable bounded one-time link; verification/reset completes; old password and reused/expired reset link denied; SMTP relay/forwarding disabled | PENDING |
| ENV-04 / #32 | Independently remove/stop Logto, PG, IdP, mail, Pi, FFmpeg, Chromium or their required libraries | Required validation job exits unsuccessfully with named missing/dead dependency; bounded timeout; no skip/xfail, mock success, anonymous fallback, or optional-profile omission | PENDING |
| AUTH-01 / #26 | U1/U2 real authorization-code login, fresh registration, safe return | Correct immutable backend issuer/sub and local user ID; personal default, or only explicitly verified organization hint; `/api/auth/me` safe DTO; actual browser/Logto/BFF round trip | PENDING |
| AUTH-02 / #25,#26 | Concurrent U1/U2 login/refresh, callbacks completed in reverse order, application restart | Request-specific SDK/storage isolation; cookie/session IDs and encrypted SDK state do not cross users; each `/me`, workspace, and subsequent resource result stays correct across restart | PENDING |
| AUTH-03 / #25,#26 | Login fixation attempt; login/workspace/sensitive identity rotation | Opaque cookie at least 256 random bits, HttpOnly, Path=/, SameSite=Lax, no Domain, Secure on HTTPS; old identifier atomically revoked; new CSRF/context; no tokens in cookie or browser storage | PENDING |
| AUTH-04 / #25,#26 | Default/shortened absolute 24h, idle 8h, interaction 10m expiry; revoked interaction; stale cookie after restart | Expired/revoked state cannot authenticate or accept new writes; shortened real-session expiry may use labeled time fault; expiry is never disabled; invalid callback leaves no partial login | PENDING |
| AUTH-05 / #26 | Missing/wrong/cross-browser state; replay and two concurrent callbacks for one interaction | Public callback rejects mismatch; at most one atomic consumption/session authorization; no evidence or tokens leak in error/log/location | PENDING |
| AUTH-06 / #26 | Missing/wrong/replayed nonce; reused PKCE verifier, mismatched code/verifier | Independent state/nonce and stored exact return path/verifier verified; rejection at public callback; official locked SDK gaps cannot relax mandatory verification | PENDING |
| AUTH-07 / #26 | Wrong issuer, signature/JWKS/key, audience/authorized party, expiry and invalid token data | Negative signed/protocol fixtures explicitly labeled; rejected callback does not mint a session; successful baseline remains real Logto | PENDING |
| AUTH-08 / #26,#31 | Allowed localized library/studio/board/invitation return; absolute/protocol-relative, encoded escapes, backslashes, controls, unknown route | Safe allowed return only; malformed input safe denial without open redirect; exact configured callback/logout URIs ignore untrusted Host/forwarded headers | PENDING |
| AUTH-09 / #26 | Missing/foreign/stale CSRF; invalid/null/cross-origin Origin; forged Host; arbitrary Forwarded/X-Forwarded-*; CORS preflight | Protected JSON writes denied before business work; untrusted proxy headers cannot establish trust; no permissive CORS; current valid same-origin write succeeds with cookie+CSRF+context | PENDING |
| AUTH-10 / #26 | Select personal own/other user; member/nonmember A/B; forged organization hint; roles unknown/empty and admin+viewer | Only current verified membership/own personal space selects; foreign selection safe 404; rotate context on success; role union/order correct and Z grants nothing; one selected workspace per session | PENDING |
| AUTH-11 / #26,#28,#31 | Logout including disabled account and remote sign-out outage; reuse old cookie and IdP session | Local revoke immediate even if remote fails; safe explicit remote status; browser follows configured end-session when available; new operations denied; already accepted task continues under original actor/owner | PENDING |
| AUTH-12 / #25,#26 | Wrong/missing session key, corrupt ciphertext, concurrent rotation/one-time use, key rotation and restart | Integrity-protected versioned ciphertext; cookie identifiers hashed at rest; fail closed on key/integrity failure; no cross-session recovery; atomic old-ID invalidation; private decryptable backup evidence | PENDING |
| AUTH-13 / #26,#32 | Missing issuer/app/client secret/session key, unavailable startup dependency, unsafe callback/host settings | Authenticated startup/access explicitly fails; never changes mode to anonymous; health remains safe constant and discloses no failing service details | PENDING |
| AUTH-14 / #26 | GET `/api/auth/login` with `return_to`, `organization_id`, `screen_hint=sign_in\|sign_up` | Public 302 only to configured Logto; new isolated expiring interaction; validate hints/return path; hinted organization grants no membership; N can initiate without business data access | PENDING |
| AUTH-15 / #26 | GET `/api/auth/callback` with code/state or protocol error | Public only for bound interaction; success rotates session and 303 to validated return path; error/cancel/replay has safe denial and no partial session or secret URL/log residue | PENDING |
| AUTH-16 / #26 | GET `/api/auth/me` | 200 exact safe user/workspace/role/permissions/context/CSRF/expires DTO for verified identity, 401 signed out and 503 authority unavailable; issuer/sub/session_ref/tokens never projected; explicit local mode returns local without fake Logto user | PENDING |
| AUTH-17 / #26 | POST `/api/auth/logout` with `{}` | Current session/CSRF/context required; local revoke and safe 200 logout_url or null even when remote unavailable/disabled account; configured end-session URL only; no task cancellation | PENDING |
| AUTH-18 / #26 | POST `/api/auth/workspace` with kind/owner_id | Current authority verifies own personal or actual org membership; 200 current `/me` projection after cookie/CSRF/context rotation; invalid/foreign/stale selection leaves no switched state or cross-context replay | PENDING |

## Exact HTTP and browser entry-point matrix

Repeat protected entries with N, U1/U2, A/B, M selecting each space, every applicable role, R/D/Z, H and absent target. Use valid CSRF/context for capability/ownership cases so boundary errors cannot mask the intended 403/404. List/create/config cases check capability before business work. Protected reads/identity HTML/media/SSE require `Cache-Control: private, no-store`, `Vary: Cookie`, and `X-Content-Type-Options: nosniff`; authorization happens before protected headers, 304, Range/416, or file/thumbnail work.

| ID / owner | Exact surface | Required positive and denial assertions | Status |
| --- | --- | --- | --- |
| HTTP-01 / #28 | GET `/api/studio/config` | Authorized safe configured profiles/readiness; viewer can read; no secrets/runtime/session data; no role can change platform configuration | PENDING |
| HTTP-02 / #28 | POST `/api/studio/tasks` | 202 after durable commit for personal/admin/editor; correct backend owner/creator/actor; viewer 403; client owner/actor/issuer/sub/organization/credential/endpoint/command/path additions rejected safely with no creation | PENDING |
| HTTP-03 / #25,#28 | GET `/api/studio/tasks?limit=...&after=...` | Existing list shape/slug cursor retained; scope before limit/cursor; empty space has no global count/IDs; foreign task cursor 404; limits 1–100 and invalid input safe | PENDING |
| HTTP-04 / #28 | GET `/api/studio/tasks/{task_id}` | Authorized task/gate/cost/pending-call fields only; foreign/H/absent uniform 404 regardless of target state; no internal RunContext, Pi JSONL or credentials | PENDING |
| HTTP-05 / #28 | POST `/api/studio/tasks/{task_id}/approvals/{gate_id}/decision` | Personal/admin/editor exact existing gate allowed; viewer own 403, foreign/absent 404 even with bad gate/version/hash/key; accepted actor is current verified user; see PROD-02/03 | PENDING |
| HTTP-06 / #28 | POST `/api/studio/tasks/{task_id}/cancel` | Authorized version/key-bound 202 command once; own viewer 403 versus foreign/absent 404; accepted actor preserved; canceled work retains cost/media/evidence | PENDING |
| HTTP-07 / #28 | POST `/api/studio/tasks/{task_id}/resume` | Authorized resume reconciles exact original task/run/project/session; foreign/stale context denied before replay/recovery; no completed-stage rerun or uncertain new submission | PENDING |
| HTTP-08 / #28 | GET `/api/studio/tasks/{task_id}/events` with `after`/`Last-Event-ID` | Owned replay and live sends only; numeric sequence gaps legitimate; foreign cursor cannot disclose events; persisted ordered replay/resync preserved; see REV matrix | PENDING |
| HTTP-09 / #28 | GET `/api/studio/tasks/{task_id}/result`, `?download=1`, Range/conditional variants | Verified final preview/download and headers only after auth; foreign/H/absent no protected bytes/hash/size/name/cache metadata; tampered result not delivered | PENDING |
| HTTP-10 / #29 | GET `/api/projects` | Existing filtered library shape/summaries/counts; U1 personal, A, B separate even for M; H hidden; warm global summary cache cannot share authorized results | PENDING |
| HTTP-11 / #29 | GET `/api/project/{project_id}/state` | Owned board artifacts/state; guessed foreign/H/absent path uniform 404; no path/title/checkpoint/profile leakage | PENDING |
| HTTP-12 / #29 | GET `/api/project/{project_id}/events` | Owned project stream, hello/change/heartbeat retained; every sensitive frame scoped; no invented durable cursor promise for baseline project SSE | PENDING |
| HTTP-13 / #29 | GET `/api/library/events` | Only selected-workspace changes; queued B notification never sent in A; reconnect/context change reauthorizes; no global IDs/counts | PENDING |
| HTTP-14 / #29 | GET `/media/{project_id}/{file_path:path}` | Authorized playable file/MIME and range behavior; foreign/H no bytes/cache evidence; same-root aliases/traversal/symlinks cannot bypass owner or sandbox/private-file policy | PENDING |
| HTTP-15 / #29 | GET `/thumb/{project_id}/{file_path:path}?w=...` | Authorized image/video thumbnail and safe fallback; unauthorized cannot trigger thumbnail or expose cached output; raw active-content fallback repaired per MEDIA-05 | PENDING |
| HTTP-16 / #29,#31 | GET `/`, `/studio`, `/p/{project_path:path}` | Legacy redirects preserve allowed path/query behavior and auth policy; no title/inline state; foreign project page 404 after redirects; signed-out project redirects only to safe login | PENDING |
| HTTP-17 / #29,#31 | GET `/zh`, `/ja`, `/en`; `/zh/studio`, `/ja/studio`, `/en/studio`; `/zh/p/{project_path:path}`, `/ja/p/{project_path:path}`, `/en/p/{project_path:path}` | Every language alias tested; public signed-out shells empty of business data; authorized board lookup; `?static=1` and `?task=...` grant no bypass; unsupported locales safe 404 | PENDING |
| HTTP-18 / #26,#31 | GET, HEAD `/ui/{path:path}`; `/ui` namespace redirect; direct `/ui/index.html`, `/ui/studio.html`, `/ui/board.html` | Static shells/assets only, no embedded user/project state, runtime/fixture/private files or secrets; GET/HEAD and slash redirect cannot bypass protected APIs; `/ui/` need not imply index serving | PENDING |
| HTTP-19 / #26 | GET, HEAD `/openapi.json`; `/docs`, `/redoc` | Authenticated mode schema 404; local mode may retain baseline schema; docs/redoc UIs disabled; no runtime/business identity information | PENDING |
| HTTP-20 / #26 | GET `/api/health` | Public constant `{"ok":true,"app":"backlot"}`, without identity/config/database fields; future readiness only safe 503 if actually introduced | PENDING |
| HTTP-21 / #26,#28,#29 | Trailing-slash redirects, encoded/nested legacy paths, repeated queries; HEAD on other surfaces | Follow each actual redirect with the same authorization; record registered method support. Baseline explicit GET APIs/media/results are GET-only: HEAD may be 405. If HEAD is introduced, run the full authorization/metadata matrix before delivery | PENDING |

`GET /api/library` and top-level `/resume` are not baseline routes. Do not use them to stand in for `/api/projects` or `/api/studio/tasks/{task_id}/resume`. Baseline has 24 declared method/path bindings plus implicit schema/static surfaces. Final runtime inventory must include the new auth/organization bindings below and compare every actual alias/method to this inventory, without calling GET-only routes successful HEAD implementations.

## Organization, invitation and SSO matrix

Unsafe organization/member/invitation operations require current session, Origin/Host, CSRF/context, live authority and scoped Idempotency-Key. Logout/workspace rotation are not replays across a changed session. Invitation acceptance may start in the intended user's personal space. Test real remote state as well as local intent results: HTTP success without confirmed authoritative membership/role is insufficient.

| ID / owner | Exact surface or flow | Observable assertion and required evidence | Status |
| --- | --- | --- | --- |
| ORG-01 / #27 | GET `/api/organizations?limit=...&after=...` | Only actor's current memberships; safe list envelope; pagination 1–100; endpoint/actor/workspace-bound cursors, cross-context or invalid cursor 422 invalid_cursor without global data | PENDING |
| ORG-02 / #27 | POST `/api/organizations` with validated name | Personal user 201 only after organization plus creator admin confirmed; org-space editor/viewer/admin cannot use personal creation capability; repeated same key produces one organization/grant | PENDING |
| ORG-03 / #27 | GET `/api/organizations/{organization_id}` | Selected matching organization details for admin/editor/viewer; foreign/unselected/absent uniform 404; membership in B while selecting A does not permit B detail | PENDING |
| ORG-04 / #27 | GET `/api/organizations/{organization_id}/members` and `/invitations` | Admin-only safe paginated management lists; owned editor/viewer 403 versus foreign/absent 404; invalid/cross-context cursor 422; no platform identity dump or raw tickets | PENDING |
| ORG-05 / #24,#27,#31 | POST `/api/organizations/{organization_id}/invitations`; actual mail delivery | Admin 201 safe invitation for admin/editor/viewer target role; one intended SMTP message/result on committed replay; no ticket/M2M secret in list/event/task; role outside recognized set rejected | PENDING |
| ORG-06 / #27,#31 | POST `/api/invitations/accept` from actual landing link and intended verified identity | Actual accepted membership/role and safe 200 organization DTO; ticket stored hashed, expiry within 7 days; link removed from history promptly, no-referrer and no third-party resources, no ticket in storage/telemetry | PENDING |
| ORG-07 / #27 | Wrong identity/email, unverified email, expired/cancelled/used/malformed ticket; concurrent acceptance | Safe 409 invitation_invalid where contract applies, no target secrets/grant; one acceptance; same authorized actor/workspace/key/payload replays result, different identity cannot read replay | PENDING |
| ORG-08 / #27 | DELETE `/api/organizations/{organization_id}/invitations/{invitation_id}` | Admin 200 confirmed cancelled safe DTO; later acceptance denied; cancellation/replay/foreign ticket cannot expose state or recreate invitation | PENDING |
| ORG-09 / #27 | PATCH `/api/organizations/{organization_id}/members/{user_id}`; DELETE same path | Admin 200 safe member or 204 after authoritative change/removal; own insufficient role 403, foreign org/member/absent 404; current backend actor recorded; affected local authority/session state invalidated immediately | PENDING |
| ORG-10 / #25,#27 | Last admin self-removal/downgrade and concurrent two-admin removal/downgrade | 409 last_admin without mutation when final admin would disappear; serialized real-PG intent protects invariant until remote outcome reconciled; restart/lost response cannot release logical serialization prematurely | PENDING |
| ORG-11 / #25,#27 | Lost response after remote create/invite/grant/revoke; rate limit, partial completion, authority outage, API restart | Stable scope/operation intent and stage/result; reconcile real authoritative resource markers before retry; no duplicate create/mail/grant; unresolved outcome explicit 503/recovery, never manufactured success | PENDING |
| ORG-12 / #27,#32 | Foreign admin, removed member, spoofed actor/organization, arbitrary management path/role/tenant parameters | No cross-org M2M proxy or platform privilege; fixed validated adapter only; denial without remote/local side effects; management credential inaccessible to browser/Pi | PENDING |
| SSO-01 / #24,#26,#30,#31 | Application-initiated standard OIDC IdP → Logto → BFF browser login | Actual discovery/JWKS/code exchange and browser redirects on locked releases; correct backend identity, verified membership/role and selected allowed organization; safe resource success plus foreign 404 | PENDING |
| SSO-02 / #24,#26,#30,#31 | Application/SP-initiated standard SAML IdP → Logto → BFF browser login | Actual metadata/certificate/ACS exchange and standard signed assertion processing; correct identity/membership/role; real browser round trip and protected resource success; no fixture assertion as successful login | PENDING |
| SSO-03 / #30 | OIDC wrong issuer/audience/signature/expiry/redirect, replayed code/state/nonce; SAML wrong issuer/audience/recipient/destination/certificate/time, replayed assertion/InResponseTo | Negative fixtures may inject protocol faults, with the actual accepting boundary identified; safe rejection and no authenticated enterprise grant; both real successful protocol rows remain independently required | PENDING |
| SSO-04 / #26,#30 | Nonmember, foreign organization hint, A+B member with different roles, same email/different subjects or issuers | No domain/JIT membership or implicit account merge; only explicit verified Logto linking, if offered; same email does not expose another personal account or organization; backend issuer is verified Logto, upstream provenance retained privately | PENDING |
| SSO-05 / #26,#30,#31 | User cancel, IdP/Logto outage, metadata/certificate misconfiguration, logout | Safe explicit recovery without partial enterprise identity or fallback authorization; old local session revoked on logout; protocol assertions/tokens/connector secrets absent from normal business evidence | PENDING |
| SSO-06 / #30,#32 | Independent reproduction from owner-published connector/onboarding steps | Actual connector type, locked IdP/Logto versions, endpoints and certificate fingerprint recorded without secret keys/assertions; customer IdP and commercial MFA remain unexecuted; optional basic MFA reported only if actually exercised | PENDING |

## Real production, approval, replay and browser context matrix

Run the complete successful flow for U1 personal and organization A through the integrated UI, then repeat isolation with U2/B and M. Require actual worker and official Pi RPC process; controlled local services record protocol submissions, and real FFmpeg makes moving video. Preserve the existing canonical/checkpoint, single-slot, lease/fence, journal, and exact-session semantics. Identity access alone never constitutes creative/cost approval.

| ID / owner | Required exercise | Observable assertion and required evidence | Status |
| --- | --- | --- | --- |
| PROD-01 / #28,#31,#32 | Browser brief → durable task/start command → real Pi/worker pipeline → all required gates → canonical compose/render → preview/download | Personal and organization runs finish only with canonical completed compose checkpoint, schema-valid render_report, existing nonempty verified file, positive ffprobe duration/dimensions/streams, byte hashes, and advancing browser playback; ACK/agent_end/agent_settled alone cannot pass | PENDING |
| PROD-02 / #28 | Ordinary gate approve/revise/reject/abort, allowed selected options; stale version/gate/run and altered exact bytes/revisions/scope | Binding includes task/run/gate, artifact/checkpoint revision and exact SHA256, canonical scope hash; provider/model/runtime/narration/config/budget scope unchanged unless fresh revision/gate; old/forged evidence cannot continue; actor current verified user | PENDING |
| PROD-03 / #28 | Independent model_cost/media_cost gates; unknown-price consent, over-budget/concurrent reservation, altered approved scope | Exact displayed consent/reservation limit and budget bound; unknown never treated as zero; quoted reservations cannot oversell; viewer/foreign denied; stale binding cannot authorize any extra service submission or fee | PENDING |
| PROD-04 / #28 | Creator leaves organization; actor approves another member's task; logout/switch/expiry while accepted work runs | Organization remains owner, creator stays immutable, accepted command actor stays original; worker derives context from committed records rather than current cookie; new operations require current identity/membership; accepted work may complete | PENDING |
| PROD-05 / #28 | Cancel/resume plus worker/API/DB restart, lease loss and stale writer; multiple waiting tasks | Exactly one execution slot/writer; deny new model/tool sends after cancel acceptance; bound shutdown/process cleanup before takeover; monotonic fence guards writes; resume preserves original Pi session, completed stages, files/cost/commands | PENDING |
| PROD-06 / #28 | Known remote job receipt versus uncertain submission; reconcile/retry/resume after response loss | Known job resumes/query/download without new submission; outcome_unknown keeps reservation and explicit recovery; generic continue/resume cannot resubmit; accepted ownership and actor unchanged | PENDING |
| PROD-07 / #28,#29 | Modify final video/report/checkpoint after recorded verification; symlink substitution | Result verifier rejects mismatched bytes/report/revision/path; no verified DTO flag bypass; personal and org result routes do not serve altered artifact as accepted deliverable | PENDING |
| REPLAY-01 / #25,#27,#28 | Same key/payload within scope; same key/different payload; simultaneous identical requests | One committed outcome; exact replay remains accepted evidence even if task version advanced, after current authorization; changed fingerprint 409; concurrent acceptance no duplicate command/grant/cost | PENDING |
| REPLAY-02 / #25,#28 | Same key across U1/U2, personal/A/B, two actors in same org, operations and resources | Scope is `(issuer, sub, workspace kind, owner_id, operation, resource_id, key)`; distinct scopes cannot expose/reuse another accepted result; appropriate distinct creates; targeted foreign replay stays 404 | PENDING |
| REPLAY-03 / #26,#27,#28 | Replay after role downgrade/removal/disable/logout, using already committed key | Verify current authority and ownership before replay lookup; forbidden/unauthenticated/foreign response without saved evidence or new side effects | PENDING |
| REPLAY-04 / #25,#27,#28,#31 | Accepted HTTP response dropped; API restart; same-context browser retry | Preserved user/workspace/key/payload/exact binding; recover committed result without duplicate remote/local operation; unknown outcome remains explicit; actual dropped-response injection labeled separately from real successful flow | PENDING |
| UI-01 / #26,#28,#31 | Two tabs: delayed write versus workspace switch, including create with no resource ID | If accepted before switch, original owner/actor persists; if old context reaches server after rotation, 409 auth_context_conflict and no side effect; never resend pending write into the new workspace | PENDING |
| UI-02 / #31 | U1 logout → U2 login in same browser; drafts/last-task/cursors/pending keys/cache; refresh/back/bfcache and old task links | No U1 cached data or recovered write under U2; storage scoped/cleared; old task URL denied; stale identity response discarded using X-Auth-Context; new identity visible before action | PENDING |
| UI-03 / #31 | Delayed read/SSE response from A after switch to B; logout/remove during browser media/events | Old data not rendered; old streams closed; reconnect obtains authorized current context; browser UI/backend both reject removed authority without stale frame/byte acceptance | PENDING |
| UI-04 / #31 | 401, genuine permission 403, csrf_invalid, context conflict and dependency error | 401 safe sign-in recovery; permission 403 never blind retry; CSRF refresh/retry only same verified identity/workspace and preserved key; changed context discards write; outage explicit | PENDING |
| UI-05 / #31 | Real organization creation/mail invitation/acceptance/member changes and both SSO flows from UI | Backend and IdP/mail round trips, readable current account/workspace, viewer read/play with unavailable writes explained; no static/mock page substitutes | PENDING |
| UI-06 / #31 | zh/ja/en at desktop and 390px; keyboard-only account/workspace/invite/approval/error recovery | Accessible names/labels/focus and actionable errors; core controls usable at 390px; identity/storage/request URL/DOM secret checks; preserve existing Studio/board behaviors | PENDING |

## Media, cache, live events and revocation matrix

For media/thumb/result, test warm and cold caches with the same exact path and another user/space. Carry a legitimate owner's ETag/Last-Modified/Range into foreign, signed-out, expired, revoked and unassigned requests. Conditional support is established from actual runtime behavior: an authorized conditional GET may return 200, or 304 if supported; a foreign request must never return 304 or protected metadata. Existing static conditional 304 is a separate public-asset case. Do not introduce HEAD or new media-format requirements solely for this matrix.

| ID / owner | Required exercise | Observable assertion and required evidence | Status |
| --- | --- | --- | --- |
| MEDIA-01 / #28,#29,#32 | Full result preview/download and project media GET for personal/admin/editor/viewer | Actual playback currentTime advances while playing; decoded frames at distinct timestamps show expected motion; successful seeking; download nonempty bytes exactly match verified report/video hash; ffprobe validates expected streams, duration and dimensions | PENDING |
| MEDIA-02 / #28,#29 | Valid single byte, open-ended, suffix and supported multipart Range; invalid/unsatisfiable Range | Correct authorized 206/Content-Range/body byte slices or supported multipart boundaries; safe authorized 416; foreign/N/H always auth denial before length/ETag/Range/416; record actual method/range support | PENDING |
| MEDIA-03 / #28,#29 | If-None-Match, If-Modified-Since, If-Range; warm thumbnail/summary/file caches | Correct authorized conditional/range behavior; cross-context never protected 304, bytes, basename, size or cache metadata; private no-store/Vary Cookie/nosniff retained on protected responses | PENDING |
| MEDIA-04 / #29 | Allowed thumbnail widths, cached thumbnail, invalid/nonexistent file, concurrent warmup in A and B | Owned thumbnails usable; filtering before source/cache/generation; foreign error independent of file/cache existence; no global project metadata in cache response | PENDING |
| MEDIA-05 / #29 | HTML/HTM/SVG/XHTML through media and raw thumbnail fallback | Media sandbox CSP and nosniff retained; thumb either safe rejection or equivalent sandbox/nosniff; actual browser attempt cannot run same-origin script/read identity; no owner exception to active-content policy | PENDING |
| MEDIA-06 / #29 | Dotfiles/private project config, credential/control markers, `.studio-owner.json`, runtime/session references; direct static traversal | Never delivered through media/thumb even to owner; no runtime/fixture file through `/ui`; private configuration and Pi session bytes absent from downloads/results | PENDING |
| MEDIA-07 / #29 | Lexical/encoded traversal, absolute path, escaped symlink, same-root project alias and symlink swapped after lookup | Resolved-root containment and actual ownership enforced at I/O; alias cannot grant another owner; unauthorized target safe denial before private filename/headers; canonical legitimate file still works | PENDING |
| MEDIA-08 / #26,#28,#29 | HEAD only where registered; schema/static GET/HEAD, and any final supported private HEAD | Public asset headers safe, schema disabled in auth mode; private supported HEAD authorizes before metadata; unsupported API HEAD records 405 rather than fictitious success; slash redirects retain policy | PENDING |
| REV-01 / #26,#28,#29 | Open task, project and library SSE; revoke by logout, business remove/role change, external Console remove/downgrade, account disable | Measure each stream separately: local revocation immediate for new requests; external authority change reflected within 30s; recheck at least every 15s, cache age at most 30s; authorize immediately before sensitive frame | PENDING |
| REV-02 / #26,#28,#29 | Open long media/result response; repeat revoke variants and authority/DB outage mid-transfer | Bounded recheck before further chunks; no newly authorized/sent protected chunk after 30s maximum; no old successful authority cache fallback on failure; reconnect/new Range denied; already delivered/buffered pre-revoke bytes identified separately | PENDING |
| REV-03 / #28,#29 | Foreign task Last-Event-ID/after, retained events/reconnect, queued foreign library/project changes before revoke/switch | Scope before replay and each send; no foreign IDs/payloads, even queued; revoked connection ends without sensitive diagnostic; reconnect reauthorizes and task retention resync stays scoped | PENDING |
| REV-04 / #26,#28,#29 | Logto query failure after successful cache warmup, stale token claims, repeated reconnect during outage | No stale-success fallback; writes rejected when current authority cannot be verified; readers/streams close at next bounded check; no anonymous mode; safe 503/new unauthenticated behavior as applicable | PENDING |

Revocation evidence must include UTC and monotonic timestamps for authoritative change/confirmation (`t0`), last authority verification, last protected frame/chunk sent, stream termination and subsequent denied request. Use a transfer/event source lasting longer than the window and controls that distinguish server sends from client buffering; an already completed two-second file cannot prove mid-transfer revocation. Report measurement uncertainty, highest observed delay, polling interval, and each cause/surface combination. “Browser closed the tab,” a heartbeat screenshot, or a single reconnect denial is insufficient. Downgrade admin→viewer removes writes immediately but retains permitted reads; downgrade/removal to no read capability must stop read streams. Accepted background production remains independent of browser revocation.

## Real PostgreSQL, migration and operator recovery matrix

The limited real-PG seams are already approved because persistence, migration and atomicity are public requirements. Rehearse against an isolated copy of real pre-identity rows and canonical files, never the running Studio database. Preserve immutable accepted ownership, exact artifacts/checkpoints/history, approval/budget evidence, command/fence/lease journals, and Pi JSONL references. The operator mapping uses #25's released format, not a browser claim endpoint or a new schema defined by this matrix.

| ID / owner | Required exercise | Observable assertion and required evidence | Status |
| --- | --- | --- | --- |
| DB-01 / #25,#32 | Upgrade populated pre-identity control plane; repeat additive migrations and restart | Existing tasks/runs/commands/events/gates/calls/file intents/fences survive; nullable historical ownership; migration reentrant; schema/constraints and file/journal before/after manifests agree; existing upgrade/regression nodes run on real PG | PENDING |
| DB-02 / #25,#26,#29 | First and subsequent real login after upgrade; direct H task/project/media/events lookup | H never assigned to first login or guessed by path; authenticated lists/pages/SSE/bytes hide H; explicit local compatibility retains legacy behavior without fabricated Logto identity | PENDING |
| DB-03 / #25,#32 | Explicit resource→personal/organization + creator mapping dry-run | Known verified user/org and existing project references required; report all proposed changes/conflicts without mutation; no wildcard/global claim or inference from email; exact #25 mapping format/version recorded | PENDING |
| DB-04 / #25,#32 | Back up before mapping; apply valid mapping, then reapply identical mapping | Atomic scoped ownership with audit and trusted operator attribution; identical mapping no-op; only mapped resources become visible to intended workspace; byte/layout/hash/revision/journal/session references preserved | PENDING |
| DB-05 / #25,#32 | Conflicting ownership, absent/invalid resource/identity/org/reference, mixed valid+invalid batch, concurrent mapping | Fail without partial reassignment; lock/transaction protects affected records; accepted task owner cannot change; incompatibility explicit rather than silently overwriting | PENDING |
| DB-06 / #25,#28,#32 | Active lease/run, queued accepted command, waiting/recovering work during import/upgrade | Import refuses unsafe active/queued reassignment; upgrade/runbook quiesces new acceptance and drains or stops/reconciles workers safely; no lease timeout shortcut or lost reservations/unknown outcome | PENDING |
| DB-07 / #25,#32 | Session store create/read/rotate/revoke/expiry and concurrent interaction use on real PG | Per-session encrypted SDK state and hash-only cookie IDs; real uniqueness/transaction/expiry constraints; privileged private session access separated from ordinary business reads; restart isolation and wrong-key fail-closed evidence | PENDING |
| DB-08 / #25,#27,#28 | Real concurrent scoped creates/replays and organization intent serialization | PostgreSQL uniqueness/locking prevents duplicate local acceptance and final-admin loss; actor/owner persisted in command/audit; authorization failure cannot expose replay rows | PENDING |
| DB-09 / #24,#25,#32 | Coordinated business, Logto and IdP DB backup plus canonical files, runtime references and session key backup | Private access-controlled backups; consistent recovery point and documented writer quiescence; keys backed up separately without printing; actual hashes/versions/backup tool commands and retention/permission evidence | PENDING |
| DB-10 / #24,#25,#26,#28,#32 | Restore backups into fresh isolated DBs/directories; start restored auth/app/worker with documented keys | Real login and selected ownership remain correct; membership/roles preserved; valid session decrypt/revoke behavior defined; restored hashes/gates/cost/queue/fences/JSONL references consistent; unknown outcomes do not resubmit | PENDING |
| DB-11 / #25,#26,#32 | Session key rotation, missing previous key, corrupt backup, interrupted restore | Required old ciphertext remains decryptable under protected retained key/version, or explicit revoke/relogin; missing/corrupt input fails closed with no partially public restored data; rollback of key rotation documented and reproduced | PENDING |
| DB-12 / #26,#32 | Enable auth with trusted HTTPS callback/logout origins and inbound proxy identity; restart | Only configured Host/proxy chain trusted; Secure cookies under HTTPS, exact callback/logouts; operator config/private permissions and least credentials checked; no permissive forwarded-host reconstruction or silent anonymous mode | PENDING |
| DB-13 / #26,#32 | Explicit trusted loopback local mode plus offline CLI; non-loopback/wildcard bind or spoofed peer/Host/proxy | CLI canonical file workflow remains compatible; local `/me` says local without fake user; actual peer/Host/bind guard rejects anonymous exposure; identity outage never enables local mode | PENDING |
| DB-14 / #25,#26,#29,#32 | Safe rollback rehearsal after upgrade/import, including unsupported old application/schema | Keep authenticated/private access enforced or stop external service; restore matched private DB/files/runtime/key recovery point in isolation; older anonymous build never serves authenticated data to non-loopback clients; no destructive schema downgrade/data loss | PENDING |
| DB-15 / #25,#28,#32 | Restored interrupted file intent, differing external bytes, pending fees/remote results | Old/new byte reconciliation and exact fence rules retained; third hash explicit conflict, no overwrite; unknown cost/result remains unresolved until evidence; same exact session resumed, no “latest session” substitution | PENDING |

Activation/rollback instructions must ultimately name the actual released configuration keys, operator CLI argv, DB/schema/role boundaries, service order, healthy conditions, stop/drain rules and restoration destinations. Those commands are unresolved until #24–26 release them; this preparation does not invent an executable migration or deployment interface. Rehearsal is local and disposable; production cutover remains unexecuted and outside scope.

## Platform secrets and required validation accounting

| ID / owner | Required exercise | Observable assertion and required evidence | Status |
| --- | --- | --- | --- |
| SECRET-01 / #24–32 | Place unique private test credential/ticket/token sentinels; examine all public DTOs, errors, logs, URL/history, DOM, browser storage, downloads and SSE | No app/M2M/access/refresh credentials, raw assertions/SDK storage/JSONL, unnecessary foreign profiles, or raw callback/ticket data outside necessary short-lived protocol exchange; redact necessary callback evidence; scan reports locations without matching values | PENDING |
| SECRET-02 / #25,#28 | Read ordinary business records, task snapshots, argv/diagnostics, bridge inputs and Pi subprocess environment | Ownership/actor records contain no credential; private session ciphertext only in protected store; Pi receives no BFF/M2M/session secrets, host credential/config inheritance or arbitrary network/command privilege | PENDING |
| SECRET-03 / #27,#28,#31 | Business roles attempt platform credential/endpoint/provider/profile mutation and arbitrary M2M/tool forwarding | Role cannot alter trusted platform configuration or escape approved production scope; validated safe input denial; no secret echo, remote submission or audit contamination | PENDING |
| SECRET-04 / #24,#32 | Git tracked/staged files, generated browser bundles, fixture outputs, CI artifacts and private backups | No credentials/private config/key/dump/raw HAR/cookie jar in version control or published artifacts; permission/ignore checks recorded; sanitized evidence reproducible without secret values | PENDING |
| CI-01 / #32 | Required real dependency preflight plus exact required scenario/test-node manifest | All required cases collected/executed on the candidate; each missing service/library/binary/login/SMTP/protocol is hard failure; no importorskip, skip, xfail, empty selection or fixture-only success for required identity flows | PENDING |
| CI-02 / #32 | Existing lint/contracts/Repository/API/Backlot/Pi/worker/policy/approval/cost/recovery/browser regressions | Commands, exact SHA, exit codes and summaries retained; optional skipped/xfail node IDs and reasons listed separately, including comparison to baseline 17/3; no test-count-only acceptance | PENDING |
| CI-03 / #32 | Independent reviewer reruns critical real success and denial cases after repairs | Named reviewer, same exact SHA/environment, independent commands/observations and zero blockers; reproduce both SSO protocols, approval+moving result, 403/404 order, replay/revocation and upgrade/restore | PENDING |
| CI-04 / #32 | Parent integrates focused Refs #32 change and runs necessary validation on target main | CI run URL, completed successful status and head_sha equal actual integrated main SHA; required identity job plus regressions accounted for; corrected SHA re-reviewed/rechecked; parent posts evidence and manually closes only after all gates | PENDING |

Required scenario IDs must map to concrete collected test nodes and/or independently reproducible operator/browser cases when implementation exists. Grouped variants require individual outcomes. A skipped, xfailed, uncollected, or fault-only required real-success case is incomplete and blocks acceptance even when pytest exits zero. Optional cases get a separate ledger with node ID, reason, scope and disposition; they never contribute to required identity success. No CI file or test implementation is created in this phase.

## Version and environment record to fill at execution

These observations came from the private pre-implementation audit and #23 evidence. They are historical observations or proposed pins, not executed #32 dependency acceptance. #24 freezes actual identity images/SDK/IdP/mail locks; final runs capture actual rather than silently treating a tag as proof.

| Component | Prior observation or proposed baseline | Actual final version/digest and evidence |
| --- | --- | --- |
| Logto OSS | Contract baseline 1.44.0; proposed image `ghcr.io/logto-io/logto:1.44.0@sha256:847cdd2759ad60f61b9733af0ea352183c805681dff5c9f8140a80f52d8e1340`; not running at audit | PENDING: #24 lock + running binary/image + real login |
| Standard IdP | Proposed Keycloak 26.8.0, `quay.io/keycloak/keycloak:26.8.0@sha256:27b3230fbabb8c9ff3c66c864cf09dcd8a41aca82e5100679d109ef0eeb05741`; not running at audit | PENDING: #24 lock, metadata/cert fingerprint + both real protocols |
| Test SMTP sink | Proposed Mailpit v1.31.5, `ghcr.io/axllent/mailpit:v1.31.5@sha256:94f6b70c1436defae6799d9ac7ff0eeff9540465b842026d5cdb018f1c0234ea`; not running at audit | PENDING: #24 lock + actual message receipt |
| PostgreSQL | Existing real 17.11; cached `public.ecr.aws/docker/library/postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24`; identity compatibility unproven | PENDING: isolated running DB versions, restricted roles, migrations/restores |
| Official Pi | Installed 1.1.0; source `abe508e1b89912adde45528136c3221eb69acdd7`; [tracked lock](../pi-runtime/source.lock.json); no #32 RPC execution | PENDING: source/archive/package locks, installed CLI hash/version, actual RPC/full task |
| Python / SDK | Audit Python 3.10.22; Logto SDK absent then; actual SDK/auth lock owned by #24/#26 | PENDING: interpreter/platform, exact SDK/dependency locks and storage/verification behavior |
| Node/npm | Host 24.21.0/11.19.0; existing CI Node 22.19.0; Pi minimum 22.19.0 | PENDING: actual local and CI runtime versions |
| API/DB/test libraries | Audit FastAPI 0.143.0, Starlette 1.7.0, psycopg 3.3.6, httpx 0.28.1, pytest 9.1.1, pytest-asyncio 1.4.0 | PENDING: installed versions and final lock sources |
| Browser | Audit Playwright 1.63.0; cached headless Chromium 153.0.8010.12; container Chromium 154.0.8037.92 | PENDING: actual executable/build, launched browser, playback and protocol evidence |
| FFmpeg/ffprobe | Host 4.4/n4.4.1 and container 5.1.9 produced probe bytes; virtualenv n7.1 lacked `testsrc2` | PENDING: chosen binary path/version/filter capability, actual pipeline output + ffprobe |
| Compose/Docker/host | Audit Compose 5.5.1, Engine 29.8.2; macOS 26.6 ARM64, 48 GiB | PENDING: execution host/architecture, engine/runtime and isolation inventory |

Do not activate a virtualenv and assume its FFmpeg is usable. Choose a validated media binary directory explicitly for fixture subprocess PATH while invoking the chosen Python interpreter explicitly. An FFmpeg capability probe is prerequisite evidence only; it cannot satisfy PROD-01 or real browser playback. Do not rebuild or render through dirty existing media bind mounts.

## Future command worksheet — not executed

All commands in this section are **future proposals, PENDING**. Run from the chosen integrated candidate repository root, with private configuration selected by the operator and no live DSN fallback. Variable names below are worksheet inputs, not new application configuration or already implemented test flags. Use a trusted shell-compatible private env file containing operator-approved test inputs; actual runtime configuration and credential provisioning follow #24–26's released instructions. Keep raw outputs private and publish only sanitized copies. Do not enable shell tracing.

### Candidate and dependency prerequisites

```bash
set -euo pipefail
umask 077
cd "$(git rev-parse --show-toplevel)"
: "${AUTH32_PRIVATE_ENV:?Choose the operator-owned private validation env file}"
test -f "$AUTH32_PRIVATE_ENV"
set -a
. "$AUTH32_PRIVATE_ENV"
set +a
: "${AUTH32_CANDIDATE_SHA:?Exact integrated candidate SHA}"
: "${AUTH32_EVIDENCE_DIR:?Private evidence directory outside tracked source}"
: "${AUTH32_PYTHON:?Explicit test interpreter path}"
: "${AUTH32_VENV:?Chosen interpreter environment directory}"
: "${AUTH32_PI_CLI:?CLI installed from the tracked official Pi lock}"
: "${AUTH32_MEDIA_BIN_DIR:?Directory with validated FFmpeg and ffprobe}"
: "${STUDIO_TEST_DATABASE_URL:?Explicit isolated business-test DSN; no fallback}"
test "$(git rev-parse HEAD)" = "$AUTH32_CANDIDATE_SHA"
test -z "$(git status --porcelain)"
mkdir -p "$AUTH32_EVIDENCE_DIR"
export PYTHONDONTWRITEBYTECODE=1 STUDIO_REQUIRE_INTEGRATION=1
export PATH="$AUTH32_MEDIA_BIN_DIR:$PATH"
unset OPENMONTAGE_ALLOW_NETWORK STUDIO_XVAN_LIVE_SMOKE STUDIO_XVAN_PAID_APPROVED
git rev-parse HEAD
"$AUTH32_PYTHON" --version
node --version
npm --version
node "$AUTH32_PI_CLI" --offline --version
ffmpeg -version
ffprobe -version
"$AUTH32_PYTHON" -B -c 'import psycopg, logto, httpx, pytest; from playwright.sync_api import sync_playwright'
```

An import/version probe is not real login or service readiness. The future required preflight must also perform real bounded PG/discovery/JWKS/IdP metadata/SMTP/browser/Pi checks, validate locked artifacts and DB isolation, and hard-fail each absent dependency before tests. The released #24–26 preflight entry points and actual identity-node manifest are PENDING; no fictional `STUDIO_REQUIRE_IDENTITY` flag is assumed.

### Bounded isolated startup and media capability

The private Compose file, project and service names must come from #24's actual delivery; none below names a service already present at this preparation base. Stage pinned images through approved egress first. Preserve existing running environments and use the owner-documented cleanup scope only.

```bash
: "${AUTH32_COMPOSE_FILE:?Owner-delivered Compose file}"
: "${AUTH32_COMPOSE_PROJECT:?Dedicated disposable validation project}"
: "${AUTH32_IDENTITY_PG_SERVICE:?Actual isolated identity PG service}"
: "${AUTH32_LOGTO_SERVICE:?Actual Logto service}"
: "${AUTH32_IDP_SERVICE:?Actual standard IdP service}"
: "${AUTH32_MAIL_SERVICE:?Actual SMTP sink service}"
docker compose --project-name "$AUTH32_COMPOSE_PROJECT" --file "$AUTH32_COMPOSE_FILE" \
  up --detach --wait --wait-timeout 120 --pull never --no-build \
  "$AUTH32_IDENTITY_PG_SERVICE" "$AUTH32_LOGTO_SERVICE" "$AUTH32_IDP_SERVICE" "$AUTH32_MAIL_SERVICE"
docker compose --project-name "$AUTH32_COMPOSE_PROJECT" --file "$AUTH32_COMPOSE_FILE" ps
ffmpeg -v error -f lavfi -i testsrc2=size=1280x720:rate=30 \
  -f lavfi -i sine=frequency=440:sample_rate=48000 -t 2 \
  -c:v libx264 -preset ultrafast -c:a aac -pix_fmt yuv420p \
  -movflags frag_keyframe+empty_moov -f mp4 pipe:1 \
  > "$AUTH32_EVIDENCE_DIR/capability-probe.mp4"
ffprobe -v error -show_streams -show_format -of json \
  "$AUTH32_EVIDENCE_DIR/capability-probe.mp4" \
  > "$AUTH32_EVIDENCE_DIR/capability-probe.json"
```

Run #24's actual initializer twice and its narrowly scoped cleanup/restart checks; exact argv PENDING from owner delivery. These commands do not start the integrated application or prove initialized protocol/mail fixtures. Capture bounded startup failure rather than indefinite retries. Required media capability failure blocks execution; a different unverified binary is not an automatic pass.

### Real HTTP probes and delivered bytes

Cookie jars must come from the real browser login, never handcrafted sessions. Private header files contain the current Origin, CSRF/context and operation key obtained from that same session. The task request file uses the existing creative DTO and a controlled local configured profile. A blank/foreign/stale session has a separately captured jar/headers; do not reuse U1's write headers to claim a U2 capability test.

```bash
: "${AUTH32_APP_ORIGIN:?Actual isolated same-origin application URL}"
: "${AUTH32_OWNER_COOKIE_JAR:?Private jar exported after real owner login}"
: "${AUTH32_OTHER_COOKIE_JAR:?Private jar exported after real foreign login}"
: "${AUTH32_WRITE_HEADERS_FILE:?Private current Origin/CSRF/context/key headers}"
: "${AUTH32_TASK_REQUEST_FILE:?Validated controlled-profile creative request JSON}"
curl --silent --show-error --connect-timeout 5 --max-time 15 \
  --cookie "$AUTH32_OWNER_COOKIE_JAR" --dump-header "$AUTH32_EVIDENCE_DIR/me.private.headers" \
  --output "$AUTH32_EVIDENCE_DIR/me.private.json" --write-out '%{http_code}\n' \
  "$AUTH32_APP_ORIGIN/api/auth/me"
curl --silent --show-error --connect-timeout 5 --max-time 15 \
  --cookie "$AUTH32_OWNER_COOKIE_JAR" --header "@$AUTH32_WRITE_HEADERS_FILE" \
  --header 'Content-Type: application/json' --request POST \
  --data-binary "@$AUTH32_TASK_REQUEST_FILE" \
  --output "$AUTH32_EVIDENCE_DIR/create.private.json" --write-out '%{http_code}\n' \
  "$AUTH32_APP_ORIGIN/api/studio/tasks"
: "${AUTH32_TASK_ID:?Actual completed owned task ID from this real run}"
curl --silent --show-error --connect-timeout 5 --max-time 30 \
  --cookie "$AUTH32_OWNER_COOKIE_JAR" --dump-header "$AUTH32_EVIDENCE_DIR/download.private.headers" \
  --output "$AUTH32_EVIDENCE_DIR/final.mp4" --write-out '%{http_code}\n' \
  "$AUTH32_APP_ORIGIN/api/studio/tasks/$AUTH32_TASK_ID/result?download=1"
ffprobe -v error -show_streams -show_format -of json "$AUTH32_EVIDENCE_DIR/final.mp4" \
  > "$AUTH32_EVIDENCE_DIR/final.ffprobe.json"
ffmpeg -v error -i "$AUTH32_EVIDENCE_DIR/final.mp4" -vf 'fps=1' -an -f framemd5 pipe:1 \
  > "$AUTH32_EVIDENCE_DIR/final.frames.txt"
"$AUTH32_PYTHON" -B -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
  "$AUTH32_EVIDENCE_DIR/final.mp4"
curl --silent --show-error --connect-timeout 5 --max-time 15 \
  --cookie "$AUTH32_OWNER_COOKIE_JAR" --header 'Range: bytes=0-1023' \
  --dump-header "$AUTH32_EVIDENCE_DIR/range.private.headers" \
  --output "$AUTH32_EVIDENCE_DIR/range.bin" --write-out '%{http_code}\n' \
  "$AUTH32_APP_ORIGIN/api/studio/tasks/$AUTH32_TASK_ID/result"
: "${AUTH32_RESULT_ETAG:?Owner ETag from actual delivered headers, if present}"
curl --silent --show-error --connect-timeout 5 --max-time 15 \
  --cookie "$AUTH32_OTHER_COOKIE_JAR" --header "If-None-Match: $AUTH32_RESULT_ETAG" \
  --header 'Range: bytes=0-1023' --dump-header "$AUTH32_EVIDENCE_DIR/foreign.private.headers" \
  --output "$AUTH32_EVIDENCE_DIR/foreign.private.json" --write-out '%{http_code}\n' \
  "$AUTH32_APP_ORIGIN/api/studio/tasks/$AUTH32_TASK_ID/result"
```

Required observations for these probes: `/me` 200 correct safe identity/context; create 202 durable original owner/actor; completed download 200 and exact verified hash; valid single Range 206 with correct bytes; foreign conditional/Range 404 with no protected metadata/bytes, never 304/416. Probes capture responses but do not assert the whole matrix: real browser playback and all approval steps remain mandatory. Raw headers/DTOs are private because they may include cookies/CSRF/context; sanitize before publishing. If an ETag is genuinely absent, record that actual support and still test foreign ordinary/Range and supported conditional forms; do not invent a header to count positive conditional support.

### Existing regressions and future required identity nodes

These are existing audited regression paths, proposed for execution after readiness with an isolated DSN and validated media/Pi/browser environment. The full suite remains required alongside targeted reproduction. `STUDIO_REQUIRE_INTEGRATION=1` alone is insufficient: baseline helpers can still `importorskip` before that check, and existing optional cases remain separately accounted for.

```bash
make lint VENV_DIR="$AUTH32_VENV"
"$AUTH32_PYTHON" -B -m pytest -p no:cacheprovider -ra \
  --junitxml="$AUTH32_EVIDENCE_DIR/regression-focused.private.xml" \
  tests/contracts/test_studio_contracts.py tests/contracts/test_backlot_contract.py \
  tests/backlot/test_server.py \
  tests/integration/test_studio_repository.py \
  tests/integration/test_studio_worker.py tests/integration/test_studio_recovery.py \
  tests/integration/test_studio_approvals.py tests/integration/test_studio_policy.py \
  tests/integration/test_studio_end_to_end.py \
  tests/browser/test_studio_submission.py tests/browser/test_studio_board_controls.py \
  tests/browser/test_studio_end_to_end.py
"$AUTH32_PYTHON" -B -m pytest -p no:cacheprovider -ra \
  --junitxml="$AUTH32_EVIDENCE_DIR/regression-all.private.xml" tests
```

The existing upgrade node is `tests/integration/test_studio_repository.py::test_existing_control_plane_upgrades_to_call_and_file_journals`. It validates the earlier control plane, not a future identity migration by itself. Final required identity nodes must be supplied by the integrated module owners and mapped to every matrix ID/variant. Do not invent filenames or tests for absent implementations now.

```bash
# PENDING: replace this entire placeholder with actual reviewed, collected node IDs.
AUTH32_REQUIRED_NODEIDS=( '<PENDING: required real identity nodes from #24–31 and final #32>' )
"$AUTH32_PYTHON" -B -m pytest -p no:cacheprovider -ra --strict-markers \
  --junitxml="$AUTH32_EVIDENCE_DIR/identity-required.private.xml" \
  "${AUTH32_REQUIRED_NODEIDS[@]}"
```

This placeholder cannot yield a successful required run. Future acceptance checks the collected-node manifest, each real-success/negative result, missing dependencies, skip/xfail ledger and actual artifacts independently of pytest exit code. Keep external/paid smoke flags disabled. The existing test network guard covers Python sockets; verify subprocess transports and approved local profiles also cannot contact paid providers. Neither this snippet nor the matrix adds a workflow.

### Backup, explicit mapping and restore worksheet

Only run after owner-delivered isolation/configuration, backup credentials and empty restore destinations are verified. Authentication for PG tools must come from private service/pass files, never password-bearing argv or printed DSNs. Commands below illustrate a real business-database dump/restore; the final rehearsal also requires separate Logto/IdP DBs and matched private canonical/runtime/key backups per DB-09/10. Never restore into an existing live database or clean an existing volume.

```bash
: "${AUTH32_BUSINESS_PG_SERVICE:?Actual isolated business PG service}"
: "${AUTH32_BUSINESS_DB_NAME:?Disposable source DB name verified by preflight}"
: "${AUTH32_BACKUP_DB_USER:?Privately provisioned authorized backup user}"
: "${AUTH32_RESTORE_DB_NAME:?New empty isolated restore DB, created by operator}"
: "${AUTH32_RESTORE_DB_USER:?Privately provisioned restore user}"
test "$AUTH32_BUSINESS_DB_NAME" != "$AUTH32_RESTORE_DB_NAME"
docker compose --project-name "$AUTH32_COMPOSE_PROJECT" --file "$AUTH32_COMPOSE_FILE" \
  exec -T "$AUTH32_BUSINESS_PG_SERVICE" pg_dump --format=custom \
  --username="$AUTH32_BACKUP_DB_USER" --dbname="$AUTH32_BUSINESS_DB_NAME" \
  > "$AUTH32_EVIDENCE_DIR/business.private.pgdump"
docker compose --project-name "$AUTH32_COMPOSE_PROJECT" --file "$AUTH32_COMPOSE_FILE" \
  exec -T "$AUTH32_BUSINESS_PG_SERVICE" pg_restore --exit-on-error --no-owner \
  --username="$AUTH32_RESTORE_DB_USER" --dbname="$AUTH32_RESTORE_DB_NAME" \
  < "$AUTH32_EVIDENCE_DIR/business.private.pgdump"
# PENDING: exact #25 released mapping CLI/format, private mapping file and dry-run argv.
AUTH32_MAPPING_DRY_RUN_ARGV=( '<PENDING: #25 operator dry-run command and isolated-target arguments>' )
# PENDING: exact reviewed apply argv, after verified backup and clean dry-run only.
AUTH32_MAPPING_APPLY_ARGV=( '<PENDING: #25 operator apply command and isolated-target arguments>' )
"${AUTH32_MAPPING_DRY_RUN_ARGV[@]}"
"${AUTH32_MAPPING_APPLY_ARGV[@]}"
```

The future operator record must preserve the exact successful argv with secret values replaced by references, pre/post resource mapping and hash/journal manifests, actual backup permissions and restore destinations, and failure/no-partial-write evidence. No browser claim API, schema downgrade, worker drain command, key-rotation command, or customer deployment procedure is assumed to exist; their real released instructions remain PENDING for independent reproduction.

## Per-scenario evidence template and final gate

Use one record per variant/run, linked to private raw evidence and publishable sanitized evidence. Do not publish cookie jars, raw callback/assertion captures, private DTO headers, credentials, DB dumps or session keys. The portable report links to durable GitHub evidence or tracked sanitized artifacts, not temporary filesystem reports. IDs/timestamps/hashes may be retained where authorized; secrets are replaced by reference names.

```yaml
scenario_id: PENDING  # matrix ID plus explicit variant
status: PENDING
execution: not_run
mode: PENDING  # real_success | real_denial | fault_injection | operator_rehearsal
issue_owner: PENDING
candidate_sha: PENDING
integrated_main_sha: PENDING
utc_start_end: PENDING
runtime_versions_and_image_digests: PENDING
fixture_seed_and_private_config_reference: PENDING  # no secret values
actor_selected_workspace_roles_target_owner: PENDING
exact_command_or_browser_steps_redacted: PENDING
expected_http_headers_state_and_side_effects: PENDING
observed_http_headers_state_and_side_effects: PENDING
output_file_hash_ffprobe_playback: PENDING  # where applicable
revocation_t0_last_send_close_uncertainty: PENDING  # where applicable
backup_restore_mapping_before_after_evidence: PENDING  # where applicable
raw_evidence_private_reference: PENDING
sanitized_evidence_link: PENDING
exit_code_and_collected_test_node: PENDING
skip_xfail_or_missing_variant: PENDING
defect_owner_fix_sha_and_recheck: PENDING
independent_reviewer_name_sha_commands_verdict: PENDING
ci_run_url_head_sha_status: PENDING
```

Statuses change only when evidence exists: PENDING → observed pass/failure, with a separately named independent review and integrated-target check. A fault-injection pass does not update a corresponding unexecuted real-success record. Preparation checks of this document do not update scenario statuses.

| Separate optional/unverified ledger | Required accounting | Preparation state |
| --- | --- | --- |
| Existing optional skips | Full collected node IDs, reasons and scope; baseline count 17 is historical, actual final count unknown | PENDING |
| Existing xfails | Full node IDs, reasons, expected failure and actual result; baseline count 3 is historical, required identity xfail blocks | PENDING |
| Required identity gaps | Every unrun, missing, skipped, xfailed or fault-only real-success variant; each remains a blocker | All matrix scenarios PENDING |
| Production/customer/paid/Cloud | No production cutover, real customer IdP, paid provider generation or Cloud procurement performed | Unexecuted; outside this preparation/authorized local acceptance |

Final acceptance requires all of the following, currently PENDING:

1. #24–31 each have completed/reviewed/integrated implementation, required CI signoff and manual closure evidence. Parent confirms this before #32 final acceptance begins.
2. Every required matrix variant has reproducible real observations; both real SSO protocols and mail flows, personal/organization moving-media production, strict 401/403/404/context/replay isolation and measured revocation are covered. Required dependency gaps fail explicitly.
3. Real PG upgrade, explicit mapping, complete private backup/restore, session/key recovery, safe local compatibility and non-public rollback are independently reproduced without changing canonical production semantics.
4. Secret/platform-isolation checks and all necessary regressions pass; optional skip/xfail ledger is separate; all blockers repaired by their owner and rechecked on the repaired SHA.
5. A different named reviewer reproduces critical success/denial/recovery behavior on the exact candidate. Parent integrates, validates the actual target-main SHA with successful required CI and integrated regression, posts durable evidence and performs manual closure. #23's historical CI is not a substitute for this new target-main run.

Preparation handoff consists only of this focused document and its commit SHA. Test/fixture/CI implementation, final validation, acceptance and closure remain future work; no scenario in this document is signed off.
