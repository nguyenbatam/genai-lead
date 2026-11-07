"""Postgres adapter for control records and run checkpoints."""

from __future__ import annotations

from pathlib import Path

from psycopg.errors import UniqueViolation
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from control.codec import (
    dump,
    load_agent,
    load_model,
    load_prompt,
    load_route,
    load_run,
    load_usage,
)
from control.errors import IdempotencyConflict, IdempotencyPending
from control.records import (
    AgentDef,
    ApiKey,
    Audit,
    ModelCard,
    Org,
    Principal,
    Project,
    Prompt,
    Route,
    RunRecord,
    Usage,
)
from control.store import (
    OPEN_RUN,
    RESERVE_TTL_MS,
    ROLES,
    WINDOW_MS,
    Store,
    hash_secret,
    new_id,
)
from control.vocab import (
    EVAL_RUNNING,
    MAX_ROUTE_FALLBACKS,
    OUTCOME_CANCELLED,
    RUN_CANCELLED,
    TIER_AFFINITIES,
    TIER_ORDERS,
    USAGE_SUCCESS,
)

_POOLS: dict[str, ConnectionPool] = {}


_MIGRATION_LOCK = 864213


def _statements(script: str) -> list[str]:
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


# Marker for the consolidated schema.sql. "6" means the old 002–006 chain finished.
_SCHEMA_VERSION = "schema"

# Numbered patches after the base schema. Idempotent SQL only.
_PATCHES: tuple[tuple[str, str], ...] = (
    (
        "7",
        """
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS body_ref text NOT NULL DEFAULT '';
        ALTER TABLE chat_replays
            ADD COLUMN IF NOT EXISTS async_pending boolean NOT NULL DEFAULT false;
        ALTER TABLE chat_replays ADD COLUMN IF NOT EXISTS request jsonb;
        """,
    ),
)


