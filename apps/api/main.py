"""Platform HTTP API."""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.settings import load_settings
from control.errors import PlatformError
from control.records import AgentDef, Budget, Member, ModelCard, Principal
from control.store import TERMINAL, new_id
from control.vocab import PUBLISH_DRAFT, RUN_WAITING_APPROVAL
from core.services import Services, build_services

logger = logging.getLogger("genai")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectIn(BaseModel):
    org_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    request_limit: int = Field(default=1_000, gt=0, le=1_000_000)
    cost_limit_usd: float = Field(default=0, ge=0, le=1_000_000)
    max_in_flight_chat: int = Field(default=2, gt=0, le=64)
    max_in_flight_run: int = Field(default=2, gt=0, le=64)
    max_open_runs: int = Field(default=32, gt=0, le=10_000)
    max_queued_jobs: int = Field(default=64, gt=0, le=10_000)
    max_provider_inflight: int = Field(default=2, gt=0, le=64)


class KeyIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=32)
    groups: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("groups")
    @classmethod
    def _groups(cls, value: list[str]) -> list[str]:
        for group in value:
            if not group or len(group) > 64:
                raise ValueError("invalid group")
        return value


class ModelIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    profile: str = Field(min_length=1, max_length=64)
    capabilities: list[str] = Field(min_length=1, max_length=8)
    input_price: float = Field(ge=0)
    output_price: float = Field(ge=0)
    pricing_version: str = Field(min_length=1, max_length=64)
    provider: str = Field(default="", max_length=64)
    tier_affinity: str = Field(default="any", pattern="^(any|free_only|paid_only)$")
    tier_order: str = Field(
        default="free_paid_local",
        pattern="^(free_paid_local|free_local_paid)$",
    )


class RouteIn(BaseModel):
    task: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=80)
    fallback_model_id: str | None = None
    fallback_model_ids: list[str] = Field(default_factory=list, max_length=2)
    tier_order: str = Field(default="", pattern="^(|free_paid_local|free_local_paid)$")


class ChatIn(BaseModel):
    task: str = Field(min_length=1, max_length=64)
    model_id: str | None = None
    messages: list[str] = Field(min_length=1, max_length=32)
    idempotency_key: str = Field(default="", max_length=200)
    async_mode: bool = False

    @field_validator("messages")
    @classmethod
    def _messages(cls, value: list[str]) -> list[str]:
        for message in value:
            if len(message) > 8_000:
                raise ValueError("message too long")
        return value


class PromptIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    template: str = Field(max_length=16_000)


class MemberIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    model_profile: str = Field(min_length=1, max_length=64)
    allowed_tools: list[str] = Field(default_factory=list, max_length=32)
    max_turns: int = Field(default=2, ge=1, le=32)


class BudgetIn(BaseModel):
    max_steps: int = Field(default=8, ge=1, le=64)
    max_tokens: int = Field(default=10_000, ge=1, le=1_000_000)
    max_cost_usd: float = Field(default=1.0, gt=0, le=10_000)
    max_wall_ms: int = Field(default=60_000, ge=1, le=600_000)
    max_handoffs: int = Field(default=4, ge=1, le=32)
    max_parallel: int = Field(default=3, ge=1, le=16)


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mode: str = Field(min_length=1, max_length=32)
    pattern: str = ""
    prompt_id: str = Field(min_length=1, max_length=80)
    model_profile: str = Field(min_length=1, max_length=64)
    allowed_tools: list[str] = Field(default_factory=list, max_length=32)
    allowed_models: list[str] = Field(default_factory=list, max_length=16)
    prefer_cheap: bool = True
    max_hint_calls: int = Field(default=1, ge=0, le=32)
    max_hint_cost_usd: float = Field(default=0.0, ge=0, le=10_000)
    hint_canary_percent: int = Field(default=100, ge=0, le=100)
    members: list[MemberIn] = Field(default_factory=list, max_length=16)
    entrypoint: str = ""
    steps: list[str] = Field(default_factory=list, max_length=16)
    budget: BudgetIn = Field(default_factory=BudgetIn)
    kb_id: str = ""


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=8_000)
    idempotency_key: str = Field(default="", max_length=200)


