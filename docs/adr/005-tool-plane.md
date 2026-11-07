# ADR 005 — Tool plane (execution split)

## Context

App teams need to add agents without rebuilding the platform. Most agents only
need config. Some need new tool **implementations**. Keeping every handler inside
the API image couples tool shipping to API releases. Putting each agent in its
own container confuses tenancy with packaging and breaks shared quota/gateway.

## Decision

1. Split **execution** into `apps/tools` (HTTP): catalog + execute.
2. Keep **policy** in platform `ToolRuntime`: allow-list, approval, write idempotency.
3. Authenticate platform → tools with a shared service token (`TOOLS_TOKEN`).
4. Leave `search_knowledge_base` as a builtin in the engine (ACL + knowledge store).
5. When `TOOLS_URL` is unset, run write-tool handlers in-process so unit tests stay Docker-free.

Rejected:

- One container per agent (scales teams poorly; duplicates control plane).
- Letting the tools service grant permission (violates “MCP/tools do not grant authz”).
- Moving RAG search into tools (would re-implement project ACL and vector access).

## Consequences

- New tool code ships by deploying `tools`; API image unchanged if the catalog
  already exposes the name and agents allow-list it after publish.
- Tools outage fails execute after policy gates; runs surface `failed` / retry via
  worker — other planes stay up.
- Compose gains a `tools` service; local DX documents `TOOLS_URL` / `TOOLS_TOKEN`.
