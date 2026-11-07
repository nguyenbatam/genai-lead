"""Qdrant index. One collection per project. Hash for CI; fastembed for production-like bench."""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

HASH_DIM = 64
FASTEMBED_DIM = 384
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"

_backend = "hash"
_model_name = DEFAULT_FASTEMBED_MODEL
_dim = HASH_DIM
_fastembed_model: object | None = None


class VectorError(Exception):
    pass


class Child(Protocol):
    id: str
    project_id: str
    kb_id: str
    document_id: str
    version: int
    parent_id: str
    text: str
    acl: tuple[str, ...]


@dataclass(frozen=True)
class Hit:
    id: str
    project_id: str
    kb_id: str
    document_id: str
    version: int
    parent_id: str
    text: str
    acl: tuple[str, ...]


def configure_embedding(backend: str = "hash", model: str = DEFAULT_FASTEMBED_MODEL) -> None:
    """Set process-wide embed backend. Call once at process start."""
    global _backend, _model_name, _dim, _fastembed_model
    name = (backend or "hash").strip().lower()
    if name not in {"hash", "fastembed"}:
        raise ValueError("embedding_backend must be hash or fastembed")
    _backend = name
    _model_name = model or DEFAULT_FASTEMBED_MODEL
    _fastembed_model = None
    _dim = FASTEMBED_DIM if name == "fastembed" else HASH_DIM


def embedding_backend() -> str:
    return _backend


def embedding_dim() -> int:
    return _dim


def embed(text: str) -> list[float]:
    if _backend == "fastembed":
        return _embed_fastembed(text)
    return _embed_hash(text)


def _embed_hash(text: str) -> list[float]:
    """Stable bag-of-tokens vector. Same text hashes the same in every process."""
    vector = [0.0] * HASH_DIM
    tokens = text.lower().split()
    if not tokens:
        vector[0] = 1.0
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode()).digest()
        bucket = int.from_bytes(digest[:4], "big") % HASH_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(item * item for item in vector)) or 1.0
    return [item / norm for item in vector]


def _embed_fastembed(text: str) -> list[float]:
    model = _load_fastembed()
    vectors = list(model.embed([text]))
    if not vectors:
        raise VectorError("fastembed returned no vector")
    raw = [float(item) for item in vectors[0]]
    if len(raw) != FASTEMBED_DIM:
        raise VectorError(f"fastembed dim {len(raw)} != {FASTEMBED_DIM}")
    return raw


def _load_fastembed() -> object:
    global _fastembed_model
    if _fastembed_model is not None:
        return _fastembed_model
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise VectorError("fastembed is not installed") from exc
    _fastembed_model = TextEmbedding(model_name=_model_name)
    return _fastembed_model


class QdrantIndex:
    def __init__(self, url: str = "", location: str = "") -> None:
        from qdrant_client import QdrantClient

        if url:
            self.client = QdrantClient(url=url, timeout=2, check_compatibility=False)
        else:
            self.client = QdrantClient(location=location or ":memory:")
        self._ready: set[str] = set()

    def ping(self) -> bool:
        try:
            self.client.get_collections()
        except _DOWN:
            return False
        return True

    def replace(
        self, project_id: str, kb_id: str, document_id: str, children: Sequence[Child]
    ) -> None:
        self._delete(project_id, kb_id, document_id)
        if not children:
            return
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(
                id=_point_id(child.id),
                vector=embed(child.text),
                payload=_payload(child),
            )
            for child in children
        ]
        try:
            self.client.upsert(self._collection(project_id), points=points)
        except _DOWN as exc:
            raise VectorError("qdrant write failed") from exc

    def delete_document(self, project_id: str, kb_id: str, document_id: str) -> None:
        self._delete(project_id, kb_id, document_id)

    def search(self, project_id: str, kb_id: str, query: str, groups: tuple[str, ...]) -> list[Hit]:
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

        allowed = list(dict.fromkeys([*groups, "*"]))
        query_filter = Filter(
            must=[
                FieldCondition(key="kb_id", match=MatchValue(value=kb_id)),
                FieldCondition(key="kind", match=MatchValue(value="child")),
                FieldCondition(key="acl", match=MatchAny(any=allowed)),
            ]
        )
        try:
            found = self.client.query_points(
                self._collection(project_id),
                query=embed(query),
                query_filter=query_filter,
                limit=4,
            )
        except _DOWN as exc:
            raise VectorError("qdrant read failed") from exc
        hits: list[Hit] = []
        for point in found.points:
            payload = point.payload or {}
            if payload.get("project_id") != project_id or payload.get("kb_id") != kb_id:
                continue
            hits.append(
                Hit(
                    id=str(payload.get("id") or ""),
                    project_id=project_id,
                    kb_id=kb_id,
                    document_id=str(payload.get("document_id") or ""),
                    version=int(payload.get("version") or 0),
                    parent_id=str(payload.get("parent_id") or ""),
                    text=str(payload.get("text") or ""),
                    acl=tuple(payload.get("acl") or ()),
                )
            )
        return hits

    def _delete(self, project_id: str, kb_id: str, document_id: str) -> None:
        from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

        name = self._collection(project_id)
        if not self._exists(name):
            return
        selector = FilterSelector(
            filter=Filter(
                must=[
                    FieldCondition(key="kb_id", match=MatchValue(value=kb_id)),
                    FieldCondition(key="document_id", match=MatchValue(value=document_id)),
                ]
            )
        )
        try:
            self.client.delete(name, points_selector=selector)
        except _DOWN as exc:
            raise VectorError("qdrant write failed") from exc

    def _collection(self, project_id: str) -> str:
        # Dim suffix avoids collisions when switching hash <-> fastembed.
        name = f"p_{project_id.replace('-', '_')}_d{embedding_dim()}"
        if name not in self._ready:
            self._ensure(name)
            self._ready.add(name)
        return name

    def _ensure(self, name: str) -> None:
        from qdrant_client.models import Distance, VectorParams

        if self._exists(name):
            return
        try:
            self.client.create_collection(
                name,
                vectors_config=VectorParams(size=embedding_dim(), distance=Distance.COSINE),
            )
        except _DOWN as exc:
            if not self._exists(name):
                raise VectorError("qdrant write failed") from exc

    def _exists(self, name: str) -> bool:
        try:
            return bool(self.client.collection_exists(name))
        except _DOWN as exc:
            raise VectorError("qdrant read failed") from exc


def _payload(child: Child) -> dict:
    return {
        "id": child.id,
        "project_id": child.project_id,
        "kb_id": child.kb_id,
        "document_id": child.document_id,
        "version": child.version,
        "parent_id": child.parent_id,
        "kind": "child",
        "text": child.text,
        "acl": list(child.acl),
    }


def _point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


def _down_types() -> tuple[type[BaseException], ...]:
    from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

    return (OSError, ResponseHandlingException, UnexpectedResponse)


_DOWN = _down_types()

# Back-compat for imports that still reference DIM (hash default at import time).
DIM = HASH_DIM
