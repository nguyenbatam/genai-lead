"""Thin OpenAPI client for sample apps. Talks HTTP only."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DONE = frozenset({"completed", "failed", "cancelled"})
STOP = frozenset({"completed", "failed", "cancelled", "waiting_approval"})


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} -> {status}: {body}")
        self.status = status
        self.body = body


class PlatformClient:
    def __init__(self, base_url: str, token: str, *, timeout_s: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PlatformClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, headers=self._headers, **kwargs)
        if response.status_code >= 400:
            raise ApiError(method, path, response.status_code, response.text)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def post(self, path: str, json_body: dict | None = None) -> Any:
        return self.request("POST", path, json=json_body or {})

    def get(self, path: str) -> Any:
        return self.request("GET", path)


def env_base_url() -> str:
    return os.environ.get("PLATFORM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def env_admin_token() -> str:
    token = os.environ.get("PLATFORM_ADMIN_TOKEN", "")
    if not token:
        raise SystemExit("Set PLATFORM_ADMIN_TOKEN (see .env.example)")
    return token


def env_api_key() -> str | None:
    key = os.environ.get("PLATFORM_API_KEY", "").strip()
    return key or None


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"Missing {path}. Run register.py first.")
    return load_json(path)


def bootstrap_project(
    admin: PlatformClient,
    *,
    org_name: str,
    project_name: str,
    groups: list[str],
) -> tuple[str, str, str]:
    """Create org + project + developer key. Returns org_id, project_id, raw key."""
    org = admin.post("/v1/orgs", {"name": org_name})
    project = admin.post(
        "/v1/projects",
        {"org_id": org["id"], "name": project_name},
    )
    issued = admin.post(
        "/v1/api-keys",
        {
            "project_id": project["id"],
            "role": "developer",
            "groups": groups,
        },
    )
    return org["id"], project["id"], issued["key"]


def ensure_cheap_route(client: PlatformClient, *, name: str = "example-small") -> str:
    model = client.post(
        "/v1/models",
        {
            "name": name,
            "profile": "cheap",
            "capabilities": ["cheap"],
            "input_price": 0,
            "output_price": 0,
            "pricing_version": "2026-11",
        },
    )
    client.post("/v1/routes", {"task": "cheap", "model_id": model["id"]})
    return model["id"]


def publish_prompt(client: PlatformClient, name: str, template: str) -> str:
    prompt = client.post("/v1/prompts", {"name": name, "template": template})
    client.post(f"/v1/prompts/{prompt['id']}/publish")
    return prompt["id"]


def create_and_publish_agent(client: PlatformClient, body: dict) -> dict:
    agent = client.post("/v1/agents", body)
    return client.post(f"/v1/agents/{agent['id']}/publish")


def wait_job(
    client: PlatformClient,
    kb_id: str,
    job_id: str,
    *,
    timeout_s: float = 120.0,
    poll_s: float = 0.5,
) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = client.get(f"/v1/knowledge-bases/{kb_id}/jobs/{job_id}")
        if job["status"] in {"indexed", "failed"}:
            return job
        time.sleep(poll_s)
    raise TimeoutError(f"ingest job {job_id} still running after {timeout_s}s")


def wait_run(
    client: PlatformClient,
    run_id: str,
    *,
    timeout_s: float = 120.0,
    poll_s: float = 0.5,
    stop_on_approval: bool = True,
) -> dict:
    deadline = time.time() + timeout_s
    stop = STOP if stop_on_approval else DONE
    while time.time() < deadline:
        run = client.get(f"/v1/runs/{run_id}")
        if run["status"] in stop:
            return run
        time.sleep(poll_s)
    raise TimeoutError(f"run {run_id} still open after {timeout_s}s")
