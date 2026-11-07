# ADR 002 — Fair admission

## Context

Request quota and spend caps are per project, but in-flight chat/run slots and
worker claim were process-global or FIFO. One project could fill the shared
semaphores or the ingest/run batch and starve others while still under its rate
limit. The shared HTTP provider pool could also be dominated by one tenant.

## Decision

1. Per-project concurrency (`max_in_flight_chat`, `max_in_flight_run`) via a
   limiter (memory in tests, Redis Lua in Compose). Checked before the global
   semaphore. Denied calls do not hold a slot.
2. Open-run and queued-ingest caps (`max_open_runs`, `max_queued_jobs`) so
   enqueue/submit cannot grow unbounded even when in-flight is free.
3. Fair claim: run `claim_due` takes at most one due run per project per batch;
   ingest prefers the project with the fewest running jobs.
4. Worker sweep acquires the same run limiter before `_drive`. If the project is
   at capacity, the lease is deferred (~1s) so other projects can be claimed.
5. Gateway acquires `max_provider_inflight` before calling the model so the
   shared HTTP pool cannot be monopolized. Chat returns `429`; a run is
   re-queued with a short defer.
6. Create-project API accepts the fair-admission fields (defaults unchanged).

Rejected: scheduling by estimated completion time / SJF. LLM latency estimates
are too noisy; a flood of “short” jobs would still crowd the queue.

## Consequences

Spam from one project returns `429` for that project. Another project on the
same process still gets chat/run/ingest/provider turns. Global semaphores and
the HTTP `max_connections` ceiling remain overload protection. Crash can leave
a Redis in-flight key until TTL (120s).
