# Reliability

Sources: AWS Builders' Library ("Timeouts, retries and backoff with jitter", "Avoiding insurmountable
queue backlogs", "Making retries safe with idempotent APIs") · Michael Nygard, *Release It!* (stability
patterns) · Azure Architecture Center (Retry, Circuit Breaker, Bulkhead, Queue-Based Load Leveling,
Async Request-Reply) · Google SRE book (handling overload, cascading failures).

## Timeouts
- Every outbound call (HTTP, DB, LLM, subprocess) has a timeout; an end-to-end deadline per request/job
  bounds the sum. No infinite waits anywhere.

## Retries
- Retry only transient failures (timeouts, 429, 5xx, connection reset) of idempotent operations.
- Exponential backoff with **full jitter**, capped attempts (typically 3) and a max delay; honour `Retry-After`.
- Retry at ONE layer only (avoid retry multiplication across layers); use a retry budget under load.
- Distinguish *transport* retries (same request) from *content* retries (re-ask with feedback, e.g. LLM
  output that failed validation) — the latter need a cap and a fallback.

## Degradation & isolation
- **Fallbacks** are explicit, logged, and visible to the client (flag in the response), never silent.
- **Circuit breaker** around flaky dependencies: fail fast while open, probe half-open.
- **Bulkheads**: separate pools/semaphores per dependency so one slow dependency cannot starve others.
- **Load shedding / backpressure**: bounded queues; reject early with 429/503 + `Retry-After` instead of
  queueing forever.

## Async work
- At-least-once processing + idempotent handlers (dedupe key or state check before acting).
- Persist state before and after each step; on restart, recover or fail in-flight work explicitly.
- Poison messages go to a dead-letter queue / `failed` state with the error, after N attempts.
- Every job has a max runtime; a watchdog fails jobs that exceed it.

## Lifecycle
- Graceful shutdown on SIGTERM: stop accepting work, drain in-flight up to a deadline, then exit.
- Health endpoints: **liveness** (process OK — no dependency checks) vs **readiness** (can serve:
  dependencies reachable, migrations applied).
- Startup fails fast on invalid config.
