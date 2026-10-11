// Prepares the isolated OpenClaw state for this project (runs as root in the OpenClaw image,
// before the Gateway starts). Never touches a personal ~/.openclaw: everything lives in the
// Compose volumes openclaw_state / openclaw_workspace / openclaw_traces.
import { chmodSync, chownSync, copyFileSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const SRC = "/opt/digest";
const STATE = process.env.OPENCLAW_STATE_DIR ?? "/home/node/.openclaw";
const CONFIG = process.env.OPENCLAW_CONFIG_PATH ?? `${STATE}/openclaw.json`;
const AGENTS = ["editor", "researcher"]; // workspace: /workspace/<agent>
const TRACES = "/traces/openclaw";
const UID = 1000, GID = 1000; // `node` user of the official image

const fakeUrl = process.env.OFFLINE_FAKE_LLM_URL; // offline integration check only
// Model access: ChatGPT/Codex subscription sign-in (OAuth profiles in the state volume) on OpenClaw's own
// runtime. "fake" exists only for the offline integration check. There is no API-key provider and no fallback.
const provider = fakeUrl ? "fake" : "openai-codex";
const DEFAULT_CODEX_MODEL = "gpt-5.6-luna";
const defaultModel = provider === "openai-codex" ? `openai/${process.env.OPENAI_CODEX_MODEL || DEFAULT_CODEX_MODEL}` : undefined;
const models = {
  editor: process.env.OPENCLAW_EDITOR_MODEL || defaultModel,
  researcher: process.env.OPENCLAW_RESEARCHER_MODEL
    || (provider === "openai-codex" && process.env.OPENAI_CODEX_RESEARCHER_MODEL
      ? `openai/${process.env.OPENAI_CODEX_RESEARCHER_MODEL}` : defaultModel),
};
for (const [agent, model] of Object.entries(models)) {
  if (!model) throw new Error(`No model for ${agent}: offline mode needs OPENCLAW_EDITOR_MODEL / OPENCLAW_RESEARCHER_MODEL`);
  const prefix = { fake: "fake/", "openai-codex": "openai/" }[provider];
  if (!model.startsWith(prefix)) throw new Error(`${provider} mode needs a ${prefix}* model for ${agent}, got ${model}`);
}

const config = JSON.parse(readFileSync(`${SRC}/openclaw.template.json`, "utf8")
  .replaceAll("__MODEL__", models.editor)
  .replaceAll("__RESEARCHER_MODEL__", models.researcher)
  .replaceAll("__THINKING__", process.env.OPENCLAW_THINKING ?? "low"));

if (provider === "openai-codex") {
  // Codex login, but OpenClaw's own agent runtime (not the native Codex app-server harness): keeps the
  // client-tool loop, tool policy and telemetry hooks. The codex harness plugin stays disabled.
  config.agents.defaults.models = Object.fromEntries(
    [...new Set(Object.values(models))].map((m) => [m, { agentRuntime: { id: "openclaw" }, codeMode: false }]));
  config.plugins.entries.codex = { enabled: false };
}

if (provider === "fake") {
  config.models = {
    mode: "merge",
    providers: {
      fake: {
        baseUrl: fakeUrl, apiKey: "offline-fake-provider", api: "openai-completions", timeoutSeconds: 60,
        models: [...new Set(Object.values(models))].map((m) => ({
          id: m.slice(5), name: `Scripted ${m.slice(5)} (offline)`, reasoning: false, input: ["text"],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 200000, maxTokens: 4096 })),
      },
    },
  };
  for (const agent of AGENTS) delete config.agents.entries[agent].thinkingDefault;
}

// Only the configured models may run (no silent switch to another, possibly metered, model).
config.agents.defaults.modelPolicy = { allow: [...new Set(Object.values(models))] };

// The Gateway adds its own keys to the config (meta, wizard, migrations, control UI origins, provider plugin
// enablement). Dropping them makes OpenClaw treat the file as clobbered and restore its last-good copy, so the
// project's keys are merged over the existing file. Sections owned by this project are replaced as a whole.
const OWNED = ["agents.entries", "agents.defaults.models", "agents.defaults.modelPolicy", "tools", "models",
  "plugins.load", "plugins.entries.digest-telemetry", "plugins.entries.codex", "gateway.auth", "gateway.http"];
const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
const merge = (base, over, path = "") => {
  const out = { ...base };
  for (const [k, v] of Object.entries(over)) {
    const p = path ? `${path}.${k}` : k;
    out[k] = isObj(v) && isObj(base?.[k]) && !OWNED.includes(p) ? merge(base[k], v, p) : v;
  }
  return out;
};
const existing = existsSync(CONFIG) ? JSON.parse(readFileSync(CONFIG, "utf8")) : {};
const merged = merge(existing, config);
if (!config.models) delete merged.models;            // provider overrides only when this mode defines them
if (!config.plugins.entries.codex) delete merged.plugins.entries.codex;

for (const dir of [STATE, TRACES, ...AGENTS.map((a) => `/workspace/${a}`)]) mkdirSync(dir, { recursive: true });
// Re-runs on every `docker compose up`, possibly next to a running Gateway: rewrite only on change.
const rendered = `${JSON.stringify(merged, null, 2)}\n`;
if (!existsSync(CONFIG) || readFileSync(CONFIG, "utf8") !== rendered) writeFileSync(CONFIG, rendered);
chmodSync(CONFIG, 0o600);
for (const agent of AGENTS) {
  for (const file of readdirSync(`${SRC}/workspace/${agent}`)) {
    copyFileSync(join(`${SRC}/workspace/${agent}`, file), join(`/workspace/${agent}`, file));
  }
}
// lstat: never follow symlinks. The Gateway links plugin-skills/* into /app/dist of the image;
// following them chowns image files and blocks on an overlayfs copy-up.
const chownTree = (path) => {
  const st = lstatSync(path);
  if (st.isSymbolicLink()) return;
  if (st.uid !== UID || st.gid !== GID) chownSync(path, UID, GID);
  if (st.isDirectory()) for (const entry of readdirSync(path)) chownTree(join(path, entry));
};
for (const dir of [STATE, "/workspace", TRACES]) chownTree(dir);
console.log(`openclaw-init: provider=${provider} runtime=openclaw `
  + AGENTS.map((a) => `${a}=${models[a]}`).join(" "));
