# Trusted Pi configuration

Web Studio's background Agent has a separate `studio` configuration in config.yaml.
It does not reuse the `newapi_llm` tool's smoke-test token limit and does not change
the model hosting this chat. Existing New API media/direct provider settings remain
available; the new Agent configuration does not override them.

The trusted default profile is `xvan`: https://xvan.ai/v1, `openai-responses`,
`gpt-5.6-sol`, credential reference `NEW_API_KEY`. Media defaults are
`Images2.5-Flare` and `dreamina-seedance-2-5-260628`; narration defaults to false.
Neither task APIs nor worker logic require xvan. Deployment operators configure
additional profiles in YAML; a browser selects only an existing profile ID.

```yaml
studio:
  enabled: true
  default_profile: xvan
  concurrency: 1
  profiles:
    xvan:
      provider: xvan
      base_url: https://xvan.ai/v1
      api: openai-responses
      model: gpt-5.6-sol
      credential_env: NEW_API_KEY
      input: [text, image]
      context_window: 200000
      max_output_tokens: 16384
      reasoning: true
      thinking_level: medium
      price: null
    local:
      provider: local
      base_url: http://127.0.0.1:8001/v1
      api: openai-completions
      model: local-model
      credential_env: LOCAL_MODEL_KEY
      input: [text]
      context_window: 32768
      max_output_tokens: 1024
      reasoning: false
      thinking_level: off
      sampling_params: {temperature: 0.35, top_p: 0.8}
```

Load precedence: defaults → trusted config.yaml → backend `STUDIO_ENABLED` and
`STUDIO_DEFAULT_PROFILE`. `STUDIO_ENABLED` accepts true/false/1/0; the default
profile must exist. Credentials come only from the declared environment references
passed by the backend. Backend credential loading is separate from configuration
values: do not put raw keys or `${KEY}` expansions into YAML model values. Extra
headers use `header_env: {X-Channel-Key: CHANNEL_KEY}`, resolving the referenced value
only inside the private process environment. Routing headers and isolation variables
(HOME, PATH, NODE_OPTIONS, Pi agent-directory controls) cannot be credential references.
Models.json never contains a literal secret or `!command` credential expansion.

| Trusted profile field | Pi/native mapping or enforcement owner |
| --- | --- |
| provider/base_url/api/model | private models.json provider metadata and exact CLI model selection |
| input/context_window | models.json input/contextWindow; Agent requires text capability |
| max_output_tokens | models.json maxTokens; real provider request receives bounded max token parameter |
| reasoning/thinking_level | model reasoning plus CLI/settings thinking; non-reasoning requires off |
| sampling_params | models.json samplingParams for Responses/Completions only |
| header_env/credential_env | environment interpolation in private models.json; values resolved backend-only |
| request_timeout_seconds | retry.provider.timeoutMs; PiRPC command timeout and guarded transport |
| idle_timeout_seconds | httpIdleTimeoutMs; worker/runner idle deadline |
| startup_timeout_seconds | PiRPC startup/version/readiness timeout |
| task_timeout_seconds/max_turns | worker supervisory limits; no limit can be only a YAML annotation |
| max_retries | supervisor retry ceiling; provider SDK retries are always zero, unknown outcome is never retried |
| price | estimated four token rates; shared budget journal remains billing authority for the task |

Supported native APIs in this version are `openai-responses`, `openai-completions`,
and `anthropic-messages`. The OpenMontage media label `responses` is not a Pi API
identifier; translate it explicitly. A directory's generic OpenAI label never proves
endpoint compatibility. Unsupported APIs or parameters fail explicitly instead of
switching protocols or models. Sampling accepts temperature, top_p, top_k, min_p,
frequency_penalty, presence_penalty, repetition_penalty and seed with numeric range
checks; APIs that do not map samplingParams reject it.

All price rates are integer USD micros per million tokens: input, output, cache_read
and cache_write. Quote all four rates explicitly, including any known free rate as 0.
Missing price is `null` / unquoted. Pi may report native zero pricing for omitted
metadata; public costs and budget settlement use the trusted profile/journal, never
reinterpret this zero as free. Rates are estimates and cannot guarantee a remote
billing hard cap. Unknown usage/submission remains unresolved.

`snapshot_for` saves a non-secret selection and SHA256 of the exact trusted profile
configuration; `profile_for_snapshot` refuses changed configuration. Runs retain
approved budget and media selections. Credentials can be rotated behind the same
reference without copying them into the snapshot. An active run never silently
adopts a new provider/model/token setting; changed configuration requires explicit
reconciliation/new approval scope.

`prepare_pi` creates private `.runtime/studio/agents/<task>/<run>/` models/settings
and empty auth.json, private HOME/work directories and a sessions root outside
projects. It uses the pinned official Pi CLI installed by `make studio-pi` and returns
ManagedPiConfig for PiRPC. The caller passes its argv, env, cwd, session_root, secret
redaction values and timeouts; PiRPC appends the exact persisted session path.
It never starts `--continue`, `--no-session` or a shell command.

The process drops host auth/models/settings, unrelated credentials, NODE_OPTIONS,
ambient extensions/MCP and inherited project context. It uses offline model catalog,
no built-in tools, no extensions/MCP/skills/context files and no project approval;
the worker explicitly loads only the trusted OpenMontage extension. Native compaction
is enabled only for `studio-guarded`; its summary requests use the same journal,
budget and turn limit as ordinary model requests. Cache warming and automatic SDK
retries stay disabled. Normal requests retain Pi's native session/cache-affinity hints;
these do not guarantee a gateway cache hit or lower fees. Positive retry configuration
requires that extension and
is still bounded by worker safe-retry policy. Unknown billed outcomes cannot be
repeated regardless of configured retry count.

With that extension, generated models.json sets the provider API to `studio-guarded`
so native profile merging cannot replace its fail-closed transport wrapper. The
original profile API remains in the frozen snapshot and is delivered through the
private bridge for delegation. The extension forces request authorization/reservation
before the native provider network call; a thrown before_provider_request hook alone
is insufficient because Pi v1.1.0 catches those hook exceptions.

Tests in `tests/contracts/test_studio_pi_config.py` use real pinned Pi with local
Responses/Completions services. They inspect actual request tokens/sampling/thinking,
model capability selection, persistent managed sessions, credential redaction and
host contamination isolation. Full worker deadline/turn-limit and permission/budget
interception behavior is covered by the worker/bridge integration acceptance. No
paid xvan call is required for these tests; live gateway smoke remains separately
opt-in and requires a quote/authorization.
