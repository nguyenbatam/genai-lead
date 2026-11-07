"""In-memory control plane. Every read takes the caller's project."""

from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
import threading
import uuid

from control.errors import IdempotencyConflict, IdempotencyPending
from control.fairness import fair_take
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
from control.vocab import (
    EVAL_QUEUED,
    EVAL_RUNNING,
    MAX_ROUTE_FALLBACKS,
    OPEN_RUN,
    OUTCOME_CANCELLED,
    ROLES,
    RUN_CANCELLED,
    RUN_CLAIMABLE,
    TERMINAL,
    TIER_AFFINITIES,
    TIER_ORDERS,
    USAGE_SUCCESS,
)

# Re-export for call sites that historically imported these from control.store.
__all__ = [
    "OPEN_RUN",
    "RESERVE_TTL_MS",
    "ROLES",
    "TERMINAL",
    "WINDOW_MS",
    "Store",
    "hash_secret",
    "new_id",
]

WINDOW_MS = 60_000


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def hash_secret(raw: str, pepper: str = "") -> str:
    if pepper:
        return hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return hashlib.sha256(raw.encode()).hexdigest()


def _clamped_charge(project: Project, micros: int, release_held: int) -> int:
    """Spend added after releasing `release_held`, never past the USD cap."""
    limit = int(round(project.cost_limit_usd * 1_000_000))
    if limit <= 0 or micros <= 0:
        return micros
    reserved = max(0, project.reserved_micros - max(0, release_held))
    room = limit - project.spend_micros - reserved
    return max(0, min(micros, room))


RESERVE_TTL_MS = 120_000


