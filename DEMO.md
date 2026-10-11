# DEMO: step-by-step live scenario

The live scenario needs your Telegram bot and a ChatGPT sign-in for both agents (README §2–§5). Messages
in steps 1–12 are sent by the owner from their own Telegram account. Offline tests and the offline OpenClaw
integration do not prove a live run (README §6).

## 0. Preparation

```bash
docker compose up -d --build
docker compose ps          # openclaw, researcher, app, dashboard: (healthy); openclaw-init: Exited (0)
make auth-status           # editor and researcher: [{"type":"oauth", ...}]
make smoke-turn            # real Coordinator turn with a tool call
make model-check           # real Researcher model call returning a tool call
```

Expected:
- `[ OK ] OpenClaw Gateway: live; agent targets ['openclaw/editor', 'openclaw/researcher'] available`
- `[ OK ] Researcher Agent Card: AI News Researcher v0.1.0 JSONRPC A2A 1.0 @ http://researcher:9100`
- `[ OK ] Telegram getMe: @<your_bot>`
- `[ OK ] OpenClaw Coordinator turn: trace tr_…, runs […], app-executed tools ['get_user_settings:…', 'reply:…']`
- `[ OK ] OpenClaw researcher model call: run chatcmpl_…, session agent:researcher:research-model-check-…, tool_call ['submit_selection']`

Dashboard: http://127.0.0.1:8501.

## Scenario

The final demo settings are **at most 4 items and a 72-hour period**. Step 8 temporarily raises them to
10 items / 168 hours to get a digest that needs several Telegram messages, then restores 4 / 72.

| # | Owner action | Expected result | Where to check |
|---|---|---|---|
| 1 | `Remember: I'm interested in AI agents, MCP and AWS Bedrock. Write in English, at most 4 news items` | Short confirmation | Run `telegram_message`: `editor (openclaw)` `llm_input` (`native_tools_after_policy: []`, `workspace_instructions_loaded: True`) → app `tool_call update_user_settings` → `memory_write settings` → `tool_call reply` → `telegram_send` |
| 2 | `/digest` | ≤ 4 items: title, UTC date, source, summary, "💡 … (agent's inference)", link | One `trace_id` with `app (python)`, `editor (openclaw)`, `researcher (python)`, `researcher (openclaw)`: `openclaw_call` pinned to `request_research` → `a2a_send` (`TASK_STATE_COMPLETED`) → researcher `fetch_rss` ×3 → `submit_selection` → `link_check` → `telegram_send` → `memory_write delivered_urls` |
| 3 | Open this run in the dashboard | `agents (runtime)` lists all four roles; OpenClaw runs, sessions, A2A task and tokens are filled; two "OpenClaw evidence" lines (editor, researcher) | Runs table + Timeline |
| 4 | `docker compose restart openclaw researcher app`, then `docker compose up -d --force-recreate openclaw researcher app` | All healthy again; sign-in and memory intact | `make auth-status`, `make model-check`, `/settings` |
| 5 | `/new` | "🆕 New conversation: a new OpenClaw Coordinator session was created …" | Other events: `session /new` with a new `openclaw_session_key` |
| 6 | `What interests and settings have you saved?` (and `/settings`) | Limit **4**, period 72 h, interests AI agents / MCP / AWS Bedrock; `/settings` shows "source: SQLite" | `memory_read settings` (`source: sqlite`), `tool_call get_user_settings`; the first model call of the new session has `history_messages: 0` |
| 7 | `Build another digest and skip the news you already sent` | New links only, or an honest "no relevant news" | A2A input `exclude_urls: N`, Researcher `rss_fetch` stats `excluded_delivered` |
| 8 | `Remember: at most 10 news items, period 168 hours` → `/digest` → `/last` → `Show the last digest` → `Remember: at most 4 news items, period 72 hours` | The new digest arrives in several messages without truncation; both re-send forms deliver the **whole** saved digest with "↩️ Re-sending the saved digest …"; settings end at 4 / 72 | `delivery resend_last_digest resent`; digest and delivered-URL counts unchanged by re-sends. Compare re-sends by content, not by message size: the re-send header changes the first chunk's length |
| 9 | `docker compose stop researcher` → `/digest` | "❌ The Researcher is unavailable, so no digest was produced …"; no news | Run `failed`: `a2a_retry` ×2, `tool_call request_research failed`, OpenClaw closes the tool loop |
| 10 | `docker compose start researcher` → `/digest` | Works again | `a2a_send` `TASK_STATE_COMPLETED` |
| 11 | `docker compose stop openclaw` → any message | "❌ The Coordinator (OpenClaw) is unavailable; the request was not processed …"; no hidden fallback | Run `failed`, `openclaw_call failed`, no `editor (openclaw)` events |
| 12 | `docker compose start openclaw` (wait for healthy) → any message | Works again | |

Optional checks:
- **another account**: writing from a different Telegram account gets "⛔ Access denied"; event
  `auth reject_user denied`, no OpenClaw calls;
- **digest without research**: `Answer with a list of news without searching` — no news is delivered;
  either the agent calls `request_research`, or `reply` is rejected and the turn fails.

## Recorded live run (2026-10-11)

Executed against the live Compose project `ai-news-digest` with OpenClaw 2026.9.9, model
`openai/gpt-5.6-luna` for both agents and ChatGPT Plus sign-in (one OAuth profile per agent).
**The owner's messages were sent in Russian** (the Russian equivalents of the messages above); this
also exercised multilingual input. Fixed bot texts were Russian at that time and are English now; no
English-language live run has been recorded since the translation. Evidence types: **trace** = verified
in the project's traces/SQLite; **owner** = message seen by the owner in Telegram (pasted back during
the run).

