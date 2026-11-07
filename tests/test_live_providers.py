"""Opt-in live provider smoke. Skipped unless credentials load."""

from __future__ import annotations

import pytest

from scripts.bench_lib import (
    COMPAT_PROVIDERS,
    ROOT,
    build_live_services,
    live_ready,
    load_catalog,
    load_dotenv_files,
    register_catalog_models,
    seed_project,
    smoke_providers,
)

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def _env() -> None:
    load_dotenv_files(ROOT / ".env", ROOT / ".env.llm")


def test_live_openai_compatible_providers_record_credential(_env: None) -> None:
    ready, detail = live_ready()
    if not ready:
        pytest.skip(detail)
    catalog = load_catalog()
    svc = build_live_services(memory_qdrant=False)
    principal, _ = seed_project(svc, "live-smoke")
    registered = register_catalog_models(svc, principal, catalog)
    if not registered:
        pytest.skip("no live catalog models registered")
    rows = smoke_providers(svc, principal, catalog, registered)
    attempted = [row for row in rows if row.get("provider") in COMPAT_PROVIDERS]
    assert attempted, "expected at least one OpenAI-compatible catalog entry"
    ok = [row for row in attempted if row.get("status") == "ok"]
    if not ok:
        reasons = {row.get("provider"): row.get("reason") for row in attempted}
        pytest.skip(f"no provider succeeded: {reasons}")
    assert any(row.get("credential_id") for row in ok)
