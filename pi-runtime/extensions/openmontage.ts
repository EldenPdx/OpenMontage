/** Trusted run-bound bridge. Provider interception happens inside streamSimple, not advisory hooks. */
import { createHash, randomUUID } from "node:crypto";
import {
  anthropicMessagesApi, createAssistantMessageEventStream, openAICompletionsApi, openAIResponsesApi,
} from "@earendil-works/pi-ai/compat";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

class BridgeError extends Error {
  constructor(public code: string) { super(`studio-policy:${code}`); }
}

export default async function (pi: ExtensionAPI) {
  const endpoint = process.env.STUDIO_BRIDGE_URL;
  const token = process.env.STUDIO_BRIDGE_TOKEN;
  if (!endpoint || !/^http:\/\/127\.0\.0\.1:\d+$/.test(endpoint) || !token) {
    throw new BridgeError("bridge_unavailable");
  }
  async function bridge(action: string, input: Record<string, unknown> = {}, signal?: AbortSignal) {
    const timeout = AbortSignal.timeout(["execute", "resume"].includes(action) ? profile.task_timeout_seconds * 1000 : 30_000);
    const response = await fetch(endpoint + "/" + action, {
      method: "POST", headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify(input), signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    });
    const result = await response.json() as any;
    if (!response.ok || !result.ok) throw new BridgeError(result.error?.code || "bridge_unavailable");
    return result.data;
  }
  const profile = await bridge("profile");
  const delegates: Record<string, any> = {
    "openai-responses": openAIResponsesApi(), "openai-completions": openAICompletionsApi(),
    "anthropic-messages": anthropicMessagesApi(),
  };
  const delegate = delegates[profile.api];
  if (!delegate || !process.env[profile.credential_env]) throw new BridgeError("profile_unavailable");

  function emptyMessage(model: any) {
    return { role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
               cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      stopReason: "stop", timestamp: Date.now() } as any;
  }
  pi.registerProvider(profile.provider, {
    baseUrl: profile.base_url, apiKey: "$" + profile.credential_env, api: "studio-guarded",
    models: [{ id: profile.model, name: profile.model, reasoning: profile.reasoning, input: profile.input,
      contextWindow: profile.context_window, maxTokens: profile.max_output_tokens,
      cost: profile.price ? { input: profile.price.input / 1e6, output: profile.price.output / 1e6,
                             cacheRead: profile.price.cache_read / 1e6, cacheWrite: profile.price.cache_write / 1e6 }
                          : { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple(model: any, context: any, options: any) {
      const output = createAssistantMessageEventStream();
      void (async () => {
        let intent: any = null;
        let terminal: any = null;
        try {
          if (model.provider !== profile.provider || model.id !== profile.model || options?.signal?.aborted) {
            throw new BridgeError("forbidden");
          }
          intent = await bridge("authorize", {
            call_id: "model-" + randomUUID().replaceAll("-", ""), kind: "model", operation: "inference",
            provider: profile.provider, model: profile.model, price_status: "unquoted", status: "prepared",
            request_sha256: createHash("sha256").update(JSON.stringify(context)).digest("hex"),
          });
          const headers = Object.fromEntries(Object.entries(profile.header_env).map(([name, reference]) => [name, process.env[String(reference)]]));
          const underlying = delegate.streamSimple({ ...model, api: profile.api, baseUrl: profile.base_url }, context, {
            ...options, apiKey: process.env[profile.credential_env], headers,
            maxTokens: profile.max_output_tokens, reasoning: profile.thinking_level, samplingParams: profile.sampling_params,
            maxRetries: 0, timeoutMs: profile.request_timeout_seconds * 1000, transport: "sse", cacheRetention: "none",
            onPayload: undefined, onResponse: undefined, fetch: undefined,
          });
          for await (const event of underlying) {
            if (event.type === "done" || event.type === "error") terminal = event;
            else output.push(event);
          }
          if (!terminal) throw new BridgeError("outcome_unknown");
          const message = terminal.message || terminal.error;
          const { task_id, run_id, fence, ...record } = intent;
          const known = terminal.type === "done" || message.usage.totalTokens > 0;
          await bridge("settle", { ...record, status: known ? "settled" : "outcome_unknown",
                                   usage: { input: message.usage.input, output: message.usage.output,
                                            cacheRead: message.usage.cacheRead, cacheWrite: message.usage.cacheWrite } });
          output.push(terminal);
          output.end();
        } catch (error) {
          if (intent && !terminal) {
            const { task_id, run_id, fence, ...record } = intent;
            try { await bridge("settle", { ...record, status: "outcome_unknown", usage: {} }); } catch { /* Persisted reservation stays held. */ }
          }
          const code = error instanceof BridgeError ? error.code : "bridge_unavailable";
          const message = emptyMessage(model);
          if (code === "approval_conflict") {
            message.content = [{ type: "text", text: "[studio-paused] Awaiting the browser's current approval." }];
            output.push({ type: "done", reason: "stop", message });
          } else {
            message.stopReason = "error";
            message.errorMessage = `studio-policy:${code}`;
            output.push({ type: "error", reason: "error", error: message });
          }
          output.end();
        }
      })();
      return output;
    },
  });

  pi.registerTool({
    name: "openmontage", label: "OpenMontage", description: "Run controlled production actions. Read AGENT_GUIDE.md, the selected manifest and director/provider skills. Use schema-valid artifacts and checkpoints; browser approval is supplied by the backend only.",
    parameters: Type.Object({
      action: Type.Union(["catalog", "read", "read_project", "initialize", "artifact", "checkpoint", "execute", "resume"].map(value => Type.Literal(value))),
      input: Type.Record(Type.String(), Type.Unknown()),
    }),
    async execute(toolCallId, params, signal) {
      try {
        const input = params.action === "execute"
          ? { ...params.input, call_id: "tool-" + createHash("sha256").update(toolCallId).digest("hex") }
          : params.input;
        const result = await bridge(params.action, input, signal);
        return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
      } catch (error) {
        const code = error instanceof BridgeError ? error.code : "bridge_unavailable";
        return { content: [{ type: "text", text: `studio-policy:${code}` }], details: { error: code }, isError: true };
      }
    },
  });
}