| # | Status | Evidence |
|---|---|---|
| 1 | ✅ trace, owner | `tr_fce80253e6134114`: `update_user_settings` saved interests AI agents / MCP / AWS Bedrock, language `ru`, **max_items 4** (the owner chose 4 on purpose), `reply` delivered, tool loop closed |
| 2 | ✅ trace, owner | `tr_f8f5a78e10c7434f`: editor pinned to `request_research`; A2A task `65804cd7-37be-410f-872c-85d91d5e354e` `TASK_STATE_COMPLETED`; researcher session `research-52d7db8f1eb1465a90c8b97a4fb15d1d`: `fetch_rss` ×3 → `submit_selection` (6 articles); `submit_digest` with 4 items, "4 links verified against Researcher result", 1/1 message delivered, 4 URLs recorded. The 4 URLs were independently found in the live `aws_whats_new` feed |
| 3 | ✅ trace (programmatic) | Dashboard rendered without errors on the live traces (Streamlit AppTest inside the dashboard container): the row of `tr_f8f5a78e10c7434f` lists `app (python), editor (openclaw), researcher (openclaw), researcher (python)`, 5 OpenClaw runs, both sessions, the A2A task and tokens 25089/1574; evidence lines for both agents show route `openai-chatgpt-responses`. An owner visual check was not reported |
| 4 | ✅ trace | `docker compose restart` and `up -d --force-recreate` (new container ids, all 6 volumes kept); both OAuth profiles still present; SQLite still had limit 4, interests and 4 delivered URLs; `model-check` passed afterwards (run `chatcmpl_e54d2295-ce06-4316-94d5-d284c0bd7c33`); new OpenClaw events contain token counts and no `cost` field |
| 5 | ✅ trace, owner | `tr_3554903e903f4304` `session /new`: Coordinator session `digest-3b3cb6fba60f4bff` → `digest-53171bf289e543cb` |
| 6 | ✅ trace, owner | `/settings` (`tr_98c3dea366e642a4`): source SQLite, up to 4 items, 4 delivered links. Question to the agent `tr_c71bcbc36f0c426f`: first model call in the new session had `history_messages: 0`; `get_user_settings` from SQLite; answer listed max 4 items, 72 h |
| 7 | ✅ trace, owner | `tr_fea4fc60797b4319`: A2A task `0f161228-7e36-4baf-8b63-9f4077e5173e` with `exclude_urls: 4`; Researcher stats `aws_whats_new` `excluded_delivered: 4`; 4 new items; no overlap with digest 1 (8 delivered URLs in total) |
| 8 | ✅ trace, owner | Temporary settings 10 / 168 h (`tr_9716ae6bceb545e3`). Digest `tr_fe1927764bb14df1` (A2A task `c72dc40b-93ca-493a-a97e-dde99753f4d4`): 7 items delivered as **2 messages** (3572 + 1251 characters; 5 + 2 items), HTML of each part balanced. `/last` `tr_b5ce8b9007e64904` and "show the last digest" twice (`tr_8ad209b82b274a52`, `tr_609205271710492a`, the model chose `show_last_digest`): each re-sent 2/2 messages with the full digest; still 3 digests and 15 delivered URLs. Settings restored to **4 / 72 h** (`tr_a102e424664243f2`) |
| 9 | ✅ trace, owner | Researcher stopped. `tr_dfc90ddc017d4589`: Agent Card discovery failed twice (`a2a_retry`), `tool_call request_research failed`, run `failed`, honest error message, OpenClaw closed the tool loop; memory unchanged |
| 10 | ✅ trace, owner | Researcher started. `tr_13ab98ca2db84ae6`: A2A task `e7bcb51f-86d0-4085-a00d-6cebae6af3ed` `TASK_STATE_COMPLETED`; 1 new item (Hugging Face, marked "summary based on the title"); 16 delivered URLs in total |
| 11 | ⏭ **Skipped by owner; not live-verified** | The OpenClaw-down path is covered offline only: `tests/test_coordinator_flow.py::test_openclaw_unavailable_gives_error_without_fallback` and the integration check "OpenClaw down -> error, no hidden fallback" |
| 12 | ⏭ **Skipped by owner; not live-verified** | — |

Additional live checks:
- `tr_a21b8e6ffd06471f` — first real Coordinator turn through the subscription (`get_user_settings` → `reply`);
- `tr_22c8b1b8274b4ed2` + Researcher `model-check` run concurrently — both OAuth profiles work at the same time;
- after the documentation/cleanup changes: `smoke --agent-turn` `tr_01ce62803ce94361` and `model-check`
  run `chatcmpl_321cc2e1-13e8-4d96-9584-cda977c8fddc` passed; final stored settings: interests
  AI agents / MCP / AWS Bedrock, digest language `ru` (the owner's saved preference), at most 4 items,
  72 hours; 4 digests and 16 delivered URLs.

## Offline integration with the real OpenClaw (no credentials)

The Gateway and both agents are real; the model is a scripted fake provider. Separate project, separate
volumes, dashboard on port 8502; live volumes are not touched.

```bash
export OPENCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)
make openclaw-check        # up the offline project + scripts/openclaw_integration_check.py
make openclaw-check-down   # remove the offline project and its volumes
```

Expected `29/29 checks passed`. The report is written to `/data/traces/openclaw-integration-report.json`
in the offline project's volume.
