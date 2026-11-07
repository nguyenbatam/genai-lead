-- Single schema. Idempotent: safe on fresh install and on upgrade from the
-- old 002–006 chain. Status/role stay text with CHECKs (not smallint).

CREATE TABLE IF NOT EXISTS orgs (
    id text PRIMARY KEY,
    name text NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id text PRIMARY KEY,
    org_id text NOT NULL REFERENCES orgs (id),
    name text NOT NULL,
    request_limit integer NOT NULL CHECK (request_limit > 0),
    request_count integer NOT NULL DEFAULT 0,
    window_start_ms bigint NOT NULL DEFAULT 0,
    cost_limit_usd double precision NOT NULL DEFAULT 0,
    spend_micros bigint NOT NULL DEFAULT 0,
    reserved_micros bigint NOT NULL DEFAULT 0,
    reserved_until_ms bigint NOT NULL DEFAULT 0,
    max_in_flight_chat integer NOT NULL DEFAULT 2,
    max_in_flight_run integer NOT NULL DEFAULT 2,
    max_open_runs integer NOT NULL DEFAULT 32,
    max_queued_jobs integer NOT NULL DEFAULT 64,
    max_provider_inflight integer NOT NULL DEFAULT 2
);

CREATE TABLE IF NOT EXISTS api_keys (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects (id),
    key_hash text NOT NULL UNIQUE,
    role text NOT NULL,
    groups jsonb NOT NULL,
    revoked boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id text NOT NULL,
    action text NOT NULL,
    actor text NOT NULL,
    ref text NOT NULL
);

CREATE TABLE IF NOT EXISTS prompts (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    document jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    document jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    project_id text NOT NULL,
    task text NOT NULL,
    document jsonb NOT NULL,
    PRIMARY KEY (project_id, task)
);

