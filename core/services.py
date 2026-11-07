"""Composition root. HTTP handlers call this and do not grow their own rules."""

from __future__ import annotations

import hashlib
import json
import threading

from control.errors import IdempotencyConflict, IdempotencyPending, PlatformError
from control.records import (
    AgentDef,
    Budget,
    Member,
    ModelCard,
    Org,
    Principal,
    Project,
    Prompt,
    ProviderCredential,
    Route,
    RunRecord,
    Usage,
)
from control.store import Store, new_id
from control.vocab import (
    EVAL_COMPLETED,
    EVAL_FAILED,
    MAX_ROUTE_FALLBACKS,
    PUBLISH_DRAFT,
    PUBLISH_PUBLISHED,
    RISK_READ,
    RISK_WRITE,
    TIER_ORDERS,
)
from core.authlimit import AuthLimiter
from core.blobs import build_blobs
from core.credentials import (
    CredentialPool,
    MemoryCredentialPool,
    RedisCredentialPool,
    load_credentials,
)
from core.define import prompt_block, validate
from core.engine import Clock, Engine
from core.eval import new_eval, prefer, score_run
from core.gateway import Breaker, Gateway, ProviderBusy, RedisBreaker, SpendExceeded
from core.limiter import Limiter, MemoryLimiter, RedisLimiter
from core.metrics import Metrics
from core.model import HttpModel, ModelOutput, ProviderDown, ProviderRejected, ScriptedModel
from core.quota import MemoryQuota, Quota, RedisQuota
from core.rag import DocumentConflict, Job, Knowledge
from core.redact import redact
from core.tools import ToolRuntime, ToolsClient, ToolSpec


