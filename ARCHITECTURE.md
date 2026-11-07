# Architecture — GenAI Lead

Multi-tenant GenAI **platform**, not a super-agent. Agents are versioned config.
Heavy work is split across planes so each can scale and deploy on its own cadence.

## Four planes

```text
App team (examples/*)  --HTTP-->  API
                                    |
        +---------------------------+---------------------------+
        |                           |                           |
   Control plane              Data plane                  Async plane
   Postgres                   api + gateway               workers
   org/project/key            ToolRuntime (policy)        ingest/embed/eval/runs
   agent/prompt/quota         LLM providers               Redis / Qdrant
                                    |
                              Tool plane
                              apps/tools (execution only)
```

| Plane | Process | Owns | Does not own |
|---|---|---|---|
| Control | Postgres (+ API write paths) | Identity, ACL groups, agent publish, spend/quota config | Model calls, tool side effects |
| Data | `apps/api` | Auth, enqueue, gateway, ToolRuntime **policy** | Durable job loops |
| Async | `apps/workers` | Ingest, embed, eval batch, run sweep | Agent config schema |
| Tools | `apps/tools` | Execute allow-listed tool bodies | Permission, approval, idempotency |

LLM inference sits **outside** these processes (HTTP providers or Compose profile `local-llm`).

## Agent model

- An agent is a row: mode (`workflow` | `agent` | `supervisor`), prompt, tools, budget, members.
- A **run** is the unit of work. Any healthy API/worker replica may drive it via checkpoint + lease.
- Agents are **not** machines, pods, or Docker images.

### Adding work without restarting the platform

| Change | Restart `api`? | What you do |
|---|---|---|
| New agent using existing tools/modes | No | `POST /v1/agents` → publish |
| New prompt / route / budget | No | Control API |
| New **tool implementation** | Deploy **`tools`** service | Catalog + handlers in `apps/tools`; then point agent `allowed_tools` |
| New policy / engine / schema | Rolling `api`/`worker` | Platform change |

### Buggy agent isolation

- Tenant: project key cannot read another project's agents/docs.
- Publish gate: draft → eval → publish.
- Allow-list: unknown tool → `denied`.
- Budget + fair admission: runaway run hits wall/`429`; other projects keep slots.
- Write tools: approval + idempotency key; retry does not double-apply.

## Tool plane contract

1. Platform `ToolRuntime` checks allow-list, approval, and write idempotency **before** any execute call.
2. `apps/tools` receives only authenticated service calls (`X-Tools-Token`). It never sees API keys or decides tenancy.
3. Builtin `search_knowledge_base` stays in the data/async plane (needs KB ACL + vectors).
4. Empty `TOOLS_URL` keeps in-process handlers for unit tests (no Docker).

See ADR `docs/adr/005-tool-plane.md`.

## Local ≈ production topology

`compose.yaml`: `postgres`, `redis`, `qdrant`, `api`, `worker`, `tools` (+ optional `llm-local`).

```bash
make up
make sim-users          # multi-user journey (scripted LLM)
.venv/bin/python -m examples.knowledge.register
```

Autoscaling (HPA) is an ops concern on the same service boundaries; this repo ships the split, not Kubernetes.
