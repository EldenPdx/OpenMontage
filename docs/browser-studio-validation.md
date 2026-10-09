# Browser Studio validation

The integration source is official Pi v1.1.0, commit
`abe508e1b89912adde45528136c3221eb69acdd7`. The installer verifies the official
source archive and package-lock checksums recorded in `pi-runtime/source.lock.json`.
Local verification uses real PostgreSQL 17.11, Python 3.10.22, and Node 24.21.0;
the clean development image uses Node 22.19.0 and the same real Pi.

## Required evidence

| Boundary | Verification |
| --- | --- |
| DTO/state/approval contracts | Executable schema, invalid inputs/states, exact revision/hash/scope and idempotency checks |
| Configuration | Real Pi consumes two providers and Responses/Completions; actual wire credentials, headers, token/sampling/thinking limits and polluted-host isolation |
| PostgreSQL | Real v1→v2 migration, competing workers, persistent commands/events/gates, stale fences, atomic budget reservations and unknown-result holds |
| Pi RPC | Real startup/stream/session restore/cancel/exit; optional fault transports cover framing, UTF-8, CRLF, ordering, oversized frames and hung/crashed processes |
| Tool boundary | Canonical input/media hashes, managed paths, browser-only approval evidence, CLI ownership and zero model requests when admission is denied |
| Recovery | Process identity/group cleanup, file intents, gateway job receipt captured during native polling, resume with total POST count remaining one |
| Full production | Real Pi and PG execute the existing animated-explainer pipeline, including script revision and every proposal/script/scene/assets gate |
| Browser delivery | Real Chromium submits, reviews, revises, approves, plays advancing video time, downloads MP4 and independently ffprobes 1280×720 / two seconds |
| Build | `make lint`, complete `make test`, JS checks, clean Docker build, image-local real Pi RPC/health and corresponding main CI |

The deterministic media fixtures create real moving video and audio. They test
production control and local rendering; they do not certify a paid gateway's
creative quality or compatibility. Failure scenarios intentionally assert blocked,
failed or recovery states, rather than converting them into success.

## Independent xvan smoke

**Status for this implementation: not run.** No paid inference, image or video
request is part of default validation, and no production deployment was performed.

Supply credentials only through the backend environment. Start with the read-only
catalog check:

```sh
OPENMONTAGE_ALLOW_NETWORK=1 STUDIO_XVAN_LIVE_SMOKE=1 .venv/bin/python -m pytest tests/qa/test_studio_pi_live.py::test_studio_xvan_readonly_catalog
```

Only after separately approving the sample cost and unknown-fee reservation, run
the real Pi Responses streaming/tool-result/usage sample:

```sh
OPENMONTAGE_ALLOW_NETWORK=1 STUDIO_XVAN_LIVE_SMOKE=1 STUDIO_XVAN_PAID_APPROVED=1 .venv/bin/python -m pytest tests/qa/test_studio_pi_live.py::test_studio_xvan_real_pi_stream_tool_result_and_usage
```

It permits at most two model requests, stores fee journals in the persistent
`studio_live_smoke` PostgreSQL schema, and writes a sanitized report under the
generated project. Unquoted holds remain for trusted reconciliation; the
reservation does not guarantee the gateway's actual charge. Reconcile earlier
live samples before another one. Failure never switches model or protocol.

The existing separately authorized media smoke covers the configured default
image/video models; narration is excluded:

```sh
OPENMONTAGE_ALLOW_NETWORK=1 OPENMONTAGE_NEWAPI_LIVE_SMOKE=1 NEW_API_LIVE_COST_APPROVED=1 .venv/bin/python -m pytest tests/qa/test_newapi_live.py -k 'readonly or image_generation or video_generation'
```

Catalog metadata alone does not prove Responses streaming, tool calls, media
permissions or pricing. Record these live results independently from CI results.
