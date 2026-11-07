# Architecture

Sources: The Twelve-Factor App · Hexagonal / Ports & Adapters (Alistair Cockburn) · C4 model (Simon Brown)
· AWS Well-Architected Framework (6 pillars) · ADR (Michael Nygard; MADR template) ·
*Building Evolutionary Architectures* (fitness functions) · "Choose Boring Technology" (Dan McKinley).

## Structure
- **Ports & adapters**: domain and use-cases depend on interfaces (repositories, providers, queues);
  frameworks, DBs and vendors are adapters at the edge. Swapping an adapter touches one package.
- Dependencies point inward (api → services → domain); the domain imports nothing from infrastructure.
- **Modular monolith first**: clear modules in one deployable; split into services only for a proven
  scaling, ownership or isolation need.
- **Fitness functions**: encode architecture rules as tests (import/layer checks, contract tests,
  performance budgets) so they are enforced, not just documented.
- Prefer boring, well-understood technology; every new technology needs a written reason.

## Twelve-Factor essentials
Config in the environment · backing services as attached resources (swappable by config) · stateless
processes (state in DB/object store) · fast startup and graceful shutdown · dev/prod parity ·
logs as event streams to stdout.

## Documentation
- **C4**: Context (system + users + external systems) and Container (deployables, data stores, queues)
  diagrams are usually enough; Mermaid in the repo.
- **ADRs**: one decision per record — context, options considered, decision, consequences. Short.
  Superseded, never deleted.

## Review lens — AWS Well-Architected pillars
| Pillar | Ask |
|---|---|
| Operational excellence | Can we deploy, observe, and debug it? Runbooks? |
| Security | AuthN/Z, input validation, secrets, least privilege, OWASP checks? |
| Reliability | Timeouts, retries, idempotency, recovery from crash/restart, no stuck work? |
| Performance efficiency | Bottlenecks measured? Right data structures, indexes, async where needed? |
| Cost optimization | Cost per request/job known and bounded? Caching, model/instance choice? |
| Sustainability | Avoids wasteful work (duplicate jobs, re-rendering, over-provisioning)? |

## Production evolution checklist (for prototypes)
For each in-process / local component, name the production replacement and the seam it plugs into:
in-memory or SQLite store → managed Postgres; local files → object storage + CDN; in-process worker →
queue + autoscaled workers; polling → + webhooks/SSE; print logs → OTel pipeline; env file → secret manager.
