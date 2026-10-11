# DEMO: пошаговый сценарий

Live-сценарий требует ваших credentials в `.env` (README §3–4). Фактические результаты записывайте в колонку «Факт» или в `live-results/` (папка игнорируется Git). Результаты fake-тестов и offline-интеграции не подтверждают live-работу.

## 0. Подготовка

```bash
cp .env.example .env && chmod 600 .env   # ANTHROPIC_API_KEY, OPENCLAW_GATEWAY_TOKEN, TELEGRAM_*, приватно
docker compose up -d --build
docker compose ps                         # openclaw, researcher, app, dashboard: (healthy); openclaw-init: exited (0)
docker compose exec app python -m ai_news_digest.cli smoke --agent-turn
docker compose exec researcher python -m ai_news_digest.cli model-check
```

Ожидается:
- `[ OK ] OpenClaw Gateway: live; agent target openclaw/editor available`
- `[ OK ] Researcher Agent Card: AI News Researcher 0.1.0 JSONRPC A2A 1.0 @ http://researcher:9100`
- `[ OK ] Telegram getMe: @<ваш_бот>`
- `[ OK ] OpenClaw Coordinator turn: trace tr_…, runs ['chatcmpl_…', …]: '…'` — реальная модель через OpenClaw
- `[ OK ] Anthropic model available: <ANTHROPIC_MODEL>` — модель Researcher

Dashboard: http://127.0.0.1:8501 (в боковой панели включите «Auto-refresh»).

## Live-сценарий

| # | Действие | Ожидаемый результат | Где проверить | Факт |
|---|---|---|---|---|
| 1 | `Запомни: меня интересуют AI agents, MCP и AWS Bedrock. Пиши по-русски, максимум 2 новости` | Короткое подтверждение | Run `telegram_message`: `openclaw_call` → hooks `editor (openclaw)`: `llm_input` (`native_tools_after_policy: []`, `workspace_instructions_loaded: True`), `llm_call`, `llm_output` (usage) → app `tool_call update_user_settings` → `memory_write settings` → `tool_call reply` → `telegram_send` | |
| 2 | `/digest` | ≤ 2 новости: заголовок, дата UTC, источник, саммари, «💡 … (вывод агента)», ссылка | Один `trace_id`: `openclaw_call` (`tool_choice` = request_research) → app `tool_call request_research` → `a2a_discovery` → `a2a_send` (task_id, `TASK_STATE_COMPLETED`) → `researcher (python)`: `run research`, `llm_call`, `tool_call fetch_rss` ×3, `rss_fetch`, `submit_selection` → `a2a_result` → OpenClaw `llm_call` → app `tool_call submit_digest` → `link_check` → `telegram_send digest_chunk` → `memory_write delivered_urls` → `delivery delivered` → `openclaw_call gateway close tool loop` | |
| 3 | Выберите этот запуск в dashboard | В `agents (runtime)` есть `app (python)`, `editor (openclaw)`, `researcher (python)`. Заполнены `openclaw_runs`, `openclaw_session`, `a2a_task`, токены | Таблица Runs + Timeline + строка «OpenClaw evidence» | |
| 4 | `docker compose restart openclaw researcher app` | Через 1–2 мин все `healthy` | `docker compose ps` | |
| 5 | `/new` | «Новая беседа: создана новая сессия OpenClaw Coordinator…» | Other events: `session /new` с новым `openclaw_session_key`. Следующие события OpenClaw идут с новым session key | |
| 6 | `Какие интересы и настройки ты сохранил?` (дополнительно `/settings`) | Лимит **2**, интересы AI agents / MCP / AWS Bedrock. `/settings` показывает «источник: SQLite» | `memory_read settings` (`source: sqlite`) и `tool_call get_user_settings` / ответ агента по `<saved_settings>` | |
| 7 | `Собери ещё один дайджест, пропусти уже отправленные новости` | Новые ссылки без ссылок шага 2, либо честное «релевантных новостей не найдено» | `a2a_send` input `exclude_urls: N>0`, у Researcher `rss_fetch` stats `excluded_delivered` | |
| 8 | Длинный дайджест: `Запомни: максимум 10 новостей` → `/digest`, затем `/last` и `Покажи последний дайджест` | Новый дайджест приходит несколькими сообщениями без обрезки. Оба способа повтора присылают **весь** сохранённый дайджест с пометкой «↩️ Повтор…» | `delivery resend_last_digest resent`, `/settings` → «Доставлено ссылок» не изменилось | |
| 9 | Остановка Researcher: `docker compose stop researcher` → `/digest` | «❌ Researcher недоступен, дайджест не сформирован…». Новостей нет | Run `failed`: `a2a_retry` ×2, `tool_call request_research failed`, OpenClaw закрывает tool loop | |
| 10 | `docker compose start researcher` → `/digest` | Работает снова | | |
| 11 | Остановка OpenClaw: `docker compose stop openclaw` → любое сообщение | «❌ Coordinator (OpenClaw) недоступен, запрос не выполнен…». Скрытого fallback нет | Run `failed`, `openclaw_call failed`, событий `editor (openclaw)` нет | |
| 12 | `docker compose start openclaw` (подождать `healthy`) → сообщение | Работает снова | | |

Дополнительно можно проверить:
- **отказ постороннему**: напишите боту с другого аккаунта. Ответ «⛔ Доступ запрещён», событие `auth reject_user denied`, вызовов OpenClaw нет;
- **попытку вывести дайджест без исследования**: `Ответь списком новостей без поиска` — бот не пришлёт новости. Либо агент вызовет `request_research`, либо `reply` будет отклонён (`rejected`) и запрос завершится ошибкой.

## Интеграция с настоящим OpenClaw без credentials

Gateway и агент настоящие, модель — контролируемый fake-провайдер. Проект отдельный, live-volumes не затрагиваются.

```bash
export OPENCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)
make openclaw-check        # up offline-проекта + scripts/openclaw_integration_check.py
make openclaw-check-down   # удалить offline-проект и его volumes
```

Ожидается `25/25 checks passed`. Скрипт проверяет: workspace в system prompt, только allowlist tools у модели, 0 нативных tools, отказ `web_fetch`, повторную отправку, `/new`, restart, отсутствие дублирования transcript, общий trace трёх участников, ошибки без fallback. Отчёт пишется в `/data/traces/openclaw-integration-report.json` (volume offline-проекта). Dashboard этого проекта: http://127.0.0.1:8501, пока он запущен.

## Fake-демо dashboard (без OpenClaw и credentials)

```bash
make venv && make offline-demo
TRACE_FILES=data/offline-demo/traces/app/events.jsonl,data/offline-demo/traces/researcher/events.jsonl \
  .venv/bin/streamlit run dashboard/app.py
```

Здесь вместо OpenClaw используется **fake Gateway**. Демо показывает только трассы app и Researcher.
