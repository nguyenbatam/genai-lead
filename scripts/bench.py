"""Production-like local bench: real providers, catalog models, dataset cases.

How to run (from repo root):

  set -a && source .env && set +a
  export EMBEDDING_BACKEND=fastembed QDRANT_MEMORY=1
  .venv/bin/python -m scripts.bench

Or: make bench

Reading the report
------------------
- Prefer profile `cheap` when success_rate meets the case bar and cost/latency win.
- Escalate `reason` only when cheap fails outcome or stays ungrounded.
- Embed: if fastembed hit_rate clearly beats hash on the same corpus, ship fastembed.
- Scripted unit scores are not model quality (PLAN).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from scripts.bench_lib import (
    ROOT,
    build_live_services,
    churn_cases_for_step,
    churn_entries,
    churn_families,
    compare_embed_backends,
    corpus_entries,
    ingest_knowledge,
    live_ready,
    load_catalog,
    load_dotenv_files,
    load_jsonl,
    measure_churn,
    measure_retrieval,
    measure_security_retrieval,
    pick_profile_cards,
    register_catalog_models,
    run_desk_cases,
    run_eval_compare,
    run_knowledge_cases,
    seed_project,
    smoke_providers,
    summarize_report,
)


def main() -> None:
    load_dotenv_files(ROOT / ".env", ROOT / ".env.llm")
    ready, detail = live_ready()
    if not ready:
        raise SystemExit(f"live bench not ready: {detail}")

    started = time.time()
    catalog = load_catalog()
    svc = build_live_services(memory_qdrant=True)
    principal, _raw = seed_project(svc)
    registered = register_catalog_models(svc, principal, catalog)

    provider_rows = smoke_providers(svc, principal, catalog, registered)
    cheap, reason = pick_profile_cards(registered, catalog)

    kb_id = ""
    retrieval_rows: list[dict] = []
    security_rows: list[dict] = []
    knowledge_rows: list[dict] = []
    desk_rows: list[dict] = []
    eval_report: dict = {}
    embed_compare: list[dict] = []

    try:
        kb_id = ingest_knowledge(svc, principal)
        retrieval_rows = measure_retrieval(svc, principal, kb_id)
        security_rows = measure_security_retrieval(svc, principal, kb_id)
    except (OSError, RuntimeError, ValueError) as exc:
        retrieval_rows = [{"status": "error", "reason": str(exc)[:200]}]

    if cheap is not None:
        try:
            knowledge_rows.extend(run_knowledge_cases(svc, principal, kb_id, "cheap", cheap))
        except (OSError, RuntimeError, ValueError) as exc:
            knowledge_rows.append({"profile": "cheap", "status": "error", "reason": str(exc)[:200]})
        try:
            desk_rows.extend(run_desk_cases(svc, principal, "cheap", cheap))
        except (OSError, RuntimeError, ValueError) as exc:
            desk_rows.append({"profile": "cheap", "status": "error", "reason": str(exc)[:200]})

    if reason is not None:
        try:
            knowledge_rows.extend(run_knowledge_cases(svc, principal, kb_id, "reason", reason))
        except (OSError, RuntimeError, ValueError) as exc:
            knowledge_rows.append(
                {"profile": "reason", "status": "error", "reason": str(exc)[:200]}
            )

    if cheap is not None and reason is not None:
        try:
            eval_report = run_eval_compare(svc, principal, cheap, reason)
        except (OSError, RuntimeError, ValueError) as exc:
            eval_report = {"status": "error", "reason": str(exc)[:200]}

    embed_compare = compare_embed_backends(seed_project)
    churn_report = measure_churn()
    all_churn_steps = churn_entries()

    usage_models = summarize_report(svc, principal)
    report = {
        "ready": detail,
        "elapsed_s": round(time.time() - started, 2),
        "dataset": {
            "corpus_docs": len(corpus_entries()),
            "knowledge_cases": len(load_jsonl(ROOT / "datasets" / "knowledge" / "cases.jsonl")),
            "desk_cases": len(load_jsonl(ROOT / "datasets" / "desk" / "cases.jsonl")),
            "security_cases": len(load_jsonl(ROOT / "datasets" / "security" / "cases.jsonl")),
            "security_executed": sum(1 for row in security_rows if row.get("status") == "executed"),
            "security_unit_covered": sum(
                1 for row in security_rows if row.get("status") == "unit_covered"
            ),
            "churn_families": len(churn_families()),
            "churn_steps": len(all_churn_steps),
            "churn_cases": sum(len(churn_cases_for_step(item)) for item in all_churn_steps),
        },
        "providers": provider_rows,
        "retrieval": retrieval_rows,
        "security": security_rows,
        "knowledge_runs": knowledge_rows,
        "desk_runs": desk_rows,
        "eval": eval_report,
        "embed_compare": embed_compare,
        "churn": churn_report,
        "usage_summary": usage_models,
        "decision_hints": _hints(provider_rows, knowledge_rows, embed_compare, eval_report),
    }

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "bench.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _print_summary(report, out_path)


def _hints(
    providers: list[dict],
    knowledge: list[dict],
    embeds: list[dict],
    eval_report: dict,
) -> list[str]:
    hints: list[str] = []
    ok_providers = [row for row in providers if row.get("status") == "ok"]
    if ok_providers:
        cheapest = sorted(
            ok_providers, key=lambda row: (row.get("cost") or 0, row.get("latency_ms") or 0)
        )
        best = cheapest[0]
        hints.append(
            f"smoke_ok prefer {best.get('provider')}/{best.get('model')} "
            f"tier={best.get('tier')} cost={best.get('cost')} latency_ms={best.get('latency_ms')}"
        )
    by_profile: dict[str, list[dict]] = {}
    for row in knowledge:
        by_profile.setdefault(str(row.get("profile") or ""), []).append(row)
    for profile, rows in by_profile.items():
        if not rows:
            continue
        rate = sum(1 for row in rows if row.get("success")) / len(rows)
        hints.append(f"knowledge_{profile}_success_rate={rate:.2f} n={len(rows)}")
    hash_row = next((row for row in embeds if row.get("backend") == "hash"), None)
    fast_row = next((row for row in embeds if row.get("backend") == "fastembed"), None)
    if hash_row and fast_row and hash_row.get("status") == "ok" and fast_row.get("status") == "ok":
        if float(fast_row.get("hit_rate") or 0) > float(hash_row.get("hit_rate") or 0):
            hints.append("embed: ship fastembed (higher hit_rate than hash)")
        elif float(fast_row.get("hit_rate") or 0) == float(hash_row.get("hit_rate") or 0):
            hints.append(
                "embed: hash and fastembed tied on this fixture; keep fastembed in production"
            )
        else:
            hints.append(
                "embed: hash beat fastembed on this fixture; inspect queries before switching"
            )
    preferred = eval_report.get("preferred")
    if preferred:
        hints.append(
            f"eval preferred experiment={preferred} gate_red={eval_report.get('gate_red')}"
        )
    if not hints:
        hints.append("insufficient live signal; check provider errors in report")
    return hints


def _print_summary(report: dict, path: Path) -> None:
    print(f"wrote {path}")
    print("--- providers ---")
    for row in report.get("providers") or []:
        print(
            f"  {row.get('status'):8} {row.get('provider'):12} {row.get('model')} "
            f"cred={row.get('credential_id') or '-'} tier={row.get('tier') or '-'} "
            f"{row.get('reason') or ''}"
        )
    print("--- embed_compare ---")
    for row in report.get("embed_compare") or []:
        print(f"  {row}")
    print("--- decision_hints ---")
    for hint in report.get("decision_hints") or []:
        print(f"  * {hint}")


if __name__ == "__main__":
    main()
