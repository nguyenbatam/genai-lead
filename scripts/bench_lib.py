"""Shared helpers for live bench and pytest live markers. No secrets logged."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from control.errors import PlatformError
from control.records import AgentDef, ModelCard, Principal
from control.store import new_id
from core.services import Services, budget, build_services
from core.usage_summary import summarize_usage
from core.vectors import configure_embedding, embedding_backend

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "model_catalog.yaml"
DATASETS = ROOT / "datasets"
COMPAT_PROVIDERS = frozenset({"openrouter", "groq", "cerebras", "mistral", "openai", "gemini"})


def load_dotenv_files(*paths: Path) -> None:
    """Load KEY=VALUE into os.environ without overriding existing keys."""
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_catalog(path: Path = CATALOG_PATH) -> list[dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    models = raw.get("models") or []
    if not isinstance(models, list):
        raise ValueError("model catalog models must be a list")
    return [item for item in models if isinstance(item, dict)]


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rows.append(json.loads(stripped))
    return rows


def live_ready() -> tuple[bool, str]:
    """True when credential env can load at least one OpenAI-compatible key."""
    from apps.settings import load_settings
    from core.credentials import load_credentials

    settings = load_settings()
    try:
        creds = load_credentials(settings)
    except ValueError as exc:
        return False, str(exc)
    if not creds:
        return False, "no credentials loaded (source .env and set api_key_env values)"
    if not any(item.provider in COMPAT_PROVIDERS for item in creds):
        return False, "no OpenAI-compatible provider credentials"
    return True, f"{len(creds)} credentials"


def build_live_services(*, memory_qdrant: bool = True) -> Services:
    """In-process Services with HttpModel + credential pool. No Postgres required."""
    from apps.settings import load_settings

    settings = dict(load_settings())
    # Bench stays local-memory unless caller already pointed at Compose.
    settings["database_url"] = ""
    settings["redis_url"] = ""
    if memory_qdrant and not settings.get("qdrant_url"):
        settings["qdrant_memory"] = "1"
    if not settings.get("embedding_backend"):
        settings["embedding_backend"] = "hash"
    return build_services(settings)


def seed_project(svc: Services, name: str = "bench") -> tuple[Principal, str]:
    org = svc.create_org("bench-org")
    created = svc.create_project(org.id, name, 10_000, cost_limit_usd=50)
    raw = svc.issue_key(created.id, "developer", ("finance", "hr", "it", "ops", "*"))
    principal = svc.store.authenticate(raw)
    assert principal is not None
    return principal, raw


def corpus_entries() -> list[dict]:
    """Manifest rows with absolute path and acl for every knowledge document."""
    root = DATASETS / "knowledge" / "corpus"
    rows: list[dict] = []
    for item in load_jsonl(root / "manifest.jsonl"):
        path = root / str(item["path"])
        rows.append(
            {
                "document_id": str(item["document_id"]),
                "path": path,
                "acl": tuple(item.get("acl") or ("*",)),
                "version": int(item.get("version") or 1),
                "text": path.read_text(encoding="utf-8"),
            }
        )
    return rows


def case_limit(cases: list[dict]) -> list[dict]:
    """Optional BENCH_CASE_LIMIT caps live LLM case fan-out; retrieval still uses full set."""
    raw = os.environ.get("BENCH_CASE_LIMIT", "").strip()
    if not raw:
        return cases
    try:
        limit = max(1, int(raw))
    except ValueError:
        return cases
    return cases[:limit]


def register_catalog_models(
    svc: Services, principal: Principal, catalog: list[dict]
) -> dict[str, ModelCard]:
    """Register live-eligible models. Returns catalog_id -> ModelCard."""
    out: dict[str, ModelCard] = {}
    for entry in catalog:
        if not entry.get("live", True):
            continue
        if str(entry.get("provider") or "") not in COMPAT_PROVIDERS:
            continue
        card = ModelCard(
            id=new_id("mdl"),
            project_id=principal.project_id,
            name=str(entry["name"]),
            profile=str(entry["profile"]),
            capabilities=tuple(entry.get("capabilities") or (entry["profile"],)),
            input_price=float(entry.get("input_price") or 0),
            output_price=float(entry.get("output_price") or 0),
            pricing_version=str(entry.get("pricing_version") or "2026-11"),
            provider=str(entry["provider"]),
            tier_affinity=str(entry.get("tier_affinity") or "any"),
            tier_order=str(entry.get("tier_order") or "free_paid_local"),
        )
        created = svc.register_model(principal, card)
        out[str(entry["id"])] = created
    return out


def route_profile(svc: Services, principal: Principal, profile: str, card: ModelCard) -> None:
    svc.set_route(principal, profile, card.id, None, tier_order=card.tier_order)


def smoke_providers(
    svc: Services, principal: Principal, catalog: list[dict], registered: dict[str, ModelCard]
) -> list[dict]:
    rows: list[dict] = []
    for entry in catalog:
        catalog_id = str(entry.get("id") or "")
        provider = str(entry.get("provider") or "")
        if not entry.get("live", True) or provider not in COMPAT_PROVIDERS:
            rows.append(
                {
                    "catalog_id": catalog_id,
                    "provider": provider,
                    "model": entry.get("name"),
                    "profile": entry.get("profile"),
                    "status": "skipped",
                    "reason": entry.get("skip_reason") or "not_live",
                }
            )
            continue
        card = registered.get(catalog_id)
        if card is None:
            rows.append(
                {
                    "catalog_id": catalog_id,
                    "provider": provider,
                    "model": entry.get("name"),
                    "profile": entry.get("profile"),
                    "status": "skipped",
                    "reason": "not_registered",
                }
            )
            continue
        profile = str(entry["profile"])
        route_profile(svc, principal, profile, card)
        try:
            output = svc.chat(principal, profile, card.id, ["Reply with exactly: pong"])
            usage = svc.usage(principal, limit=50)
            matched = next(
                (row for row in usage if row.model_id == card.id and row.status == "success"),
                next((row for row in usage if row.model_id == card.id), None),
            )
            rows.append(
                {
                    "catalog_id": catalog_id,
                    "provider": provider,
                    "model": card.name,
                    "profile": profile,
                    "status": "ok",
                    "text_len": len(output.text or ""),
                    "credential_id": getattr(matched, "credential_id", "") or "",
                    "tier": getattr(matched, "tier", "") or "",
                    "latency_ms": getattr(matched, "latency_ms", 0) or 0,
                    "cost": getattr(matched, "cost", 0) or 0,
                    "input_tokens": output.input_tokens,
                    "output_tokens": output.output_tokens,
                }
            )
        except PlatformError as exc:
            usage = svc.usage(principal, limit=50)
            matched = next((row for row in usage if row.model_id == card.id), None)
            rows.append(
                {
                    "catalog_id": catalog_id,
                    "provider": provider,
                    "model": card.name,
                    "profile": profile,
                    "status": "error",
                    "reason": exc.detail,
                    "http_status": exc.status,
                    "credential_id": getattr(matched, "credential_id", "") or "",
                    "tier": getattr(matched, "tier", "") or "",
                }
            )
    return rows


def ingest_knowledge(svc: Services, principal: Principal) -> str:
    kb = svc.create_kb(principal, "policies")
    for entry in corpus_entries():
        svc.submit_document(
            principal,
            kb,
            entry["document_id"],
            entry["version"],
            entry["text"],
            entry["acl"],
            f"{entry['document_id']}-v{entry['version']}",
        )
        done = svc.work()
        if done is None or done.status != "indexed":
            raise RuntimeError(
                f"ingest failed for {entry['document_id']}: {getattr(done, 'status', None)}"
            )
    return kb


def measure_retrieval(svc: Services, principal: Principal, kb_id: str) -> list[dict]:
    cases = load_jsonl(DATASETS / "knowledge" / "cases.jsonl")
    rows: list[dict] = []
    for case in cases:
        groups = tuple(case.get("groups") or ("finance",))
        hits = svc.knowledge.search(principal.project_id, kb_id, str(case["query"]), groups)
        needle = str(case.get("expect_retrieval_substr") or "")
        joined = " ".join(hit.text for hit in hits)
        hit_ok = (not needle and not hits) or (bool(needle) and needle.lower() in joined.lower())
        if not needle:
            hit_ok = len(hits) == 0
        rows.append(
            {
                "case_id": case.get("id"),
                "query": case.get("query"),
                "hit_count": len(hits),
                "hit_ok": hit_ok,
                "embedding_backend": embedding_backend(),
                "expect_retrieval_substr": needle,
            }
        )
    return rows


def _churn_marker(document_id: str, version: int) -> str:
    safe = document_id.replace("-", "_").upper()
    return f"MARKER_{safe}_V{version}"


def _churn_threshold(base: int, version: int, unit: str) -> str:
    return f"{base * version} {unit}"


def _render_churn_text(family: dict, version: int) -> str:
    doc_id = str(family["document_id"])
    topic = str(family["topic"])
    unit = str(family.get("unit") or "ngày")
    base = int(family.get("base") or 5)
    marker = _churn_marker(doc_id, version)
    threshold = _churn_threshold(base, version, unit)
    # Keep marker+threshold in ONE sentence (no '.') so child-split does not separate them.
    # Never embed previous markers — stale retrieval must not find them in served text.
    retire = " Bản trước đã bị thay thế hoàn toàn." if version > 1 else ""
    return (
        f"{topic} phiên bản {version} — ngưỡng hiện hành {threshold} với token {marker}.{retire} "
        f"Tài liệu {doc_id} chỉ còn hiệu lực ở version {version}."
    )


def churn_families(*, max_families: int | None = None) -> list[dict]:
    """Load compact seed; optional cap for fast unit runs."""
    path = DATASETS / "knowledge" / "churn" / "seed.jsonl"
    if not path.is_file():
        return []
    rows = load_jsonl(path)
    raw = os.environ.get("CHURN_MAX_FAMILIES", "").strip()
    if max_families is None and raw:
        try:
            max_families = max(1, int(raw))
        except ValueError:
            max_families = None
    if max_families is not None:
        return rows[:max_families]
    return rows


def churn_entries(*, max_families: int | None = None) -> list[dict]:
    """Expand seed into ordered version steps (continuous corpus updates)."""
    rows: list[dict] = []
    step = 0
    for family in churn_families(max_families=max_families):
        versions = max(1, int(family.get("versions") or 1))
        acl = tuple(family.get("acl") or ("*",))
        doc_id = str(family["document_id"])
        for version in range(1, versions + 1):
            step += 1
            rows.append(
                {
                    "step": step,
                    "document_id": doc_id,
                    "acl": acl,
                    "version": version,
                    "text": _render_churn_text(family, version),
                    "marker": _churn_marker(doc_id, version),
                    "prev_marker": _churn_marker(doc_id, version - 1) if version > 1 else "",
                    "threshold": _churn_threshold(
                        int(family.get("base") or 5),
                        version,
                        str(family.get("unit") or "ngày"),
                    ),
                    "topic": str(family["topic"]),
                }
            )
    return rows


def churn_cases_for_step(entry: dict) -> list[dict]:
    """Synthetic checks right after one version lands."""
    groups = list(entry["acl"]) if entry["acl"] != ("*",) else ["finance"]
    if entry["acl"] == ("*",):
        groups = ["finance", "hr", "it", "ops"]
    # Query the unique marker (not shared topic phrases) so multi-doc corpora stay decisive.
    cases = [
        {
            "id": f"{entry['document_id']}-v{entry['version']}-hit",
            "after_step": entry["step"],
            "query": entry["marker"],
            "groups": groups[:1],
            "must_contain": entry["marker"],
            "must_not_contain": entry["prev_marker"],
            "expect_empty": False,
        },
        {
            "id": f"{entry['document_id']}-v{entry['version']}-threshold",
            "after_step": entry["step"],
            "query": entry["marker"],
            "groups": groups[:1],
            "must_contain": entry["threshold"],
            "must_not_contain": entry["prev_marker"],
            "expect_empty": False,
        },
    ]
    if entry["prev_marker"]:
        # Served text must not still contain the retired marker after replace().
        cases.append(
            {
                "id": f"{entry['document_id']}-v{entry['version']}-stale",
                "after_step": entry["step"],
                "query": entry["marker"],
                "groups": groups[:1],
                "must_contain": entry["marker"],
                "must_not_contain": entry["prev_marker"],
                "expect_empty": False,
            }
        )
    return cases


def apply_churn_step(svc: Services, principal: Principal, kb_id: str, entry: dict) -> None:
    svc.submit_document(
        principal,
        kb_id,
        entry["document_id"],
        entry["version"],
        entry["text"],
        entry["acl"],
        f"churn-{entry['document_id']}-v{entry['version']}",
    )
    done = svc.work()
    if done is None or done.status != "indexed":
        raise RuntimeError(f"churn step {entry['step']} failed: {getattr(done, 'status', None)}")


def measure_churn(
    svc: Services | None = None,
    principal: Principal | None = None,
    *,
    max_families: int | None = None,
) -> dict:
    """Replay version updates; after each step assert stale markers die and new ones hit.

    Safe for CI (no live LLM). Cap with max_families / CHURN_MAX_FAMILIES for speed.
    Uses lexical Knowledge (no vectors) so unique markers stay decisive under load.
    """
    from core.rag import Knowledge

    if svc is None:
        svc = Services(knowledge=Knowledge())
    if principal is None:
        principal, _ = seed_project(svc, "churn")
    kb = svc.create_kb(principal, "churn-policies")
    entries = churn_entries(max_families=max_families)
    rows: list[dict] = []
    for entry in entries:
        apply_churn_step(svc, principal, kb, entry)
        for case in churn_cases_for_step(entry):
            groups = tuple(case.get("groups") or ("finance",))
            hits = svc.knowledge.search(principal.project_id, kb, str(case["query"]), groups)
            joined = " ".join(hit.text for hit in hits)
            must = str(case.get("must_contain") or "")
            must_not = str(case.get("must_not_contain") or "")
            expect_empty = bool(case.get("expect_empty"))
            contain_ok = (not must) or (must.lower() in joined.lower())
            absent_ok = (not must_not) or (must_not.lower() not in joined.lower())
            empty_ok = (not expect_empty) or (len(hits) == 0)
            ok = contain_ok and absent_ok and empty_ok
            rows.append(
                {
                    "case_id": case.get("id"),
                    "after_step": entry["step"],
                    "document_id": entry["document_id"],
                    "version": entry["version"],
                    "hit_count": len(hits),
                    "success": ok,
                    "must_contain": must,
                    "must_not_contain": must_not,
                }
            )
    total = len(rows)
    ok_n = sum(1 for row in rows if row.get("success"))
    return {
        "status": "ok",
        "families": len(churn_families(max_families=max_families)),
        "steps": len(entries),
        "cases": total,
        "success_rate": round(ok_n / total, 4) if total else 0.0,
        "success": ok_n,
        "rows": rows,
    }


def run_knowledge_cases(
    svc: Services, principal: Principal, kb_id: str, profile: str, card: ModelCard
) -> list[dict]:
    """Live knowledge workflow. Outcomes vary by model protocol compliance; still scored."""
    if not kb_id:
        return [{"profile": profile, "status": "skipped", "reason": "no_kb"}]
    prompt = svc.create_prompt(principal, f"knowledge-answer-{profile}", "Answer from data only.")
    prompt = svc.publish_prompt(principal, prompt.id)
    agent = AgentDef(
        id=new_id("agt"),
        project_id=principal.project_id,
        name=f"knowledge-{profile}",
        version=1,
        status="draft",
        mode="workflow",
        pattern="",
        prompt_id=prompt.id,
        model_profile=profile,
        allowed_tools=("search_knowledge_base",),
        members=(),
        entrypoint="",
        steps=("retrieve", "answer"),
        budget=budget(),
        kb_id=kb_id,
    )
    svc.create_agent(principal, agent)
    svc.publish_agent(principal, agent.id)
    route_profile(svc, principal, profile, card)
    rows: list[dict] = []
    for case in case_limit(load_jsonl(DATASETS / "knowledge" / "cases.jsonl")):
        groups = tuple(case.get("groups") or ("finance",))
        runner = principal
        if set(groups) - set(principal.groups) and "*" not in principal.groups:
            raw = svc.issue_key(principal.project_id, "developer", groups)
            found = svc.store.authenticate(raw)
            assert found is not None
            runner = found
        elif set(groups) != set(principal.groups) and "*" not in groups:
            # Narrow ACL for cross-group denial cases (e.g. hr-only key).
            raw = svc.issue_key(principal.project_id, "developer", groups)
            found = svc.store.authenticate(raw)
            assert found is not None
            runner = found
        try:
            run = svc.start_run(runner, agent.id, str(case["query"]))
            expect = str(case.get("expect_outcome") or "completed")
            cite = str(case.get("expect_cite_substr") or "")
            cite_ok = (not cite) or cite.lower() in (run.answer or "").lower()
            rows.append(
                {
                    "case_id": case.get("id"),
                    "profile": profile,
                    "model": card.name,
                    "provider": card.provider,
                    "outcome": run.outcome,
                    "expect_outcome": expect,
                    "success": run.outcome == expect,
                    "cite_ok": cite_ok if expect == "completed" else True,
                    "cost": run.cost,
                    "tokens": run.tokens,
                    "latency_ms": max(0, (run.finished_ms or 0) - (run.started_ms or 0)),
                }
            )
        except PlatformError as exc:
            rows.append(
                {
                    "case_id": case.get("id"),
                    "profile": profile,
                    "model": card.name,
                    "provider": card.provider,
                    "outcome": "failed",
                    "expect_outcome": case.get("expect_outcome"),
                    "success": False,
                    "reason": exc.detail,
                }
            )
    return rows


def run_desk_cases(
    svc: Services, principal: Principal, profile: str, card: ModelCard
) -> list[dict]:
    prompt = svc.create_prompt(principal, f"desk-{profile}", "Open a ticket when asked.")
    prompt = svc.publish_prompt(principal, prompt.id)
    agent = AgentDef(
        id=new_id("agt"),
        project_id=principal.project_id,
        name=f"desk-{profile}",
        version=1,
        status="draft",
        mode="agent",
        pattern="",
        prompt_id=prompt.id,
        model_profile=profile,
        allowed_tools=("create_record",),
        members=(),
        entrypoint="",
        steps=(),
        budget=budget(),
        kb_id="",
    )
    svc.create_agent(principal, agent)
    svc.publish_agent(principal, agent.id)
    route_profile(svc, principal, profile, card)
    rows: list[dict] = []
    for case in case_limit(load_jsonl(DATASETS / "desk" / "cases.jsonl")):
        try:
            run = svc.start_run(principal, agent.id, str(case["query"]))
            expect = str(case.get("expect_outcome") or "waiting_approval")
            rows.append(
                {
                    "case_id": case.get("id"),
                    "profile": profile,
                    "model": card.name,
                    "provider": card.provider,
                    "outcome": run.outcome,
                    "expect_outcome": expect,
                    "success": run.outcome == expect,
                    "pending": bool(run.pending_payload),
                    "cost": run.cost,
                    "tokens": run.tokens,
                }
            )
        except PlatformError as exc:
            rows.append(
                {
                    "case_id": case.get("id"),
                    "profile": profile,
                    "model": card.name,
                    "provider": card.provider,
                    "outcome": "failed",
                    "success": False,
                    "reason": exc.detail,
                }
            )
    return rows


def measure_security_retrieval(svc: Services, principal: Principal, kb_id: str) -> list[dict]:
    """Execute security dataset checks that do not need a live LLM.

    - no_answer: ACL retrieval must be empty for the case groups
    - denied / waiting_approval / completed / unverified: counted as unit-covered
      (see tests/test_security_dataset.py); reported here for bench dataset totals
    """
    rows: list[dict] = []
    for case in load_jsonl(DATASETS / "security" / "cases.jsonl"):
        groups = tuple(case.get("groups") or ("finance",))
        expect = str(case.get("expect_outcome") or "")
        if expect == "no_answer":
            hits = svc.knowledge.search(principal.project_id, kb_id, str(case["query"]), groups)
            rows.append(
                {
                    "case_id": case.get("id"),
                    "status": "executed",
                    "hit_count": len(hits),
                    "success": len(hits) == 0,
                    "expect_outcome": expect,
                }
            )
            continue
        rows.append(
            {
                "case_id": case.get("id"),
                "status": "unit_covered",
                "success": True,
                "expect_outcome": expect,
                "note": "asserted in tests/test_security_dataset.py",
            }
        )
    return rows


def run_eval_compare(
    svc: Services, principal: Principal, cheap: ModelCard, reason: ModelCard
) -> dict:
    prompt = svc.create_prompt(principal, "eval-prompt", "If unsure reply [NO_ANSWER].")
    prompt = svc.publish_prompt(principal, prompt.id)

    def _agent(name: str, profile: str) -> AgentDef:
        agent = AgentDef(
            id=new_id("agt"),
            project_id=principal.project_id,
            name=name,
            version=1,
            status="draft",
            mode="agent",
            pattern="",
            prompt_id=prompt.id,
            model_profile=profile,
            allowed_tools=(),
            members=(),
            entrypoint="",
            steps=(),
            budget=budget(),
            kb_id="",
        )
        svc.create_agent(principal, agent)
        return svc.publish_agent(principal, agent.id)

    route_profile(svc, principal, "cheap", cheap)
    route_profile(svc, principal, "reason", reason)
    a_cheap = _agent("exp-cheap", "cheap")
    a_reason = _agent("exp-reason", "reason")
    cases = [
        {"query": "Reply with [NO_ANSWER] only.", "expect_outcome": "no_answer"},
        {"query": "Say hello in one word.", "expect_outcome": "completed"},
    ]
    job = svc.submit_eval(
        principal,
        "profile-compare",
        cases,
        [
            {"name": "cheap", "agent_id": a_cheap.id},
            {"name": "reason", "agent_id": a_reason.id},
        ],
    )
    done = svc.work_eval()
    while done is None and job["status"] == "queued":
        done = svc.work_eval()
    report = (done or {}).get("report") or svc.eval_report(principal, job["id"]).get("report") or {}
    return {
        "eval_id": job["id"],
        "status": (done or job).get("status"),
        "preferred": report.get("preferred"),
        "gate_red": report.get("gate_red"),
        "experiments": report.get("experiments"),
    }


def pick_profile_cards(
    registered: dict[str, ModelCard], catalog: list[dict]
) -> tuple[ModelCard | None, ModelCard | None]:
    cheap = None
    reason = None
    by_id = {str(item["id"]): item for item in catalog}
    for catalog_id, card in registered.items():
        entry = by_id.get(catalog_id) or {}
        if entry.get("profile") == "cheap" and cheap is None:
            cheap = card
        if entry.get("profile") == "reason" and reason is None:
            reason = card
    return cheap, reason


def summarize_report(svc: Services, principal: Principal) -> list[dict]:
    rows = svc.usage(principal, limit=5_000)
    return summarize_usage(rows)


def compare_embed_backends(principal_factory, cases: list[dict] | None = None) -> list[dict]:
    """Ingest full corpus with hash then fastembed; return hit-rate rows."""
    from core.rag import Knowledge
    from core.services import Services as Svc
    from core.vectors import QdrantIndex, VectorError

    cases = cases if cases is not None else load_jsonl(DATASETS / "knowledge" / "cases.jsonl")
    out: list[dict] = []
    for backend in ("hash", "fastembed"):
        try:
            configure_embedding(backend)
            svc = Svc(knowledge=Knowledge(QdrantIndex()))
            principal, _ = principal_factory(svc)
            kb = ingest_knowledge(svc, principal)
            hits_ok = 0
            total = 0
            for case in cases:
                total += 1
                groups = tuple(case.get("groups") or ("finance",))
                hits = svc.knowledge.search(principal.project_id, kb, str(case["query"]), groups)
                needle = str(case.get("expect_retrieval_substr") or "")
                joined = " ".join(hit.text for hit in hits)
                if not needle:
                    ok = len(hits) == 0
                else:
                    ok = needle.lower() in joined.lower()
                hits_ok += int(ok)
            out.append(
                {
                    "backend": backend,
                    "status": "ok",
                    "docs": len(corpus_entries()),
                    "hit_rate": round(hits_ok / total, 4) if total else 0.0,
                    "hits_ok": hits_ok,
                    "total": total,
                }
            )
        except (VectorError, ImportError, OSError, RuntimeError, ValueError) as exc:
            out.append({"backend": backend, "status": "error", "reason": str(exc)[:200]})
    configure_embedding("hash")
    return out


__all__ = [
    "COMPAT_PROVIDERS",
    "DATASETS",
    "ROOT",
    "build_live_services",
    "apply_churn_step",
    "case_limit",
    "churn_cases_for_step",
    "churn_entries",
    "churn_families",
    "compare_embed_backends",
    "corpus_entries",
    "ingest_knowledge",
    "live_ready",
    "load_catalog",
    "load_dotenv_files",
    "load_jsonl",
    "measure_churn",
    "measure_retrieval",
    "measure_security_retrieval",
    "pick_profile_cards",
    "register_catalog_models",
    "run_desk_cases",
    "run_eval_compare",
    "run_knowledge_cases",
    "seed_project",
    "smoke_providers",
    "summarize_report",
]
