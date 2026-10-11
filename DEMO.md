# DEMO: пошаговый сценарий

Live-сценарий требует ваших credentials в `.env` (см. README §3–4). Фактические результаты прогона записывайте в колонку «Факт» или в `live-results/` (папка игнорируется Git). Скриншоты и логи из offline-режима не подтверждают live-работу.

## 0. Подготовка

```bash
cp .env.example .env && chmod 600 .env   # заполнить приватно
docker compose up -d --build
docker compose ps                        # researcher, app, dashboard: (healthy)
docker compose exec app python -m ai_news_digest.cli smoke
```

Ожидается от `smoke`:
- `[ OK ] Anthropic model available: <ANTHROPIC_MODEL> …`
- `[ OK ] Anthropic messages.create: stop_reason=end_turn …`
- `[ OK ] Telegram getMe: @<ваш_бот>`
- `[ OK ] Researcher Agent Card: AI News Researcher 0.1.0 JSONRPC A2A 1.0 @ http://researcher:9100`

Откройте dashboard: http://127.0.0.1:8501 (включите «Auto-refresh» в боковой панели).

## Live-сценарий

| # | Действие | Ожидаемый результат | Где проверить | Факт |
|---|---|---|---|---|
| 1 | В Telegram: `Запомни: меня интересуют AI agents, MCP и AWS Bedrock. Пиши по-русски, максимум 2 новости` | Короткое подтверждение | Dashboard → run `telegram_message`: `llm_call`, `tool_call update_user_settings`, `memory_write settings`, `telegram_send reply` | |
| 2 | `Подготовь дайджест AI-новостей за последние 72 часа` | ≤ 2 новости: заголовок, дата UTC, источник, саммари, «💡 Возможное применение … (вывод агента)», ссылка | Timeline: editor `tool_call request_research` → `a2a_discovery` → `a2a_send` (task_id, `TASK_STATE_COMPLETED`) → researcher `run research`, `llm_call`, `tool_call fetch_rss` ×3, `rss_fetch`, `submit_selection` → editor `a2a_result`, `submit_digest`, `link_check`, `telegram_send digest_chunk`, `memory_write delivered_urls`, `delivery … delivered` | |
| 3 | В dashboard выберите этот запуск | Видны обе роли (`editor`, `researcher`), RSS tool, A2A task id, успешная доставка и токены usage | Колонки `agents`, `a2a_task`, `tokens in/out` | |
| 4 | `docker compose restart app researcher` (volumes не удаляются) | Сервисы снова `healthy` | `docker compose ps` | |
| 5 | В Telegram: `/new` | «Новая беседа: краткосрочный контекст очищен… Настройки … сохранены в SQLite» | Вкладка «Other events»: `session /new` | |
| 6 | `Какие интересы и настройки ты сохранил?` (дополнительно `/settings`) | Лимит **2**, интересы AI agents / MCP / AWS Bedrock. В `/settings` строка «источник: SQLite» | Run: `memory_read settings` с `"source": "sqlite"` | |
| 7 | `Собери ещё один дайджест, пропусти уже отправленные новости` | Новые ссылки, без ссылок из шага 2. Если свежих нет — сообщение, что релевантных новостей не найдено | `a2a_send` input: `exclude_urls: N` > 0. Researcher `rss_fetch` stats: `excluded_delivered` | |
| 8 | Контролируемая ошибка: `docker compose stop researcher`, затем `Подготовь дайджест` | «❌ Researcher недоступен, дайджест не сформирован…». Выдуманных новостей нет | Run со статусом `failed`: `a2a_retry` ×2 (`transient: true`), `tool_call request_research`, ошибка в блоке Errors | |
| 9 | `docker compose start researcher` | Следующий дайджест снова работает | | |

Дополнительно можно проверить:
- отказ постороннему: напишите боту с другого аккаунта. Ответ «⛔ Доступ запрещён», в dashboard событие `auth reject_user` со статусом `denied`, модель не вызывается;
- `Покажи последний дайджест` / `/last` — повтор сохранённого текста из SQLite;
- `docker compose exec dashboard python -m ai_news_digest.cli runs` — текстовая сводка запусков.

## Offline-демо (без credentials)

Здесь используется fake LLM и RSS fixtures. A2A настоящий, по HTTP на localhost. Это демонстрирует трассы и dashboard, но **не** работу модели и Telegram.

```bash
make venv
make offline-demo     # печатает диалог и пишет трассы в data/offline-demo/traces
TRACE_FILES=data/offline-demo/traces/editor/events.jsonl,data/offline-demo/traces/researcher/events.jsonl \
  .venv/bin/streamlit run dashboard/app.py
```

Ожидается 5 запусков:
- «Запомни» и «настройки» — `completed`;
- два дайджеста — `completed_with_errors`, потому что в demo добавлен заведомо сломанный источник `broken_source` (HTTP 503), а дайджест помечен «⚠️ Результат неполный»;
- запуск при остановленном Researcher — `failed`.
