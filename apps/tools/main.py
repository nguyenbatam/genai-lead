"""HTTP tool execution service. Platform calls this after policy gates."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from apps.tools.handlers import CATALOG, execute

app = FastAPI(title="GenAI Tools", version="1")


class ExecuteIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)


def _token() -> str:
    return os.environ.get("TOOLS_TOKEN", "")


def _authorize(header: str | None) -> None:
    expected = _token()
    if not expected:
        raise HTTPException(status_code=503, detail="TOOLS_TOKEN is not configured")
    if not header or header != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/catalog")
def catalog(x_tools_token: str | None = Header(default=None)) -> dict[str, Any]:
    _authorize(x_tools_token)
    return {"tools": list(CATALOG)}


@app.post("/v1/execute")
def run_tool(
    body: ExecuteIn,
    x_tools_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _authorize(x_tools_token)
    try:
        result = execute(body.name, body.args)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="tool not found") from exc
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"result": result}


def main() -> None:
    import uvicorn

    port = int(os.environ.get("TOOLS_PORT", "8081"))
    uvicorn.run("apps.tools.main:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