CREATE TABLE IF NOT EXISTS agents (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    document jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_rows (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id text NOT NULL,
    document jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    status text NOT NULL,
    idempotency_key text NOT NULL DEFAULT '',
    lease_ms bigint NOT NULL DEFAULT 0,
    deadline_ms bigint NOT NULL DEFAULT 0,
    document jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_bases (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    name text NOT NULL,
    corpus_version bigint NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    kb_id text NOT NULL,
    document_id text NOT NULL,
    version integer NOT NULL,
    body text NOT NULL,
    acl jsonb NOT NULL,
    idempotency_key text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    status text NOT NULL,
    claimed_ms bigint NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunks (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    kb_id text NOT NULL,
    document_id text NOT NULL,
    version integer NOT NULL,
    parent_id text NOT NULL,
    kind text NOT NULL,
    body text NOT NULL,
    acl jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_replays (
    project_id text NOT NULL,
    idempotency_key text NOT NULL,
    fingerprint text NOT NULL,
    text text NOT NULL DEFAULT '',
    finish boolean NOT NULL DEFAULT false,
    ready boolean NOT NULL DEFAULT false,
    claimed_ms bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tool_effects (
    idem_key text PRIMARY KEY,
    project_id text NOT NULL,
    result jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_counters (
    id integer PRIMARY KEY,
    tokens bigint NOT NULL,
    cost_micros bigint NOT NULL,
    errors bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS evals (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    status text NOT NULL,
    lease_ms bigint NOT NULL DEFAULT 0,
    document jsonb NOT NULL
);

-- Upgrade path: columns added after the first CREATE TABLE shape.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS window_start_ms bigint NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS cost_limit_usd double precision NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS spend_micros bigint NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS reserved_micros bigint NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS reserved_until_ms bigint NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS max_in_flight_chat integer NOT NULL DEFAULT 2;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS max_in_flight_run integer NOT NULL DEFAULT 2;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS max_open_runs integer NOT NULL DEFAULT 32;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS max_queued_jobs integer NOT NULL DEFAULT 64;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS max_provider_inflight integer NOT NULL DEFAULT 2;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS deadline_ms bigint NOT NULL DEFAULT 0;
ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS corpus_version bigint NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_ms bigint NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS body_ref text NOT NULL DEFAULT '';
ALTER TABLE chat_replays ADD COLUMN IF NOT EXISTS async_pending boolean NOT NULL DEFAULT false;
ALTER TABLE chat_replays ADD COLUMN IF NOT EXISTS request jsonb;

ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_role_check;
ALTER TABLE api_keys
    ADD CONSTRAINT api_keys_role_check
    CHECK (role IN ('owner', 'admin', 'developer', 'viewer'));

ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_status_check;
ALTER TABLE runs
    ADD CONSTRAINT runs_status_check
    CHECK (
        status IN (
            'queued',
            'running',
            'waiting_approval',
            'completed',
            'failed',
            'denied',
            'cancelled'
        )
    );

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE jobs
    ADD CONSTRAINT jobs_status_check
    CHECK (
        status IN (
            'queued',
            'running',
            'indexed',
            'rejected',
            'cancelled',
            'dead'
        )
    );

ALTER TABLE evals DROP CONSTRAINT IF EXISTS evals_status_check;
ALTER TABLE evals
    ADD CONSTRAINT evals_status_check
    CHECK (status IN ('queued', 'running', 'completed', 'failed'));

CREATE UNIQUE INDEX IF NOT EXISTS runs_idem
    ON runs (project_id, idempotency_key)
    WHERE idempotency_key <> '';

CREATE INDEX IF NOT EXISTS runs_open ON runs (status);

CREATE INDEX IF NOT EXISTS runs_due ON runs (status, lease_ms);

CREATE INDEX IF NOT EXISTS runs_deadline ON runs (status, deadline_ms);

CREATE UNIQUE INDEX IF NOT EXISTS jobs_idem ON jobs (project_id, idempotency_key);

CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (status);

CREATE INDEX IF NOT EXISTS jobs_claim ON jobs (status, claimed_ms);

CREATE UNIQUE INDEX IF NOT EXISTS jobs_doc_version
    ON jobs (project_id, kb_id, document_id, version)
    WHERE status IN ('queued', 'running', 'indexed');

CREATE INDEX IF NOT EXISTS chunks_lookup ON chunks (project_id, kb_id, kind);

CREATE INDEX IF NOT EXISTS chunks_doc ON chunks (project_id, kb_id, document_id);

CREATE INDEX IF NOT EXISTS usage_project ON usage_rows (project_id, id);

CREATE INDEX IF NOT EXISTS evals_claim ON evals (status, lease_ms);

INSERT INTO usage_counters (id, tokens, cost_micros, errors)
VALUES (1, 0, 0, 0)
ON CONFLICT (id) DO NOTHING;

-- One-shot backfill for installs that recorded usage before spend_micros existed.
UPDATE projects AS p
SET spend_micros = COALESCE((
    SELECT SUM(ROUND(COALESCE((u.document->>'cost')::numeric, 0) * 1000000))::bigint
    FROM usage_rows AS u
    WHERE u.project_id = p.id AND u.document->>'status' = 'success'
), 0)
WHERE p.spend_micros = 0
  AND EXISTS (
    SELECT 1 FROM usage_rows AS u
    WHERE u.project_id = p.id AND u.document->>'status' = 'success'
  );

UPDATE usage_counters
SET
    tokens = src.tokens,
    cost_micros = src.cost_micros,
    errors = src.errors
FROM (
    SELECT
        COALESCE(SUM(
            COALESCE((document->>'input_tokens')::bigint, 0)
            + COALESCE((document->>'output_tokens')::bigint, 0)
        ), 0) AS tokens,
        COALESCE(SUM(
            CASE
                WHEN document->>'status' = 'success'
                THEN ROUND(COALESCE((document->>'cost')::numeric, 0) * 1000000)::bigint
                ELSE 0
            END
        ), 0) AS cost_micros,
        COUNT(*) FILTER (WHERE document->>'status' IS DISTINCT FROM 'success') AS errors
    FROM usage_rows
) AS src
WHERE usage_counters.id = 1
  AND usage_counters.tokens = 0
  AND usage_counters.cost_micros = 0
  AND usage_counters.errors = 0
  AND EXISTS (SELECT 1 FROM usage_rows);