class Store:
    def __init__(self, pepper: str = "") -> None:
        self.pepper = pepper
        self.orgs: dict[str, Org] = {}
        self.projects: dict[str, Project] = {}
        self.keys: dict[str, ApiKey] = {}
        self.prompts: dict[str, Prompt] = {}
        self.models: dict[str, ModelCard] = {}
        self.routes: list[Route] = []
        self.agents: dict[str, AgentDef] = {}
        self.usage: list[Usage] = []
        self.audit: list[Audit] = []
        self.runs: dict[str, RunRecord] = {}
        self.effects: dict[str, dict] = {}
        self.chats: dict[tuple[str, str], dict] = {}
        self.evals: dict[str, dict] = {}
        self.tokens_total = 0
        self.cost_micros = 0
        self.errors_total = 0
        self._by_hash: dict[str, str] = {}
        self._lease: dict[str, int] = {}
        self._lock = threading.RLock()

    def ping(self) -> bool:
        return True

    def create_org(self, name: str) -> Org:
        org = Org(id=new_id("org"), name=name)
        self.orgs[org.id] = org
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
        if org_id not in self.orgs:
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
        self.projects[project.id] = project
        return project

    def get_project(self, project_id: str) -> Project | None:
        return self.projects.get(project_id)

    def issue_key(
        self, project_id: str, role: str, groups: tuple[str, ...] = ()
    ) -> tuple[ApiKey, str]:
        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        if len(groups) > 32 or any(not group or len(group) > 64 for group in groups):
            raise ValueError("invalid groups")
        if project_id not in self.projects:
            raise KeyError(project_id)
        raw = "sk_" + secrets.token_urlsafe(24)
        record = ApiKey(
            id=new_id("key"),
            project_id=project_id,
            key_hash=hash_secret(raw, self.pepper),
            role=role,
            groups=groups,
        )
        with self._lock:
            self.keys[record.id] = record
            self._by_hash[record.key_hash] = record.id
        self._audit(project_id, "api_key.created", record.id, record.id)
        return record, raw

    def authenticate(self, raw: str) -> Principal | None:
        digest = hash_secret(raw, self.pepper)
        key_id = self._by_hash.get(digest)
        if key_id is None:
            return None
        record = self.keys.get(key_id)
        if record is None or record.revoked or record.key_hash != digest:
            return None
        project = self.projects[record.project_id]
        return Principal(project.org_id, project.id, record.id, record.role, record.groups)

    def keys_for(self, project_id: str) -> list[ApiKey]:
        return [key for key in self.keys.values() if key.project_id == project_id]

    def revoke(self, principal: Principal, key_id: str) -> None:
        record = self._owned_key(principal, key_id)
        record.revoked = True
        self._audit(principal.project_id, "api_key.revoked", principal.key_id, key_id)

    def consume_request(self, project_id: str, now_ms: int) -> bool:
        with self._lock:
            project = self.projects[project_id]
            if _window_elapsed(project.window_start_ms, now_ms):
                project.window_start_ms = now_ms
                project.request_count = 0
            if project.request_limit < 1 or project.request_count >= project.request_limit:
                return False
            project.request_count += 1
            return True

    def retry_after(self, project_id: str, now_ms: int) -> int:
        project = self.projects[project_id]
        remain = WINDOW_MS - (now_ms - project.window_start_ms)
        return max(1, (remain + 999) // 1000)

    def project_limit(self, project_id: str) -> tuple[int, int]:
        project = self.projects[project_id]
        return project.request_limit, project.request_count

    def note_window(self, project_id: str, window_start_ms: int, count: int) -> None:
        """Keep the newest window and, within it, the highest count seen."""
        with self._lock:
            project = self.projects[project_id]
            if window_start_ms < project.window_start_ms:
                return
            if window_start_ms == project.window_start_ms and count <= project.request_count:
                return
            project.window_start_ms = window_start_ms
            project.request_count = count

    def cost_limit(self, project_id: str) -> float:
        return self.projects[project_id].cost_limit_usd

    def project_spend(self, project_id: str) -> float:
        return self.projects[project_id].spend_micros / 1_000_000

    def reserve_spend(self, project_id: str, ceiling_micros: int, now_ms: int = 0) -> int | None:
        """Hold spend against the cap. None means the remainder is already held.

        A limit of zero, or a free model, holds nothing. A ceiling larger than the
        remainder holds the remainder, so one call can still start and a second cannot.
        """
        with self._lock:
            project = self.projects[project_id]
            limit = int(round(project.cost_limit_usd * 1_000_000))
            if limit <= 0 or ceiling_micros <= 0:
                return 0
            room = limit - project.spend_micros - project.reserved_micros
            if room <= 0:
                return None
            hold = min(ceiling_micros, room)
            project.reserved_micros += hold
            if now_ms:
                project.reserved_until_ms = max(project.reserved_until_ms, now_ms + RESERVE_TTL_MS)
            return hold

    def release_spend(self, project_id: str, held_micros: int) -> None:
        if held_micros <= 0:
            return
        with self._lock:
            project = self.projects[project_id]
            project.reserved_micros = max(0, project.reserved_micros - held_micros)
            if project.reserved_micros == 0:
                project.reserved_until_ms = 0

    def reconcile_reserved(self, now_ms: int) -> int:
        """Drop holds whose TTL expired after a crash. Safe under the spend lock."""
        changed = 0
        with self._lock:
            for project in self.projects.values():
                if project.reserved_micros <= 0:
                    continue
                if project.reserved_until_ms and project.reserved_until_ms <= now_ms:
                    project.reserved_micros = 0
                    project.reserved_until_ms = 0
                    changed += 1
        return changed

    def add_prompt(self, prompt: Prompt) -> Prompt:
        self._must_project(prompt.project_id)
        self.prompts[prompt.id] = prompt
        return prompt

    def prompt_for(self, principal: Principal, prompt_id: str) -> Prompt:
        prompt = self.prompts[prompt_id]
        if prompt.project_id != principal.project_id:
            raise KeyError(prompt_id)
        return prompt

    def prompt_by_id(self, prompt_id: str) -> Prompt | None:
        return self.prompts.get(prompt_id)

    def save_prompt(self, prompt: Prompt) -> None:
        self.prompts[prompt.id] = prompt

    def add_model(self, card: ModelCard) -> ModelCard:
        self._must_project(card.project_id)
        if card.input_price < 0 or card.output_price < 0:
            raise ValueError("price must be zero or positive")
        if card.tier_affinity not in TIER_AFFINITIES:
            raise ValueError("tier_affinity must be any, free_only, or paid_only")
        if card.tier_order not in TIER_ORDERS:
            raise ValueError("tier_order must be free_paid_local or free_local_paid")
        self.models[card.id] = card
        return card

    def model_for(self, principal: Principal, model_id: str) -> ModelCard:
        card = self.models[model_id]
        if card.project_id != principal.project_id:
            raise KeyError(model_id)
        return card

    def model_by_name(self, principal: Principal, name: str) -> ModelCard | None:
        for card in self.models.values():
            if card.project_id == principal.project_id and card.name == name:
                return card
        return None

    def model_ref(self, principal: Principal, ref: str) -> ModelCard | None:
        try:
            return self.model_for(principal, ref)
        except KeyError:
            return self.model_by_name(principal, ref)

    def set_route(self, route: Route) -> None:
        self._must_project(route.project_id)
        if len(route.fallback_model_ids) > MAX_ROUTE_FALLBACKS:
            raise ValueError("at most two fallback models")
        if route.tier_order and route.tier_order not in TIER_ORDERS:
            raise ValueError("tier_order must be free_paid_local or free_local_paid")
        self.routes = [
            item
            for item in self.routes
            if not (item.project_id == route.project_id and item.task == route.task)
        ]
        self.routes.append(route)

    def route_for(self, project_id: str, task: str) -> Route | None:
        for route in self.routes:
            if route.project_id == project_id and route.task == task:
                return route
        return None

    def add_agent(self, agent: AgentDef) -> AgentDef:
        self._must_project(agent.project_id)
        self.agents[agent.id] = agent
        return agent

    def agent_for(self, principal: Principal, agent_id: str) -> AgentDef:
        agent = self.agents[agent_id]
        if agent.project_id != principal.project_id:
            raise KeyError(agent_id)
        return agent

    def agent_by_id(self, agent_id: str) -> AgentDef | None:
        return self.agents.get(agent_id)

    def save_agent(self, agent: AgentDef) -> None:
        self.agents[agent.id] = agent

    def record_usage(self, row: Usage, release_held: int = 0) -> None:
        """Ledger the call. On success, spend and release the hold in one critical section.

        Spend never crosses the project USD cap: charge is clamped to remaining room
        after the hold is released. The usage row still records the provider's cost.
        """
        with self._lock:
            self.usage.append(row)
            self.tokens_total += row.input_tokens + row.output_tokens
            micros = int(round(row.cost * 1_000_000))
            self.cost_micros += micros
            project = self.projects.get(row.project_id)
            if row.status == USAGE_SUCCESS:
                if project is not None:
                    project.spend_micros += _clamped_charge(project, micros, release_held)
            else:
                self.errors_total += 1
            if release_held > 0 and project is not None:
                project.reserved_micros = max(0, project.reserved_micros - release_held)
                if project.reserved_micros == 0:
                    project.reserved_until_ms = 0

    def usage_totals(self) -> tuple[int, float, int]:
        return self.tokens_total, self.cost_micros / 1_000_000, self.errors_total

    def project_usage(self, principal: Principal, limit: int = 100, offset: int = 0) -> list[Usage]:
        rows = [row for row in self.usage if row.project_id == principal.project_id]
        return rows[offset : offset + limit]

    def project_usage_recent(self, principal: Principal, limit: int) -> list[Usage]:
        """Newest `limit` rows for cost/summary scans. Oldest-first list order."""
        rows = [row for row in self.usage if row.project_id == principal.project_id]
        if limit >= len(rows):
            return rows
        return rows[-limit:]

    def usage_page(
        self, principal: Principal, limit: int, after_id: int
    ) -> tuple[list[Usage], int]:
        rows = [row for row in self.usage if row.project_id == principal.project_id]
        indexed = [(index, row) for index, row in enumerate(rows, start=1) if index > after_id]
        page = indexed[:limit]
        if len(indexed) > limit and page:
            return [row for _, row in page], page[-1][0]
        return [row for _, row in page], 0

    def all_usage(self) -> list[Usage]:
        return list(self.usage)

    def insert_run(self, run: RunRecord) -> RunRecord:
        with self._lock:
            if run.idempotency_key:
                found = self._find_idem(run.project_id, run.idempotency_key)
                if found is not None:
                    return copy.deepcopy(found)
            stored = copy.deepcopy(run)
            self.runs[run.id] = stored
            self._lease[run.id] = run.lease_ms
            return run

    def save_run(
        self,
        run: RunRecord,
        held_lease: int | None = None,
        effect: tuple[str, str, dict] | None = None,
        inserted: list[bool] | None = None,
    ) -> bool:
        with self._lock:
            if run.id not in self.runs:
                self._store_effect(effect, inserted)
                self.runs[run.id] = copy.deepcopy(run)
                self._lease[run.id] = run.lease_ms
                return True
            current = self._lease.get(run.id)
            if held_lease is None:
                held_lease = current
            if current != held_lease:
                return False
            stored = self.runs[run.id]
            if stored.status == RUN_CANCELLED and run.status != RUN_CANCELLED:
                return False
            self._store_effect(effect, inserted)
            self.runs[run.id] = copy.deepcopy(run)
            self._lease[run.id] = run.lease_ms
            return True

    def _store_effect(
        self, effect: tuple[str, str, dict] | None, inserted: list[bool] | None
    ) -> None:
        if effect is None:
            return
        _project_id, idem_key, result = effect
        if idem_key in self.effects:
            return
        self.effects[idem_key] = copy.deepcopy(result)
        if inserted is not None:
            inserted.append(True)

    def renew_lease(self, run_id: str, held: int, new_lease: int) -> bool:
        with self._lock:
            if self._lease.get(run_id) != held:
                return False
            self._lease[run_id] = new_lease
            stored = self.runs.get(run_id)
            if stored is not None:
                stored.lease_ms = new_lease
            return True

    def lease_of(self, run_id: str) -> int:
        with self._lock:
            return self._lease.get(run_id, 0)

    def effect(self, idem_key: str) -> dict | None:
        with self._lock:
            found = self.effects.get(idem_key)
            return None if found is None else copy.deepcopy(found)

    def claim_chat(
        self, project_id: str, key: str, fingerprint: str, now_ms: int
    ) -> tuple[str, bool] | None:
        with self._lock:
            found = self.chats.get((project_id, key))
            if found is None:
                self.chats[(project_id, key)] = {
                    "fingerprint": fingerprint,
                    "text": "",
                    "finish": False,
                    "ready": False,
                    "claimed_ms": now_ms,
                }
                return None
            if found["fingerprint"] != fingerprint:
                raise IdempotencyConflict()
            if found["ready"]:
                return str(found["text"]), bool(found["finish"])
            if now_ms - int(found["claimed_ms"]) >= WINDOW_MS:
                found["fingerprint"] = fingerprint
                found["claimed_ms"] = now_ms
                return None
            raise IdempotencyPending()

    def finish_chat(self, project_id: str, key: str, text: str, finish: bool) -> None:
        with self._lock:
            found = self.chats[(project_id, key)]
            found["text"] = text
            found["finish"] = finish
            found["ready"] = True

    def drop_chat(self, project_id: str, key: str) -> None:
        with self._lock:
            found = self.chats.get((project_id, key))
            if found is not None and not found["ready"]:
                self.chats.pop((project_id, key), None)

    def park_async_chat(
        self,
        project_id: str,
        key: str,
        fingerprint: str,
        request: dict,
        now_ms: int,
    ) -> tuple[str, bool] | None:
        """Queue a chat for the worker. Returns a ready replay when already finished."""
        with self._lock:
            found = self.chats.get((project_id, key))
            if found is None:
                self.chats[(project_id, key)] = {
                    "fingerprint": fingerprint,
                    "text": "",
                    "finish": False,
                    "ready": False,
                    "claimed_ms": 0,
                    "async_pending": True,
                    "request": copy.deepcopy(request),
                }
                return None
            if found["fingerprint"] != fingerprint:
                raise IdempotencyConflict()
            if found["ready"]:
                return str(found["text"]), bool(found["finish"])
            if found.get("async_pending"):
                return None
            raise IdempotencyPending()

    def claim_async_chat(self, now_ms: int) -> dict | None:
        with self._lock:
            due = [
                (key, item)
                for key, item in self.chats.items()
                if item.get("async_pending")
                and not item.get("ready")
                and int(item.get("claimed_ms") or 0) <= now_ms
            ]
            if not due:
                return None
            due.sort(key=lambda pair: int(pair[1].get("claimed_ms") or 0))
            picked = fair_take(due, lambda pair: pair[0][0], limit=1)
            (project_id, key), item = picked[0]
            item["claimed_ms"] = now_ms + RESERVE_TTL_MS
            item["async_pending"] = True
            return {
                "project_id": project_id,
                "idempotency_key": key,
                "fingerprint": item["fingerprint"],
                "request": copy.deepcopy(item.get("request") or {}),
            }

    def chat_status(self, project_id: str, key: str) -> dict | None:
        with self._lock:
            found = self.chats.get((project_id, key))
            if found is None:
                return None
            return {
                "ready": bool(found["ready"]),
                "text": str(found.get("text") or ""),
                "finish": bool(found.get("finish")),
                "async_pending": bool(found.get("async_pending")),
            }

    def put_effect(self, project_id: str, idem_key: str, result: dict) -> tuple[dict, bool]:
        del project_id
        with self._lock:
            found = self.effects.get(idem_key)
            if found is not None:
                return found, False
            stored = copy.deepcopy(result)
            self.effects[idem_key] = stored
            return stored, True

    def run_for(self, principal: Principal, run_id: str) -> RunRecord:
        with self._lock:
            run = self.runs[run_id]
            if run.project_id != principal.project_id:
                raise KeyError(run_id)
            return copy.deepcopy(run)

    def run_by_idem(self, project_id: str, key: str) -> RunRecord | None:
        if not key:
            return None
        with self._lock:
            found = self._find_idem(project_id, key)
            return None if found is None else copy.deepcopy(found)

    def _find_idem(self, project_id: str, key: str) -> RunRecord | None:
        for run in self.runs.values():
            if run.project_id == project_id and run.idempotency_key == key:
                return run
        return None

    def open_runs(self) -> list[RunRecord]:
        return [run for run in self.runs.values() if run.status in OPEN_RUN]

    def open_run_count(self, project_id: str) -> int:
        with self._lock:
            return sum(
                1
                for run in self.runs.values()
                if run.project_id == project_id and run.status in OPEN_RUN
            )

    def deadline_due(self, now: int, limit: int = 32) -> list[RunRecord]:
        with self._lock:
            due = [
                copy.deepcopy(run)
                for run in self.runs.values()
                if run.status in OPEN_RUN and run.deadline_ms and run.deadline_ms <= now
            ]
        due.sort(key=lambda run: run.deadline_ms)
        return due[:limit]

    def claim_due(self, now: int, lease_for: int, limit: int = 16) -> list[RunRecord]:

        claimed: list[RunRecord] = []
        with self._lock:
            due = [
                run
                for run in self.runs.values()
                if run.status in RUN_CLAIMABLE
                and self._lease.get(run.id, 0) <= now
                and (run.deadline_ms == 0 or run.deadline_ms > now)
            ]
            due.sort(key=lambda run: (self._lease.get(run.id, 0), run.id))
            for run in fair_take(due, lambda item: item.project_id, limit=limit):
                new_lease = now + lease_for
                self._lease[run.id] = new_lease
                run.lease_ms = new_lease
                claimed.append(copy.deepcopy(run))
        return claimed

    def claim_run(self, run_id: str, now: int, lease_for: int) -> bool:
        with self._lock:
            run = self.runs.get(run_id)
            if run is None or run.status not in OPEN_RUN or self._lease.get(run_id, 0) > now:
                return False
            new_lease = now + lease_for
            self._lease[run_id] = new_lease
            run.lease_ms = new_lease
            return True

    def cancel_run(self, project_id: str, run_id: str, finished_ms: int = 0) -> RunRecord | None:
        with self._lock:
            run = self.runs.get(run_id)
            if run is None or run.project_id != project_id:
                return None
            if run.status not in OPEN_RUN:
                return copy.deepcopy(run)
            run.status = RUN_CANCELLED
            run.outcome = OUTCOME_CANCELLED
            run.lease_ms = 0
            if finished_ms and run.finished_ms == 0:
                run.finished_ms = finished_ms
            run.events.append({"name": RUN_CANCELLED, "step": run.steps})
            self._lease[run_id] = 0
            stored = copy.deepcopy(run)
            self.runs[run_id] = stored
            return copy.deepcopy(stored)

    def plaintext_secrets(self) -> list[str]:
        """Values that must never contain a raw API key."""
        return [record.key_hash for record in self.keys.values()]

    def insert_eval(self, record: dict) -> dict:
        with self._lock:
            stored = copy.deepcopy(record)
            self.evals[str(record["id"])] = stored
            return copy.deepcopy(stored)

    def eval_for(self, principal: Principal, eval_id: str) -> dict:
        with self._lock:
            found = self.evals[eval_id]
            if found["project_id"] != principal.project_id:
                raise KeyError(eval_id)
            return copy.deepcopy(found)

    def save_eval(self, record: dict) -> None:
        with self._lock:
            self.evals[str(record["id"])] = copy.deepcopy(record)

    def claim_eval(self, now_ms: int) -> dict | None:
        with self._lock:
            due = [
                item
                for item in self.evals.values()
                if item.get("status") == EVAL_QUEUED
                or (item.get("status") == EVAL_RUNNING and int(item.get("lease_ms", 0)) <= now_ms)
            ]
            if not due:
                return None
            due.sort(key=lambda item: int(item.get("lease_ms", 0)))
            picked = fair_take(due, lambda item: str(item.get("project_id") or ""), limit=1)
            job = picked[0]
            job["status"] = EVAL_RUNNING
            job["lease_ms"] = now_ms + RESERVE_TTL_MS
            return copy.deepcopy(job)

    def _owned_key(self, principal: Principal, key_id: str) -> ApiKey:
        record = self.keys[key_id]
        if record.project_id != principal.project_id:
            raise KeyError(key_id)
        return record

    def _must_project(self, project_id: str) -> Project:
        return self.projects[project_id]

    def _audit(self, project_id: str, action: str, actor: str, ref: str) -> None:
        self.audit.append(Audit(project_id, action, actor, ref))


def _window_elapsed(window_start_ms: int, now_ms: int) -> bool:
    return window_start_ms == 0 or now_ms - window_start_ms >= WINDOW_MS
