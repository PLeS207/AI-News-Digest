# HOMEWORK CHECKLIST

Статусы:
- 🧪 **fake** — pytest с fake LLM / **fake Gateway** / fake Telegram Bot API; A2A при этом настоящий, по HTTP. Интеграцию с OpenClaw не доказывает;
- 🦞 **openclaw** — `scripts/openclaw_integration_check.py` против **настоящего OpenClaw 2026.9.9**, модель — контролируемый fake-провайдер (`compose.offline.yaml`). Реальную модель и Telegram не доказывает;
- 🐳 **env** — ручная проверка в Docker без пользовательских credentials;
- ⏳ **live** — требует ваших ключей, шаги в DEMO.md. В этом окружении **не выполнялось**.

Дата проверки: 2026-10-11.
- pytest: **44 passed** локально (Python 3.13) и **44 passed** в Docker-образе.
- Интеграция с настоящим OpenClaw: **25/25 checks passed** (exit 0).

| # | Критерий | Реализация | Проверка | Статус |
|---|---|---|---|---|
| 1 | Coordinator реально выполняется в OpenClaw: инструкции агента, модель, выбор tools | `openclaw/openclaw.template.json` (агент `editor`), `openclaw/workspace/editor/*`, `coordinator/openclaw_client.py`, `coordinator/service.py` | openclaw-check: «editor workspace AGENTS.md + SOUL.md in the system prompt», «model calls were made by OpenClaw» (24 запроса к провайдеру), hooks `llm_input/llm_call/run` | 🦞 ✅; ⏳ live (`smoke --agent-turn`, DEMO 1–3) |
| 1a | Python не запускает скрытый LLM Editor | старый `editor/agent.py` удалён; `app` не получает `ANTHROPIC_API_KEY` | `tests/test_coordinator_flow.py::test_openclaw_unavailable_gives_error_without_fallback` (нет `llm_call` в app); openclaw-check «OpenClaw down → error, no hidden fallback» | 🧪 ✅ 🦞 ✅ |
| 2 | Разные роли и права; ограничение нативных tools OpenClaw | `tools.profile=minimal`, `toolSearch=false`, `deny` всех групп; plugin `before_tool_call`; `coordinator/tools.py` (allowlist + схемы + состояние); Researcher `researcher/agent.py` | openclaw-check: «native OpenClaw tools after policy = []», «tools offered to the model are only the Coordinator allowlist», «runtime refused web_fetch»; `tests/test_permissions.py`, `test_forbidden_tool_is_denied_by_app` | 🦞 ✅ 🧪 ✅ |
| 2a | Привязка tools к контексту запроса (trace, исследование) | `TurnState`, `ToolContext`; `extra="forbid"` | `test_unknown_arguments_and_foreign_trace_are_rejected` | 🧪 ✅ |
| 3 | Digest только через `request_research` + валидированный `submit_digest`; свободный текст не считается результатом | `route_intent`, `tool_choice` (pinned/required), `_reply` (запрет в digest-потоке и ссылок), `NoStructuredAction` | `test_text_only_answer_is_not_a_digest`, `test_reply_cannot_replace_a_digest`, `test_hallucinated_link_is_rejected`, `test_empty_research_result_is_reported_honestly`; openclaw-check «text-only answer to a digest request is not a digest», «invented link rejected», «/digest pins request_research» | 🧪 ✅ 🦞 ✅ |
| 4 | Настоящий A2A (official SDK) из tool Coordinator | `coordinator/researcher_client.py`, `researcher/server.py` (a2a-sdk 1.2.2, A2A 1.0, JSON-RPC) | `tests/test_a2a_roundtrip.py` (discovery, task_id, COMPLETED/FAILED/REJECTED, retry); openclaw-check «Coordinator's request_research produced a real A2A task» | 🧪 ✅ 🦞 ✅ |
| 4a | Researcher недоступен → честная ошибка, нет новостей и доставки | `ResearcherUnavailable` → `_researcher_error_text`, закрытие tool loop | `test_researcher_unavailable_gives_error_and_no_digest`; openclaw-check «researcher down → honest error» | 🧪 ✅ 🦞 ✅; ⏳ live DEMO 9 |
| 4b | OpenClaw недоступен → понятная ошибка и failed trace | `OpenClawUnavailable` | `test_openclaw_unavailable_gives_error_without_fallback`, `test_wrong_gateway_token_is_an_error`; openclaw-check; в Docker без ключа Gateway вернул `401 No API key found for provider "anthropic"` → ошибка пользователю | 🧪 ✅ 🦞 ✅ 🐳 ✅; ⏳ live DEMO 11 |
| 5 | Persistent memory после restart и `/new`; `/new` = новая сессия OpenClaw | `coordinator/memory.py` (+`conversations`), `x-openclaw-session-key=digest-<id>` | `test_full_scenario_restart_new_and_skip_delivered`; openclaw-check «after restart + /new the agent reads limit 2 from SQLite», «/new rotated the OpenClaw session», «new session carries no earlier conversation» | 🧪 ✅ 🦞 ✅; ⏳ live DEMO 4–6 |
| 5a | Совместимость с существующими данными | тот же volume `editor_memory` и путь; `CREATE TABLE IF NOT EXISTS` | `tests/test_memory.py::test_pre_migration_database_is_opened_without_data_loss` | 🧪 ✅ |
| 5b | Tool follow-up не дублирует transcript | follow-up = только assistant tool-call + `role:"tool"`; закрытие `tool_choice:"none"` | openclaw-check «tool follow-ups do not duplicate a user request»; `test_full_scenario…` (follow-ups содержат только assistant/tool) | 🦞 ✅ 🧪 ✅ |
| 6 | `/last` и «Покажи последний дайджест» — общий путь, полный дайджест, без `[:4096]`, корректный HTML, счётчики не меняются | `coordinator/delivery.py::resend_last_digest`, `coordinator/digest.py` | `tests/test_digest_render.py` (длинная статья, длинная ссылка, `& < >`, баланс тегов, оба пути, частичная доставка); `test_ptb_application_wiring_with_fake_bot_api` (`/last`); openclaw-check «natural 'show last digest' uses the common full re-send path» | 🧪 ✅ 🦞 ✅; ⏳ live DEMO 8 |
| 6a | Частичная доставка не отмечает неотправленное | `deliver_digest` | `test_partial_delivery_marks_only_sent_chunks` | 🧪 ✅ |
| 7 | Telegram: allowlist до runtime, `/digest`, `/new`, `/settings`, `/last` | `telegram_bot.py` | `tests/test_telegram.py` (Gateway не вызывается для постороннего; PTB-хендлеры на фейковом Bot API) | 🧪 ✅; ⏳ live |
| 8 | Dashboard: роли и runtime, OpenClaw run/session id, вызовы модели и tools, A2A task_id, память, доставка, ошибки, общий trace | `dashboard/app.py`, `tracing.join_openclaw_events`, plugin `digest-telemetry` | openclaw-check «OpenClaw events joined to app traces» (146/146), «one trace holds app, OpenClaw editor and Researcher»; `tests/test_dashboard.py` (join на тестовых данных + AppTest) | 🦞 ✅ 🧪 ✅; ⏳ live DEMO 3 |
| 9 | Изоляция state OpenClaw, секреты по сервисам, Gateway только внутри сети | `compose.yaml` (volumes `openclaw_*`, `expose` без `ports`, `environment` по сервисам), `openclaw/init.mjs` | 🐳: `docker compose config` OK; live-конфиг с `anthropic/claude-opus-5-5` принят Gateway; ключ берётся из env (невалидный ключ → `invalid x-api-key` от Anthropic); `~/.openclaw` не затрагивался | 🐳 ✅ |
| 9a | `sources.toml` обновляется без пересборки | `./config:/app/config:ro` в `app` и `researcher` | README §RSS | ✅ (конфигурация) |
| 10 | Документация, workshop mapping, Hermes | `README.md`, `DEMO.md`, этот файл; старое ТЗ помечено как частично заменённое | — | ✅ |

## Не проверено в этом окружении (нет credentials)
1. Реальная модель через OpenClaw (`smoke --agent-turn`) и в Researcher (`model-check`).
2. Telegram: получение сообщений и доставка (DEMO 1–12).
3. Live-сценарий: лимит 2 → дайджест → dashboard → restart → `/new` → настройки → новые ссылки → длинный дайджест и повтор → остановка и восстановление Researcher и OpenClaw.

После live-прогона заполните колонку «Факт» в DEMO.md и поменяйте ⏳ на ✅ здесь.
