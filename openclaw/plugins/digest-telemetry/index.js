// OpenClaw plugin: records real runtime hook events for the AI News Digest dashboard and
// enforces the Coordinator tool allowlist inside the Gateway (defense in depth; the app
// re-checks every client tool call as well). Never records prompts, keys or user ids.
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const clip = (value, limit = 400) => {
  if (value === undefined || value === null) return null;
  let text = typeof value === "string" ? value : JSON.stringify(value);
  text = text
    .replace(/sk-ant-[A-Za-z0-9_-]{10,}/g, "[REDACTED]")
    .replace(/\b\d{6,12}:[A-Za-z0-9_-]{30,}\b/g, "[REDACTED]")
    .replace(/(authorization|x-api-key)\s*[:=]\s*(bearer\s+)?[^\s"',}]+/gi, "[REDACTED]");
  return text.length > limit ? `${text.slice(0, limit)}… [+${text.length - limit} chars]` : text;
};

const toolNames = (tools) =>
  (Array.isArray(tools) ? tools : [])
    .map((t) => t?.name ?? t?.function?.name ?? t?.toolName)
    .filter((n) => typeof n === "string")
    .sort();

export default definePluginEntry({
  id: "digest-telemetry",
  name: "AI News Digest telemetry",
  description: "Hook events -> JSONL traces; Coordinator tool allowlist.",
  register(api) {
    const cfg = api.pluginConfig ?? {};
    const traceFile = cfg.traceFile;
    const allowed = new Set(cfg.allowedTools ?? []);
    const marker = cfg.workspaceMarker;
    mkdirSync(dirname(traceFile), { recursive: true });

    const write = (kind, name, status, ctx, extra = {}) => {
      const { error, durationMs, output, input, ...data } = extra;
      const record = {
        ts: new Date().toISOString(),
        trace_id: null, // joined to the app trace via data.openclaw_run_id
        span_id: randomUUID().replace(/-/g, "").slice(0, 16),
        service: "openclaw",
        agent: ctx?.agentId ?? "editor",
        runtime: "openclaw",
        kind,
        name,
        status,
        duration_ms: typeof durationMs === "number" ? Math.round(durationMs * 10) / 10 : null,
        input: clip(input),
        output: clip(output),
        error: clip(error, 1000),
        data: {
          openclaw_run_id: ctx?.runId ?? data.runId ?? null,
          openclaw_session_key: ctx?.sessionKey ?? data.sessionKey ?? null,
          openclaw_session_id: ctx?.sessionId ?? data.sessionId ?? null,
          ...data,
        },
      };
      delete record.data.runId;
      delete record.data.sessionKey;
      delete record.data.sessionId;
      try {
        appendFileSync(traceFile, `${JSON.stringify(record)}\n`);
      } catch (err) {
        api.logger?.warn?.(`digest-telemetry: cannot write trace: ${String(err)}`);
      }
    };

    api.on("model_call_started", (event, ctx) => {
      write("llm_call", `openclaw.model_call ${event.callId}`, "started", { ...ctx, runId: event.runId, sessionKey: event.sessionKey },
        { call_id: event.callId, provider: event.provider, model: event.model, api: event.api });
    });
    api.on("model_call_ended", (event, ctx) => {
      write("llm_call", `openclaw.model_call ${event.callId}`, event.outcome === "completed" ? "completed" : "failed",
        { ...ctx, runId: event.runId, sessionKey: event.sessionKey },
        { call_id: event.callId, provider: event.provider, model: event.model, durationMs: event.durationMs,
          error: event.outcome === "error" ? (event.errorCategory ?? event.failureKind ?? "error") : undefined,
          time_to_first_byte_ms: event.timeToFirstByteMs });
    });
    api.on("llm_input", (event, ctx) => {
      const system = event.systemPrompt ?? "";
      write("llm_input", "openclaw.llm_input", "completed", { ...ctx, runId: event.runId, sessionId: event.sessionId }, {
        provider: event.provider, model: event.model,
        native_tools_after_policy: toolNames(event.tools),
        system_prompt_sha256: createHash("sha256").update(system).digest("hex").slice(0, 16),
        system_prompt_chars: system.length,
        workspace_instructions_loaded: marker ? system.includes(marker) : null,
        history_messages: Array.isArray(event.historyMessages) ? event.historyMessages.length : null,
      });
    });
    api.on("llm_output", (event, ctx) => {
      write("llm_output", "openclaw.llm_output", "completed", { ...ctx, runId: event.runId, sessionId: event.sessionId }, {
        provider: event.provider, model: event.model, resolved_ref: event.resolvedRef,
        usage: event.usage ?? null, output: (event.assistantTexts ?? []).join("\n"),
      });
    });
    api.on("before_tool_call", (event, ctx) => {
      const name = event.toolName ?? ctx?.toolName;
      if (!allowed.has(name)) {
        write("tool_call", name, "denied", { ...ctx, runId: event.runId ?? ctx?.runId },
          { input: event.params, error: `tool '${name}' is not in the Coordinator allowlist`, tool_call_id: event.toolCallId });
        return { block: true, blockReason: `Tool '${name}' is not allowed for the editor agent.` };
      }
      write("tool_call", name, "started", { ...ctx, runId: event.runId ?? ctx?.runId },
        { input: event.params, tool_call_id: event.toolCallId, executed_by: "openclaw" });
      return undefined;
    });
    api.on("after_tool_call", (event, ctx) => {
      write("tool_call", event.toolName, event.error ? "failed" : "completed", { ...ctx, runId: event.runId ?? ctx?.runId },
        { output: event.result, error: event.error, durationMs: event.durationMs, tool_call_id: event.toolCallId });
    });
    api.on("agent_end", (event, ctx) => {
      write("run", "openclaw.agent_run", event.success ? "completed" : "failed", { ...ctx, runId: event.runId ?? ctx?.runId },
        { durationMs: event.durationMs, error: event.error, messages: Array.isArray(event.messages) ? event.messages.length : null });
    });
  },
});
