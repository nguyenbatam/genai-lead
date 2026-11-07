"""Tool registry. Permission, approval, and idempotency live here, not in the prompt."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from control.store import Store
from control.vocab import RISK_WRITE, TOOL_DENIED, TOOL_OK, TOOL_RISKS, TOOL_WAITING_APPROVAL

Handler = Callable[[dict], dict]
OnWrite = Callable[[dict], None]

TOOLS_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class ToolSpec:
    name: str
    project_id: str
    risk: str
    requires_approval: bool
    remote: bool = False


class ToolsClient:
    """HTTP client for the tool execution plane."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = TOOLS_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owns = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            headers={"X-Tools-Token": token},
        )

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def execute(self, name: str, args: dict) -> dict:
        response = self._client.post("/v1/execute", json={"name": name, "args": args})
        if response.status_code == 404:
            raise KeyError(name)
        response.raise_for_status()
        body = response.json()
        result = body.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("tools execute returned invalid result")
        return result

    def catalog(self) -> list[dict]:
        response = self._client.get("/v1/catalog")
        response.raise_for_status()
        tools = response.json().get("tools") or []
        if not isinstance(tools, list):
            raise RuntimeError("tools catalog invalid")
        return [item for item in tools if isinstance(item, dict)]


class ToolRuntime:
    def __init__(
        self,
        store: Store,
        on_write: OnWrite | None = None,
        remote: ToolsClient | None = None,
    ) -> None:
        self.store = store
        self.on_write = on_write
        self.remote = remote
        self.specs: dict[tuple[str, str], ToolSpec] = {}
        self.handlers: dict[tuple[str, str], Handler] = {}
        self.applied: list[str] = []

    def register(self, spec: ToolSpec, handler: Handler | None = None) -> None:
        if spec.risk not in TOOL_RISKS:
            raise ValueError("tool risk must be read or write")
        if spec.remote and handler is not None:
            raise ValueError("remote tools must not register a local handler")
        if not spec.remote and handler is None:
            raise ValueError("local tools require a handler")
        key = (spec.project_id, spec.name)
        self.specs[key] = spec
        if handler is not None:
            self.handlers[key] = handler

    def names(self, project_id: str) -> set[str]:
        return {name for (owner, name) in self.specs if owner in {project_id, "*"}}

    def call(
        self,
        project_id: str,
        name: str,
        args: dict,
        allowed: tuple[str, ...] | set[str],
        idem_key: str,
        approved: set[str],
    ) -> dict:
        if name not in allowed:
            return {"status": TOOL_DENIED}
        spec = self.specs.get((project_id, name)) or self.specs.get(("*", name))
        if spec is None:
            return {"status": TOOL_DENIED}
        payload = json.dumps({"args": args, "tool": name}, sort_keys=True)
        if spec.requires_approval and payload not in approved:
            return {
                "status": TOOL_WAITING_APPROVAL,
                "payload": payload,
                "tool": name,
                "args": args,
            }
        if spec.risk == RISK_WRITE:
            found = self.store.effect(idem_key)
            if found is not None:
                return {"status": TOOL_OK, "result": found, "replayed": True}
            preview = dict(self._invoke(spec, args))
            preview.setdefault("id", _effect_id(idem_key))
            return {
                "status": TOOL_OK,
                "result": preview,
                "replayed": False,
                "effect": (project_id, idem_key, preview),
            }
        result = self._invoke(spec, args)
        return {"status": TOOL_OK, "result": result, "replayed": False}

    def _invoke(self, spec: ToolSpec, args: dict) -> dict:
        if spec.remote:
            if self.remote is None:
                raise RuntimeError(f"remote tool {spec.name} has no ToolsClient")
            return self.remote.execute(spec.name, args)
        return self.handlers[(spec.project_id, spec.name)](args)

    def observe_write(self, idem_key: str, result: dict) -> None:
        self.applied.append(idem_key)
        if self.on_write is not None:
            self.on_write(result)


def _effect_id(idem_key: str) -> str:
    return "rec_" + hashlib.sha256(idem_key.encode()).hexdigest()[:12]