class ApprovalIn(BaseModel):
    payload: str = Field(max_length=8_000)


class KbIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class DocIn(BaseModel):
    document_id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1, le=1_000_000)
    text: str = Field(max_length=200_000)
    acl: list[str] = Field(max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=200)


class EvalCaseIn(BaseModel):
    query: str = Field(min_length=1, max_length=8_000)
    expect_outcome: str = Field(default="", max_length=64)


class EvalExperimentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(min_length=1, max_length=80)


class EvalIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cases: list[EvalCaseIn] = Field(min_length=1, max_length=32)
    experiments: list[EvalExperimentIn] = Field(min_length=1, max_length=8)


def _auth_bucket(request: Request, raw: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{raw[:12]}"


def principal_from(
    services: Services, authorization: str | None, request: Request | None = None
) -> Principal:
    if not authorization:
        raise PlatformError(401, "invalid api key")
    raw = authorization.removeprefix("Bearer ").strip()
    bucket = _auth_bucket(request, raw) if request is not None else raw[:12]
    if not services.auth_limiter.allow(bucket):
        raise PlatformError(429, "too many auth failures", retry_after=60)
    principal = services.store.authenticate(raw)
    if principal is None:
        services.auth_limiter.fail(bucket)
        raise PlatformError(401, "invalid api key")
    return principal


def get_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    return principal_from(request.app.state.svc, authorization, request)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_admin(
    authorization: str | None, settings: dict[str, str], request: Request | None = None
) -> None:
    if not _admin_ok(authorization, settings, request):
        raise PlatformError(401, "admin token required")


def _admin_ok(
    authorization: str | None, settings: dict[str, str], request: Request | None = None
) -> bool:
    token = settings.get("admin_token", "")
    if not token or not authorization:
        return False
    raw = authorization.removeprefix("Bearer ").strip()
    services = request.app.state.svc if request is not None else None
    bucket = _auth_bucket(request, raw) if request is not None else raw[:12]
    if services is not None and not services.auth_limiter.allow(bucket):
        raise PlatformError(429, "too many auth failures", retry_after=60)
    ok = secrets.compare_digest(raw, token)
    if not ok and services is not None:
        services.auth_limiter.fail(bucket)
    return ok


def readiness(settings: dict[str, str], services: Services) -> tuple[int, dict]:
    database = bool(settings.get("database_url"))
    cache = bool(settings.get("redis_url"))
    body: dict = {"llm_configured": bool(settings.get("llm_base_url"))}
    if database != cache:
        body["status"] = "misconfigured"
        return 503, body
    if not database:
        body["status"] = "unavailable"
        body["store"] = "memory"
        return 503, body
    postgres_ok = services.store.ping()
    redis_ok = services.quota.ping()
    qdrant_ok = True
    if settings.get("qdrant_url"):
        qdrant_ok = services.knowledge.ping()
        body["qdrant"] = qdrant_ok
    body["postgres"] = postgres_ok
    body["redis"] = redis_ok
    ready = postgres_ok and redis_ok and qdrant_ok
    body["status"] = "ok" if ready else "unavailable"
    return (200 if ready else 503), body


def _metrics_text(app: FastAPI) -> str:
    tokens, cost, errors = app.state.svc.store.usage_totals()
    return (
        f"genai_tokens_total {tokens}\n"
        f"genai_cost_usd_total {cost}\n"
        f"genai_provider_errors_total {errors}\n"
        f"{app.state.svc.metrics.render()}\n"
    )


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = self.server.render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        del fmt, args


class _MetricsServer(ThreadingHTTPServer):
    render: callable


def _start_metrics(app: FastAPI) -> _MetricsServer | None:
    raw = app.state.settings.get("metrics_port", "")
    if not raw:
        return None
    httpd = _MetricsServer(("0.0.0.0", int(raw)), _MetricsHandler)
    httpd.render = lambda: _metrics_text(app)
    app.state.metrics_bound = int(httpd.server_address[1])
    threading.Thread(target=httpd.serve_forever, name="metrics", daemon=True).start()
    return httpd


def create_app(services: Services | None = None) -> FastAPI:
    _configure_logging()
    settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        httpd = _start_metrics(app)
        try:
            yield
        finally:
            _drain_inflight(app)
            model = getattr(app.state.svc, "model", None)
            close = getattr(model, "close", None)
            if callable(close):
                close()
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()

    app = FastAPI(title="GenAI Lead", lifespan=lifespan)
    app.state.settings = settings
    app.state.svc = services if services is not None else build_services(settings)
    app.state.metrics_bound = 0
    app.state.inflight = 0
    app.state.inflight_lock = threading.Lock()
    try:
        from starlette.middleware.body_limit import RequestBodyLimitMiddleware

        app.add_middleware(RequestBodyLimitMiddleware, max_body_size=512_000)
    except ImportError:
        pass

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        from core.trace import correlation_id as correlation_var

        incoming = request.headers.get("x-request-id", "")
        correlation_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        request.state.correlation_id = correlation_id
        token = correlation_var.set(correlation_id)
        with request.app.state.inflight_lock:
            request.app.state.inflight += 1
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            with request.app.state.inflight_lock:
                request.app.state.inflight = max(0, request.app.state.inflight - 1)
            correlation_var.reset(token)
        elapsed = int((time.perf_counter() - started) * 1000)
        request.app.state.svc.metrics.requests.observe(elapsed)
        response.headers["x-request-id"] = correlation_id
        return response

    @app.exception_handler(PlatformError)
    def _problem(request: Request, exc: PlatformError) -> JSONResponse:
        body = {
            "type": f"https://genai.local/problems/{exc.code}",
            "title": exc.detail,
            "status": exc.status,
            "detail": exc.detail,
            "code": exc.code,
            "correlation_id": getattr(request.state, "correlation_id", ""),
        }
        headers = {}
        if exc.retry_after is not None:
            headers["retry-after"] = str(exc.retry_after)
        return JSONResponse(
            body, status_code=exc.status, media_type="application/problem+json", headers=headers
        )

    @app.exception_handler(Exception)
    def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, PlatformError):
            raise exc
        log_failure(getattr(request.state, "correlation_id", ""), exc)
        body = {
            "type": "https://genai.local/problems/internal_error",
            "title": "internal error",
            "status": 500,
            "detail": "internal error",
            "code": "internal_error",
            "correlation_id": getattr(request.state, "correlation_id", ""),
        }
        return JSONResponse(body, status_code=500, media_type="application/problem+json")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        status, body = readiness(app.state.settings, app.state.svc)
        return JSONResponse(body, status_code=status)

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        if app.state.settings.get("metrics_port"):
            raise PlatformError(404, "metrics are served on the metrics port")
        return PlainTextResponse(_metrics_text(app))

    @app.post("/v1/orgs")
    def create_org(
        request: Request, body: OrgIn, authorization: Annotated[str | None, Header()] = None
    ) -> dict:
        require_admin(authorization, app.state.settings, request)
        org = app.state.svc.create_org(body.name)
        return {"id": org.id, "name": org.name}

    @app.post("/v1/projects")
    def create_project(
        request: Request,
        body: ProjectIn,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        require_admin(authorization, app.state.settings, request)
        project = app.state.svc.create_project(
            body.org_id,
            body.name,
            body.request_limit,
            body.cost_limit_usd,
            max_in_flight_chat=body.max_in_flight_chat,
            max_in_flight_run=body.max_in_flight_run,
            max_open_runs=body.max_open_runs,
            max_queued_jobs=body.max_queued_jobs,
            max_provider_inflight=body.max_provider_inflight,
        )
        return {
            "id": project.id,
            "org_id": project.org_id,
            "request_limit": project.request_limit,
            "cost_limit_usd": project.cost_limit_usd,
            "max_in_flight_chat": project.max_in_flight_chat,
            "max_in_flight_run": project.max_in_flight_run,
            "max_open_runs": project.max_open_runs,
            "max_queued_jobs": project.max_queued_jobs,
            "max_provider_inflight": project.max_provider_inflight,
        }

    @app.post("/v1/api-keys")
    def create_key(
        request: Request, body: KeyIn, authorization: Annotated[str | None, Header()] = None
    ) -> dict:
        svc: Services = app.state.svc
        if svc.store.get_project(body.project_id) is None:
            raise PlatformError(404, "project not found")
        if not _admin_ok(authorization, app.state.settings, request):
            principal = principal_from(svc, authorization, request)
            if principal.project_id != body.project_id or principal.role != "owner":
                raise PlatformError(403, "only the owner issues more keys")
        raw = svc.issue_key(body.project_id, body.role, tuple(body.groups))
        return {"key": raw}

    @app.post("/v1/models")
    def create_model(body: ModelIn, principal: PrincipalDep) -> dict:
        card = app.state.svc.register_model(
            principal,
            ModelCard(
                id=new_id("mdl"),
                project_id=principal.project_id,
                name=body.name,
                profile=body.profile,
                capabilities=tuple(body.capabilities),
                input_price=body.input_price,
                output_price=body.output_price,
                pricing_version=body.pricing_version,
                provider=body.provider,
                tier_affinity=body.tier_affinity,
                tier_order=body.tier_order,
            ),
        )
        return {"id": card.id}

    @app.post("/v1/routes")
    def create_route(body: RouteIn, principal: PrincipalDep) -> dict:
        app.state.svc.set_route(
            principal,
            body.task,
            body.model_id,
            body.fallback_model_id,
            fallback_model_ids=body.fallback_model_ids,
            tier_order=body.tier_order,
        )
        return {"task": body.task}

    @app.post("/v1/chat/completions", response_model=None)
    def chat(body: ChatIn, principal: PrincipalDep):
        if body.async_mode:
            output = app.state.svc.chat_async(
                principal, body.task, body.model_id, body.messages, body.idempotency_key
            )
            if output is not None:
                return {"text": output.text, "finish": output.finish, "status": "completed"}
            return JSONResponse(
                {
                    "status": "queued",
                    "idempotency_key": body.idempotency_key,
                },
                status_code=202,
                headers={"Location": f"/v1/chat/completions/{body.idempotency_key}"},
            )
        output = app.state.svc.chat(
            principal, body.task, body.model_id, body.messages, body.idempotency_key
        )
        return {"text": output.text, "finish": output.finish}

    @app.get("/v1/chat/completions/{idempotency_key}")
    def chat_status(idempotency_key: str, principal: PrincipalDep) -> dict:
        return app.state.svc.chat_status(principal, idempotency_key)

    @app.post("/v1/prompts")
    def create_prompt(body: PromptIn, principal: PrincipalDep) -> dict:
        prompt = app.state.svc.create_prompt(principal, body.name, body.template)
        return {"id": prompt.id, "version": prompt.version, "status": prompt.status}

    @app.post("/v1/prompts/{prompt_id}/publish")
    def publish_prompt(prompt_id: str, principal: PrincipalDep) -> dict:
        prompt = app.state.svc.publish_prompt(principal, prompt_id)
        return {"id": prompt.id, "status": prompt.status}

    @app.post("/v1/agents")
    def create_agent(body: AgentIn, principal: PrincipalDep) -> dict:
        agent = app.state.svc.create_agent(
            principal,
            _agent_def(principal.project_id, new_id("agt"), body),
        )
        return {"id": agent.id, "status": agent.status}

    @app.post("/v1/agents/{agent_id}/validate")
    def validate_agent(agent_id: str, principal: PrincipalDep) -> dict:
        return {"errors": app.state.svc.validate_agent(principal, agent_id)}

    @app.post("/v1/agents/{agent_id}/publish")
    def publish_agent(agent_id: str, principal: PrincipalDep) -> dict:
        agent = app.state.svc.publish_agent(principal, agent_id)
        return {"id": agent.id, "status": agent.status, "version": agent.version}

    @app.post("/v1/agents/{agent_id}/runs")
    def start_run(agent_id: str, body: RunIn, principal: PrincipalDep) -> JSONResponse:
        run = app.state.svc.enqueue_run(principal, agent_id, body.query, body.idempotency_key)
        status = 200 if run.status in TERMINAL else 202
        headers = {"Location": f"/v1/runs/{run.id}"} if status == 202 else {}
        return JSONResponse(_run_body(run), status_code=status, headers=headers)

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, principal: PrincipalDep) -> dict:
        return _run_body(app.state.svc.run(principal, run_id))

    @app.get("/v1/runs/{run_id}/events")
    def events(run_id: str, principal: PrincipalDep) -> StreamingResponse:
        def stream():
            seen = 0
            deadline = time.time() + 10
            while time.time() < deadline:
                run = app.state.svc.run(principal, run_id)
                for event in run.events[seen:]:
                    yield _event_line(event)
                    seen += 1
                if run.status in TERMINAL or run.status == RUN_WAITING_APPROVAL:
                    yield "event: end\ndata: done\n\n"
                    return
                yield ": keepalive\n\n"
                time.sleep(0.05)
            yield "event: end\ndata: timeout\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/v1/runs/{run_id}/approvals")
    def approve(run_id: str, body: ApprovalIn, principal: PrincipalDep) -> JSONResponse:
        run = app.state.svc.approve(principal, run_id, body.payload)
        status = 200 if run.status in TERMINAL else 202
        headers = {"Location": f"/v1/runs/{run.id}"} if status == 202 else {}
        return JSONResponse(_run_body(run), status_code=status, headers=headers)

    @app.post("/v1/runs/{run_id}/cancel")
    def cancel(run_id: str, principal: PrincipalDep) -> dict:
        return _run_body(app.state.svc.cancel(principal, run_id))

    @app.post("/v1/knowledge-bases")
    def create_kb(body: KbIn, principal: PrincipalDep) -> dict:
        return {"id": app.state.svc.create_kb(principal, body.name)}

    @app.post("/v1/knowledge-bases/{kb_id}/documents")
    def submit_doc(kb_id: str, body: DocIn, principal: PrincipalDep) -> JSONResponse:
        job = app.state.svc.submit_document(
            principal,
            kb_id,
            body.document_id,
            body.version,
            body.text,
            tuple(body.acl),
            body.idempotency_key,
        )
        return JSONResponse(
            {"id": job.id, "status": job.status},
            status_code=202,
            headers={"Location": f"/v1/knowledge-bases/{kb_id}/jobs/{job.id}"},
        )

    @app.get("/v1/knowledge-bases/{kb_id}/jobs/{job_id}")
    def get_job(kb_id: str, job_id: str, principal: PrincipalDep) -> dict:
        job = app.state.svc.job(principal, kb_id, job_id)
        return {
            "id": job.id,
            "status": job.status,
            "document_id": job.document_id,
            "version": job.version,
            "attempts": job.attempts,
        }

    @app.post("/v1/evals")
    def create_eval(body: EvalIn, principal: PrincipalDep) -> JSONResponse:
        record = app.state.svc.submit_eval(
            principal,
            body.name,
            [item.model_dump() for item in body.cases],
            [item.model_dump() for item in body.experiments],
        )
        return JSONResponse(
            {"id": record["id"], "status": record["status"]},
            status_code=202,
            headers={"Location": f"/v1/evals/{record['id']}"},
        )

    @app.get("/v1/evals/{eval_id}")
    def get_eval(eval_id: str, principal: PrincipalDep) -> dict:
        record = app.state.svc.eval_report(principal, eval_id)
        return {
            "id": record["id"],
            "status": record["status"],
            "name": record.get("name", ""),
            "report": record.get("report") or {},
            "failure": record.get("failure", ""),
        }

    @app.get("/v1/usage")
    def usage(
        principal: PrincipalDep,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        after: int = Query(default=0, ge=0),
    ) -> dict:
        if after:
            rows, next_after = app.state.svc.usage_page(principal, limit, after)
        else:
            rows = app.state.svc.usage(principal, limit, offset)
            next_after = 0
        return {
            "next_after": next_after,
            "rows": [
                {
                    "model_id": row.model_id,
                    "model": row.model_name,
                    "pricing_version": row.pricing_version,
                    "status": row.status,
                    "cost": row.cost,
                    "fallback_from": row.fallback_from,
                    "latency_ms": row.latency_ms,
                    "credential_id": row.credential_id,
                    "tier": row.tier,
                }
                for row in rows
            ],
        }

    @app.get("/v1/usage/summary")
    def usage_summary(
        principal: PrincipalDep,
        limit: int = Query(default=5_000, ge=1, le=5_000),
    ) -> dict:
        """Per-model cost mix (free/paid/local) and a tier_order hint."""
        return app.state.svc.usage_summary(principal, limit)

    return app


