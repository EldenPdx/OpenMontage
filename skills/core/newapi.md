# New API Gateway Usage

## When to Use

Read this when a run selects `newapi` for text, images, video, or narration, or when resuming a New API media job. The administrator supplies the gateway address, model aliases, capabilities and protocol profiles. The user's only required credential is `NEW_API_KEY`.

## Process

1. Discover the registry and inspect `provider_menu_summary()` and the selected tool's `get_info()`. These read deployment profiles without contacting the gateway. An optional, explicit `refresh_models(load_settings())` from `tools._newapi.models` and `tools._newapi.config` performs the authenticated, read-only `/v1/models` check. Visibility is cached per gateway and credential; it does not establish every plugin operation's availability.
2. Check the configured profile and the current pipeline stage. Announce the tool, gateway model alias and operation, obtain the existing budget approval, and use the current pipeline's approval/checkpoint rules. Prices are deployment facts: `estimate_cost` raises `PriceQuoteRequired`, and results have `cost_usd=None` / `cost_status="unquoted"` until a real quote is supplied. Asset-manifest cost fields accept numbers: omit unknown numeric `cost_usd` / `total_cost_usd` fields, retain the unquoted status in metadata, and let Backlot show unknown cost until a quote is available.
3. Lock the path with `hosting_provider="newapi"`, `preferred_tool`, or `allowed_providers=["newapi"]`. `preferred_provider="newapi"` is a preference and can lose to another provider's score. Explicit model aliases remain unchanged on the wire. Conflicting `model` / `model_name` / TTS `model_id` inputs fail.
4. Execute through the registry/selector. Save assets under `projects/<id>/assets/...`, preserve returned provider/model/usage metadata, and record canonical artifacts with the existing schemas and checkpoint API. A tool result is not a creative-stage approval.
5. On a pending or download failure, retain `resume_job` and `<output>.job.json`. Resume the same tool/gateway/model/output with that job; resumed media calls make GET requests rather than another paid POST. A submission disconnect without a usable public ID has an unknown outcome: inspect the gateway before deciding whether to submit again.

## Administrator Configuration

The following is an example shape for `config.yaml`, not a claim that these aliases exist in a deployment. Replace every alias, capability, operation, parameter and limit with verified channel/plugin facts. Keep vendor credentials and the gateway key outside this file.

```yaml
newapi:
  base_url: https://gateway.example/v1
  default_llm_protocol: responses
  default_models:
    text_generation: gateway-text
    image_generation: gateway-image
    video_generation: gateway-video
    tts: gateway-speech
  models:
    gateway-text:
      capabilities: [text_generation]
      protocols: [responses]
      operations: [responses]
      supported_parameters: [temperature, max_output_tokens]
      defaults: {max_output_tokens: 256}
    gateway-image:
      capabilities: [image_generation]
      operations: [generate, edit, async, async_edit]
      supports_async: true
      supported_parameters: [prompt, n, size, quality, response_format, image, mask]
      defaults: {n: 1}
      parameter_map: {json_image: image, json_mask: mask, multipart_image: image, multipart_mask: mask}
    gateway-video:
      capabilities: [video_generation]
      operations: [text_to_video, image_to_video]
      supports_sync: false
      supports_async: true
      supported_parameters: [prompt, seconds, size, input_reference]
      parameter_map: {duration: seconds, reference_image: input_reference}
      limits:
        seconds: {type: integer, enum: [4, 8, 12]}
    gateway-speech:
      capabilities: [tts]
      operations: [speech]
      supported_parameters: [input, voice, response_format, speed]
      defaults: {voice: alloy, response_format: wav, speed: 1}
```

The base URL accepts a service root or a single `/v1` suffix. Remote gateways use HTTPS; an explicitly configured local/private gateway may use HTTP. `NEW_API_BASE_URL` is an optional administrator override. Empty configuration leaves New API unavailable and preserves other providers. Environment credentials take precedence over `.env`; the adapter uses its own gateway setting and leaves global OpenAI SDK base URLs unchanged.

Profiles, not model-name prefixes, determine protocols and operations. `/v1/models` metadata cannot infer TTS, edits or asynchronous image support. The gateway owns channel selection, vendor model mapping and pricing. The user token must permit the chosen model; the deployed gateway must have the required channels and plugins.

The generic `openai` catalog marker describes a preferred surface, not an exhaustive list of relay protocols. It does not erase an explicitly configured Responses profile or prove Responses compatibility; verify that exact model/protocol with a separately authorized sample. Concrete protocol conflicts still fail rather than silently selecting another protocol.

## Approved Tool Calls

These are calls within an already approved pipeline stage, after initialization and budget approval. They use administrator defaults; an explicit `model` can select another declared alias.

