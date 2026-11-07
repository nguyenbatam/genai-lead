# ADR 003 — Provider credential pool

## Context

A model may be reachable through many provider keys (free and paid), each with its
own RPM, daily, weekly, or retry-after limits. Project API keys authenticate tenants.
Provider secrets authenticate upstream calls. Mixing them loses isolation and makes
cost control impossible.

## Decision

1. Gateway owns credential pick, rotate, and cool-down. The HTTP adapter only posts.
2. Credentials are platform secrets. Prefer `config/llm_credentials.yaml` (path via
   `LLM_CREDENTIALS_FILE`); inline `LLM_CREDENTIALS` JSON overrides the file; a single
   `LLM_API_KEY` remains the one-key fallback. Secret values come from env names in
   the file (`api_key_env`), not from git. They are never logged or returned on HTTP.
3. Pick order is free before paid unless the model sets `tier_affinity` to
   `free_only` or `paid_only`. Within a tier, weighted round-robin.
4. Each credential carries stacked `LimitPolicy` windows (`1m`/`1h`/`1d`/`7d`/`30d`,
   calendar or rolling, requests/tokens/spend). A 429 cools that key only; the model
   breaker opens only when the pool for that model is exhausted.
5. Usage rows record `credential_id` and `tier` so free ratio and paid spend are visible.

## Consequences

Total cost stays near zero while free capacity remains. Model fallback policy is
unchanged: rotating keys is not a model change. Multi-replica deployments share
counters through Redis; secrets stay in process memory.