def apply_migrations(conn) -> None:
    """Apply control/schema.sql once, then numbered patches. Advisory lock serializes boot."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    root = Path(__file__).parent
    if _SCHEMA_VERSION not in applied and "6" not in applied:
        for statement in _statements((root / "schema.sql").read_text()):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
            (_SCHEMA_VERSION,),
        )
        applied.add(_SCHEMA_VERSION)
    for version, script in _PATCHES:
        if version in applied:
            continue
        for statement in _statements(script):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
            (version,),
        )


def shared_pool(url: str) -> ConnectionPool:
    existing = _POOLS.get(url)
    if existing is not None:
        return existing
    pool = ConnectionPool(
        url,
        min_size=1,
        max_size=10,
        open=True,
        kwargs={
            "connect_timeout": 3,
            "options": "-c statement_timeout=30000 -c application_name=genai",
        },
    )
    with pool.connection() as conn:
        apply_migrations(conn)
    _POOLS[url] = pool
    return pool


class PostgresStore(Store):
    def __init__(self, url: str, pepper: str = "") -> None:
        super().__init__(pepper=pepper)
        self.pool = shared_pool(url)

    def ping(self) -> bool:
        import psycopg

        try:
            with self.pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        except (OSError, psycopg.OperationalError):
            return False

    def create_org(self, name: str) -> Org:
        org = Org(id=new_id("org"), name=name)
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org.id, org.name))
        return org

    def create_project(
        self,
        org_id: str,
        name: str,
        request_limit: int = 1_000,
        cost_limit_usd: float = 0,
        *,
        max_in_flight_chat: int = 2,
        max_in_flight_run: int = 2,
        max_open_runs: int = 32,
        max_queued_jobs: int = 64,
        max_provider_inflight: int = 2,
    ) -> Project:
        if request_limit < 1:
            raise ValueError("request limit must be positive")
        if cost_limit_usd < 0:
            raise ValueError("cost limit must be zero or positive")
        for label, value in (
            ("max_in_flight_chat", max_in_flight_chat),
            ("max_in_flight_run", max_in_flight_run),
            ("max_open_runs", max_open_runs),
            ("max_queued_jobs", max_queued_jobs),
            ("max_provider_inflight", max_provider_inflight),
        ):
            if value < 1:
                raise ValueError(f"{label} must be positive")
        with self.pool.connection() as conn:
            found = conn.execute("SELECT id FROM orgs WHERE id = %s", (org_id,)).fetchone()
            if found is None:
                raise KeyError(org_id)
            project = Project(
                id=new_id("prj"),
                org_id=org_id,
                name=name,
                request_limit=request_limit,
                cost_limit_usd=cost_limit_usd,
                max_in_flight_chat=max_in_flight_chat,
                max_in_flight_run=max_in_flight_run,
                max_open_runs=max_open_runs,
                max_queued_jobs=max_queued_jobs,
                max_provider_inflight=max_provider_inflight,
            )
            conn.execute(
                """
                INSERT INTO projects (
                    id, org_id, name, request_limit, request_count, cost_limit_usd,
                    max_in_flight_chat, max_in_flight_run, max_open_runs, max_queued_jobs,
                    max_provider_inflight
                ) VALUES (%s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s)
                """,
                (
                    project.id,
                    project.org_id,
                    project.name,
                    project.request_limit,
                    project.cost_limit_usd,
                    project.max_in_flight_chat,
                    project.max_in_flight_run,
                    project.max_open_runs,
                    project.max_queued_jobs,
                    project.max_provider_inflight,
                ),
            )
        return project

    def get_project(self, project_id: str) -> Project | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT id, org_id, name, request_limit, request_count, window_start_ms,
                       cost_limit_usd, spend_micros, reserved_micros, reserved_until_ms,
                       max_in_flight_chat, max_in_flight_run, max_open_runs, max_queued_jobs,
                       max_provider_inflight
                FROM projects WHERE id = %s
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return Project(
            id=row[0],
            org_id=row[1],
            name=row[2],
            request_limit=row[3],
            request_count=row[4],
            window_start_ms=row[5],
            cost_limit_usd=float(row[6] or 0),
            spend_micros=int(row[7] or 0),
            reserved_micros=int(row[8] or 0),
            reserved_until_ms=int(row[9] or 0),
            max_in_flight_chat=int(row[10] or 2),
            max_in_flight_run=int(row[11] or 2),
            max_open_runs=int(row[12] or 32),
            max_queued_jobs=int(row[13] or 64),
            max_provider_inflight=int(row[14] or 2),
        )

    def issue_key(
        self, project_id: str, role: str, groups: tuple[str, ...] = ()
    ) -> tuple[ApiKey, str]:
        import secrets

        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        if len(groups) > 32 or any(not group or len(group) > 64 for group in groups):
            raise ValueError("invalid groups")
        if self.get_project(project_id) is None:
            raise KeyError(project_id)
        raw = "sk_" + secrets.token_urlsafe(24)
        record = ApiKey(
            id=new_id("key"),
            project_id=project_id,
            key_hash=hash_secret(raw, self.pepper),
            role=role,
            groups=groups,
        )
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO api_keys (id, project_id, key_hash, role, groups, revoked)
                VALUES (%s, %s, %s, %s, %s, false)
                """,
                (record.id, record.project_id, record.key_hash, record.role, Json(list(groups))),
            )
            self._audit_conn(conn, project_id, "api_key.created", record.id, record.id)
        return record, raw

    def authenticate(self, raw: str) -> Principal | None:
        digest = hash_secret(raw, self.pepper)
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT k.id, k.project_id, k.role, k.groups, p.org_id
                FROM api_keys k
                JOIN projects p ON p.id = k.project_id
                WHERE k.key_hash = %s AND k.revoked = false
                """,
                (digest,),
            ).fetchone()
        if row is None:
            return None
        groups = tuple(row[3] or ())
        return Principal(row[4], row[1], row[0], row[2], groups)

    def keys_for(self, project_id: str) -> list[ApiKey]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, project_id, key_hash, role, revoked, groups
                FROM api_keys WHERE project_id = %s
                """,
                (project_id,),
            ).fetchall()
        return [
            ApiKey(
                id=row[0],
                project_id=row[1],
                key_hash=row[2],
                role=row[3],
                revoked=row[4],
                groups=tuple(row[5] or ()),
            )
            for row in rows
        ]

    def revoke(self, principal: Principal, key_id: str) -> None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE api_keys SET revoked = true
                WHERE id = %s AND project_id = %s
                RETURNING id
                """,
                (key_id, principal.project_id),
            ).fetchone()
            if row is None:
                raise KeyError(key_id)
            self._audit_conn(
                conn, principal.project_id, "api_key.revoked", principal.key_id, key_id
            )

    def consume_request(self, project_id: str, now_ms: int) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE projects
                SET window_start_ms = CASE
                        WHEN window_start_ms = 0 OR %s - window_start_ms >= %s THEN %s
                        ELSE window_start_ms
                    END,
                    request_count = CASE
                        WHEN window_start_ms = 0 OR %s - window_start_ms >= %s THEN 1
                        ELSE request_count + 1
                    END
                WHERE id = %s
                  AND request_limit > 0
                  AND (
                    window_start_ms = 0
                    OR %s - window_start_ms >= %s
                    OR request_count < request_limit
                  )
                RETURNING request_count
                """,
                (
                    now_ms,
                    WINDOW_MS,
                    now_ms,
                    now_ms,
                    WINDOW_MS,
                    project_id,
                    now_ms,
                    WINDOW_MS,
                ),
            ).fetchone()
        return row is not None

    def retry_after(self, project_id: str, now_ms: int) -> int:
        project = self.get_project(project_id)
        if project is None:
            return 1
        remain = WINDOW_MS - (now_ms - project.window_start_ms)
        return max(1, (remain + 999) // 1000)

    def project_limit(self, project_id: str) -> tuple[int, int]:
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        return project.request_limit, project.request_count

    def note_window(self, project_id: str, window_start_ms: int, count: int) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE projects
                SET window_start_ms = %s, request_count = %s
                WHERE id = %s
                  AND (
                    %s > window_start_ms
                    OR (%s = window_start_ms AND %s > request_count)
                  )
                """,
                (window_start_ms, count, project_id, window_start_ms, window_start_ms, count),
            )

    def cost_limit(self, project_id: str) -> float:
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        return project.cost_limit_usd

    def project_spend(self, project_id: str) -> float:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT spend_micros FROM projects WHERE id = %s",
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return int(row[0]) / 1_000_000

    def reserve_spend(self, project_id: str, ceiling_micros: int, now_ms: int = 0) -> int | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT cost_limit_usd, spend_micros, reserved_micros, reserved_until_ms
                FROM projects WHERE id = %s FOR UPDATE
                """,
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(project_id)
            limit = int(round(float(row[0]) * 1_000_000))
            if limit <= 0 or ceiling_micros <= 0:
                return 0
            room = limit - int(row[1]) - int(row[2])
            if room <= 0:
                return None
            hold = min(ceiling_micros, room)
            until = max(int(row[3] or 0), now_ms + RESERVE_TTL_MS) if now_ms else int(row[3] or 0)
            conn.execute(
                """
                UPDATE projects
                SET reserved_micros = reserved_micros + %s,
                    reserved_until_ms = %s
                WHERE id = %s
                """,
                (hold, until, project_id),
            )
        return hold

    def release_spend(self, project_id: str, held_micros: int) -> None:
        if held_micros <= 0:
            return
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE projects
                SET reserved_micros = GREATEST(reserved_micros - %s, 0),
                    reserved_until_ms = CASE
                        WHEN GREATEST(reserved_micros - %s, 0) = 0 THEN 0
                        ELSE reserved_until_ms
                    END
                WHERE id = %s
                """,
                (held_micros, held_micros, project_id),
            )

    def reconcile_reserved(self, now_ms: int) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE projects
                SET reserved_micros = 0, reserved_until_ms = 0
                WHERE reserved_micros > 0
                  AND reserved_until_ms > 0
                  AND reserved_until_ms <= %s
                """,
                (now_ms,),
            )
            return row.rowcount or 0

    def add_prompt(self, prompt: Prompt) -> Prompt:
        self._must_project_id(prompt.project_id)
        self.save_prompt(prompt)
        return prompt

    def save_prompt(self, prompt: Prompt) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO prompts (id, project_id, document) VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET document = EXCLUDED.document
                """,
                (prompt.id, prompt.project_id, Json(dump(prompt))),
            )

    def prompt_for(self, principal: Principal, prompt_id: str) -> Prompt:
        prompt = self.prompt_by_id(prompt_id)
        if prompt is None or prompt.project_id != principal.project_id:
            raise KeyError(prompt_id)
        return prompt

    def prompt_by_id(self, prompt_id: str) -> Prompt | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT document FROM prompts WHERE id = %s", (prompt_id,)
            ).fetchone()
        if row is None:
            return None
        return load_prompt(row[0])

    def add_model(self, card: ModelCard) -> ModelCard:
        self._must_project_id(card.project_id)
        if card.input_price < 0 or card.output_price < 0:
            raise ValueError("price must be zero or positive")
        if card.tier_affinity not in TIER_AFFINITIES:
            raise ValueError("tier_affinity must be any, free_only, or paid_only")
        if card.tier_order not in TIER_ORDERS:
            raise ValueError("tier_order must be free_paid_local or free_local_paid")
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO models (id, project_id, document) VALUES (%s, %s, %s)",
                (card.id, card.project_id, Json(dump(card))),
            )
        return card

    def model_for(self, principal: Principal, model_id: str) -> ModelCard:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT document FROM models WHERE id = %s AND project_id = %s",
                (model_id, principal.project_id),
            ).fetchone()
        if row is None:
            raise KeyError(model_id)
        return load_model(row[0])

    def model_by_name(self, principal: Principal, name: str) -> ModelCard | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT document FROM models
                WHERE project_id = %s AND document->>'name' = %s
                LIMIT 1
                """,
                (principal.project_id, name),
            ).fetchone()
        if row is None:
            return None
        return load_model(row[0])

    def model_ref(self, principal: Principal, ref: str) -> ModelCard | None:
        try:
            return self.model_for(principal, ref)
        except KeyError:
            return self.model_by_name(principal, ref)

    def set_route(self, route: Route) -> None:
        self._must_project_id(route.project_id)
        if len(route.fallback_model_ids) > MAX_ROUTE_FALLBACKS:
            raise ValueError("at most two fallback models")
        if route.tier_order and route.tier_order not in TIER_ORDERS:
            raise ValueError("tier_order must be free_paid_local or free_local_paid")
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO routes (project_id, task, document) VALUES (%s, %s, %s)
                ON CONFLICT (project_id, task) DO UPDATE SET document = EXCLUDED.document
                """,
                (route.project_id, route.task, Json(dump(route))),
            )

    def route_for(self, project_id: str, task: str) -> Route | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT document FROM routes WHERE project_id = %s AND task = %s",
                (project_id, task),
            ).fetchone()
        if row is None:
            return None
        return load_route(row[0])

    def add_agent(self, agent: AgentDef) -> AgentDef:
        self._must_project_id(agent.project_id)
        self.save_agent(agent)
        return agent

    def save_agent(self, agent: AgentDef) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO agents (id, project_id, document) VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET document = EXCLUDED.document
                """,
                (agent.id, agent.project_id, Json(dump(agent))),
            )

    def agent_for(self, principal: Principal, agent_id: str) -> AgentDef:
        agent = self.agent_by_id(agent_id)
        if agent is None or agent.project_id != principal.project_id:
            raise KeyError(agent_id)
        return agent

    def agent_by_id(self, agent_id: str) -> AgentDef | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT document FROM agents WHERE id = %s", (agent_id,)).fetchone()
        if row is None:
            return None
        return load_agent(row[0])

    def record_usage(self, row: Usage, release_held: int = 0) -> None:
        micros = int(round(row.cost * 1_000_000))
        tokens = row.input_tokens + row.output_tokens
        failed = 0 if row.status == USAGE_SUCCESS else 1
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO usage_rows (project_id, document) VALUES (%s, %s)",
                (row.project_id, Json(dump(row))),
            )
            if row.status == USAGE_SUCCESS or release_held > 0:
                # Charge is clamped to room after releasing the hold so the USD cap holds.
                conn.execute(
                    """
                    UPDATE projects
                    SET spend_micros = spend_micros + CASE
                            WHEN %s = 0 THEN 0
                            WHEN cost_limit_usd <= 0 THEN %s
                            ELSE LEAST(
                                %s::bigint,
                                GREATEST(
                                    0,
                                    ROUND(cost_limit_usd * 1000000)::bigint
                                      - spend_micros
                                      - GREATEST(reserved_micros - %s, 0)
                                )
                            )
                        END,
                        reserved_micros = GREATEST(reserved_micros - %s, 0),
                        reserved_until_ms = CASE
                            WHEN GREATEST(reserved_micros - %s, 0) = 0 THEN 0
                            ELSE reserved_until_ms
                        END
                    WHERE id = %s
                    """,
                    (
                        1 if row.status == USAGE_SUCCESS else 0,
                        micros,
                        micros,
                        release_held,
                        release_held,
                        release_held,
                        row.project_id,
                    ),
                )
            conn.execute(
                """
                UPDATE usage_counters
                SET tokens = tokens + %s,
                    cost_micros = cost_micros + %s,
                    errors = errors + %s
                WHERE id = 1
                """,
                (tokens, micros if row.status == USAGE_SUCCESS else 0, failed),
            )

    def usage_totals(self) -> tuple[int, float, int]:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT tokens, cost_micros, errors FROM usage_counters WHERE id = 1"
            ).fetchone()
        if row is None:
            return 0, 0.0, 0
        return int(row[0]), int(row[1]) / 1_000_000, int(row[2])

    def project_usage(self, principal: Principal, limit: int = 100, offset: int = 0) -> list[Usage]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT document FROM usage_rows
                WHERE project_id = %s ORDER BY id LIMIT %s OFFSET %s
                """,
                (principal.project_id, limit, offset),
            ).fetchall()
        return [load_usage(row[0]) for row in rows]

    def project_usage_recent(self, principal: Principal, limit: int) -> list[Usage]:
        """Newest `limit` rows for cost/summary scans. Returned oldest-first."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT document FROM (
                    SELECT id, document FROM usage_rows
                    WHERE project_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                ) recent
                ORDER BY id
                """,
                (principal.project_id, limit),
            ).fetchall()
        return [load_usage(row[0]) for row in rows]

    def usage_page(
        self, principal: Principal, limit: int, after_id: int
    ) -> tuple[list[Usage], int]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, document FROM usage_rows
                WHERE project_id = %s AND id > %s
                ORDER BY id
                LIMIT %s
                """,
                (principal.project_id, after_id, limit + 1),
            ).fetchall()
        page = rows[:limit]
        next_after = int(page[-1][0]) if len(rows) > limit and page else 0
        return [load_usage(row[1]) for row in page], next_after

    def all_usage(self) -> list[Usage]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT document FROM usage_rows ORDER BY id").fetchall()
        return [load_usage(row[0]) for row in rows]

    def insert_run(self, run: RunRecord) -> RunRecord:
        try:
            with self.pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO runs (
                        id, project_id, status, idempotency_key, lease_ms, deadline_ms, document
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run.id,
                        run.project_id,
                        run.status,
                        run.idempotency_key,
                        run.lease_ms,
                        run.deadline_ms,
                        Json(dump(run)),
                    ),
                )
        except UniqueViolation:
            existing = self.run_by_idem(run.project_id, run.idempotency_key)
            if existing is None:
                raise
            return existing
        return run

    def save_run(
        self,
        run: RunRecord,
        held_lease: int | None = None,
        effect: tuple[str, str, dict] | None = None,
        inserted: list[bool] | None = None,
    ) -> bool:
        with self.pool.connection() as conn:
            current = conn.execute(
                "SELECT status, lease_ms FROM runs WHERE id = %s FOR UPDATE",
                (run.id,),
            ).fetchone()
            if current is None:
                return False
            status, lease = current
            expected = lease if held_lease is None else held_lease
            if lease != expected or (status == RUN_CANCELLED and run.status != RUN_CANCELLED):
                return False
            if effect is not None:
                project_id, idem_key, result = effect
                wrote = conn.execute(
                    """
                    INSERT INTO tool_effects (idem_key, project_id, result)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (idem_key) DO NOTHING
                    RETURNING idem_key
                    """,
                    (idem_key, project_id, Json(result)),
                ).fetchone()
                if wrote is not None and inserted is not None:
                    inserted.append(True)
            conn.execute(
                """
                UPDATE runs
                SET status = %s,
                    idempotency_key = %s,
                    lease_ms = %s,
                    deadline_ms = %s,
                    document = %s
                WHERE id = %s
                """,
                (
                    run.status,
                    run.idempotency_key,
                    run.lease_ms,
                    run.deadline_ms,
                    Json(dump(run)),
                    run.id,
                ),
            )
        return True

    def renew_lease(self, run_id: str, held: int, new_lease: int) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE runs
                SET lease_ms = %s,
                    document = jsonb_set(document, '{lease_ms}', to_jsonb(%s::bigint))
                WHERE id = %s AND lease_ms = %s
                RETURNING id
                """,
                (new_lease, new_lease, run_id, held),
            ).fetchone()
        return row is not None

    def lease_of(self, run_id: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT lease_ms FROM runs WHERE id = %s", (run_id,)).fetchone()
        if row is None:
            return 0
        return int(row[0])

    def effect(self, idem_key: str) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT result FROM tool_effects WHERE idem_key = %s",
                (idem_key,),
            ).fetchone()
        if row is None:
            return None
        return dict(row[0])

    def claim_chat(
        self, project_id: str, key: str, fingerprint: str, now_ms: int
    ) -> tuple[str, bool] | None:
        stale = now_ms - WINDOW_MS
        with self.pool.connection() as conn:
            wrote = conn.execute(
                """
                INSERT INTO chat_replays (
                    project_id, idempotency_key, fingerprint, claimed_ms
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (project_id, idempotency_key) DO NOTHING
                RETURNING idempotency_key
                """,
                (project_id, key, fingerprint, now_ms),
            ).fetchone()
            if wrote is not None:
                return None
            row = conn.execute(
                """
                SELECT fingerprint, text, finish, ready, claimed_ms
                FROM chat_replays
                WHERE project_id = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (project_id, key),
            ).fetchone()
            if row is None:
                raise IdempotencyPending()
            if row[0] != fingerprint:
                raise IdempotencyConflict()
            if row[3]:
                return str(row[1]), bool(row[2])
            if int(row[4]) > stale:
                raise IdempotencyPending()
            conn.execute(
                """
                UPDATE chat_replays
                SET fingerprint = %s, text = '', finish = false, ready = false, claimed_ms = %s
                WHERE project_id = %s AND idempotency_key = %s
                """,
                (fingerprint, now_ms, project_id, key),
            )
        return None

    def finish_chat(self, project_id: str, key: str, text: str, finish: bool) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE chat_replays
                SET text = %s, finish = %s, ready = true
                WHERE project_id = %s AND idempotency_key = %s
                """,
                (text, finish, project_id, key),
            )

    def drop_chat(self, project_id: str, key: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                DELETE FROM chat_replays
                WHERE project_id = %s AND idempotency_key = %s AND ready = false
                """,
                (project_id, key),
            )

    def park_async_chat(
        self,
        project_id: str,
        key: str,
        fingerprint: str,
        request: dict,
        now_ms: int,
    ) -> tuple[str, bool] | None:
        del now_ms
        with self.pool.connection() as conn:
            wrote = conn.execute(
                """
                INSERT INTO chat_replays (
                    project_id, idempotency_key, fingerprint, claimed_ms,
                    async_pending, request
                ) VALUES (%s, %s, %s, 0, true, %s)
                ON CONFLICT (project_id, idempotency_key) DO NOTHING
                RETURNING idempotency_key
                """,
                (project_id, key, fingerprint, Json(request)),
            ).fetchone()
            if wrote is not None:
                return None
            row = conn.execute(
                """
                SELECT fingerprint, text, finish, ready, async_pending
                FROM chat_replays
                WHERE project_id = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (project_id, key),
            ).fetchone()
            if row is None:
                raise IdempotencyPending()
            if row[0] != fingerprint:
                raise IdempotencyConflict()
            if row[3]:
                return str(row[1]), bool(row[2])
            if row[4]:
                return None
            raise IdempotencyPending()

    def claim_async_chat(self, now_ms: int) -> dict | None:
        until = now_ms + RESERVE_TTL_MS
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE chat_replays
                SET claimed_ms = %s
                WHERE (project_id, idempotency_key) = (
                    SELECT project_id, idempotency_key FROM chat_replays
                    WHERE async_pending = true AND ready = false AND claimed_ms <= %s
                    ORDER BY (
                        SELECT COUNT(*) FROM chat_replays r
                        WHERE r.project_id = chat_replays.project_id
                          AND r.async_pending = true
                          AND r.ready = false
                          AND r.claimed_ms > %s
                    ), claimed_ms, idempotency_key
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING project_id, idempotency_key, fingerprint, request
                """,
                (until, now_ms, now_ms),
            ).fetchone()
        if row is None:
            return None
        return {
            "project_id": row[0],
            "idempotency_key": row[1],
            "fingerprint": row[2],
            "request": dict(row[3] or {}),
        }

    def chat_status(self, project_id: str, key: str) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT ready, text, finish, async_pending
                FROM chat_replays
                WHERE project_id = %s AND idempotency_key = %s
                """,
                (project_id, key),
            ).fetchone()
        if row is None:
            return None
        return {
            "ready": bool(row[0]),
            "text": str(row[1] or ""),
            "finish": bool(row[2]),
            "async_pending": bool(row[3]),
        }

    def put_effect(self, project_id: str, idem_key: str, result: dict) -> tuple[dict, bool]:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO tool_effects (idem_key, project_id, result)
                VALUES (%s, %s, %s)
                ON CONFLICT (idem_key) DO NOTHING
                RETURNING result
                """,
                (idem_key, project_id, Json(result)),
            ).fetchone()
            if row is not None:
                return dict(row[0]), True
            stored = conn.execute(
                "SELECT result FROM tool_effects WHERE idem_key = %s",
                (idem_key,),
            ).fetchone()
        return dict(stored[0]), False

    def run_for(self, principal: Principal, run_id: str) -> RunRecord:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT document FROM runs WHERE id = %s AND project_id = %s",
                (run_id, principal.project_id),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return load_run(row[0])

    def run_by_idem(self, project_id: str, key: str) -> RunRecord | None:
        if not key:
            return None
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT document FROM runs
                WHERE project_id = %s AND idempotency_key = %s
                """,
                (project_id, key),
            ).fetchone()
        if row is None:
            return None
        return load_run(row[0])

    def open_runs(self) -> list[RunRecord]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT document FROM runs WHERE status = ANY(%s)",
                (list(OPEN_RUN),),
            ).fetchall()
        return [load_run(row[0]) for row in rows]

    def open_run_count(self, project_id: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM runs
                WHERE project_id = %s AND status = ANY(%s)
                """,
                (project_id, list(OPEN_RUN)),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def deadline_due(self, now: int, limit: int = 32) -> list[RunRecord]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT document FROM runs
                WHERE status = ANY(%s) AND deadline_ms > 0 AND deadline_ms <= %s
                ORDER BY deadline_ms
                LIMIT %s
                """,
                (list(OPEN_RUN), now, limit),
            ).fetchall()
        return [load_run(row[0]) for row in rows]

    def claim_due(self, now: int, lease_for: int, limit: int = 16) -> list[RunRecord]:
        """Claim at most one due run per project. SKIP LOCKED for multi-worker safety."""
        until = now + lease_for
        claimed: list[RunRecord] = []
        skipped: list[str] = []
        for _ in range(max(1, limit)):
            with self.pool.connection() as conn:
                row = conn.execute(
                    """
                    UPDATE runs
                    SET lease_ms = %s,
                        document = jsonb_set(document, '{lease_ms}', to_jsonb(%s::bigint))
                    WHERE id = (
                        SELECT id FROM runs
                        WHERE status IN ('queued', 'running')
                          AND lease_ms <= %s
                          AND (deadline_ms = 0 OR deadline_ms > %s)
                          AND NOT (project_id = ANY(%s))
                        ORDER BY lease_ms, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING document, project_id
                    """,
                    (until, until, now, now, skipped),
                ).fetchone()
            if row is None:
                break
            run = load_run(row[0])
            run.lease_ms = until
            claimed.append(run)
            skipped.append(str(row[1]))
        return claimed

    def claim_run(self, run_id: str, now: int, lease_for: int) -> bool:
        until = now + lease_for
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE runs
                SET lease_ms = %s,
                    document = jsonb_set(document, '{lease_ms}', to_jsonb(%s::bigint))
                WHERE id = %s AND status = ANY(%s) AND lease_ms <= %s
                RETURNING id
                """,
                (until, until, run_id, list(OPEN_RUN), now),
            ).fetchone()
        return row is not None

    def cancel_run(self, project_id: str, run_id: str, finished_ms: int = 0) -> RunRecord | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT document FROM runs
                WHERE id = %s AND project_id = %s
                FOR UPDATE
                """,
                (run_id, project_id),
            ).fetchone()
            if row is None:
                return None
            run = load_run(row[0])
            if run.status not in OPEN_RUN:
                return run
            run.status = RUN_CANCELLED
            run.outcome = OUTCOME_CANCELLED
            run.lease_ms = 0
            if finished_ms and run.finished_ms == 0:
                run.finished_ms = finished_ms
            run.events.append({"name": RUN_CANCELLED, "step": run.steps})
            conn.execute(
                """
                UPDATE runs
                SET status = 'cancelled', lease_ms = 0, document = %s
                WHERE id = %s
                """,
                (Json(dump(run)), run_id),
            )
        return run

    def plaintext_secrets(self) -> list[str]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT key_hash FROM api_keys").fetchall()
        return [row[0] for row in rows]

    def insert_eval(self, record: dict) -> dict:
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO evals (id, project_id, status, lease_ms, document)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    record["id"],
                    record["project_id"],
                    record["status"],
                    int(record.get("lease_ms") or 0),
                    Json(record),
                ),
            )
        return dict(record)

    def eval_for(self, principal: Principal, eval_id: str) -> dict:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT document FROM evals
                WHERE id = %s AND project_id = %s
                """,
                (eval_id, principal.project_id),
            ).fetchone()
        if row is None:
            raise KeyError(eval_id)
        return dict(row[0])

    def save_eval(self, record: dict) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE evals
                SET status = %s, lease_ms = %s, document = %s
                WHERE id = %s
                """,
                (
                    record["status"],
                    int(record.get("lease_ms") or 0),
                    Json(record),
                    record["id"],
                ),
            )

    def claim_eval(self, now_ms: int) -> dict | None:
        until = now_ms + RESERVE_TTL_MS
        stale = now_ms
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE evals
                SET status = 'running',
                    lease_ms = %s,
                    document = jsonb_set(
                        jsonb_set(document, '{status}', '"running"'),
                        '{lease_ms}', to_jsonb(%s::bigint)
                    )
                WHERE id = (
                    SELECT e.id FROM evals e
                    WHERE e.status = 'queued'
                       OR (e.status = 'running' AND e.lease_ms <= %s)
                    ORDER BY (
                        SELECT COUNT(*) FROM evals r
                        WHERE r.project_id = e.project_id
                          AND r.status = 'running'
                          AND r.lease_ms > %s
                    ), e.lease_ms, e.id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING document
                """,
                (until, until, stale, stale),
            ).fetchone()
        if row is None:
            return None
        found = dict(row[0])
        found["status"] = EVAL_RUNNING
        found["lease_ms"] = until
        return found

    def _must_project_id(self, project_id: str) -> None:
        if self.get_project(project_id) is None:
            raise KeyError(project_id)

    def _audit_conn(self, conn, project_id: str, action: str, actor: str, ref: str) -> None:
        conn.execute(
            "INSERT INTO audit (project_id, action, actor, ref) VALUES (%s, %s, %s, %s)",
            (project_id, action, actor, ref),
        )
        self.audit.append(Audit(project_id, action, actor, ref))