class Services:
    def __init__(
        self,
        model: ScriptedModel | None = None,
        clock: Clock | None = None,
        store: Store | None = None,
        quota: Quota | None = None,
        knowledge: Knowledge | None = None,
        breaker: Breaker | RedisBreaker | None = None,
        auth_limiter: AuthLimiter | None = None,
        limiter: Limiter | None = None,
        pool: CredentialPool | None = None,
        tools_client: ToolsClient | None = None,
    ) -> None:
        self.store = store or Store()
        self.model = model or ScriptedModel()
        self.metrics = Metrics()
        self.limiter = limiter or MemoryLimiter()
        self.pool = pool
        self.gateway = Gateway(
            self.store,
            self.model,
            self.metrics.provider.observe,
            breaker,
            limiter=self.limiter,
            pool=pool,
            metrics=self.metrics,
        )
        self._runs = threading.BoundedSemaphore(8)
        self._chats = threading.BoundedSemaphore(8)
        self.knowledge = knowledge or Knowledge()
        self.clock = clock or Clock()
        self.quota = quota or MemoryQuota(self.store)
        self.auth_limiter = auth_limiter or AuthLimiter()
        self.records: list[dict] = []
        self.tools = ToolRuntime(self.store, self._remember_record, remote=tools_client)
        self.engine = Engine(
            self.store,
            self.gateway,
            self.tools,
            self.knowledge,
            self.clock,
            limiter=self.limiter,
        )
        self._register_tools()

    def create_org(self, name: str) -> Org:
        return self.store.create_org(name)

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
        try:
            return self.store.create_project(
                org_id,
                name,
                request_limit,
                cost_limit_usd,
                max_in_flight_chat=max_in_flight_chat,
                max_in_flight_run=max_in_flight_run,
                max_open_runs=max_open_runs,
                max_queued_jobs=max_queued_jobs,
                max_provider_inflight=max_provider_inflight,
            )
        except KeyError as exc:
            raise PlatformError(404, "org not found") from exc
        except ValueError as exc:
            raise PlatformError(400, str(exc)) from exc

    def issue_key(self, project_id: str, role: str, groups: tuple[str, ...] = ()) -> str:
        try:
            _record, raw = self.store.issue_key(project_id, role, groups)
        except KeyError as exc:
            raise PlatformError(404, "project not found") from exc
        except ValueError as exc:
            raise PlatformError(400, str(exc)) from exc
        return raw

    def revoke_key(self, principal: Principal, key_id: str) -> None:
        self._write(principal)
        try:
            self.store.revoke(principal, key_id)
        except KeyError as exc:
            raise PlatformError(404, "key not found") from exc

    def register_model(self, principal: Principal, card: ModelCard) -> ModelCard:
        self._write(principal)
        if card.project_id != principal.project_id:
            raise PlatformError(403, "model project does not match the key")
        try:
            return self.store.add_model(card)
        except ValueError as exc:
            raise PlatformError(400, str(exc)) from exc

    def add_credential(self, credential: ProviderCredential) -> None:
        """Platform secret. Tests and bootstrapping call this; HTTP does not expose keys."""
        if self.pool is None:
            self.pool = MemoryCredentialPool()
            self.gateway.pool = self.pool
        try:
            self.pool.add(credential)
        except ValueError as exc:
            raise PlatformError(400, str(exc)) from exc

    def set_route(
        self,
        principal: Principal,
        task: str,
        model_id: str,
        fallback_model_id: str | None = None,
        fallback_model_ids: list[str] | tuple[str, ...] | None = None,
        tier_order: str = "",
    ) -> None:
        self._write(principal)
        ids = tuple(item for item in (fallback_model_ids or ()) if item)
        if not ids and fallback_model_id:
            ids = (fallback_model_id,)
        if len(ids) > MAX_ROUTE_FALLBACKS:
            raise PlatformError(400, "at most two fallback models")
        if tier_order and tier_order not in TIER_ORDERS:
            raise PlatformError(400, "tier_order must be free_paid_local or free_local_paid")
        try:
            self.store.model_for(principal, model_id)
            for item in ids:
                self.store.model_for(principal, item)
        except KeyError as exc:
            raise PlatformError(404, "model not found") from exc
        try:
            self.store.set_route(
                Route(
                    principal.project_id,
                    task,
                    model_id,
                    fallback_model_ids=ids,
                    tier_order=tier_order,
                )
            )
        except ValueError as exc:
            raise PlatformError(400, str(exc)) from exc

    def chat(
        self,
        principal: Principal,
        task: str,
        model_id: str | None,
        messages: list[str],
        idempotency_key: str = "",
    ) -> ModelOutput:
        if principal.role == "viewer":
            raise PlatformError(403, "viewer cannot call the model")
        safe = [redact(message) for message in messages]
        owned = False
        if idempotency_key:
            try:
                replay = self.store.claim_chat(
                    principal.project_id,
                    idempotency_key,
                    _chat_fingerprint(task, model_id, safe),
                    self.clock.ms,
                )
            except IdempotencyConflict as exc:
                raise PlatformError(409, "idempotency conflict") from exc
            except IdempotencyPending as exc:
                raise PlatformError(409, "idempotency in progress", retry_after=1) from exc
            if replay is not None:
                text, finish = replay
                return ModelOutput(text=text, finish=finish, input_tokens=0, output_tokens=0)
            owned = True
        slot = False
        held = False
        try:
            try:
                self._quota(principal)
                self._project_slot(principal, "chat")
                held = True
                self._chat_slot()
                slot = True
                output, _card, _spent = self.gateway.complete(
                    principal, task=task, model_id=model_id, messages=safe
                )
            except ProviderRejected as exc:
                self._drop_chat(principal, idempotency_key, owned)
                raise PlatformError(400, "provider rejected the request") from exc
            except ProviderDown as exc:
                self._drop_chat(principal, idempotency_key, owned)
                raise PlatformError(502, "provider down") from exc
            except SpendExceeded as exc:
                self._drop_chat(principal, idempotency_key, owned)
                raise PlatformError(429, "project spend exceeded") from exc
            except ProviderBusy as exc:
                self._drop_chat(principal, idempotency_key, owned)
                raise PlatformError(
                    429, "project provider concurrency exceeded", retry_after=1
                ) from (exc)
            except KeyError as exc:
                self._drop_chat(principal, idempotency_key, owned)
                raise PlatformError(404, "model or route not found") from exc
            except PlatformError:
                self._drop_chat(principal, idempotency_key, owned)
                raise
        finally:
            if slot:
                self._chats.release()
            if held:
                self.limiter.release(principal.project_id, "chat")
        if owned:
            self.store.finish_chat(
                principal.project_id, idempotency_key, output.text, output.finish
            )
        return output

    def chat_async(
        self,
        principal: Principal,
        task: str,
        model_id: str | None,
        messages: list[str],
        idempotency_key: str,
    ) -> ModelOutput | None:
        """Park a chat for the worker. Returns a replay when already finished."""
        if principal.role == "viewer":
            raise PlatformError(403, "viewer cannot call the model")
        if not idempotency_key:
            raise PlatformError(400, "async chat requires idempotency_key")
        safe = [redact(message) for message in messages]
        fingerprint = _chat_fingerprint(task, model_id, safe)
        request = {"task": task, "model_id": model_id, "messages": safe}
        try:
            replay = self.store.park_async_chat(
                principal.project_id,
                idempotency_key,
                fingerprint,
                request,
                self.clock.ms,
            )
        except IdempotencyConflict as exc:
            raise PlatformError(409, "idempotency conflict") from exc
        except IdempotencyPending as exc:
            raise PlatformError(409, "idempotency in progress", retry_after=1) from exc
        if replay is not None:
            text, finish = replay
            return ModelOutput(text=text, finish=finish, input_tokens=0, output_tokens=0)
        return None

    def chat_status(self, principal: Principal, idempotency_key: str) -> dict:
        found = self.store.chat_status(principal.project_id, idempotency_key)
        if found is None:
            raise PlatformError(404, "chat not found")
        if found["ready"]:
            return {
                "status": "completed",
                "text": found["text"],
                "finish": found["finish"],
            }
        return {"status": "queued" if found["async_pending"] else "running"}

    def work_chat(self) -> bool:
        job = self.store.claim_async_chat(self.clock.ms)
        if job is None:
            return False
        project = self.store.get_project(str(job["project_id"]))
        if project is None:
            self.store.drop_chat(str(job["project_id"]), str(job["idempotency_key"]))
            return True
        request = job.get("request") or {}
        principal = Principal(project.org_id, project.id, "worker", "admin", ("*",))
        try:
            self._quota(principal)
            self._project_slot(principal, "chat")
            try:
                self._chat_slot()
                try:
                    output, _card, _spent = self.gateway.complete(
                        principal,
                        task=str(request.get("task") or "cheap"),
                        model_id=request.get("model_id"),
                        messages=list(request.get("messages") or []),
                    )
                finally:
                    self._chats.release()
            finally:
                self.limiter.release(principal.project_id, "chat")
        except (
            ProviderRejected,
            ProviderDown,
            SpendExceeded,
            ProviderBusy,
            KeyError,
            PlatformError,
        ):
            self.store.drop_chat(str(job["project_id"]), str(job["idempotency_key"]))
            return True
        self.store.finish_chat(
            str(job["project_id"]),
            str(job["idempotency_key"]),
            output.text,
            output.finish,
        )
        return True

    def usage(self, principal: Principal, limit: int = 100, offset: int = 0) -> list[Usage]:
        return self.store.project_usage(principal, limit, offset)

    def usage_page(
        self, principal: Principal, limit: int, after_id: int
    ) -> tuple[list[Usage], int]:
        return self.store.usage_page(principal, limit, after_id)

    def usage_summary(self, principal: Principal, limit: int | None = None) -> dict:
        from core.usage_summary import SUMMARY_SCAN_LIMIT, summarize_usage

        scan = SUMMARY_SCAN_LIMIT if limit is None else max(1, min(limit, SUMMARY_SCAN_LIMIT))
        rows = self.store.project_usage_recent(principal, scan)
        return {"scanned": len(rows), "models": summarize_usage(rows)}

    def create_prompt(self, principal: Principal, name: str, template: str) -> Prompt:
        self._write(principal)
        prompt = Prompt(
            id=new_id("prm"),
            project_id=principal.project_id,
            name=name,
            version=1,
            template=template,
            status=PUBLISH_DRAFT,
        )
        return self.store.add_prompt(prompt)

    def revise_prompt(self, principal: Principal, prompt_id: str, template: str) -> Prompt:
        self._write(principal)
        current = self._prompt(principal, prompt_id)
        draft = Prompt(
            id=new_id("prm"),
            project_id=principal.project_id,
            name=current.name,
            version=current.version + 1,
            template=template,
            status=PUBLISH_DRAFT,
        )
        return self.store.add_prompt(draft)

    def mark_eval(self, principal: Principal, prompt_id: str, passed: bool) -> None:
        self._write(principal)
        prompt = self._prompt(principal, prompt_id)
        prompt.eval_passed = passed
        self.store.save_prompt(prompt)

    def publish_prompt(self, principal: Principal, prompt_id: str) -> Prompt:
        self._write(principal)
        prompt = self._prompt(principal, prompt_id)
        blocked = prompt_block(prompt)
        if blocked:
            raise PlatformError(409, blocked)
        prompt.status = PUBLISH_PUBLISHED
        self.store.save_prompt(prompt)
        return prompt

    def create_agent(self, principal: Principal, agent: AgentDef) -> AgentDef:
        self._write(principal)
        if agent.project_id != principal.project_id:
            raise PlatformError(403, "agent project does not match the key")
        agent.status = PUBLISH_DRAFT
        return self.store.add_agent(agent)

    def validate_agent(self, principal: Principal, agent_id: str) -> list[str]:
        agent = self._agent(principal, agent_id)
        return validate(agent, self._prompt_or_none(agent), self.tools.names(principal.project_id))

    def publish_agent(self, principal: Principal, agent_id: str) -> AgentDef:
        self._write(principal)
        agent = self._agent(principal, agent_id)
        errors = validate(
            agent, self._prompt_or_none(agent), self.tools.names(principal.project_id)
        )
        if errors:
            raise PlatformError(409, ",".join(errors))
        if agent.status == PUBLISH_PUBLISHED:
            raise PlatformError(409, "immutable")
        agent.status = PUBLISH_PUBLISHED
        self.store.save_agent(agent)
        return agent

    def create_kb(self, principal: Principal, name: str) -> str:
        self._write(principal)
        return self.knowledge.create_kb(principal.project_id, name)

    def submit_document(
        self,
        principal: Principal,
        kb_id: str,
        document_id: str,
        version: int,
        text: str,
        acl: tuple[str, ...],
        idempotency_key: str,
    ) -> Job:
        self._write(principal)
        self._quota(principal)
        self._job_cap(principal)
        try:
            return self.knowledge.submit(
                principal.project_id,
                kb_id,
                document_id,
                version,
                text,
                acl,
                idempotency_key,
            )
        except KeyError as exc:
            raise PlatformError(404, "knowledge base not found") from exc
        except DocumentConflict as exc:
            raise PlatformError(409, exc.detail) from exc
        except ValueError as exc:
            raise PlatformError(400, str(exc)) from exc

    def work(self) -> Job | None:
        return self.knowledge.process_next()

    def delete_document(self, principal: Principal, kb_id: str, document_id: str) -> None:
        self._write(principal)
        try:
            self.knowledge.delete(principal.project_id, kb_id, document_id)
        except KeyError as exc:
            raise PlatformError(404, "knowledge base not found") from exc

    def enqueue_run(
        self, principal: Principal, agent_id: str, query: str, idempotency_key: str = ""
    ) -> RunRecord:
        self._quota(principal)
        agent = self._agent(principal, agent_id)
        if idempotency_key and self.store.run_by_idem(principal.project_id, idempotency_key):
            return self.engine.enqueue(principal, agent, query, idempotency_key)
        self._open_run_cap(principal)
        return self.engine.enqueue(principal, agent, query, idempotency_key)

    def start_run(
        self, principal: Principal, agent_id: str, query: str, idempotency_key: str = ""
    ) -> RunRecord:
        self._quota(principal)
        agent = self._agent(principal, agent_id)
        if idempotency_key and self.store.run_by_idem(principal.project_id, idempotency_key):
            return self.engine.start(principal, agent, query, idempotency_key)
        self._open_run_cap(principal)
        self._project_slot(principal, "run")
        try:
            self._slot()
            try:
                return self.engine.start(principal, agent, query, idempotency_key)
            finally:
                self._runs.release()
        finally:
            self.limiter.release(principal.project_id, "run")

    def approve(self, principal: Principal, run_id: str, payload: str) -> RunRecord:
        if principal.role == "viewer":
            raise PlatformError(403, "viewer cannot approve")
        return self.engine.approve(principal, run_id, payload)

    def resume(self, principal: Principal, run_id: str) -> RunRecord:
        self._project_slot(principal, "run")
        try:
            self._slot()
            try:
                return self.engine.resume(principal, run_id)
            finally:
                self._runs.release()
        finally:
            self.limiter.release(principal.project_id, "run")

    def cancel(self, principal: Principal, run_id: str) -> RunRecord:
        return self.engine.cancel(principal, run_id)

    def job(self, principal: Principal, kb_id: str, job_id: str) -> Job:
        try:
            found = self.knowledge.job(principal.project_id, kb_id, job_id)
        except KeyError as exc:
            raise PlatformError(404, "knowledge base not found") from exc
        if found is None:
            raise PlatformError(404, "job not found")
        return found

    def run(self, principal: Principal, run_id: str) -> RunRecord:
        try:
            return self.store.run_for(principal, run_id)
        except KeyError as exc:
            raise PlatformError(404, "run not found") from exc

    def prefer(self, rows: list[dict]) -> str:
        """Pick the cheapest successful mode. Equal success does not prefer more agents."""
        return prefer(rows)

    def submit_eval(
        self,
        principal: Principal,
        name: str,
        cases: list[dict],
        experiments: list[dict],
    ) -> dict:
        self._write(principal)
        if not cases or not experiments:
            raise PlatformError(400, "eval needs cases and experiments")
        if len(cases) > 32 or len(experiments) > 8:
            raise PlatformError(400, "eval too large")
        for experiment in experiments:
            self._agent(principal, str(experiment["agent_id"]))
        record = new_eval(principal.project_id, name, cases, experiments)
        return self.store.insert_eval(record)

    def eval_report(self, principal: Principal, eval_id: str) -> dict:
        try:
            return self.store.eval_for(principal, eval_id)
        except KeyError as exc:
            raise PlatformError(404, "eval not found") from exc

    def work_eval(self) -> dict | None:
        job = self.store.claim_eval(self.clock.ms)
        if job is None:
            return None
        try:
            job["report"] = self._run_eval(job)
            job["status"] = EVAL_COMPLETED
            job["lease_ms"] = 0
        except (PlatformError, KeyError, ValueError) as exc:
            job["status"] = EVAL_FAILED
            job["failure"] = type(exc).__name__
            job["lease_ms"] = 0
        self.store.save_eval(job)
        return job

    def sweep_runs(self, limit: int = 16) -> int:
        self.store.reconcile_reserved(self.clock.ms)
        return self.engine.sweep(limit)

    def _run_eval(self, job: dict) -> dict:
        project = self.store.get_project(str(job["project_id"]))
        if project is None:
            raise KeyError(job["project_id"])
        principal = Principal(project.org_id, project.id, "eval", "admin", ("*",))
        rows: list[dict] = []
        security = {"unauthorized_tool": 0, "leak": 0}
        for experiment in job["experiments"]:
            agent = self._agent(principal, str(experiment["agent_id"]))
            totals = {
                "name": str(experiment.get("name") or agent.name),
                "success": True,
                "cost": 0.0,
                "tokens": 0,
                "cases": [],
            }
            for case in job["cases"]:
                run = self.engine.start(principal, agent, str(case["query"]), idempotency_key="")
                scored = score_run(run, str(case.get("expect_outcome") or ""))
                totals["cases"].append(scored)
                totals["cost"] += float(scored["cost"])
                totals["tokens"] += int(scored["tokens"])
                totals["success"] = bool(totals["success"] and scored["success"])
                if scored["unauthorized_tool"]:
                    security["unauthorized_tool"] += 1
                if scored["leak"]:
                    security["leak"] += 1
            rows.append(totals)
        return {
            "preferred": prefer(rows),
            "experiments": rows,
            "security": security,
            "gate_red": security["unauthorized_tool"] > 0 or security["leak"] > 0,
        }

    def _register_tools(self) -> None:
        self.tools.register(
            ToolSpec("search_knowledge_base", "*", RISK_READ, False),
            lambda _args: {},
        )
        if self.tools.remote is not None:
            self.tools.register(ToolSpec("create_record", "*", RISK_WRITE, True, remote=True))
            self.tools.register(ToolSpec("note", "*", RISK_WRITE, False, remote=True))
            return
        self.tools.register(
            ToolSpec("create_record", "*", RISK_WRITE, True),
            self._create_record,
        )
        self.tools.register(
            ToolSpec("note", "*", RISK_WRITE, False),
            self._create_record,
        )

    def _create_record(self, args: dict) -> dict:
        return dict(args)

    def _remember_record(self, record: dict) -> None:
        if any(item.get("id") == record.get("id") for item in self.records):
            return
        self.records.append(record)

    def _slot(self) -> None:
        if not self._runs.acquire(blocking=False):
            raise PlatformError(429, "run capacity exceeded", retry_after=1)

    def _chat_slot(self) -> None:
        if not self._chats.acquire(blocking=False):
            raise PlatformError(429, "chat capacity exceeded", retry_after=1)

    def _project_slot(self, principal: Principal, kind: str) -> None:
        project = self.store.get_project(principal.project_id)
        if project is None:
            raise PlatformError(404, "project not found")
        limit = project.max_in_flight_chat if kind == "chat" else project.max_in_flight_run
        if not self.limiter.acquire(principal.project_id, kind, limit):
            raise PlatformError(429, "project concurrency exceeded", retry_after=1)

    def _open_run_cap(self, principal: Principal) -> None:
        project = self.store.get_project(principal.project_id)
        if project is None:
            raise PlatformError(404, "project not found")
        if self.store.open_run_count(principal.project_id) >= project.max_open_runs:
            raise PlatformError(429, "project open runs exceeded", retry_after=1)

    def _job_cap(self, principal: Principal) -> None:
        project = self.store.get_project(principal.project_id)
        if project is None:
            raise PlatformError(404, "project not found")
        if self.knowledge.queued_job_count(principal.project_id) >= project.max_queued_jobs:
            raise PlatformError(429, "project ingest queue exceeded", retry_after=1)

    def _drop_chat(self, principal: Principal, key: str, owned: bool) -> None:
        if owned and key:
            self.store.drop_chat(principal.project_id, key)

    def _quota(self, principal: Principal) -> None:
        if not self.quota.consume(principal.project_id, self.clock.ms):
            raise PlatformError(
                429,
                "project quota exceeded",
                retry_after=self.store.retry_after(principal.project_id, self.clock.ms),
            )

    def _write(self, principal: Principal) -> None:
        if principal.role == "viewer":
            raise PlatformError(403, "viewer cannot change configuration")

    def _prompt(self, principal: Principal, prompt_id: str) -> Prompt:
        try:
            return self.store.prompt_for(principal, prompt_id)
        except KeyError as exc:
            raise PlatformError(404, "prompt not found") from exc

    def _agent(self, principal: Principal, agent_id: str) -> AgentDef:
        try:
            return self.store.agent_for(principal, agent_id)
        except KeyError as exc:
            raise PlatformError(404, "agent not found") from exc

    def _prompt_or_none(self, agent: AgentDef) -> Prompt | None:
        prompt = self.store.prompt_by_id(agent.prompt_id)
        if prompt is None or prompt.project_id != agent.project_id:
            return None
        return prompt


