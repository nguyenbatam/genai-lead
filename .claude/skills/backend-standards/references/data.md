# Data

Sources: "Expand and contract" / parallel change (Martin Fowler) · Transactional Outbox (microservices.io)
· RFC 9562 (UUIDs, incl. v7) · *Designing Data-Intensive Applications* (Kleppmann).

## Schema & migrations
- Schema changes are versioned migrations in the repo, applied automatically and idempotently.
- Zero-downtime changes use **expand → migrate/backfill → contract**; old and new app versions must both
  work against the schema during a rollout. No destructive change in the same release as the code change.
- Every query pattern used in production has a supporting index; no unbounded `SELECT` without limit.

## Consistency
- Keep transactions short and inside one service/DB; never hold a transaction across a network call.
- State transitions use a guarded update (`UPDATE … WHERE id=? AND status=?`) or optimistic version column
  to prevent lost updates and double processing.
- Writing to the DB and publishing an event → **transactional outbox**, not dual writes.
- Cross-service workflows → saga with compensations, not distributed transactions.

## Identifiers & time
- Server-generated ids (UUIDv4, or UUIDv7 for index locality); never expose auto-increment ids if
  enumeration matters.
- Store timestamps in UTC (`created_at`, `updated_at`); convert only at the edges.

## Files & blobs
- Blobs live in an object store (local dir in prototypes) referenced by key; DB stores metadata only.
- Writes are atomic (temp + rename / multipart complete); content-addressed or versioned keys for
  immutable artifacts; retention/cleanup policy documented.

## Privacy
- Classify PII; minimise what is stored; define retention and deletion; never log secrets or PII.
