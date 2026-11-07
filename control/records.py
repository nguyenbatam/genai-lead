"""Identity and configuration records. Plain data."""

from __future__ import annotations

from dataclasses import dataclass, field

from control.vocab import (
    AFFINITY_ANY,
    CREDENTIAL_ACTIVE,
    LIMIT_WINDOW_CALENDAR,
    TIER_ORDER_DEFAULT,
)


@dataclass(frozen=True)
class Principal:
    org_id: str
    project_id: str
    key_id: str
    role: str
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class Budget:
    max_steps: int
    max_tokens: int
    max_cost_usd: float
    max_wall_ms: int
    max_handoffs: int
    max_parallel: int


@dataclass(frozen=True)
class Member:
    id: str
    model_profile: str
    allowed_tools: tuple[str, ...]
    max_turns: int


@dataclass
class Org:
    id: str
    name: str


@dataclass
class Project:
    id: str
    org_id: str
    name: str
    request_limit: int = 1_000
    request_count: int = 0
    window_start_ms: int = 0
    cost_limit_usd: float = 0.0
    spend_micros: int = 0
    reserved_micros: int = 0
    reserved_until_ms: int = 0
    max_in_flight_chat: int = 2
    max_in_flight_run: int = 2
    max_open_runs: int = 32
    max_queued_jobs: int = 64
    max_provider_inflight: int = 2


@dataclass
class ApiKey:
    id: str
    project_id: str
    key_hash: str
    role: str
    revoked: bool = False
    groups: tuple[str, ...] = ()


@dataclass
class Prompt:
    id: str
    project_id: str
    name: str
    version: int
    template: str
    status: str
    eval_passed: bool = False


@dataclass
class ModelCard:
    id: str
    project_id: str
    name: str
    profile: str
    capabilities: tuple[str, ...]
    input_price: float
    output_price: float
    pricing_version: str
    provider: str = ""
    tier_affinity: str = AFFINITY_ANY
    tier_order: str = TIER_ORDER_DEFAULT


@dataclass(frozen=True)
class LimitPolicy:
    """One usage window on a provider credential. Several may stack on one key."""

    period: str
    unit: str
    max: float
    window: str = LIMIT_WINDOW_CALENDAR


@dataclass
class ProviderCredential:
    """Platform secret for calling a provider. Never logged; never sent to the agent."""

    id: str
    provider: str
    tier: str
    api_key: str
    base_url: str = ""
    weight: int = 1
    status: str = CREDENTIAL_ACTIVE
    models_allow: tuple[str, ...] = ("*",)
    limits: tuple[LimitPolicy, ...] = ()
    cooldown_from_header: bool = True
    default_cooldown_s: int = 60
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Route:
    project_id: str
    task: str
    model_id: str
    fallback_model_ids: tuple[str, ...] = ()
    tier_order: str = ""

    @property
    def fallback_model_id(self) -> str | None:
        """First fallback, if any. Prefer fallback_model_ids."""
        return self.fallback_model_ids[0] if self.fallback_model_ids else None


@dataclass
class AgentDef:
    id: str
    project_id: str
    name: str
    version: int
    status: str
    mode: str
    pattern: str
    prompt_id: str
    model_profile: str
    allowed_tools: tuple[str, ...]
    members: tuple[Member, ...]
    entrypoint: str
    steps: tuple[str, ...]
    budget: Budget
    kb_id: str = ""
    # Optional: agent may hint a model id/name from this allow-list for the next call.
    allowed_models: tuple[str, ...] = ()
    prefer_cheap: bool = True
    max_hint_calls: int = 1
    max_hint_cost_usd: float = 0.0
    hint_canary_percent: int = 100


@dataclass
class Usage:
    project_id: str
    model_id: str
    model_name: str
    pricing_version: str
    input_tokens: int
    output_tokens: int
    cost: float
    status: str
    fallback_from: str | None = None
    org_id: str = ""
    latency_ms: int = 0
    credential_id: str = ""
    tier: str = ""


@dataclass
class Audit:
    project_id: str
    action: str
    actor: str
    ref: str


@dataclass
class RunRecord:
    id: str
    project_id: str
    agent_id: str
    status: str
    outcome: str
    answer: str
    citations: list[str] = field(default_factory=list)
    steps: int = 0
    handoffs: int = 0
    tokens: int = 0
    cost: float = 0.0
    started_ms: int = 0
    finished_ms: int = 0
    cursor: int = 0
    current: str = ""
    previous: str = ""
    tool_calls: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    passages: dict[str, str] = field(default_factory=dict)
    pending_payload: str = ""
    pending_tool: str = ""
    pending_args: dict = field(default_factory=dict)
    approved: list[str] = field(default_factory=list)
    groups: tuple[str, ...] = ()
    query: str = ""
    applied_keys: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    member_turns: dict[str, int] = field(default_factory=dict)
    failure: str = ""
    lease_ms: int = 0
    idempotency_key: str = ""
    deadline_ms: int = 0
    staged: dict | None = None
    pending_calls: list[dict] = field(default_factory=list)
    calls_queued: bool = False
    fan_in: dict[str, dict] = field(default_factory=dict)
    hint_calls: int = 0
    hint_cost: float = 0.0
    next_model_hint: str = ""
