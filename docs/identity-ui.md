# Account and workspace UI (AUTH-08 / #31)

Backlot's existing native JavaScript library, Studio and production board now show the verified account and current personal or organization workspace in Chinese, Japanese and English. Sign-in, registration, password recovery and enterprise SSO use the same-origin BFF's existing Logto flow. The browser receives no identity SDK, provider credentials or identity tokens. Explicit local mode remains available as a local workspace.

`/api/auth/me` supplies the permissions used to enable creation, cancellation, approval and organization management. Viewers retain task, board and verified video access. The server remains the authorization boundary; hiding a control never grants access. Organization creation, invitations, cancellation and member role/removal use the frozen public HTTP contracts, including their last-admin protection.

## Client lifetime and retries

`session.js` binds the client to `(user_id, workspace.key, context_id)`. Identity or workspace changes abort pending reads, close every tracked SSE connection, discard older responses, stop media and clear drafts and task state. GET responses must match the current `X-Auth-Context`. BroadcastChannel carries only a change notification; focus, visibility and a visible-page 15-second check revalidate the real session. A restored browser history page reloads its context before resuming work. Event-stream reconnection verifies `/me` and resumes the cursor only within the original binding.

When a different tab changes the cookie without BroadcastChannel, or identity reads recover after an outage, the verified context rebuilds the library or Studio through a safe localized navigation. Organization choices and configured controls are loaded again; old task URLs and drafts are never carried into a new space. The invitation landing stays in place to preserve its short-lived memory ticket.

Authenticated pending writes, the last task and SSE cursors use storage keys bound to the same user, workspace and context. Unscoped legacy task state is discarded when entering authenticated mode; local mode retains its original keys. A lost response can be recovered by retrying the same body with the original idempotency key in the same context. Permission 403 never resends a write. Only `csrf_invalid` permits one refresh and retry after confirming the original binding. Context conflicts discard pending intent. An authority outage clears the protected view while retaining the original scoped key for a later verified recovery.

An active page retains its last verified CSRF/context in memory solely for local session revocation when identity reads become unavailable. That value cannot authorize business writes or restore protected views. An explicit logout or identity notification clears it; a new page without a verified context waits for the account service.

## Invitation landing

`/{en|zh|ja}/invitations/accept` is a public shell with no identity data or third-party resources and `Referrer-Policy: no-referrer`. Its first script captures a restricted ticket in memory and removes the hash/query from browser history before loading account modules. The ticket expires from the page after ten minutes and is removed on success or navigation; it never enters localStorage, sessionStorage, DOM attributes, events or logs. The ordinary pages keep their existing font stylesheet separately, so invitation pages do not contact the font provider.

A signed-out recipient opens Logto in a new window, then returns to the original page to accept with the displayed verified account. The accept request uses a memory-only idempotency key and the current CSRF/context. Changing the context after an accept attempt discards that attempt; reopening the original email link is required after navigation or expiry. Recipient verification and ticket reuse/cancellation/expiry are enforced by the backend.

## Validation boundary

The dedicated tests are `auth-runtime/tests/test_ui_accounts.py` and `test_ui_production.py`, with the read-only private `AUTH_TEST_FIXTURE` ledger for the assigned exact callback origin `http://127.0.0.1:4764`. They exercise real Chromium, Logto, isolated PostgreSQL, local SMTP, official Pi, controlled model/media boundaries and FFmpeg. HTTP response loss, stream disconnect and a held real response body are transport faults; successful identity and production responses are never fabricated. Native membership changes and temporary organizations are restored or removed in `finally`.

Required scenarios include lost-response recovery, genuine permission and CSRF errors, a slow old-context read, two accounts with multiple tabs and old task URLs, real invitation acceptance and member changes, last-admin rejection, three-language keyboard/error behavior and 390px layout, plus personal and organization production approval, playback and hash-verified downloads. Raw diagnostics stay in private 0600 files; publish only reviewed scenario results, never driver call logs.

#31 remains open until #27–#30's actual integrated behavior, including standard OIDC and SAML enterprise SSO, has passed independent review and target-main verification. Author checks on a candidate do not close those dependencies or replace the final CI gate. Production deployment, customer IdPs and paid generation remain outside this task.
