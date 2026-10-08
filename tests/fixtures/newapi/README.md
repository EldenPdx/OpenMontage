# New API contract fixtures

Upstream baseline: xvanai-new `00e4e91602df6b3c3c1f39d05e2ae704ad3f6d6a`.
OpenMontage baseline: `9327439db69021ab4b0e2776729bf3b58fdb5a87`.
Reviewed 2026-10-09. Fixtures and inline fake-server payloads are **manually
constructed from the source contracts**, not captures of live requests. Model
IDs, prompts, request IDs, credentials, timestamps, and generated tiny media
are synthetic; no real Key, private request, or production gateway appears.

| Fixture / contract | Upstream source and symbol |
|---|---|
| `models.json` | `controller/model.go`: `ListModels` / `GetModels` (model list), `relaykit/types/endpoint_type.go` (endpoint type strings) |
| `deployment.yaml` | OpenMontage deployment-only capability/parameter policy; synthetic administrator example, not a promise that a vendor or model exists |
| Bearer headers / HTTP error fixtures | `middleware/auth.go`: `TokenAuth`; `relaykit/dto/claude.go` (Claude error), OpenAI relay error envelopes |
| `messages` / `responses` wire fixtures | `relaykit/dto/claude.go`, `relaykit/dto/openai_request.go`, `relaykit/dto/openai_response.go`; `service/responses_usage.go` (SSE event names) |
| Image / task wire fixtures | `relaykit/dto/openai_image.go`; `controller/async_image.go` (submit/status/result), `controller/task.go`: `TaskFetch` |
| Video wire fixtures | `pkg/jsplugin/routing.go` (public video routes), `controller/relay.go` (`RelayVideo`), `controller/video_proxy.go` (authenticated content) |
| Speech binary fixtures | `relaykit/dto/audio.go`, `relay/channel/openai/audio.go` (binary speech response) |

`/v1/models` endpoint metadata is deliberately incomplete: it does not establish
TTS, image editing, async capability, parameter names, or prices. Deployment
profiles establish those facts. The sample profile uses a Sora-like video shape
only as a source-derived example; other plugins must declare their own mapping.

Tests use public configuration and HTTP adapter interfaces. Fake paid requests
are counted; interrupted submissions are never retried. All price states remain
`unquoted` / `cost_usd=None`. Real smoke execution needs an explicitly configured
gateway with deployed plugins, allowed models, a balance, and verified prices.
