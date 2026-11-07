"""Fixed system vocabulary.

Values are plain ``str`` on the wire and in Postgres. Membership checks use these
sets. Do not replace dynamic values (ids, free text, model names) with a set.
"""

from __future__ import annotations

# --- roles -------------------------------------------------------------------

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_DEVELOPER = "developer"
ROLE_VIEWER = "viewer"
ROLES = frozenset({ROLE_OWNER, ROLE_ADMIN, ROLE_DEVELOPER, ROLE_VIEWER})

# --- run status --------------------------------------------------------------

RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_WAITING_APPROVAL = "waiting_approval"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_DENIED = "denied"
RUN_CANCELLED = "cancelled"
OPEN_RUN = frozenset({RUN_QUEUED, RUN_RUNNING, RUN_WAITING_APPROVAL})
TERMINAL = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_DENIED, RUN_CANCELLED})
RUN_STATUSES = OPEN_RUN | TERMINAL
RUN_CLAIMABLE = frozenset({RUN_QUEUED, RUN_RUNNING})
RUN_FINISHABLE = frozenset({RUN_RUNNING, RUN_COMPLETED})

# --- outcomes ----------------------------------------------------------------

OUTCOME_COMPLETED = "completed"
OUTCOME_NO_ANSWER = "no_answer"
OUTCOME_UNGROUNDED = "ungrounded"
OUTCOME_UNVERIFIED = "unverified"
OUTCOME_WAITING_APPROVAL = "waiting_approval"
OUTCOME_REFUSED = "refused"
OUTCOME_DENIED = "denied"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"
OUTCOMES = frozenset(
    {
        OUTCOME_COMPLETED,
        OUTCOME_NO_ANSWER,
        OUTCOME_UNGROUNDED,
        OUTCOME_UNVERIFIED,
        OUTCOME_WAITING_APPROVAL,
        OUTCOME_REFUSED,
        OUTCOME_DENIED,
        OUTCOME_FAILED,
        OUTCOME_CANCELLED,
    }
)

# --- publish (prompt / agent) ------------------------------------------------

PUBLISH_DRAFT = "draft"
PUBLISH_PUBLISHED = "published"
PUBLISH_STATUSES = frozenset({PUBLISH_DRAFT, PUBLISH_PUBLISHED})

# --- agent mode / pattern ----------------------------------------------------

MODE_WORKFLOW = "workflow"
MODE_AGENT = "agent"
MODE_SUPERVISOR = "supervisor"
MODES = frozenset({MODE_WORKFLOW, MODE_AGENT, MODE_SUPERVISOR})

PATTERN_NONE = ""
PATTERN_SUPERVISOR = "supervisor"
PATTERN_PARALLEL = "parallel"
PATTERN_HANDOFF = "handoff"
PATTERN_PIPELINE = "pipeline"
PATTERNS = frozenset(
    {
        PATTERN_NONE,
        PATTERN_SUPERVISOR,
        PATTERN_PARALLEL,
        PATTERN_HANDOFF,
        PATTERN_PIPELINE,
    }
)

# --- ingest jobs -------------------------------------------------------------

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_INDEXED = "indexed"
JOB_REJECTED = "rejected"
JOB_CANCELLED = "cancelled"
JOB_DEAD = "dead"
JOB_STATUSES = frozenset(
    {
        JOB_QUEUED,
        JOB_RUNNING,
        JOB_INDEXED,
        JOB_REJECTED,
        JOB_CANCELLED,
        JOB_DEAD,
    }
)
JOB_VERSION_HELD = frozenset({JOB_QUEUED, JOB_RUNNING, JOB_INDEXED})

# --- evals -------------------------------------------------------------------

EVAL_QUEUED = "queued"
EVAL_RUNNING = "running"
EVAL_COMPLETED = "completed"
EVAL_FAILED = "failed"
EVAL_STATUSES = frozenset({EVAL_QUEUED, EVAL_RUNNING, EVAL_COMPLETED, EVAL_FAILED})

# --- usage rows --------------------------------------------------------------

USAGE_SUCCESS = "success"
USAGE_FAILED = "failed"
USAGE_STATUSES = frozenset({USAGE_SUCCESS, USAGE_FAILED})

# --- provider credentials ----------------------------------------------------

TIER_FREE = "free"
TIER_PAID = "paid"
TIER_LOCAL = "local"
CREDENTIAL_TIERS = frozenset({TIER_FREE, TIER_PAID, TIER_LOCAL})

CREDENTIAL_ACTIVE = "active"
CREDENTIAL_DISABLED = "disabled"
CREDENTIAL_STATUSES = frozenset({CREDENTIAL_ACTIVE, CREDENTIAL_DISABLED})

LIMIT_UNIT_REQUESTS = "requests"
LIMIT_UNIT_TOKENS = "tokens"
LIMIT_UNIT_SPEND = "spend_usd"
LIMIT_UNITS = frozenset({LIMIT_UNIT_REQUESTS, LIMIT_UNIT_TOKENS, LIMIT_UNIT_SPEND})

LIMIT_WINDOW_CALENDAR = "calendar"
LIMIT_WINDOW_ROLLING = "rolling"
LIMIT_WINDOWS = frozenset({LIMIT_WINDOW_CALENDAR, LIMIT_WINDOW_ROLLING})

AFFINITY_ANY = "any"
AFFINITY_FREE_ONLY = "free_only"
AFFINITY_PAID_ONLY = "paid_only"
TIER_AFFINITIES = frozenset({AFFINITY_ANY, AFFINITY_FREE_ONLY, AFFINITY_PAID_ONLY})

# Credential pick order when affinity is any. Route.tier_order may override the card.
TIER_ORDER_FREE_PAID_LOCAL = "free_paid_local"
TIER_ORDER_FREE_LOCAL_PAID = "free_local_paid"
TIER_ORDER_DEFAULT = TIER_ORDER_FREE_PAID_LOCAL
TIER_ORDERS = frozenset({TIER_ORDER_FREE_PAID_LOCAL, TIER_ORDER_FREE_LOCAL_PAID})

# Primary + at most this many fallback models on one route.
MAX_ROUTE_FALLBACKS = 2

# --- tools -------------------------------------------------------------------

TOOL_OK = "ok"
TOOL_DENIED = "denied"
TOOL_WAITING_APPROVAL = "waiting_approval"
TOOL_RESULTS = frozenset({TOOL_OK, TOOL_DENIED, TOOL_WAITING_APPROVAL})

RISK_READ = "read"
RISK_WRITE = "write"
TOOL_RISKS = frozenset({RISK_READ, RISK_WRITE})

# --- chunks ------------------------------------------------------------------

CHUNK_PARENT = "parent"
CHUNK_CHILD = "child"
CHUNK_KINDS = frozenset({CHUNK_PARENT, CHUNK_CHILD})
