.PHONY: venv test docker-test up down restart ps logs smoke smoke-turn model-check runs offline-demo \
        openclaw-check openclaw-check-down reset-memory

OFFLINE = docker compose -p ai-news-digest-offline -f compose.yaml -f compose.offline.yaml

venv:            ## local virtualenv with pinned deps
	python3 -m venv .venv && .venv/bin/pip install -r requirements.lock

test:            ## offline unit/integration tests (fake LLM / fake Gateway / fake Telegram, real A2A)
	.venv/bin/python -m pytest

docker-test:     ## the same tests inside the Docker image (clean environment)
	docker build -t ai-news-digest:local . && docker run --rm ai-news-digest:local python -m pytest -p no:cacheprovider

openclaw-check:  ## REAL OpenClaw Gateway + fake model provider, isolated project (needs OPENCLAW_GATEWAY_TOKEN)
	$(OFFLINE) up -d --build openclaw researcher fake-llm dashboard
	$(OFFLINE) run --rm --no-deps app python scripts/openclaw_integration_check.py

openclaw-check-down: ## remove the offline project and its volumes (live data is untouched)
	$(OFFLINE) down -v

up:              ## build and start openclaw + researcher + app + dashboard
	docker compose up -d --build

ps:
	docker compose ps

logs:
	docker compose logs -f --tail=100

smoke:           ## LIVE: Gateway + editor agent, Researcher Agent Card, Telegram getMe (no LLM tokens)
	docker compose exec app python -m ai_news_digest.cli smoke

smoke-turn:      ## LIVE: + one real OpenClaw Coordinator turn (spends tokens)
	docker compose exec app python -m ai_news_digest.cli smoke --agent-turn

model-check:     ## LIVE: Anthropic model availability from the researcher container
	docker compose exec researcher python -m ai_news_digest.cli model-check

runs:            ## text summary of traced runs from all services
	docker compose exec dashboard python -m ai_news_digest.cli runs

restart:         ## restart without touching volumes (memory and sessions survive)
	docker compose restart openclaw researcher app

down:            ## stop; volumes (memory, OpenClaw state, traces) are kept
	docker compose down

offline-demo:    ## fake-Gateway demo traces for the local dashboard (no credentials, no OpenClaw)
	.venv/bin/python scripts/offline_demo.py --out data/offline-demo

reset-memory:    ## DANGER: deletes app memory (settings, history, delivered URLs)
	docker compose run --rm --no-deps app python -m ai_news_digest.cli reset-memory --yes
