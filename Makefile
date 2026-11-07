.PHONY: test demo lint up up-local-llm bench sim-users test-local test-local-quick

test:
	.venv/bin/python -m pytest -q -m 'not integration and not live'

demo:
	.venv/bin/python -m scripts.demo

lint:
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

up:
	docker compose --env-file "$${ENV_FILE:-.env}" up --build

up-local-llm:
	docker compose --env-file "$${ENV_FILE:-.env}" --profile local-llm up --build

# Requires: set -a && source .env && set +a
bench:
	EMBEDDING_BACKEND=$${EMBEDDING_BACKEND:-fastembed} QDRANT_MEMORY=$${QDRANT_MEMORY:-1} \
		.venv/bin/python -m scripts.bench

# Multi-user journey: new keys, new agents, ACL, desk approval (scripted LLM)
sim-users:
	EMBEDDING_BACKEND=$${EMBEDDING_BACKEND:-hash} .venv/bin/python -m scripts.sim_users

# Full local: clear → compose → lint → unit → integration → sim users → live/bench
test-local:
	./scripts/test_local.sh

# Faster local: reuse volumes, skip live LLM / bench (still runs sim users)
test-local-quick:
	QUICK=1 ./scripts/test_local.sh
