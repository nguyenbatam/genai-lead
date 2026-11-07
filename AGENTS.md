# GenAI Lead

A runnable multi-tenant GenAI platform: control plane, stateless data plane, and async workers. Two sample apps share one gateway, one agent runtime, and one RAG service. The model is called only for the residual a control cannot decide. Quality, access, and cost are measured per project.

Invariants are in `PLAN.md`. Do not add a service or a library the running tree does not use. Do not name this Nexus. The sibling `nexus-platform` is an unrelated messaging system.

Sibling repos stay where they are. `studio-kit` is the game-studio runtime. `ai_deployment_platform` is the Go support agent (approvals, idempotency, tenant ACL, IdP). This repo does not re-implement either. A support-agent clone is not a second sample app.

## What this is

A developer creates a project and an API key, registers a model and an agent, and runs it. Runs execute in one of three modes: `workflow`, `agent`, or `supervisor`. Every completion goes through the gateway, which records tokens and cost against that project. Knowledge bases are a shared service with ACL. A side-effect tool waits for approval and does not double-apply on retry. A second project cannot read the first project's config, documents, or cache.

## Layout

Filled in as phases land. The target is in `PLAN.md`.

| Path | What |
|---|---|
| `control/` | Org, project, API key, quota, audit. No agent graph, no provider call |
| `core/` | Gateway, runtime, tools, RAG, eval, traces |
| `apps/api/` | HTTP composition |
| `apps/workers/` | Ingest and eval jobs |
| `apps/tools/` | Tool execution plane (allow-list/approval/idempotency stay in `core/tools`) |
| `examples/` | HTTP clients only. `knowledge`, `desk`, `_template` (app team adds agents here) |
| `compose.yaml` | Local stack. Added in phase 1 |
| `docs/adr/` | Written at the end of the phase that made the decision |
| `ARCHITECTURE.md` | Four planes, agent=config, tool split |
| `PLAN.md` | What to build, in order |
| `JD.md` | playSTUDIOS GenAI Engineering Lead/Manager role bar |
| `INTERVIEW.md` | What to say. Written in phase 7, after `make demo` is green. Maps demo to `JD.md` |

## Commands

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q -m 'not integration and not live'
.venv/bin/ruff check . && .venv/bin/ruff format --check .
make test-local          # clear → compose → unit → integration → live/bench
make test-local-quick    # reuse volumes; skip live LLM
```

## Conventions

- Python 3.13. Tools run via `.venv/bin/python`.
- Ruff: line-length 100, `select = ["E", "F", "I", "UP", "B"]`.
- Type hints on public functions. `X | None`, not `Optional[X]`.
- Fixed system vocab lives in `control/vocab.py` (`frozenset` of plain strings).
  Wire and DB stay `text`; Postgres CHECKs constrain status/role columns. Do not
  map them to smallint. Details in `.claude/rules/python.md`.
- Unit tests use in-memory adapters, no Docker, each under a second. Integration tests are marked `integration` and talk to Compose. The Stop hook runs unit tests only.
- Pin exact versions in `requirements.txt`.
- Secrets only from the environment. API keys are stored hashed and shown once. Never log a question, a document body, an answer, or a key.
- Model output is data. Citation labels are checked against passages retrieved in that run for that project. Invented labels are stripped. Tool names the agent was not granted are `denied`.
- Prompt injection follows `.claude/rules/injection.md`. Untrusted lines are withheld, what remains is spotlighted as `<<data:…>>`, and a payload cannot close that marker. The tool allow-list and the ACL filter are the boundary, not the regex.
- The model never decides identity, quota, or whether a project may read a chunk. It never receives a connection string.
- `control/` does not import the agent graph. `core/runtime/` does not hash API keys. `examples/` do not import `core`.
- Catch specific exceptions. No bare `except` that swallows.

## Outcomes

`completed` · `no_answer` · `ungrounded` · `unverified` · `waiting_approval` · `refused` (oversize, or a side effect with no approval path) · `denied` (tool or project the caller may not use) · `failed` · `cancelled`.

## Out of scope

An admin UI. A self-built IdP. Keycloak in this repo. Vault. Kubernetes. Terraform. Payment billing. A live Gmail, CRM, or Slack connector. A fine-tune. A self-built vector database. More than two sample apps. More than three execution modes. An open MCP catalog. `PLAN.md` is the list.

## Definition of done

`pytest -q -m 'not integration and not live'` green and `ruff check` / `ruff format --check` clean. Behaviour changes need a test. A new phase needs the cases named in `PLAN.md` for that phase, including one cross-project denial when the phase touches isolation, and one refusal or leak case when it touches safety.

## Skills

Vendored skills in `.claude/skills/` (see `.claude/skills/VENDORED.md`). This file wins when a skill conflicts with it.
