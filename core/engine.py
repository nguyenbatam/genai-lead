"""Execution loop for workflow, single agent, and supervisor.

A committed step is the checkpoint. Restart loads that checkpoint and does not call
the model or the write again. The lease is compare-and-swap: a lost lease stops
without writing over the owner.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

from control.errors import PlatformError
from control.records import AgentDef, Member, Principal, RunRecord
from control.store import OPEN_RUN, TERMINAL, Store, new_id
from control.vocab import (
    MODE_AGENT,
    MODE_WORKFLOW,
    OUTCOME_DENIED,
    OUTCOME_FAILED,
    OUTCOME_WAITING_APPROVAL,
    PUBLISH_PUBLISHED,
    RUN_COMPLETED,
    RUN_DENIED,
    RUN_FAILED,
    RUN_FINISHABLE,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_WAITING_APPROVAL,
    TOOL_DENIED,
    TOOL_WAITING_APPROVAL,
)
from core.gateway import Gateway, ProviderBusy, SpendExceeded, ceiling_micros
from core.guard import spotlight
from core.judge import judge
from core.limiter import Limiter, MemoryLimiter
from core.model import ModelOutput, ProviderDown, ProviderRejected, dump_output, load_output
from core.model_hint import index_models, resolve_hint_model_id
from core.rag import Knowledge
from core.redact import redact
from core.tools import ToolRuntime

LEASE_MS = 120_000
# When a project is at run concurrency, defer the lease briefly so fair claim can rotate.
_BUSY_DEFER_MS = 1_000


class Clock:
    """Wall milliseconds shared by the API and the worker. Tests move it with advance."""

    def __init__(self) -> None:
        self._extra = 0

    @property
    def ms(self) -> int:
        return int(time.time() * 1000) + self._extra

    def advance(self, ms: int) -> None:
        self._extra += ms


class LostLease(Exception):
    """Another worker owns the run. The caller reloads instead of writing."""


class Engine:
    def __init__(
        self,
        store: Store,
        gateway: Gateway,
        tools: ToolRuntime,
        knowledge: Knowledge,
        clock: Clock | None = None,
        limiter: Limiter | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.tools = tools
        self.knowledge = knowledge
        self.clock = clock or Clock()
        self.limiter = limiter or MemoryLimiter()
        self.crash_after_steps: int | None = None
        self.crash_after_model_calls: int | None = None
        self.model_calls = 0

    def start(
        self,
        principal: Principal,
        agent: AgentDef,
        query: str,
        idempotency_key: str = "",
    ) -> RunRecord:
        if agent.status != PUBLISH_PUBLISHED:
            raise PlatformError(409, "agent is not published")
        if principal.role == "viewer":
            raise PlatformError(403, "viewer cannot run")
        if len(query) > 8_000:
            raise PlatformError(400, "query too long")
        safe_query = redact(query)
        if idempotency_key:
            existing = self.store.run_by_idem(principal.project_id, idempotency_key)
            if existing is not None:
                if existing.query != safe_query:
                    raise PlatformError(409, "idempotency conflict")
                return existing
        now = self.clock.ms
        run = RunRecord(
            id=self._new_run_id(),
            project_id=principal.project_id,
            agent_id=agent.id,
            status=RUN_RUNNING,
            outcome="",
            answer="",
            started_ms=now,
            deadline_ms=now + agent.budget.max_wall_ms,
            current=agent.entrypoint,
            groups=principal.groups,
            query=safe_query,
            idempotency_key=idempotency_key,
            lease_ms=now + LEASE_MS,
        )
        stored = self.store.insert_run(run)
        if stored.id != run.id:
            if stored.query != safe_query:
                raise PlatformError(409, "idempotency conflict")
            return stored
        return self._drive(principal, agent, run, [run.lease_ms])

    def enqueue(
        self,
        principal: Principal,
        agent: AgentDef,
        query: str,
        idempotency_key: str = "",
    ) -> RunRecord:
        """Persist a run for the worker. The request returns before the model is called."""
        if agent.status != PUBLISH_PUBLISHED:
            raise PlatformError(409, "agent is not published")
        if principal.role == "viewer":
            raise PlatformError(403, "viewer cannot run")
        if len(query) > 8_000:
            raise PlatformError(400, "query too long")
        safe_query = redact(query)
        if idempotency_key:
            existing = self.store.run_by_idem(principal.project_id, idempotency_key)
            if existing is not None:
                if existing.query != safe_query:
                    raise PlatformError(409, "idempotency conflict")
                return existing
        now = self.clock.ms
        run = RunRecord(
            id=self._new_run_id(),
            project_id=principal.project_id,
            agent_id=agent.id,
            status=RUN_QUEUED,
            outcome="",
            answer="",
            started_ms=now,
            deadline_ms=now + agent.budget.max_wall_ms,
            current=agent.entrypoint,
            groups=principal.groups,
            query=safe_query,
            idempotency_key=idempotency_key,
            lease_ms=0,
        )
        stored = self.store.insert_run(run)
        if stored.id != run.id and stored.query != safe_query:
            raise PlatformError(409, "idempotency conflict")
        return stored

    def resume(self, principal: Principal, run_id: str, claimed: bool = False) -> RunRecord:
        run = self.store.run_for(principal, run_id)
        agent = self.store.agent_for(principal, run.agent_id)
        if run.status in TERMINAL:
            return run
        self._expire(run, agent)
        if run.status == RUN_FAILED:
            self._stamp_finished(run)
            self.store.save_run(run, self.store.lease_of(run.id))
            return run
        if run.status not in OPEN_RUN:
            return run
        if not claimed:
            if run.lease_ms > self.clock.ms:
                return run
            if not self.store.claim_run(run.id, self.clock.ms, LEASE_MS):
                return self.store.run_for(principal, run_id)
            run = self.store.run_for(principal, run_id)
        run.status = RUN_RUNNING
        return self._drive(principal, agent, run, [run.lease_ms])

    def approve(self, principal: Principal, run_id: str, payload: str) -> RunRecord:
        """Record approval and re-queue. The worker drives the run; the API does not."""
        run = self.store.run_for(principal, run_id)
        agent = self.store.agent_for(principal, run.agent_id)
        self._expire(run, agent)
        if run.status == RUN_FAILED:
            self._stamp_finished(run)
            self.store.save_run(run, self.store.lease_of(run.id))
            return run
        if run.status != RUN_WAITING_APPROVAL or payload != run.pending_payload:
            raise PlatformError(409, "approval does not match the pending action")
        if not self.store.claim_run(run.id, self.clock.ms, LEASE_MS):
            raise PlatformError(409, "run is busy")
        run = self.store.run_for(principal, run_id)
        if payload != run.pending_payload:
            raise PlatformError(409, "approval does not match the pending action")
        held = run.lease_ms
        if payload not in run.approved:
            run.approved.append(payload)
        run.pending_payload = ""
        run.status = RUN_QUEUED
        run.lease_ms = 0
        self._event(run, "approved")
        if not self.store.save_run(run, held):
            raise PlatformError(409, "run is busy")
        return run

    def cancel(self, principal: Principal, run_id: str) -> RunRecord:
        current = self.store.run_for(principal, run_id)
        if current.status not in OPEN_RUN:
            return current
        cancelled = self.store.cancel_run(principal.project_id, run_id, finished_ms=self.clock.ms)
        return cancelled if cancelled is not None else current

    def sweep(self, limit: int = 16) -> int:
        changed = 0
        now = self.clock.ms
        for run in self.store.deadline_due(now, limit):
            agent = self.store.agent_by_id(run.agent_id)
            if agent is None or not self._expired(run, agent):
                continue
            held = self.store.lease_of(run.id)
            if held > now:
                continue
            self._fail(run, "wall")
            run.lease_ms = 0
            self._stamp_finished(run)
            if self.store.save_run(run, held):
                changed += 1
        for run in self.store.claim_due(now, LEASE_MS, limit):
            project = self.store.get_project(run.project_id)
            agent = self.store.agent_by_id(run.agent_id)
            if project is None or agent is None:
                continue
            held = run.lease_ms
            if not self.limiter.acquire(project.id, "run", project.max_in_flight_run):
                run.lease_ms = now + _BUSY_DEFER_MS
                self.store.save_run(run, held)
                continue
            try:
                principal = Principal(project.org_id, project.id, "worker", "admin", run.groups)
                run.status = RUN_RUNNING
                self._drive(principal, agent, run, [run.lease_ms])
                changed += 1
            finally:
                self.limiter.release(project.id, "run")
        return changed

    def _drive(
        self, principal: Principal, agent: AgentDef, run: RunRecord, held: list[int]
    ) -> RunRecord:
        try:
            while run.status == RUN_RUNNING:
                self._renew(run, held)
                if self.crash_after_steps is not None and run.steps >= self.crash_after_steps:
                    run.lease_ms = self.clock.ms
                    self.store.save_run(run, held[0])
                    raise RuntimeError("worker crashed")
                self._guard(run, agent)
                if run.status != RUN_RUNNING:
                    break
                self._step(principal, agent, run, held)
                self._commit(run, held)
        except ProviderBusy:
            run.status = RUN_QUEUED
            run.lease_ms = self.clock.ms + _BUSY_DEFER_MS
            self._event(run, "provider_busy")
            self.store.save_run(run, held[0])
            return run
        except LostLease:
            return self.store.run_for(principal, run.id)
        run.lease_ms = 0
        if not self.store.save_run(run, held[0]):
            return self.store.run_for(principal, run.id)
        return run

    def _renew(self, run: RunRecord, held: list[int]) -> None:
        new_lease = self.clock.ms + LEASE_MS
        if not self.store.renew_lease(run.id, held[0], new_lease):
            raise LostLease()
        run.lease_ms = new_lease
        held[0] = new_lease

    def _commit(
        self,
        run: RunRecord,
        held: list[int],
        effect: tuple[str, str, dict] | None = None,
    ) -> None:
        self._stamp_finished(run)
        inserted: list[bool] = []
        if not self.store.save_run(run, held[0], effect, inserted):
            raise LostLease()
        if effect is not None and inserted:
            self.tools.observe_write(effect[1], effect[2])

    def _stamp_finished(self, run: RunRecord) -> None:
        if run.status in TERMINAL and run.finished_ms == 0:
            run.finished_ms = self.clock.ms

    def _guard(self, run: RunRecord, agent: AgentDef) -> None:
        budget = agent.budget
        if self._expired(run, agent):
            self._fail(run, "wall")
        elif run.steps >= budget.max_steps or run.tokens >= budget.max_tokens:
            self._fail(run, "budget")
        elif run.cost >= budget.max_cost_usd or run.handoffs > budget.max_handoffs:
            self._fail(run, "budget")

    def _expired(self, run: RunRecord, agent: AgentDef) -> bool:
        if run.deadline_ms:
            return self.clock.ms >= run.deadline_ms
        return self.clock.ms - run.started_ms >= agent.budget.max_wall_ms

    def _expire(self, run: RunRecord, agent: AgentDef) -> None:
        if run.status in OPEN_RUN and self._expired(run, agent):
            self._fail(run, "wall")

    def _step(self, principal: Principal, agent: AgentDef, run: RunRecord, held: list[int]) -> None:
        if agent.mode == MODE_WORKFLOW:
            self._workflow(principal, agent, run, held)
        elif agent.mode == MODE_AGENT:
            self._agent(principal, agent, run, held)
        else:
            self._supervisor(principal, agent, run, held)

    def _workflow(
        self, principal: Principal, agent: AgentDef, run: RunRecord, held: list[int]
    ) -> None:
        if run.cursor >= len(agent.steps):
            self._fail(run, "step")
            return
        kind = agent.steps[run.cursor]
        if kind == "retrieve":
            self._retrieve(principal, agent, run)
            if run.status != RUN_RUNNING:
                return
            run.cursor += 1
            run.steps += 1
            return
        if kind == "answer":
            self._answer(principal, agent, run, held)
            if run.status not in RUN_FINISHABLE:
                return
            run.cursor += 1
            run.steps += 1
            run.status = RUN_COMPLETED
            return
        self._fail(run, "step")

    def _agent(
        self, principal: Principal, agent: AgentDef, run: RunRecord, held: list[int]
    ) -> None:
        output = self._output_for(
            principal,
            agent,
            agent.model_profile,
            run,
            [run.query, *run.messages],
            held,
            agent.allowed_tools,
        )
        if run.status != RUN_RUNNING:
            return
        if output.tool_calls or run.calls_queued:
            self._apply_calls(principal, agent, run, output, agent.allowed_tools, held)
            if run.status != RUN_RUNNING or run.calls_queued:
                return
            run.steps += 1
            return
        run.staged = None
        run.steps += 1
        self._finish(run, output)

    def _supervisor(
        self, principal: Principal, agent: AgentDef, run: RunRecord, held: list[int]
    ) -> None:
        pattern = agent.pattern or "supervisor"
        if pattern == "pipeline":
            member = agent.members[run.cursor]
            output = self._member_step(principal, agent, run, member, f"m{run.cursor}", held)
            if run.status != RUN_RUNNING:
                return
            run.cursor += 1
            run.steps += 1
            if run.cursor >= len(agent.members):
                self._finish(run, output)
            return
        if pattern == "parallel":
            self._parallel(principal, agent, run, held)
            return
        member = self._member_by_id(agent, run.current)
        if member is None:
            run.status = RUN_DENIED
            run.outcome = OUTCOME_DENIED
            return
        output = self._member_step(principal, agent, run, member, member.id, held)
        if run.status != RUN_RUNNING:
            return
        run.steps += 1
        if output.handoff:
            if output.handoff == run.previous or self._member_by_id(agent, output.handoff) is None:
                run.status = RUN_DENIED
                run.outcome = OUTCOME_DENIED
                self._event(run, "denied", tool=output.handoff)
                return
            run.previous = run.current
            run.current = output.handoff
            run.handoffs += 1
            self._event(run, "handoff", hits=[output.handoff])
            return
        self._finish(run, output)

    def _parallel(
        self, principal: Principal, agent: AgentDef, run: RunRecord, held: list[int]
    ) -> None:
        lead = self._member_by_id(agent, agent.entrypoint)
        if lead is None:
            run.status = RUN_DENIED
            run.outcome = OUTCOME_DENIED
            return
        output = self._member_step(principal, agent, run, lead, "lead", held)
        if run.status != RUN_RUNNING:
            return
        targets = output.parallel
        if len(targets) > agent.budget.max_parallel:
            self._fail(run, "parallel")
            return
        planned: list[tuple[int, Member]] = []
        for index, target in enumerate(targets):
            member = self._member_by_id(agent, target)
            if member is None:
                run.status = RUN_DENIED
                run.outcome = OUTCOME_DENIED
                self._event(run, "denied", tool=target)
                return
            planned.append((index, member))
        if not self._can_afford_parallel(principal, agent, run, planned):
            self._fail(run, "budget")
            return
        if self._fan_out() and len(planned) > 1:
            self._prefetch_parallel(principal, agent, run, planned, held)
            if run.status != RUN_RUNNING:
                return
        texts: list[str] = []
        claims = []
        for index, member in planned:
            last = self._member_step(principal, agent, run, member, f"p{index}", held)
            if run.status != RUN_RUNNING:
                return
            texts.append(last.text)
            claims.extend(last.claims)
        run.steps += 1
        self._finish(
            run,
            ModelOutput(
                text="\n".join(part for part in texts if part),
                claims=tuple(claims),
                finish=True,
            ),
        )

    def _member_step(
        self,
        principal: Principal,
        agent: AgentDef,
        run: RunRecord,
        member: Member,
        slot: str,
        held: list[int],
    ) -> ModelOutput:
        key = f"{run.steps}:{slot}"
        entry = run.fan_in.get(key)
        if entry and entry.get("done"):
            return load_output(entry["output"])
        if entry is None:
            used = sum(
                1
                for item in run.fan_in.values()
                if item.get("member") == member.id and item.get("done")
            )
            if used >= member.max_turns:
                self._fail(run, "turns")
                return ModelOutput()
            output = self._model(
                principal,
                agent,
                member.model_profile,
                run,
                [run.query, *run.messages],
                held,
                member.allowed_tools,
            )
            if run.status != RUN_RUNNING:
                return output
            run.fan_in[key] = {"output": dump_output(output), "done": False, "member": member.id}
            self._commit(run, held)
        else:
            output = load_output(entry["output"])
        if output.tool_calls or run.calls_queued:
            self._apply_calls(principal, agent, run, output, member.allowed_tools, held)
            if run.status != RUN_RUNNING or run.calls_queued:
                return output
        fresh = run.fan_in.get(key) or {}
        if output.claims and run.status == RUN_RUNNING and not fresh.get("spoke"):
            run.messages.append(output.text)
            fresh["spoke"] = True
        fresh["done"] = True
        fresh["member"] = member.id
        run.fan_in[key] = fresh
        run.member_turns[member.id] = sum(
            1
            for item in run.fan_in.values()
            if item.get("member") == member.id and item.get("done")
        )
        self._commit(run, held)
        return output

    def _apply_calls(
        self,
        principal: Principal,
        agent: AgentDef,
        run: RunRecord,
        output: ModelOutput,
        allowed: tuple[str, ...],
        held: list[int],
    ) -> None:
        if not run.calls_queued:
            run.pending_calls = [
                {"name": call.name, "args": dict(call.args)} for call in output.tool_calls
            ]
            run.calls_queued = True
            self._commit(run, held)
        while run.pending_calls and run.status == RUN_RUNNING:
            call = run.pending_calls[0]
            stop, effect = self._act(
                principal, agent, run, str(call["name"]), dict(call["args"]), allowed
            )
            if stop:
                self._commit(run, held)
                return
            run.pending_calls.pop(0)
            run.pending_tool = ""
            run.pending_args = {}
            if not run.pending_calls:
                run.calls_queued = False
                run.staged = None
            self._commit(run, held, effect)

    def _act(
        self,
        principal: Principal,
        agent: AgentDef,
        run: RunRecord,
        name: str,
        args: dict,
        allowed: tuple[str, ...],
    ) -> tuple[bool, tuple[str, str, dict] | None]:
        """Return True when the run must leave the loop (denied or waiting)."""
        run.tool_calls.append(name)
        if name == "search_knowledge_base":
            if name not in allowed:
                run.status = RUN_DENIED
                run.outcome = OUTCOME_DENIED
                self._event(run, "denied", tool=name)
                return True, None
            self._retrieve(principal, agent, run)
            return run.status != RUN_RUNNING, None
        idem = f"{run.id}:{name}:{json.dumps(args, sort_keys=True)}"
        result = self.tools.call(principal.project_id, name, args, allowed, idem, set(run.approved))
        if result["status"] == TOOL_DENIED:
            run.status = RUN_DENIED
            run.outcome = OUTCOME_DENIED
            self._event(run, "denied", tool=name)
            return True, None
        if result["status"] == TOOL_WAITING_APPROVAL:
            run.status = RUN_WAITING_APPROVAL
            run.outcome = OUTCOME_WAITING_APPROVAL
            run.pending_payload = result["payload"]
            run.pending_tool = name
            run.pending_args = args
            self._event(run, "waiting_approval", tool=name)
            return True, None
        effect = result.get("effect")
        if idem not in run.applied_keys:
            run.applied_keys.append(idem)
            run.messages.append(json.dumps(result["result"], sort_keys=True))
            self._event(run, "tool", tool=name)
        if isinstance(effect, tuple):
            return False, effect
        return False, None

    def _retrieve(self, principal: Principal, agent: AgentDef, run: RunRecord) -> None:
        if not agent.kb_id:
            return
        try:
            children = self.knowledge.search(
                principal.project_id, agent.kb_id, run.query, run.groups
            )
        except KeyError:
            run.status = RUN_DENIED
            run.outcome = OUTCOME_DENIED
            self._event(run, "denied", tool="search_knowledge_base")
            return
        passages: dict[str, str] = {}
        for child in children:
            parent = self.knowledge.parent(
                principal.project_id, agent.kb_id, child.parent_id, run.groups
            )
            if parent is not None:
                passages[parent.id] = parent.text
        run.passages = passages
        block = "\n".join(spotlight(pid, text) for pid, text in passages.items())
        if block and (not run.messages or run.messages[-1] != block):
            run.messages.append(block)
        self._event(run, "retrieve", hits=list(passages))

    def _answer(
        self, principal: Principal, agent: AgentDef, run: RunRecord, held: list[int]
    ) -> None:
        output = self._output_for(
            principal,
            agent,
            agent.model_profile,
            run,
            [run.query, *run.messages],
            held,
            agent.allowed_tools,
        )
        if run.status != RUN_RUNNING:
            return
        run.staged = None
        self._finish(run, output)

    def _output_for(
        self,
        principal: Principal,
        agent: AgentDef,
        profile: str,
        run: RunRecord,
        messages: list[str],
        held: list[int],
        tools: tuple[str, ...] = (),
    ) -> ModelOutput:
        if run.staged:
            return load_output(run.staged)
        output = self._model(principal, agent, profile, run, messages, held, tools)
        if run.status != RUN_RUNNING:
            return output
        run.staged = dump_output(output)
        self._commit(run, held)
        return output

    def _model(
        self,
        principal: Principal,
        agent: AgentDef,
        profile: str,
        run: RunRecord,
        messages: list[str],
        held: list[int],
        tools: tuple[str, ...] = (),
    ) -> ModelOutput:
        budget = agent.budget
        if run.tokens >= budget.max_tokens or run.cost >= budget.max_cost_usd:
            self._fail(run, "budget")
            return ModelOutput(text="[NO_ANSWER]", finish=True)
        if (
            self.crash_after_model_calls is not None
            and self.model_calls >= self.crash_after_model_calls
        ):
            run.lease_ms = self.clock.ms
            self._commit(run, held)
            raise RuntimeError("worker crashed")
        result = self._invoke(principal, agent, profile, run, messages, tools)
        if isinstance(result, str):
            self._fail(run, result)
            return ModelOutput(text="[NO_ANSWER]", finish=True)
        output, spent = result
        self._account(run, agent, output, spent)
        return output

    def _invoke(
        self,
        principal: Principal,
        agent: AgentDef,
        profile: str,
        run: RunRecord,
        messages: list[str],
        tools: tuple[str, ...],
    ) -> tuple[ModelOutput, float] | str:
        prompt = self.store.prompt_by_id(agent.prompt_id)
        head = [prompt.template] if prompt is not None and prompt.template else []
        safe = [redact(message) for message in (*head, *messages)]
        model_id, hint_reason = self._hint_model_id(principal, agent, profile, run)
        try:
            output, card, spent = self.gateway.complete(
                principal, task=profile, model_id=model_id, messages=safe, tools=tools
            )
        except SpendExceeded:
            return "budget"
        except ProviderBusy:
            raise
        except (ProviderRejected, ProviderDown, KeyError):
            return "provider"
        if hint_reason == "applied":
            run.hint_calls += 1
            run.hint_cost += spent
            self._event(run, "model_hint", tool=card.id, hits=[hint_reason])
        elif run.next_model_hint and hint_reason not in {"default", "empty_allow"}:
            self._event(run, "model_hint", hits=[hint_reason])
        run.next_model_hint = ""
        if output.model_hint:
            run.next_model_hint = output.model_hint
        return output, spent

    def _hint_model_id(
        self,
        principal: Principal,
        agent: AgentDef,
        profile: str,
        run: RunRecord,
    ) -> tuple[str | None, str]:
        hint = run.next_model_hint or None
        if not agent.allowed_models:
            return None, "empty_allow"
        route = self.store.route_for(principal.project_id, profile)
        if route is None:
            return None, "default"
        try:
            default = self.store.model_for(principal, route.model_id)
        except KeyError:
            return None, "default"
        cards = []
        for ref in agent.allowed_models:
            found = self.store.model_ref(principal, ref)
            if found is not None:
                cards.append(found)
        model_id, reason = resolve_hint_model_id(
            hint=hint,
            allowed_models=agent.allowed_models,
            prefer_cheap=agent.prefer_cheap,
            hint_calls=run.hint_calls,
            max_hint_calls=agent.max_hint_calls,
            hint_cost_usd=run.hint_cost,
            max_hint_cost_usd=agent.max_hint_cost_usd,
            hint_canary_percent=agent.hint_canary_percent,
            default_card=default,
            candidates=index_models(cards),
        )
        return model_id, reason

    def _account(self, run: RunRecord, agent: AgentDef, output: ModelOutput, spent: float) -> None:
        self.model_calls += 1
        run.tokens += output.input_tokens + output.output_tokens
        run.cost += spent
        self._event(run, "model", tokens=output.input_tokens + output.output_tokens)
        budget = agent.budget
        if run.tokens > budget.max_tokens or run.cost > budget.max_cost_usd:
            self._fail(run, "budget")

    def _fan_out(self) -> bool:
        return bool(getattr(self.gateway.model, "concurrent", False)) and (
            self.crash_after_model_calls is None
        )

    def _can_afford_parallel(
        self,
        principal: Principal,
        agent: AgentDef,
        run: RunRecord,
        planned: list[tuple[int, Member]],
    ) -> bool:
        """Reject the fan-out before any call when remaining budget cannot cover all arms."""
        budget = agent.budget
        if run.tokens >= budget.max_tokens or run.cost >= budget.max_cost_usd:
            return False
        if run.tokens + len(planned) > budget.max_tokens:
            return False
        messages = [run.query, *run.messages]
        need_cost = 0.0
        for _index, member in planned:
            route = self.store.route_for(principal.project_id, member.model_profile)
            if route is None:
                continue
            try:
                card = self.store.model_for(principal, route.model_id)
            except KeyError:
                continue
            need_cost += ceiling_micros(card, messages) / 1_000_000
        return run.cost + need_cost <= budget.max_cost_usd

    def _prefetch_parallel(
        self,
        principal: Principal,
        agent: AgentDef,
        run: RunRecord,
        planned: list[tuple[int, Member]],
        held: list[int],
    ) -> None:
        snapshot = [run.query, *list(run.messages)]
        missing: list[tuple[int, Member, str]] = []
        for index, member in planned:
            key = f"{run.steps}:p{index}"
            if run.fan_in.get(key) is not None:
                continue
            used = sum(
                1
                for item in run.fan_in.values()
                if item.get("member") == member.id and item.get("done")
            )
            if used >= member.max_turns:
                self._fail(run, "turns")
                return
            missing.append((index, member, key))
        if not missing:
            return
        if run.tokens >= agent.budget.max_tokens or run.cost >= agent.budget.max_cost_usd:
            self._fail(run, "budget")
            return

        def invoke(member: Member) -> tuple[ModelOutput, float] | str:
            return self._invoke(
                principal, agent, member.model_profile, snapshot, member.allowed_tools
            )

        with ThreadPoolExecutor(max_workers=len(missing)) as pool:
            results = list(pool.map(invoke, [member for _, member, _ in missing]))
        for (_index, member, key), result in zip(missing, results, strict=True):
            if run.status != RUN_RUNNING:
                return
            if isinstance(result, str):
                self._fail(run, result)
                return
            output, spent = result
            self._account(run, agent, output, spent)
            if run.status != RUN_RUNNING:
                return
            run.fan_in[key] = {
                "output": dump_output(output),
                "done": False,
                "member": member.id,
            }
        self._commit(run, held)

    def _finish(self, run: RunRecord, output: ModelOutput) -> None:
        run.answer = redact(output.text)
        run.outcome = judge(output, run.passages)
        run.citations = [claim.cite for claim in output.claims if claim.cite in run.passages]
        run.status = RUN_COMPLETED
        self._event(run, "finish", outcome=run.outcome, citations=run.citations)

    def _fail(self, run: RunRecord, reason: str) -> None:
        run.status = RUN_FAILED
        run.outcome = OUTCOME_FAILED
        run.failure = reason
        self._event(run, "failed", outcome=reason)

    def _member_by_id(self, agent: AgentDef, member_id: str) -> Member | None:
        for member in agent.members:
            if member.id == member_id:
                return member
        return None

    def _event(self, run: RunRecord, name: str, **attrs: object) -> None:
        hidden = {"text", "query", "answer", "body", "template"}
        clean = {key: value for key, value in attrs.items() if key not in hidden}
        run.events.append({"name": name, "step": run.steps, **clean})

    def _new_run_id(self) -> str:
        return new_id("run")
