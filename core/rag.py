"""Shared knowledge base. ACL is applied on the child hit and again on the parent."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from control.store import new_id
from control.vocab import (
    CHUNK_CHILD,
    CHUNK_PARENT,
    JOB_CANCELLED,
    JOB_DEAD,
    JOB_INDEXED,
    JOB_QUEUED,
    JOB_REJECTED,
)
from core.guard import WITHHELD, scrub
from core.vectors import VectorError

CACHE_MAX = 256
TEXT_MAX = 200_000


class DocumentConflict(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass
class Chunk:
    id: str
    project_id: str
    kb_id: str
    document_id: str
    version: int
    parent_id: str
    kind: str
    text: str
    acl: tuple[str, ...]


@dataclass
class Job:
    id: str
    project_id: str
    kb_id: str
    document_id: str
    version: int
    text: str
    acl: tuple[str, ...]
    idempotency_key: str
    attempts: int = 0
    status: str = JOB_QUEUED
    body_ref: str = ""


class Knowledge:
    def __init__(self, vectors: object | None = None) -> None:
        self.owners: dict[str, str] = {}
        self.chunks: list[Chunk] = []
        self.jobs: dict[str, Job] = {}
        self.queue: list[str] = []
        self.by_idem: dict[tuple[str, str], str] = {}
        self.cache: OrderedDict[tuple, list[Chunk]] = OrderedDict()
        self.versions: dict[str, int] = {}
        self.fail_remaining = 0
        self.vectors = vectors
        self._lock = threading.Lock()
        self._running: dict[str, int] = {}

    def ping(self) -> bool:
        if self.vectors is None:
            return True
        return bool(self.vectors.ping())

    def create_kb(self, project_id: str, name: str) -> str:
        del name
        kb_id = new_id("kb")
        self.owners[kb_id] = project_id
        self.versions[kb_id] = 1
        return kb_id

    def submit(
        self,
        project_id: str,
        kb_id: str,
        document_id: str,
        version: int,
        text: str,
        acl: tuple[str, ...],
        idempotency_key: str,
    ) -> Job:
        self._owned(project_id, kb_id)
        if version < 1:
            raise ValueError("version must be positive")
        if len(text) > TEXT_MAX:
            raise ValueError("document too large")
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("invalid idempotency key")
        with self._lock:
            return self._enqueue(
                project_id, kb_id, document_id, version, text, acl, idempotency_key
            )

    def _enqueue(
        self,
        project_id: str,
        kb_id: str,
        document_id: str,
        version: int,
        text: str,
        acl: tuple[str, ...],
        idempotency_key: str,
    ) -> Job:
        existing = self.by_idem.get((project_id, idempotency_key))
        if existing:
            job = self.jobs[existing]
            if (job.document_id, job.version, job.text, job.acl) != (
                document_id,
                version,
                text,
                acl,
            ):
                raise DocumentConflict("idempotency conflict")
            return job
        ceiling = self._ceiling(project_id, kb_id, document_id)
        if version < ceiling or (ceiling > 0 and version == ceiling):
            raise DocumentConflict("version is not newer")
        job = Job(
            id=new_id("job"),
            project_id=project_id,
            kb_id=kb_id,
            document_id=document_id,
            version=version,
            text=text,
            acl=acl,
            idempotency_key=idempotency_key,
        )
        self.jobs[job.id] = job
        self.by_idem[(project_id, idempotency_key)] = job.id
        self.queue.append(job.id)
        return job

    def queued_job_count(self, project_id: str) -> int:
        with self._lock:
            return sum(
                1
                for job in self.jobs.values()
                if job.project_id == project_id and job.status == JOB_QUEUED
            )

    def process_next(self) -> Job | None:
        with self._lock:
            job = self._claim_fair()
            if job is None:
                return None
            self._running[job.project_id] = self._running.get(job.project_id, 0) + 1
        try:
            return self._process(job)
        finally:
            with self._lock:
                count = self._running.get(job.project_id, 0)
                if count <= 1:
                    self._running.pop(job.project_id, None)
                else:
                    self._running[job.project_id] = count - 1

    def _claim_fair(self) -> Job | None:
        if not self.queue:
            return None
        first_idx: dict[str, int] = {}
        queued: dict[str, int] = {}
        for index, job_id in enumerate(self.queue):
            project_id = self.jobs[job_id].project_id
            queued[project_id] = queued.get(project_id, 0) + 1
            if project_id not in first_idx:
                first_idx[project_id] = index
        project_id = min(
            first_idx,
            key=lambda pid: (self._running.get(pid, 0), queued.get(pid, 0), first_idx[pid]),
        )
        job = self.jobs[self.queue.pop(first_idx[project_id])]
        return job

    def _process(self, job: Job) -> Job:
        if job.status == JOB_CANCELLED:
            return job
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            job.attempts += 1
            job.status = attempt_status(job.attempts)
            if job.status == JOB_QUEUED:
                with self._lock:
                    self.queue.append(job.id)
            return job
        latest = latest_versions(self._chunks(job.project_id, job.kb_id)).get(job.document_id, 0)
        if job.version < latest:
            job.status = JOB_REJECTED
            return job
        try:
            self._index(job)
        except (RuntimeError, ValueError, OSError, VectorError):
            job.attempts += 1
            job.status = attempt_status(job.attempts)
            if job.status == JOB_QUEUED:
                with self._lock:
                    self.queue.append(job.id)
            return job
        job.status = JOB_INDEXED
        return job

    def job(self, project_id: str, kb_id: str, job_id: str) -> Job | None:
        self._owned(project_id, kb_id)
        found = self.jobs.get(job_id)
        if found is None or found.project_id != project_id or found.kb_id != kb_id:
            return None
        return found

    def delete(self, project_id: str, kb_id: str, document_id: str) -> None:
        self._owned(project_id, kb_id)
        self.chunks = [
            chunk
            for chunk in self.chunks
            if not (
                chunk.project_id == project_id
                and chunk.kb_id == kb_id
                and chunk.document_id == document_id
            )
        ]
        cancelled: set[str] = set()
        for job in self.jobs.values():
            if (
                job.project_id == project_id
                and job.kb_id == kb_id
                and job.document_id == document_id
                and job.status == JOB_QUEUED
            ):
                job.status = JOB_CANCELLED
                cancelled.add(job.id)
        self.queue = [job_id for job_id in self.queue if job_id not in cancelled]
        if self.vectors is not None:
            self.vectors.delete_document(project_id, kb_id, document_id)
        self._bump(project_id, kb_id)

    def search(
        self, project_id: str, kb_id: str, query: str, groups: tuple[str, ...]
    ) -> list[Chunk]:
        self._owned(project_id, kb_id)
        key = (project_id, kb_id, query, groups, self.corpus_version(project_id, kb_id))
        cached = self.cache.get(key)
        if cached is not None:
            self.cache.move_to_end(key)
            return cached
        if self.vectors is not None:
            hits = [
                _chunk_from_hit(hit)
                for hit in self.vectors.search(project_id, kb_id, query, groups)
                if hit.project_id == project_id and hit.kb_id == kb_id and _allowed(hit.acl, groups)
            ]
        else:
            loaded = self._chunks(project_id, kb_id)
            hits = rank_children(loaded, latest_versions(loaded), query, groups)
        self.cache[key] = hits
        self.cache.move_to_end(key)
        while len(self.cache) > CACHE_MAX:
            self.cache.popitem(last=False)
        return hits

    def parent(
        self, project_id: str, kb_id: str, parent_id: str, groups: tuple[str, ...]
    ) -> Chunk | None:
        self._owned(project_id, kb_id)
        return find_parent(self._chunks(project_id, kb_id), parent_id, groups)

    def _chunks(self, project_id: str, kb_id: str) -> list[Chunk]:
        return [
            chunk
            for chunk in self.chunks
            if chunk.project_id == project_id and chunk.kb_id == kb_id
        ]

    def _index(self, job: Job) -> None:
        built = build_chunks(job)
        children = [chunk for chunk in built if chunk.kind == CHUNK_CHILD]
        if self.vectors is not None:
            old = [
                chunk
                for chunk in self.chunks
                if chunk.project_id == job.project_id
                and chunk.kb_id == job.kb_id
                and chunk.document_id == job.document_id
                and chunk.kind == CHUNK_CHILD
            ]
            try:
                self.vectors.replace(job.project_id, job.kb_id, job.document_id, children)
            except VectorError:
                try:
                    self.vectors.replace(job.project_id, job.kb_id, job.document_id, old)
                except VectorError as restore_exc:
                    raise VectorError("qdrant restore failed") from restore_exc
                raise
        with self._lock:
            self.chunks = [
                chunk
                for chunk in self.chunks
                if not (
                    chunk.project_id == job.project_id
                    and chunk.kb_id == job.kb_id
                    and chunk.document_id == job.document_id
                )
            ]
            self.chunks.extend(built)
            self._bump(job.project_id, job.kb_id)

    def _ceiling(self, project_id: str, kb_id: str, document_id: str) -> int:
        latest = latest_versions(self._chunks(project_id, kb_id)).get(document_id, 0)
        queued = [
            job.version
            for job in self.jobs.values()
            if job.project_id == project_id
            and job.kb_id == kb_id
            and job.document_id == document_id
            and job.status == JOB_QUEUED
        ]
        return max([latest, *queued], default=0)

    def corpus_version(self, project_id: str, kb_id: str) -> int:
        self._owned(project_id, kb_id)
        return self.versions.get(kb_id, 0)

    def _bump(self, project_id: str, kb_id: str) -> None:
        self.versions[kb_id] = self.versions.get(kb_id, 0) + 1
        self._drop_cache(project_id, kb_id)

    def _drop_cache(self, project_id: str, kb_id: str) -> None:
        self.cache = OrderedDict(
            (key, value)
            for key, value in self.cache.items()
            if key[0] != project_id or key[1] != kb_id
        )

    def _owned(self, project_id: str, kb_id: str) -> None:
        if self.owners.get(kb_id) != project_id:
            raise KeyError(kb_id)


def _chunk_from_hit(hit: object) -> Chunk:
    return Chunk(
        id=hit.id,
        project_id=hit.project_id,
        kb_id=hit.kb_id,
        document_id=hit.document_id,
        version=hit.version,
        parent_id=hit.parent_id,
        kind=CHUNK_CHILD,
        text=hit.text,
        acl=tuple(hit.acl),
    )


def attempt_status(attempts: int) -> str:
    if attempts >= 5:
        return JOB_DEAD
    return JOB_QUEUED


def build_chunks(job: Job) -> list[Chunk]:
    built: list[Chunk] = []
    for paragraph in _paragraphs(scrub(job.text)):
        parent_id = new_id("par")
        built.append(
            Chunk(
                id=parent_id,
                project_id=job.project_id,
                kb_id=job.kb_id,
                document_id=job.document_id,
                version=job.version,
                parent_id=parent_id,
                kind=CHUNK_PARENT,
                text=paragraph,
                acl=job.acl,
            )
        )
        for sentence in _sentences(paragraph):
            built.append(
                Chunk(
                    id=new_id("chk"),
                    project_id=job.project_id,
                    kb_id=job.kb_id,
                    document_id=job.document_id,
                    version=job.version,
                    parent_id=parent_id,
                    kind=CHUNK_CHILD,
                    text=sentence,
                    acl=job.acl,
                )
            )
    return built


def latest_versions(chunks: list[Chunk]) -> dict[str, int]:
    latest: dict[str, int] = {}
    for chunk in chunks:
        if chunk.kind == CHUNK_PARENT:
            latest[chunk.document_id] = max(latest.get(chunk.document_id, 0), chunk.version)
    return latest


def rank_children(
    chunks: list[Chunk],
    versions: dict[str, int],
    query: str,
    groups: tuple[str, ...],
) -> list[Chunk]:
    wanted = set(query.lower().split())
    scored: list[tuple[int, Chunk]] = []
    for chunk in chunks:
        if chunk.kind != CHUNK_CHILD:
            continue
        if chunk.version != versions.get(chunk.document_id):
            continue
        if not _allowed(chunk.acl, groups):
            continue
        score = len(wanted & set(chunk.text.lower().split()))
        if score:
            scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _, chunk in scored[:4]]


def find_parent(chunks: list[Chunk], parent_id: str, groups: tuple[str, ...]) -> Chunk | None:
    for chunk in chunks:
        if chunk.id == parent_id and chunk.kind == CHUNK_PARENT and _allowed(chunk.acl, groups):
            return chunk
    return None


def _allowed(acl: tuple[str, ...], groups: tuple[str, ...]) -> bool:
    if "*" in acl:
        return True
    return bool(set(acl) & set(groups))


def _paragraphs(text: str) -> list[str]:
    cleaned: list[str] = []
    for part in text.split("\n\n"):
        lines = [line for line in part.splitlines() if line.strip() and line.strip() != WITHHELD]
        if lines:
            cleaned.append("\n".join(lines).strip())
    return cleaned


def _sentences(paragraph: str) -> list[str]:
    parts = [part.strip() for part in paragraph.replace("?", ".").split(".") if part.strip()]
    return parts or [paragraph]