```python
from tools.tool_registry import registry
registry.discover()

image = registry.get("image_selector").execute({
    "hosting_provider": "newapi", "prompt": "A blue room",
    "output_path": "projects/my-film/assets/images/room.png",
})
video = registry.get("video_selector").execute({
    "hosting_provider": "newapi", "operation": "image_to_video",
    "prompt": "Move slowly through the room", "duration": 4,
    "reference_image_path": "projects/my-film/assets/images/room.png",
    "output_path": "projects/my-film/assets/video/room.mp4",
})
speech = registry.get("tts_selector").execute({
    "hosting_provider": "newapi", "text": "Welcome to the room.",
    "format": "wav", "output_path": "projects/my-film/assets/audio/room.wav",
})
```

Image edits use `generation_mode="edit"` plus `image_path` / `image_paths` or `image_url` / `image_urls`, and an optional `mask_path` / `mask_url`. Source-image inputs with no explicit operation select edit mode. The image selector's `operation="generate"` execution control allows an explicit `generation_mode="edit"`; direct image-tool operation/mode conflicts still fail. `request_mode="async"` requires a profile declaring `async` / `async_edit`; synchronous mode is the default. Multipart field names follow `parameter_map`. An explicit image `n=0` is normalized to the upstream paid default of one image, not zero generated images or zero cost. Native local video references go directly to the gateway and do not require a fal key/upload.

Video durations are bounded whole seconds. The OpenAI-compatible JSON wire uses string `seconds`, integer `duration`, and canonical `ratio`; declared plugin extensions use `provider_options`. A plugin-specific `metadata` contract must be configured explicitly: the type-61 Doubao/Seedance plugin reads its rendering options and structured remote references there. JSON reference support does not imply multipart upload support. Conflicting duration or rendering aliases fail before submission. Image sizes use ASCII `x`, and split URL/Base64 representations are saved as one image, with the first successful image written to the requested output path.

`provider_params` may override declared wire defaults and standard values; it cannot replace routing, authentication, endpoints, stream/async controls or output paths. Unsupported semantics fail before submission. TTS accepts `voice` / `voice_id`, `format` / `response_format` / `output_format`, and `speed` / `speaking_rate`, with conflict checks. TTS SSE is unsupported. Raw PCM requires verified `limits.response_format.x-pcm` facts (`sample_rate`, `channels`, `sample_width`); normal audio/video validation requires ffprobe.

Rank mode (`operation="rank"`, optional `target_operation`) and dry runs do not generate media. Rank honors the same exact tool, gateway, model and allowed-provider constraints.

## Explicit LLM Calls

`newapi_llm` is an optional text tool. Configuring `llm.provider=newapi` supplies its defaults; it does not replace the model running the current chat or install an agent loop. Read-only research sources still come from the pipeline's search/fetch tools. An approved text call can draft or transform source-grounded material:

```python
result = registry.get("newapi_llm").execute({
    "protocol": "responses",
    "system": "Return a concise narration draft from the supplied facts.",
    "messages": [{"role": "user", "content": "Verified facts: ..."}],
    "max_tokens": 256,
})
```

Only consume successful, completed output. The agent turns returned `text`, native `output` / `content`, `tool_calls` and `usage` into the existing stage artifact and validates its schema. Tool calls are returned for the agent to handle. For Messages choose a declared `anthropic` profile/operation; protocol-native multimodal/tool-result inputs use `request`. Explicit SSE is aggregated into the same result surface. LLM background recovery and WebSockets are outside this adapter's contract.

## Recovery Boundaries

```python
recovered = registry.get("video_selector").execute({
    "preferred_tool": "newapi_video", "resume_job": video.data["resume_job"],
    "output_path": "projects/my-film/assets/video/room.mp4",
})
```

Use `newapi_image` with the image selector for image jobs. A saved New API `resume_job` also pins selector routing to its tool and model when route controls are omitted. A different gateway, tool, model, operation or output path is rejected. A `job_path` alone requires an explicit New API tool selection. Temporary GET failures have bounded retry/deadline handling; paid POSTs are not retried. Invalid or interrupted downloads preserve an existing valid asset and report the saved job.

An ID does not guarantee permanent remote content. Upstream async-image result payloads use a process-local cache with a 24-hour TTL and can become unavailable after restarts/expiry. Download completed images promptly. Video content may expire or return `artifact_gone`. Retain such errors and obtain a new approved decision rather than silently switching providers or reposting.

Default contract/integration tests are offline. Live smoke is separate and explicitly enabled only with a reachable deployment, model permissions and approved sample costs; mock success is not live verification.

## Self-Evaluate

Confirm the selected profile/operation is configured, the route is locked, costs are approved, output files validate, metadata is secret-free, and the stage artifact/checkpoint follows the existing pipeline contract. Record a live result only when a real authorized live call was performed.
