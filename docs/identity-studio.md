# Authenticated Studio

Studio consumes the frozen [identity contracts](identity-contracts.md). Configuration and task creation/listing require the selected workspace's capability. Task detail, approval, cancellation, resume, events and result routes perform a scoped ownership lookup before checking capability, replay, state, gate evidence or file metadata. Foreign and unassigned historical tasks return the same 404. Personal owners and organization admins/editors may submit production; viewers may read authorized resources.

Creation and commands receive their workspace and actor from the verified server AuthContext. Task DTOs project only safe user IDs and workspaces. Approval decisions retain exact version/gate/artifact/checkpoint/scope checks, while the Repository records the accepting actor. Worker, Pi and the structured tool bridge use the committed command/run context; logging out or selecting another workspace cannot move or cancel accepted work. Owned initialization rechecks the shared durable root/project protection before writing canonical files.

SSE authorizes replay, every sensitive frame and its idle heartbeat. Results authorize before verified bytes, Range/conditional headers and every body chunk. A 15-second watchdog also closes a stalled response when revalidation fails, with a bounded 10-second authorization attempt. Revoked streams can close an already-started HTTP transfer; clients must treat that as a disconnect and reauthorize any reconnect. Revocation never emits a sensitive error frame. Bytes delivered before revocation cannot be recalled.

Pi retains its existing isolated environment with only its selected model credentials and scoped bridge token. Fixed media subprocesses retain only PATH, locale, temporary-directory and existing proxy-bypass settings plus the New API gateway credential/address. Preserving the existing bypass settings avoids unsafe macOS system-proxy discovery after fork. They disable `.env` loading so BFF/session/Management credentials and the private database connection cannot be imported again. This requires python-dotenv 1.2.0 or later; shared dependency integration is owned by the Orchestrator. Browser roles cannot change credentials, providers or execution settings.

## Verification

Use a dedicated real Logto `fixture-auth28.json` dataset with two personal users, organizations A/B and admin/editor/viewer/cross-organization memberships. Provide `AUTH_TEST_FIXTURE`, `STUDIO_TEST_DATABASE_URL` and `STUDIO_TEST_SESSION_KEY` privately to the process; never place their values in test artifacts. Install the pinned official Pi runtime, Chromium, FFmpeg and ffprobe. Missing required dependencies fail these acceptance tests.

```sh
STUDIO_REQUIRE_INTEGRATION=1 python -m pytest -p tests.conftest -q auth-runtime/tests/test_studio_auth28.py
```

The suite uses public HTTP and real browser cookies plus approved PostgreSQL persistence/fault seams. A controlled local model/media server drives the actual official Pi process and FFmpeg rendering, without paid provider calls. It verifies personal/organization production, ordinary/revise/reject/resume and cost gates, committed actors, playable/hash-verified output, scoped cursor/replay behavior, lost-response recovery, historical/signed-out denial, expiration and active SSE/result revocation. A temporary trusted Pi probe observes environment names from inside the real process; a real media child checks isolation after attempting `.env` loading. It records no credential values.

Production deployment and paid/customer-provider validation remain outside this verification. Independent review and integrated target-main CI are required before closing Issue #28.