def _chat_fingerprint(task: str, model_id: str | None, messages: list[str]) -> str:
    raw = json.dumps({"messages": messages, "model_id": model_id, "task": task}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def build_services(settings: dict[str, str]) -> Services:
    database_url = settings.get("database_url", "")
    redis_url = settings.get("redis_url", "")
    pepper = settings.get("key_pepper", "")
    if bool(database_url) != bool(redis_url):
        raise PlatformError(500, "database and redis must be configured together")
    if database_url and not pepper:
        raise PlatformError(500, "PLATFORM_KEY_PEPPER is required with DATABASE_URL")
    try:
        credentials = load_credentials(settings)
    except ValueError as exc:
        raise PlatformError(500, str(exc)) from exc
    model = None
    if settings.get("llm_base_url") or credentials:
        model = HttpModel(
            settings.get("llm_base_url") or "http://127.0.0.1",
            settings.get("llm_api_key", ""),
        )
    tools_client = _tools_client(settings)
    vectors = _vectors(settings)
    blobs = build_blobs(settings)
    if not database_url:
        knowledge = Knowledge(vectors) if vectors is not None else None
        pool = MemoryCredentialPool(credentials) if credentials else None
        return Services(
            model=model,
            knowledge=knowledge,
            store=Store(pepper=pepper),
            pool=pool,
            tools_client=tools_client,
        )
    import redis

    from control.postgres import PostgresStore
    from core.corpus import PgKnowledge

    store = PostgresStore(database_url, pepper=pepper)
    client = redis.Redis.from_url(redis_url, socket_timeout=1, socket_connect_timeout=1)
    pool = RedisCredentialPool(client, credentials) if credentials else None
    return Services(
        model=model,
        store=store,
        quota=RedisQuota(redis_url, store, client=client),
        knowledge=PgKnowledge(database_url, vectors, blobs=blobs),
        breaker=RedisBreaker(client),
        auth_limiter=AuthLimiter(client=client),
        limiter=RedisLimiter(redis_url, client=client),
        pool=pool,
        tools_client=tools_client,
    )


def _tools_client(settings: dict[str, str]) -> ToolsClient | None:
    base = settings.get("tools_url", "").strip()
    token = settings.get("tools_token", "").strip()
    if not base:
        return None
    if not token:
        raise PlatformError(500, "TOOLS_TOKEN is required when TOOLS_URL is set")
    return ToolsClient(base, token)


def _vectors(settings: dict[str, str]):
    from core.vectors import QdrantIndex, configure_embedding

    configure_embedding(
        settings.get("embedding_backend") or "hash",
        settings.get("embedding_model") or "BAAI/bge-small-en-v1.5",
    )
    url = settings.get("qdrant_url", "")
    if url:
        return QdrantIndex(url=url)
    memory = (settings.get("qdrant_memory") or "").strip().lower() in {"1", "true", "yes"}
    if memory:
        return QdrantIndex()
    return None


def budget(
    steps: int = 8,
    tokens: int = 10_000,
    cost: float = 1.0,
    wall: int = 60_000,
    handoffs: int = 4,
    parallel: int = 3,
) -> Budget:
    return Budget(steps, tokens, cost, wall, handoffs, parallel)


def member(
    member_id: str, profile: str = "cheap", tools: tuple[str, ...] = (), turns: int = 2
) -> Member:
    return Member(member_id, profile, tools, turns)
