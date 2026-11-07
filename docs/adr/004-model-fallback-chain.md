# ADR 004 — Model fallback chain and tier order

## Context

Free and paid provider keys can stop without notice (429, daily caps, upstream
outage). A single fallback model is not enough when the paid path also dies.
Self-host (Ollama / OpenAI-compatible) must be an explicit last hop, not a magic
router. Credential pick order also varies by situation: some tasks should burn
paid before local; others should prefer local after free.

## Decision

1. A route carries `fallback_model_ids` (max two). Capability must cover the
   primary; mismatched hops are skipped. Explicit `model_id` on a request does
   not walk another route's chain.
2. Credential tiers are `free`, `paid`, and `local`. When `tier_affinity` is
   `any`, pick order comes from `tier_order`:
   - `free_paid_local` (default)
   - `free_local_paid`
   Affinity `free_only` / `paid_only` still wins over order. A route may override
   the model card's `tier_order`.
3. Self-host is a normal ModelCard (`provider=selfhost`) plus a `tier=local`
   credential with its own `base_url`. Compose profile `local-llm` is optional.
4. Provider latency metrics count wire time only; HTTP backoff sleep is excluded.
   Counters track attempts, HTTP retries, model fallbacks, and cost micros labeled
   by `model` and `tier`. Exhausting only rate-limited credentials does not open
   the model breaker.
5. `GET /v1/usage/summary` aggregates the newest ledger rows (cap 5_000) by
   model × tier (calls, cost, latency percentiles, error rate) and returns a
   `hint` (`prefer_free_local_paid`, `prefer_free_paid_local`, `consider_local`,
   `keep_free`, `observe`). Hints advise `tier_order`; they do not auto-route.
   Credential hops that fail before a success are ledgered so free/paid/local
   mix stays visible. An open breaker still sets `fallback_from` on the hop that
   serves the request.

## Consequences

Spend and usage stay auditable per model and credential. Operators choose order
per model or task without giving the agent control. Exhausting the chain yields
a deliberate provider error, never an undeclared model. Cost mix is visible
without scraping raw usage rows.
