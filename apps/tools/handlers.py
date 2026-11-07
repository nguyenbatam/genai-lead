"""Executable tool bodies. No tenancy or approval logic here."""

from __future__ import annotations

from typing import Any

# Catalog metadata mirrors platform ToolSpec fields the control plane cares about.
CATALOG: tuple[dict[str, Any], ...] = (
    {"name": "create_record", "risk": "write", "requires_approval": True},
    {"name": "note", "risk": "write", "requires_approval": False},
)


def create_record(args: dict) -> dict:
    return dict(args)


def note(args: dict) -> dict:
    return dict(args)


HANDLERS: dict[str, Any] = {
    "create_record": create_record,
    "note": note,
}


def execute(name: str, args: dict) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        raise KeyError(name)
    if not isinstance(args, dict):
        raise TypeError("args must be an object")
    return handler(args)
