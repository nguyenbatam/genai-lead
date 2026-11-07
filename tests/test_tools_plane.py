"""Tool plane: remote execute after local policy; in-process fallback."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.tools.main import app as tools_app
from control.store import Store
from control.vocab import TOOL_OK, TOOL_WAITING_APPROVAL
from core.tools import ToolRuntime, ToolsClient, ToolSpec


def test_tools_service_catalog_and_execute(monkeypatch):
    monkeypatch.setenv("TOOLS_TOKEN", "secret")
    client = TestClient(tools_app)
    assert client.get("/health").json()["status"] == "ok"
    denied = client.get("/v1/catalog")
    assert denied.status_code == 401
    catalog = client.get("/v1/catalog", headers={"X-Tools-Token": "secret"})
    assert catalog.status_code == 200
    names = {item["name"] for item in catalog.json()["tools"]}
    assert names == {"create_record", "note"}
    executed = client.post(
        "/v1/execute",
        headers={"X-Tools-Token": "secret"},
        json={"name": "note", "args": {"title": "hi"}},
    )
    assert executed.status_code == 200
    assert executed.json()["result"] == {"title": "hi"}
    missing = client.post(
        "/v1/execute",
        headers={"X-Tools-Token": "secret"},
        json={"name": "nope", "args": {}},
    )
    assert missing.status_code == 404


def test_remote_runtime_keeps_approval_and_idempotency_local():
    store = Store()
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append((body["name"], body["args"]))
        return httpx.Response(200, json={"result": dict(body["args"])})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(
        transport=transport,
        base_url="http://tools.test",
        headers={"X-Tools-Token": "t"},
    )
    remote = ToolsClient("http://tools.test", "t", client=http)
    runtime = ToolRuntime(store, remote=remote)
    runtime.register(ToolSpec("create_record", "*", "write", True, remote=True))

    waiting = runtime.call(
        "p1",
        "create_record",
        {"title": "x"},
        {"create_record"},
        "idem-1",
        set(),
    )
    assert waiting["status"] == TOOL_WAITING_APPROVAL
    assert calls == []

    payload = waiting["payload"]
    first = runtime.call(
        "p1",
        "create_record",
        {"title": "x"},
        {"create_record"},
        "idem-1",
        {payload},
    )
    assert first["status"] == TOOL_OK
    assert first["replayed"] is False
    assert calls == [("create_record", {"title": "x"})]
    effect = first["effect"]
    assert isinstance(effect, tuple)
    store.put_effect(effect[0], effect[1], effect[2])

    second = runtime.call(
        "p1",
        "create_record",
        {"title": "x"},
        {"create_record"},
        "idem-1",
        {payload},
    )
    assert second["status"] == TOOL_OK
    assert second["replayed"] is True
    assert calls == [("create_record", {"title": "x"})]
    remote.close()


def test_in_process_write_tool_still_works():
    store = Store()
    runtime = ToolRuntime(store)
    runtime.register(
        ToolSpec("note", "*", "write", False),
        lambda args: dict(args),
    )
    result = runtime.call("p1", "note", {"title": "n"}, {"note"}, "idem-n", set())
    assert result["status"] == TOOL_OK
    assert result["result"]["title"] == "n"


def test_build_services_requires_token_with_url():
    from control.errors import PlatformError
    from core.services import build_services

    with pytest.raises(PlatformError):
        build_services(
            {
                "database_url": "",
                "redis_url": "",
                "key_pepper": "",
                "llm_base_url": "",
                "llm_api_key": "",
                "llm_credentials": "",
                "llm_credentials_file": "",
                "tools_url": "http://tools:8081",
                "tools_token": "",
                "qdrant_url": "",
                "embedding_backend": "hash",
                "embedding_model": "x",
                "qdrant_memory": "",
                "object_storage_dir": "",
            }
        )
