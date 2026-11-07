# Sample apps (HTTP clients)

This folder is where **app teams** add agents. The platform stays in `control/`, `core/`, `apps/`.

```text
examples/
  lib/           shared HTTP helper
  knowledge/     workflow + RAG
  desk/          agent + approval write
  _template/     copy to add a new agent
```

## Lead contract

| Do | Don't |
|---|---|
| Register agents via `POST /v1/agents` | Import `core` / `control` |
| Reuse tools already in the registry | Rebuild/redeploy the platform for a new usecase |
| Add eval under `datasets/<app>/` | Hardcode a Research team of N agents in engine code |
| Run against `make up` (same topology as prod) | Start a Docker container per agent |
| Reuse tools from the catalog | Ship new tool bodies inside the API image |

Adding an agent is **config on a running API**, not a new image. New tool **code** deploys `apps/tools` (see `ARCHITECTURE.md`).

## Local (like production)

```bash
make up                          # postgres redis qdrant api worker
export PLATFORM_BASE_URL=http://127.0.0.1:8000
export PLATFORM_ADMIN_TOKEN=local-admin

.venv/bin/python -m examples.knowledge.register
.venv/bin/python -m examples.desk.register
```

Multi-user / multi-agent journey (scripted provider, no live LLM required):

```bash
.venv/bin/python -m scripts.sim_users
```

`./scripts/test_local.sh` runs that simulation after the stack is healthy.
