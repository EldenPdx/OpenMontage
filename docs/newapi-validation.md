# New API integration acceptance

Validated against OpenMontage baseline `9327439db69021ab4b0e2776729bf3b58fdb5a87` and xvanai-new baseline `00e4e91602df6b3c3c1f39d05e2ae704ad3f6d6a`. Both local repositories matched the planned baselines on 2026-10-09. The gateway repository was read-only throughout implementation.

## Offline evidence

All default tests use synthetic credentials and wire fixtures whose source/version is recorded in [tests/fixtures/newapi](../tests/fixtures/newapi/README.md). Requests terminate at loopback HTTP/TLS servers or a transport boundary; default CI cannot call a billable service. PNG, WAV, MP3 and MP4 assets are real local media, verified with Pillow and FFmpeg/ffprobe.

| Public boundary | Coverage |
|---|---|
| Configuration/client/model catalogue | Old defaults and dotenv precedence; URL prefixes and ports; explicit models refresh and credential-scoped cache; forbidden overrides; bounded GET retries/deadlines; no paid POST retries; safe errors, netrc isolation and CDN redirects |
| `newapi_llm.execute` | Messages and Responses, synchronous and SSE; native tool/media input; text and tool arguments preserved; incomplete/refusal/errors; explicit terminal events; no background/host-agent replacement |
| `image_selector` → `newapi_image` | Four generate/edit sync/async POST endpoints; JSON and repeated multipart image/mask fields; real PNG, object/list results, multi-image partial failures; persisted tasks, expiry and GET-only recovery |
| `video_selector` → `newapi_video` | JSON and native local-reference multipart; public receipt including failed submissions; status then authenticated content; real MP4 dimensions/duration; strict resume and expired artifacts |
| `tts_selector` → `newapi_tts` | Voice/format/speed aliases; binary WAV/MP3; declared PCM sampling facts; unsupported SSE; JSON/empty/truncated/interrupted media; original asset preserved on failure |
| Cross-capability integration | One gateway key and deployed profiles; real registry/selector/codec/HTTP chain; exact endpoint/auth/model/body checks; zero-POST resume; strict routing with competing providers; schema-valid artifacts, awaiting-human checkpoints, Backlot assets/events and unknown costs |

Unknown prices remain `cost_usd=None` and `cost_status="unquoted"`. Asset-manifest numeric cost fields are omitted until quoted; metadata records the unknown state. No generation result approves a creative stage.

MP3 completeness checks compare declared Info/Xing byte/frame counts with the actual stream. An MP3 without a trustworthy length declaration cannot distinguish a valid shorter clip from an omitted complete tail; transport size, container and media validity checks still apply. PCM is accepted only with explicit sample rate, channels and sample width in the deployment profile.

Run the repository acceptance commands in the installed development environment:

```sh
python -m pytest tests/contracts/test_newapi_*.py -q
make lint
make test
```

The pristine baseline passed `make lint` and `make test`: 1,897 passed, 10 skipped, 3 existing expected failures. Optional network/golden/runtime checks were skipped; the three expected failures concern existing stock-source transport tests. Final acceptance retains this distinction and includes the added tests.

Final local acceptance on 2026-10-09: `make lint` passed; `make test` passed with **2,173 passed, 16 skipped, 3 existing xfailed, 1 subtest passed** in 200.95 seconds. All changed Python modules compiled. The 31-test cross-capability matrix also passed an independent review/run, with five default live-smoke skips. Wire SSE fixtures intentionally retain their final blank event delimiter.

The opt-in runner was separately exercised against an isolated simulated deployment: five samples passed for both synchronous and async-only image profiles, with exactly four paid-submission-shaped POSTs per run. Default mode issued zero HTTP requests; read-only mode issued only one models GET and skipped all four submissions. These reports were labeled `mocked_harness_verification` with `actual_live_executed=false`.

## Live deployment acceptance

**Real gateway live smoke: NOT RUN.** This development environment did not contain a New API gateway address/profile and key for a deployed service. Offline success and a simulated check of the smoke runner are not evidence of a real deployment. No production deployment was performed.

The deployer must first provision the address, default aliases, protocols, operations, verified parameter limits, required plugins, model permissions, balance and sample prices. Follow [New API usage](../skills/core/newapi.md); the end user's only required credential is `NEW_API_KEY`. Keep normal preflight, cost approval and pipeline checkpoint rules in effect.

The smoke runner defaults to five skips. Explicit network opt-in plus `OPENMONTAGE_NEWAPI_LIVE_SMOKE=1` enables a read-only models preflight. Four paid samples additionally require `NEW_API_LIVE_COST_APPROVED=1`, acknowledging the administrator's verified quote and the existing budget approval policy.

```sh
# Read-only deployment preflight: one models GET; paid samples skip.
OPENMONTAGE_ALLOW_NETWORK=1 OPENMONTAGE_NEWAPI_LIVE_SMOKE=1 \
  python -m pytest tests/qa/test_newapi_live.py -m live_api -q -s

# After permissions, balance, prices and sample costs have been approved:
OPENMONTAGE_ALLOW_NETWORK=1 OPENMONTAGE_NEWAPI_LIVE_SMOKE=1 NEW_API_LIVE_COST_APPROVED=1 \
  python -m pytest tests/qa/test_newapi_live.py -m live_api -q -s
```

The runner chooses configured minimum sample limits when available, supports an explicitly declared async-only image default, and makes one POST per capability. It writes sanitized model/status/request-id/artifact evidence to `projects/newapi-live-smoke-<id>/live-report.json`. It uses user APIs only, does not access management endpoints and does not need vendor credentials. Asynchronous image results may expire from the gateway's process-local cache; public IDs do not promise permanent recovery. Production deployment and final real-service sign-off remain the deployer's responsibility.
