.PHONY: venv test docker-test up down restart ps logs smoke runs offline-demo reset-memory

venv:            ## local virtualenv with pinned deps
	python3 -m venv .venv && .venv/bin/pip install -r requirements.lock

test:            ## offline tests (fake LLM / Telegram, real A2A over localhost)
	.venv/bin/python -m pytest

docker-test:     ## the same tests inside the Docker image (clean environment)
	docker build -t ai-news-digest:local . && docker run --rm ai-news-digest:local python -m pytest -p no:cacheprovider

up:              ## build and start app + researcher + dashboard
	docker compose up -d --build

ps:
	docker compose ps

logs:
	docker compose logs -f --tail=100

smoke:           ## LIVE check: Anthropic model, Telegram getMe, Researcher Agent Card
	docker compose exec app python -m ai_news_digest.cli smoke

runs:            ## text summary of traced runs from both services
	docker compose exec dashboard python -m ai_news_digest.cli runs

restart:         ## restart without touching volumes (memory survives)
	docker compose restart app researcher

down:            ## stop; volumes (memory, traces) are kept
	docker compose down

offline-demo:    ## fake-LLM demo traces for the local dashboard (no credentials)
	.venv/bin/python scripts/offline_demo.py --out data/offline-demo

reset-memory:    ## DANGER: deletes Editor memory (settings, history, delivered URLs)
	docker compose run --rm --no-deps app python -m ai_news_digest.cli reset-memory --yes
