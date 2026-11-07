---
name: backend-standards
description: Backend and architecture standards checklist (HTTP API design, async jobs, reliability, data, observability, security, architecture). Use when designing or reviewing an API, service, job pipeline, data model or architecture decision, or when asked "is this production-grade?".
---

# Backend standards

Project rules in `.claude/rules/` and CLAUDE.md are more specific and win on conflict.
Read only the reference you need:

| Topic | Reference | Based on |
|---|---|---|
| HTTP API, errors, async/long-running jobs, pagination, idempotency | [api-design.md](references/api-design.md) | RFC 9110, RFC 9457, Google AIP-151/158/193, Microsoft & Zalando guidelines |
| Timeouts, retries, backoff, circuit breaking, shutdown, health | [reliability.md](references/reliability.md) | AWS Builders' Library, *Release It!*, Azure patterns |
| Schema, migrations, transactions, concurrency, ids, time | [data.md](references/data.md) | expand/contract, outbox, RFC 9562 |
| Logs, metrics, traces, SLOs | [observability.md](references/observability.md) | Google SRE, OpenTelemetry, W3C Trace Context |
| AuthZ, input, secrets, resource limits, LLM safety | [security.md](references/security.md) | OWASP API Top 10 (2023), OWASP LLM Top 10 |
| Boundaries, config, decisions, diagrams, review pillars | [architecture.md](references/architecture.md) | 12-Factor, C4, AWS Well-Architected, ADR |

## The non-negotiables (apply everywhere)
1. Every network call has a timeout; retries only for idempotent operations, capped, with backoff + jitter.
2. Every error is surfaced with a stable machine-readable code and a human message; nothing fails silently.
3. Every state change of a long-running operation is persisted and queryable; no job can get stuck.
4. Every request/job has a correlation id that appears in every log line and in the response.
5. Every input from users or models is validated at the boundary; every resource is bounded (size, rate, count, time).
6. Business logic depends on interfaces (ports), not on frameworks, databases or vendors (adapters).
7. Every significant trade-off is written down (ADR / "Key decisions"), including the rejected option.
