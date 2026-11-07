# GenAI Lead

Multi-tenant GenAI platform you can run: projects, API keys, a model gateway, an agent runtime with three modes, and a shared RAG service. Two sample apps sit on the same API.

Read `PLAN.md` before writing code. Role bar is `JD.md`. Architecture overview is `ARCHITECTURE.md`. Claude config is in `.claude/` and the entry files are `CLAUDE.md` and `AGENTS.md`.

## Layout (interview view)

| Path | Who | What |
|---|---|---|
| `control/` `core/` `apps/api` `apps/workers` | Platform team | Control + data + async planes |
| `apps/tools/` | Platform team | Tool **execution** plane (policy stays in `core/tools`) |
| `examples/` | App team | HTTP clients only — register agents without rebuilding the platform |
| `datasets/` | App team | Eval cases per sample app |
| `compose.yaml` | Ops/local | Postgres, Redis, Qdrant, API, worker, tools (+ optional Ollama) |

Agents are **config** (`POST /v1/agents`), not containers. Copy `examples/_template/` to add one. New tool code deploys `apps/tools`, not an agent image.

## Local stack

```bash
ENV_FILE=/absolute/path/.env docker compose --env-file "$ENV_FILE" up --build
# or: make up
```

`--env-file` supplies Compose itself (the host port). `env_file` in `compose.yaml` injects that same file into the container. The image build does not copy it. Variable names are listed in `.env.example`.

## Simulate real users (no live LLM required)

```bash
make sim-users
# or as part of the full local gate:
make test-local
```

`scripts/sim_users.py` creates an org, multiple API keys (alice/bob/cara/dana), registers knowledge + desk agents, then a **second** agent by a new developer on the same project, checks cross-project denial and viewer refusal.
