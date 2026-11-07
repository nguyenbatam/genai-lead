"""Opt-in live RAG retrieval with fastembed. Skipped without backend or model."""

from __future__ import annotations

import pytest

from core.rag import Knowledge
from core.services import Services
from core.vectors import QdrantIndex, VectorError, configure_embedding, embedding_dim
from scripts.bench_lib import (
    DATASETS,
    corpus_entries,
    ingest_knowledge,
    load_jsonl,
    measure_retrieval,
    seed_project,
)

pytestmark = pytest.mark.live


def test_fastembed_retrieval_hits_full_corpus() -> None:
    try:
        configure_embedding("fastembed")
    except ValueError as exc:
        pytest.skip(str(exc))
    try:
        from core.vectors import embed

        vector = embed("CFO phê duyệt")
    except (VectorError, ImportError, OSError) as exc:
        configure_embedding("hash")
        pytest.skip(f"fastembed unavailable: {exc}")
    assert len(vector) == embedding_dim() == 384
    assert len(corpus_entries()) >= 8

    svc = Services(knowledge=Knowledge(QdrantIndex()))
    principal, _ = seed_project(svc, "live-rag")
    kb = ingest_knowledge(svc, principal)
    rows = measure_retrieval(svc, principal, kb)
    hits_ok = sum(1 for row in rows if row.get("hit_ok"))
    configure_embedding("hash")
    cases = load_jsonl(DATASETS / "knowledge" / "cases.jsonl")
    assert hits_ok >= int(0.7 * len(cases)), (
        f"expected >=70% retrieval hits, got {hits_ok}/{len(cases)}"
    )
