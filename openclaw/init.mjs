// Prepares the isolated OpenClaw state for this project (runs as root in the OpenClaw image,
// before the Gateway starts). Never touches a personal ~/.openclaw: everything lives in the
// Compose volumes openclaw_state / openclaw_workspace / openclaw_traces.
import { chmodSync, chownSync, copyFileSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const SRC = "/opt/digest";
const STATE = process.env.OPENCLAW_STATE_DIR ?? "/home/node/.openclaw";
const CONFIG = process.env.OPENCLAW_CONFIG_PATH ?? `${STATE}/openclaw.json`;
const WORKSPACE = "/workspace/editor";
const TRACES = "/traces/openclaw";
const UID = 1000, GID = 1000; // `node` user of the official image

const model = process.env.OPENCLAW_EDITOR_MODEL;
if (!model) throw new Error("OPENCLAW_EDITOR_MODEL is required (e.g. anthropic/claude-opus-5-5)");
const fakeUrl = process.env.OFFLINE_FAKE_LLM_URL; // offline integration check only

const config = JSON.parse(readFileSync(`${SRC}/openclaw.template.json`, "utf8")
  .replace("__MODEL__", model)
  .replace("__THINKING__", process.env.OPENCLAW_THINKING ?? "low"));

if (fakeUrl) {
  if (!model.startsWith("fake/")) throw new Error("OFFLINE_FAKE_LLM_URL requires OPENCLAW_EDITOR_MODEL=fake/...");
  config.models = {
    mode: "merge",
    providers: {
      fake: {
        baseUrl: fakeUrl, apiKey: "offline-fake-provider", api: "openai-completions", timeoutSeconds: 60,
        models: [{ id: model.slice(5), name: "Scripted coordinator (offline)", reasoning: false, input: ["text"],
                   cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 200000, maxTokens: 4096 }],
      },
    },
  };
  delete config.agents.entries.editor.thinkingDefault;
}

for (const dir of [STATE, WORKSPACE, TRACES]) mkdirSync(dir, { recursive: true });
writeFileSync(CONFIG, `${JSON.stringify(config, null, 2)}\n`);
chmodSync(CONFIG, 0o600);
for (const file of readdirSync(`${SRC}/workspace/editor`)) {
  copyFileSync(join(`${SRC}/workspace/editor`, file), join(WORKSPACE, file));
}
const chownTree = (path) => {
  chownSync(path, UID, GID);
  if (statSync(path).isDirectory()) for (const entry of readdirSync(path)) chownTree(join(path, entry));
};
for (const dir of [STATE, "/workspace", TRACES]) chownTree(dir);
console.log(`openclaw-init: agent=editor model=${model} workspace=${WORKSPACE} fake_provider=${Boolean(fakeUrl)}`);
