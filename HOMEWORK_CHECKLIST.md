# HOMEWORK CHECKLIST

Статусы:
- ✅ **offline** — проверено автоматическими тестами или командами в этом окружении (fake LLM / fake Telegram, но настоящий A2A по HTTP и настоящий Docker);
- ✅ **env** — проверено вручную в этом окружении без credentials (Docker, сеть, живые RSS);
- ⏳ **live** — требует ваших `ANTHROPIC_API_KEY` и Telegram-токена; шаги описаны в DEMO.md, в этом окружении **не выполнялось**.

Дата проверки: 2026-10-11. Результат `pytest`: **35 passed** локально (Python 3.13 venv) и **35 passed** внутри Docker-образа.

| # | Критерий задания | Реализация | Как проверить | Статус |
|---|---|---|---|---|
| 1 | ≥ 2 LLM-агента с разными ролями и инструкциями | `editor/agent.py` (`SYSTEM_PROMPT`, `ROLE="editor"`), `researcher/agent.py` (`SYSTEM_PROMPT`, `ROLE="researcher"`) | `pytest tests/test_editor_flow.py tests/test_a2a_roundtrip.py` | ✅ offline (fake LLM); ⏳ live: DEMO шаг 2–3 |
| 1a | Фактически разные права на tools, проверка при dispatch | `agents/runtime.py` (`ToolRegistry.dispatch`, `ToolPermissionError`), registries в обоих агентах | `pytest tests/test_permissions.py tests/test_editor_flow.py::test_editor_cannot_call_rss_tool` | ✅ offline |
| 1b | Подключена модель (Anthropic SDK), ≥ 1 tool | `agents/llm.py` (`AnthropicBackend`), tools `fetch_rss`, `request_research` и др. | `docker compose exec app python -m ai_news_digest.cli smoke` | ⏳ live (нужен ключ). Вызовы SDK сверены с установленным `anthropic==1.13.0` |
| 2 | Persistent memory после перезапуска и новой беседы | `editor/memory.py` (SQLite в volume `editor_memory`), `/new` → `ConversationStore.clear` | `pytest tests/test_memory.py tests/test_editor_flow.py::test_full_scenario_memory_digest_restart_and_skip_delivered`; live: DEMO шаги 1, 4–6 | ✅ offline; ⏳ live |
| 2a | Доставленные URL помечаются только после успешной отправки, с учётом частичной доставки | `EditorService.deliver` | `pytest tests/test_editor_flow.py::test_partial_delivery_marks_only_sent_chunks` | ✅ offline |
| 2b | Отдельный явный reset памяти | `cli.py reset-memory --yes` | `pytest tests/test_memory.py::test_reset_memory_cli` | ✅ offline |
| 3 | Задача и результат через Telegram | `telegram_bot.py` (python-telegram-bot 22.8, long polling) | DEMO шаги 1–7 | ⏳ live. Offline: `tests/test_telegram.py::test_ptb_application_wiring_with_fake_bot_api` (настоящий PTB `Application` и хендлеры, фейковый Bot API) |
| 3a | Allowlist проверяется до модели; без allowlist — понятная ошибка | `Gatekeeper`, `config.allowed_telegram_user_id` | `pytest tests/test_telegram.py`; в Docker: `app` без `ALLOWED_TELEGRAM_USER_ID` → `Configuration error: ALLOWED_TELEGRAM_USER_ID is not set…` | ✅ offline + ✅ env |
| 4 | A2A через официальный SDK | `researcher/server.py` (`DefaultRequestHandler`, `create_agent_card_routes`, `create_jsonrpc_routes`, `TaskUpdater`), `editor/researcher_client.py` (`A2ACardResolver`, `create_client`, `SendMessageRequest`) — a2a-sdk 1.2.2, A2A 1.0, JSON-RPC | `pytest tests/test_a2a_roundtrip.py` (uvicorn + SDK-клиент, discovery, task id, `TASK_STATE_COMPLETED`/`FAILED`/`REJECTED`, валидация `ResearchResult`) | ✅ offline |
| 4a | Agent Card discovery в сети Compose | `compose.yaml` (`RESEARCHER_PUBLIC_URL=http://researcher:9100`) | Выполнено: из контейнера `app` получен card `AI News Researcher 0.1.0 JSONRPC 1.0 http://researcher:9100 ['ai_news_research']`; `researcher` в статусе healthy | ✅ env |
| 4b | Timeout, ограниченный retry, без выдуманных новостей при недоступности | `ResearcherClient` (2 попытки только для сетевых ошибок), `_researcher_error_text` | `pytest tests/test_a2a_roundtrip.py::test_researcher_unavailable_is_retried_then_reported tests/test_editor_flow.py::test_researcher_unavailable_gives_error_and_no_digest`; live: DEMO шаг 8 | ✅ offline; ⏳ live |
| 4c | Ссылки дайджеста только из материалов Researcher; дайджест только после A2A | `_submit_digest` (проверка URL, gate «request_research first») | `pytest tests/test_editor_flow.py::test_hallucinated_link_is_rejected ::test_digest_requires_research` | ✅ offline |
| 5 | Dashboard: runs, timeline, LLM/tool calls, A2A, статусы, ошибки, memory, отправка | `tracing.py`, `dashboard/app.py` | `pytest tests/test_dashboard.py` (Streamlit AppTest на трассах из offline-demo); в Docker: health 200, трассы из volume читаются, запись запрещена (read-only) | ✅ offline + ✅ env; ⏳ live: DEMO шаг 3 |
| 5a | Секреты не попадают в трассы | `tracing.redact` | `pytest tests/test_permissions.py::test_trace_redacts_secrets` | ✅ offline |
| 6 | Воспроизведение по README со своими credentials | `README.md`, `.env.example`, `compose.yaml`, `Dockerfile`, `Makefile` | `docker compose config` ✅; образ собирается ✅; `researcher` и `dashboard` поднимаются healthy с dummy-ключом ✅; полный live-прогон ⏳ | ✅ env; ⏳ live |
| R | RSS: парсинг, окно времени UTC, записи без дат, canonical URL, дубли, лимиты, ошибки источника, фильтр AWS | `researcher/rss.py`, `config/sources.toml`, fixtures `tests/fixtures/*` | `pytest tests/test_rss.py`; живые ленты: вызов `fetch_feed_bytes/parse_feed/filter_entries` на 3 реальных URL (openai 1287→5, hf 876→1, aws 100→8 после фильтра) | ✅ offline + ✅ env |
| A | Attribution к workshop | README, раздел-врезка в начале | — | ✅ |

## Что сделать для закрытия ⏳ live

1. Заполнить `.env` (README §3–4).
2. Выполнить `docker compose up -d --build` и `docker compose exec app python -m ai_news_digest.cli smoke`.
3. Пройти DEMO.md шаги 1–9 и заполнить колонку «Факт».
4. Обновить статусы в этой таблице.
