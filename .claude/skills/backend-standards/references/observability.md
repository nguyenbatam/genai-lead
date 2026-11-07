# Observability

Sources: Google SRE book (four golden signals, SLOs, alerting on symptoms) · RED method (Tom Wilkie) ·
USE method (Brendan Gregg) · OpenTelemetry · W3C Trace Context.

## Logs
- Structured (JSON or key=value), one event per line, levels used consistently
  (ERROR = needs action, WARNING = degraded/fallback/retry, INFO = lifecycle).
- Every line carries correlation fields: `request_id`/`trace_id`, and domain ids (`job_id`, `stage`).
- Log the *decision* and its inputs (why a retry/fallback happened), not just "error".
- No secrets, tokens or PII in logs.

## Metrics
- Services: **RED** — Rate, Errors, Duration (histograms, not averages) per endpoint.
- Resources/queues: **USE** — Utilisation, Saturation (queue depth, oldest job age), Errors.
- Domain metrics for pipelines: jobs by terminal status, retries per stage, fallback rate, per-stage latency.

## Traces
- Propagate W3C `traceparent` across HTTP and into async jobs; one span per stage / external call.
- OpenTelemetry SDK so the exporter (Jaeger, Tempo, vendor) is a config choice.

## SLOs & alerts
- Define SLIs from the user's view (e.g. % of jobs succeeding within N minutes) and an SLO target.
- Alert on symptoms / SLO burn rate, not on every cause; every alert links to a runbook.

## Prototype minimum
Structured logs with correlation ids + per-stage timings stored on the job + a health endpoint.
Name where metrics/traces would plug in.