def log_failure(correlation_id: str, exc: Exception) -> None:
    """Type and correlation id only. The message can carry a prompt or a key."""
    logger.error(
        json.dumps(
            {
                "message": "request failed",
                "correlation_id": correlation_id,
                "error": type(exc).__name__,
            }
        )
    )


def _drain_inflight(app: FastAPI) -> None:
    raw = app.state.settings.get("shutdown_drain_s", "15")
    try:
        budget = max(0.0, float(raw))
    except ValueError:
        budget = 15.0
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        with app.state.inflight_lock:
            if app.state.inflight <= 0:
                return
        time.sleep(0.05)


def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_CorrelationFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        from core.trace import correlation_id as correlation_var

        if not getattr(record, "correlation_id", None):
            record.correlation_id = correlation_var.get()
        return True


def _agent_def(project_id: str, agent_id: str, body: AgentIn) -> AgentDef:
    limits = body.budget
    return AgentDef(
        id=agent_id,
        project_id=project_id,
        name=body.name,
        version=1,
        status=PUBLISH_DRAFT,
        mode=body.mode,
        pattern=body.pattern,
        prompt_id=body.prompt_id,
        model_profile=body.model_profile,
        allowed_tools=tuple(body.allowed_tools),
        members=tuple(
            Member(item.id, item.model_profile, tuple(item.allowed_tools), item.max_turns)
            for item in body.members
        ),
        entrypoint=body.entrypoint,
        steps=tuple(body.steps),
        budget=Budget(
            limits.max_steps,
            limits.max_tokens,
            limits.max_cost_usd,
            limits.max_wall_ms,
            limits.max_handoffs,
            limits.max_parallel,
        ),
        kb_id=body.kb_id,
        allowed_models=tuple(body.allowed_models),
        prefer_cheap=body.prefer_cheap,
        max_hint_calls=body.max_hint_calls,
        max_hint_cost_usd=body.max_hint_cost_usd,
        hint_canary_percent=body.hint_canary_percent,
    )


def _run_body(run) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "outcome": run.outcome,
        "failure": run.failure,
        "answer": run.answer,
        "citations": run.citations,
        "steps": run.steps,
        "handoffs": run.handoffs,
        "tokens": run.tokens,
        "cost": run.cost,
        "pending_payload": run.pending_payload,
    }


def _event_line(event: dict) -> str:
    name = str(event.get("name", ""))
    outcome = event.get("outcome")
    if isinstance(outcome, str) and outcome.isidentifier():
        return f"data: {name} {outcome}\n\n"
    return f"data: {name}\n\n"


# Imported by `uvicorn apps.api.main:app`.
app = create_app()
