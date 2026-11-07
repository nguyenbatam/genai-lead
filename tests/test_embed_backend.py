"""Unit coverage for hash/fastembed configuration (no network)."""

from __future__ import annotations

import pytest

from core.vectors import (
    FASTEMBED_DIM,
    HASH_DIM,
    configure_embedding,
    embed,
    embedding_backend,
    embedding_dim,
)


@pytest.fixture(autouse=True)
def _reset_hash_backend():
    configure_embedding("hash")
    yield
    configure_embedding("hash")


def test_hash_embed_is_stable_and_normalized():
    configure_embedding("hash")
    assert embedding_backend() == "hash"
    assert embedding_dim() == HASH_DIM
    left = embed("CFO phê duyệt ngân sách")
    right = embed("CFO phê duyệt ngân sách")
    assert left == right
    assert len(left) == HASH_DIM
    norm = sum(item * item for item in left) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_configure_rejects_unknown_backend():
    with pytest.raises(ValueError):
        configure_embedding("mystery")


def test_collection_name_includes_dim_suffix():
    from core.vectors import QdrantIndex

    configure_embedding("hash")
    index = QdrantIndex(location=":memory:")
    name = index._collection("prj_demo")
    assert name.endswith(f"_d{HASH_DIM}")
    assert embedding_dim() == HASH_DIM
    # Touch FASTEMBED_DIM so renames stay noticed.
    assert FASTEMBED_DIM == 384
