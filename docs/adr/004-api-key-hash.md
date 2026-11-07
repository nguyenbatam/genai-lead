# ADR 004 — API key hash stays HMAC-SHA256 + pepper

## Context

A production review asked whether API keys should use Argon2id. Auth is a hot
path: every request looks up the key. Argon2id is memory-hard and needs a public
key id (or salt) beside the secret so verification is not a full table scan.

## Decision

Keep `HMAC-SHA256(pepper, raw)` for project API keys.

1. Lookup stays O(1) by unique `key_hash`.
2. `PLATFORM_KEY_PEPPER` is required when `DATABASE_URL` is set.
3. Failed auth is rate-limited per client (`AuthLimiter`).
4. Raw keys are shown once and never logged.

Rejected for now: Argon2id with `sk_<key_id>.<secret>` prefix. That is the right
next step if offline cracking of stolen hashes becomes the dominant risk; it
breaks existing keys and needs a dual-read migration.

## Consequences

Auth stays cheap under load. Stolen DB hashes without the pepper are still hard
to use. Rotate the pepper only with a planned re-issue of all keys.
