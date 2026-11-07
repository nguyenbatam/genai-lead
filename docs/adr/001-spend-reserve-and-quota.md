# ADR 001 — Spend settle and Redis admit

## Context

Gateway holds project spend before calling a provider so concurrent calls cannot
overshoot the USD cap. Redis admits request rate. Process crash and `INCR`-then-check
could leave stale holds or inflate denied counts.

## Decision

1. On a successful completion, `record_usage` releases the hold in the same critical
   section (same Postgres transaction) as the spend increment.
2. Holds carry `reserved_until_ms`. Workers call `reconcile_reserved` to clear expired
   holds after a crash.
3. Redis admit uses a Lua script that increments only while under the limit. Denied
   calls do not move the counter.

## Consequences

Multi-replica spend and quota stay correct without a second ledger. A crash can still
block spend until the TTL expires; that is preferred to silent overspend. On settle,
spend is clamped to the USD cap after releasing the hold so an underestimated ceiling
cannot push `spend_micros` past the limit; the usage row still records the provider cost.
