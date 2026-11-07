# HTTP API design

Sources: RFC 9110 (HTTP semantics) · RFC 9457 (Problem Details) · Google AIP-151 (long-running
operations), AIP-158 (pagination), AIP-193 (errors) · Microsoft REST API Guidelines · Zalando RESTful
API Guidelines · Azure "Asynchronous Request-Reply" pattern · IETF Idempotency-Key header draft.

## Resources & methods
- Plural nouns, lowercase, kebab-case paths: `/v1/videos/{id}`. Verbs only as custom actions
  (`POST /v1/videos/{id}:cancel` or `/cancel`).
- Methods per RFC 9110: GET/HEAD safe; PUT/DELETE idempotent; POST neither unless an idempotency key is used.
- Major version in the path (`/v1`). Additive changes (new optional fields, new endpoints) are non-breaking;
  removing/renaming fields, changing types or semantics is breaking → new version.
- JSON field names consistent (pick snake_case or camelCase once). Timestamps RFC 3339 in UTC.
  Enums as strings on the wire; clients must tolerate unknown enum values.
  App code may use a named set (`frozenset` / `Literal` / `StrEnum`) internally;
  do not require closed `StrEnum` validation on every response field.

## Status codes
200 OK · 201 Created (+ `Location`) · 202 Accepted (async; + `Location` of the status resource) ·
204 No Content · 400 malformed · 401 unauthenticated · 403 forbidden · 404 not found ·
409 conflict / wrong state · 412 precondition failed (ETag) · 422 semantically invalid ·
429 rate limited (+ `Retry-After`) · 500 bug · 502/503/504 dependency problems (+ `Retry-After` when known).

## Errors — RFC 9457 Problem Details
`Content-Type: application/problem+json`
```json
{"type": "https://example.com/problems/unsupported-topic", "title": "Unsupported topic",
 "status": 422, "detail": "Only these topics are supported: ...", "instance": "/v1/videos",
 "code": "unsupported_topic", "request_id": "…", "errors": [{"field": "query", "message": "…"}]}
```
- `type`/`code` stable and documented; `detail` human-readable and actionable; extensions allowed.
- Never leak stack traces, SQL, file paths, or upstream provider bodies.

## Long-running operations (async jobs) — AIP-151 / Async Request-Reply
- `POST` creates the operation → `202 Accepted`, `Location: /v1/<ops>/{id}`, body = operation resource.
- Operation resource: `id, status (enum), done (bool), progress/stage, created_at, updated_at,
  error (problem object) | result (or link to it)`.
- Terminal states are explicit (`succeeded`, `failed`, `cancelled`); `done=true` iff terminal.
- Clients poll `GET` (server may send `Retry-After`); optional webhook/SSE later — polling is the contract.
- Result retrieval of an unfinished operation → `409` with the current status, not `404`.
- List endpoint with status filter; operations retained for a documented period.

## Idempotency
- Clients may send `Idempotency-Key` on POST; server stores key → response for a TTL and replays it.
  Same key + different body → `422`. Alternatively dedupe on a natural key (documented).

## Collections
- Cursor pagination: `?page_size=&page_token=` → `next_page_token` (AIP-158). Default and max page size.
- Filtering via explicit query params; stable sort order (e.g. `created_at desc, id`).

## Concurrency & caching
- Optimistic concurrency with `ETag` + `If-Match` → `412` on mismatch.
- `Cache-Control` on immutable artifacts; `ETag`/`Last-Modified` for downloads; support `Range` for media.

## Documentation
- OpenAPI is the source of truth: every operation has summary, description, examples, and all error
  responses declared. Breaking-change check in CI when possible.
