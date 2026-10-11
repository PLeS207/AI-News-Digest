.PHONY: venv test docker-test up down restart ps logs smoke smoke-turn model-check runs \
        openclaw-check openclaw-check-down reset-memory login-editor login-researcher auth-status

OFFLINE = DASHBOARD_PORT=8502 docker compose -p ai-news-digest-offline -f compose.yaml -f compose.offline.yaml

venv:            ## local virtualenv with pinned deps
	python3 -m venv .venv && .venv/bin/pip install -r requirements.lock

test:            ## offline tests (fake Gateway / fake Telegram Bot API, real A2A over HTTP)
	.venv/bin/python -m pytest

docker-test:     ## the same tests inside the Docker image (clean environment)
	docker build -t ai-news-digest:local . && docker run --rm ai-news-digest:local python -m pytest -p no:cacheprovider

openclaw-check:  ## REAL OpenClaw Gateway (both agents) + fake model provider; isolated project, dashboard :8502
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

login-editor:    ## ChatGPT/Codex device-code sign-in for the editor agent (interactive; choose "Keep current restrictions")
	docker compose exec openclaw node openclaw.mjs models auth login --provider openai --method device-code --agent editor

login-researcher: ## the same for the researcher agent (each agent keeps its own OAuth profile)
	docker compose exec openclaw node openclaw.mjs models auth login --provider openai --method device-code --agent researcher

auth-status:     ## redacted auth metadata of both agents: profile type and expiry only (no tokens, no e-mail)
	@for a in editor researcher; do docker compose exec -T openclaw node openclaw.mjs models auth list --provider openai --agent $$a --json 2>/dev/null \
	  | docker compose exec -T openclaw node -e 'let r="";process.stdin.on("data",d=>r+=d).on("end",()=>{const d=JSON.parse(r.slice(r.indexOf("{")));console.log(d.agentId+": "+JSON.stringify(d.profiles.map(p=>({type:p.type,expiresAt:p.expiresAt}))))})'; done

smoke:           ## LIVE: Gateway + both agents, Researcher Agent Card, Telegram getMe (no model calls)
	docker compose exec app python -m ai_news_digest.cli smoke

smoke-turn:      ## LIVE: + one real Coordinator turn with a tool call (uses subscription quota)
	docker compose exec app python -m ai_news_digest.cli smoke --agent-turn

model-check:     ## LIVE: one real Researcher model call that must return a tool call (uses subscription quota)
	docker compose exec researcher python -m ai_news_digest.cli model-check

runs:            ## text summary of traced runs from all services
	docker compose exec dashboard python -m ai_news_digest.cli runs

restart:         ## restart without touching volumes (memory and sessions survive)
	docker compose restart openclaw researcher app

down:            ## stop; volumes (memory, OpenClaw state, traces) are kept
	docker compose down

reset-memory:    ## DANGER: deletes app memory (settings, history, delivered URLs)
	docker compose run --rm --no-deps app python -m ai_news_digest.cli reset-memory --yes
