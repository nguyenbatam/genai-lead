"""Compose stack smoke. Skipped when Postgres/Redis are down."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from control.errors import PlatformError
from control.records import ModelCard
from control.store import new_id
from core.model import ModelOutput
from core.services import build_services
from scripts.bench_lib import apply_churn_step, churn_entries

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]


def _compose_settings() -> dict[str, str] | None:
    database_url = os.environ.get("DATABASE_URL", "")
    redis_url = os.environ.get("REDIS_URL", "")
    pepper = os.environ.get("PLATFORM_KEY_PEPPER", "")
    if not database_url or not redis_url:
        return None
    if not pepper:
        pepper = "integration-test-pepper"
    return {
        "database_url": database_url,
        "redis_url": redis_url,
        "key_pepper": pepper,
        "admin_token": os.environ.get("PLATFORM_ADMIN_TOKEN", "local-admin"),
        "llm_base_url": "",
        "llm_api_key": "",
        "llm_credentials": "",
        "llm_credentials_file": "",
        "qdrant_url": os.environ.get("QDRANT_URL", ""),
        "embedding_backend": "hash",
        "embedding_model": "BAAI/bge-small-en-v1.5",
        "qdrant_memory": "",
        "object_storage_dir": "",
        "metrics_port": "",
        "shutdown_drain_s": "15",
    }


def _services():
    settings = _compose_settings()
    if settings is None:
        pytest.skip("DATABASE_URL and REDIS_URL required")
    try:
        return build_services(settings)
    except (PlatformError, OSError) as exc:
        pytest.skip(f"compose stack unreachable: {exc}")


def test_postgres_redis_auth_and_route() -> None:
    svc = _services()
    org = svc.create_org("integration")
    project = svc.create_project(org.id, "compose", 100)
    raw = svc.issue_key(project.id, "developer", ("*",))
    principal = svc.store.authenticate(raw)
    assert principal is not None
    card = svc.register_model(
        principal,
        ModelCard(
            id=new_id("mdl"),
            project_id=principal.project_id,
            name="scripted-small",
            profile="cheap",
            capabilities=("cheap",),
            input_price=0.0,
            output_price=0.0,
            pricing_version="2026-11",
        ),
    )
    svc.set_route(principal, "cheap", card.id, None)
    svc.model.queue.append(ModelOutput(text="ok", finish=True, input_tokens=1, output_tokens=1))
    output = svc.chat(principal, "cheap", None, ["ping"])
    assert output.text == "ok"
    rows = svc.usage(principal)
    assert rows
    assert rows[0].model_id == card.id


def test_postgres_ingest_and_acl_search() -> None:
    svc = _services()
    org = svc.create_org("integration-rag")
    project = svc.create_project(org.id, "rag", 100)
    finance_raw = svc.issue_key(project.id, "developer", ("finance",))
    hr_raw = svc.issue_key(project.id, "developer", ("hr",))
    finance = svc.store.authenticate(finance_raw)
    hr = svc.store.authenticate(hr_raw)
    assert finance is not None and hr is not None

    corpus = (ROOT / "datasets" / "knowledge" / "corpus" / "finance_approval.md").read_text(
        encoding="utf-8"
    )
    kb = svc.create_kb(finance, "policies")
    svc.submit_document(finance, kb, "finance-approval", 1, corpus, ("finance",), "integ-pol-1")
    done = svc.work()
    assert done is not None
    assert done.status == "indexed"

    hits = svc.knowledge.search(finance.project_id, kb, "CFO phê duyệt", ("finance",))
    assert hits
    assert svc.knowledge.search(hr.project_id, kb, "CFO phê duyệt", ("hr",)) == []


def test_postgres_churn_one_family_retires_stale_marker() -> None:
    svc = _services()
    org = svc.create_org("integration-churn")
    project = svc.create_project(org.id, "churn", 100)
    raw = svc.issue_key(project.id, "developer", ("finance",))
    principal = svc.store.authenticate(raw)
    assert principal is not None
    kb = svc.create_kb(principal, "churn-kb")
    steps = [item for item in churn_entries(max_families=1)]
    assert len(steps) >= 2
    apply_churn_step(svc, principal, kb, steps[0])
    apply_churn_step(svc, principal, kb, steps[1])
    joined = " ".join(
        hit.text
        for hit in svc.knowledge.search(principal.project_id, kb, steps[1]["marker"], ("finance",))
    )
    assert steps[1]["marker"] in joined
    assert steps[0]["marker"] not in joined
