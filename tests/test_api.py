from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app, log_failure
from core.model import ModelOutput


def test_ready_reports_provider_without_the_secret(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://example.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk_secret")
    client = TestClient(create_app())
    ready = client.get("/ready")
    assert ready.status_code == 503
    body = ready.text
    assert '"store":"memory"' in body.replace(" ", "")
    assert '"llm_configured":true' in body.replace(" ", "")
    assert "sk_secret" not in body
    assert "example.test" not in body


def test_http_isolates_projects_and_streams_events(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_TOKEN", "admin-test")
    app = create_app()
    client = TestClient(app)
    admin = {"Authorization": "Bearer admin-test"}
    org = client.post("/v1/orgs", headers=admin, json={"name": "acme"}).json()
    left = client.post(
        "/v1/projects", headers=admin, json={"org_id": org["id"], "name": "left"}
    ).json()
    right = client.post(
        "/v1/projects", headers=admin, json={"org_id": org["id"], "name": "right"}
    ).json()
    issued = client.post(
        "/v1/api-keys",
        headers=admin,
        json={"project_id": left["id"], "role": "developer"},
    )
    left_key = issued.json()["key"]
    right_key = client.post(
        "/v1/api-keys",
        headers=admin,
        json={"project_id": right["id"], "role": "developer"},
    ).json()["key"]
    auth = {"Authorization": f"Bearer {left_key}"}
    prompt = client.post("/v1/prompts", headers=auth, json={"name": "p", "template": "data"}).json()
    assert client.post(f"/v1/prompts/{prompt['id']}/publish", headers=auth).status_code == 200
    agent = client.post(
        "/v1/agents",
        headers=auth,
        json={
            "name": "solo",
            "mode": "agent",
            "prompt_id": prompt["id"],
            "model_profile": "cheap",
        },
    ).json()
    assert client.post(f"/v1/agents/{agent['id']}/publish", headers=auth).status_code == 200
    model = client.post(
        "/v1/models",
        headers=auth,
        json={
            "name": "small",
            "profile": "cheap",
            "capabilities": ["cheap"],
            "input_price": 0,
            "output_price": 0,
            "pricing_version": "2026-11",
        },
    ).json()
    assert (
        client.post(
            "/v1/routes", headers=auth, json={"task": "cheap", "model_id": model["id"]}
        ).status_code
        == 200
    )
    foreign = client.post(
        f"/v1/agents/{agent['id']}/validate",
        headers={"Authorization": f"Bearer {right_key}"},
    )
    assert foreign.status_code == 404
    app.state.svc.model.queue.append(
        ModelOutput(text="[NO_ANSWER]", finish=True, input_tokens=0, output_tokens=1)
    )
    rejected = client.post(
        f"/v1/agents/{agent['id']}/runs",
        headers=auth,
        json={"query": "hello", "groups": ["finance"]},
    )
    assert rejected.status_code == 422
    run = client.post(f"/v1/agents/{agent['id']}/runs", headers=auth, json={"query": "hello"})
    assert run.status_code == 202
    assert run.json()["status"] == "queued"
    assert run.headers["location"] == f"/v1/runs/{run.json()['id']}"
    assert app.state.svc.sweep_runs() == 1
    done = client.get(f"/v1/runs/{run.json()['id']}", headers=auth)
    assert done.status_code == 200
    assert done.json()["outcome"] == "no_answer"
    events = client.get(f"/v1/runs/{run.json()['id']}/events", headers=auth)
    assert events.status_code == 200
    assert "data: finish" in events.text
    assert "event: end" in events.text
    assert "hello" not in events.text
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "genai_request_ms_count" in metrics.text
    assert "genai_provider_ms_count" in metrics.text


def test_document_job_can_be_read_back(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_TOKEN", "admin-test")
    client = TestClient(create_app())
    admin = {"Authorization": "Bearer admin-test"}
    org = client.post("/v1/orgs", headers=admin, json={"name": "acme"}).json()
    created = client.post(
        "/v1/projects", headers=admin, json={"org_id": org["id"], "name": "left"}
    ).json()
    key = client.post(
        "/v1/api-keys",
        headers=admin,
        json={"project_id": created["id"], "role": "developer"},
    ).json()["key"]
    auth = {"Authorization": f"Bearer {key}"}
    kb = client.post("/v1/knowledge-bases", headers=auth, json={"name": "policies"}).json()
    accepted = client.post(
        f"/v1/knowledge-bases/{kb['id']}/documents",
        headers=auth,
        json={
            "document_id": "doc",
            "version": 1,
            "text": "secret body",
            "acl": ["*"],
            "idempotency_key": "job-1",
        },
    )
    assert accepted.status_code == 202
    job_id = accepted.json()["id"]
    assert accepted.headers["location"] == f"/v1/knowledge-bases/{kb['id']}/jobs/{job_id}"
    body = client.get(f"/v1/knowledge-bases/{kb['id']}/jobs/{job_id}", headers=auth)
    assert body.status_code == 200
    assert body.json()["status"] == "queued"
    assert "secret body" not in body.text


def test_failure_log_has_the_correlation_id_and_not_the_secret(caplog):
    import logging

    logging.getLogger("genai").propagate = True
    caplog.set_level("ERROR", logger="genai")
    log_failure("abc123", RuntimeError("sk_secret question"))
    assert "abc123" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "sk_secret" not in caplog.text
    assert "question" not in caplog.text


def test_worker_does_not_import_the_api_app():
    source = Path("apps/workers/main.py").read_text()
    assert "apps.api.main" not in source


def test_metrics_move_off_the_api_port_when_configured(monkeypatch):
    import httpx

    monkeypatch.setenv("METRICS_PORT", "0")
    app = create_app()
    with TestClient(app) as client:
        hidden = client.get("/metrics")
        assert hidden.status_code == 404
        port = client.app.state.metrics_bound
        page = httpx.get(f"http://127.0.0.1:{port}/metrics")
    assert page.status_code == 200
    assert "genai_tokens_total" in page.text
    assert "genai_request_ms_bucket" in page.text
    assert 'le="60000"' in page.text


def test_schema_has_a_version_and_one_live_document_version():
    text = Path("control/schema.sql").read_text()
    assert "schema_migrations" in text
    assert "jobs_doc_version" in text
    assert "chat_replays" in text
    assert "reserved_until_ms" in text
    assert "evals" in text


def test_problem_json_carries_a_stable_code(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_TOKEN", "admin-test")
    client = TestClient(create_app())
    denied = client.post("/v1/orgs", json={"name": "x"})
    assert denied.status_code == 401
    body = denied.json()
    assert body["code"] == "admin_token_required"
    assert "admin_token_required" in body["type"]


def test_auth_failures_are_rate_limited(monkeypatch):
    from core.authlimit import AuthLimiter

    monkeypatch.setenv("PLATFORM_ADMIN_TOKEN", "admin-test")
    app = create_app()
    app.state.svc.auth_limiter = AuthLimiter(limit=2, window_s=60)
    client = TestClient(app)
    headers = {"Authorization": "Bearer sk_wrong"}
    assert client.get("/v1/usage", headers=headers).status_code == 401
    assert client.get("/v1/usage", headers=headers).status_code == 401
    blocked = client.get("/v1/usage", headers=headers)
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after") == "60"


def test_eval_http_returns_location(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_TOKEN", "admin-test")
    app = create_app()
    client = TestClient(app)
    admin = {"Authorization": "Bearer admin-test"}
    org = client.post("/v1/orgs", headers=admin, json={"name": "acme"}).json()
    created = client.post(
        "/v1/projects", headers=admin, json={"org_id": org["id"], "name": "left"}
    ).json()
    key = client.post(
        "/v1/api-keys",
        headers=admin,
        json={"project_id": created["id"], "role": "developer"},
    ).json()["key"]
    auth = {"Authorization": f"Bearer {key}"}
    prompt = client.post("/v1/prompts", headers=auth, json={"name": "p", "template": "data"}).json()
    assert client.post(f"/v1/prompts/{prompt['id']}/publish", headers=auth).status_code == 200
    agent = client.post(
        "/v1/agents",
        headers=auth,
        json={
            "name": "solo",
            "mode": "agent",
            "prompt_id": prompt["id"],
            "model_profile": "cheap",
        },
    ).json()
    assert client.post(f"/v1/agents/{agent['id']}/publish", headers=auth).status_code == 200
    accepted = client.post(
        "/v1/evals",
        headers=auth,
        json={
            "name": "modes",
            "cases": [{"query": "q", "expect_outcome": "no_answer"}],
            "experiments": [{"name": "agent", "agent_id": agent["id"]}],
        },
    )
    assert accepted.status_code == 202
    assert accepted.headers["location"] == f"/v1/evals/{accepted.json()['id']}"
    app.state.svc.model.queue.append(
        ModelOutput(text="[NO_ANSWER]", finish=True, input_tokens=0, output_tokens=1)
    )
    assert app.state.svc.work_eval() is not None
    report = client.get(accepted.headers["location"], headers=auth)
    assert report.status_code == 200
    assert report.json()["status"] == "completed"
