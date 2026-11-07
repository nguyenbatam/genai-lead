"""Postgres corpus. The API and the worker share these rows."""

from __future__ import annotations

import time

from psycopg import Error as PgError
from psycopg.errors import UniqueViolation
from psycopg.types.json import Json

from control.postgres import shared_pool
from control.store import new_id
from control.vocab import JOB_INDEXED
from core.blobs import BlobStore, MemoryBlobStore
from core.rag import (
    TEXT_MAX,
    Chunk,
    DocumentConflict,
    Job,
    Knowledge,
    attempt_status,
    build_chunks,
    find_parent,
)
from core.vectors import VectorError

_JOB_LEASE_MS = 60_000


class PgKnowledge(Knowledge):
    def __init__(
        self, url: str, vectors: object | None = None, blobs: BlobStore | None = None
    ) -> None:
        super().__init__(vectors)
        self.pool = shared_pool(url)
        self.blobs = blobs or MemoryBlobStore()

    def create_kb(self, project_id: str, name: str) -> str:
        kb_id = new_id("kb")
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO knowledge_bases (id, project_id, name) VALUES (%s, %s, %s)",
                (kb_id, project_id, name[:200]),
            )
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
        with self.pool.connection() as conn:
            locked = conn.execute(
                """
                SELECT project_id FROM knowledge_bases WHERE id = %s FOR UPDATE
                """,
                (kb_id,),
            ).fetchone()
            if locked is None or locked[0] != project_id:
                raise KeyError(kb_id)
            existing = conn.execute(
                """
                SELECT id, kb_id, document_id, version, body, acl, idempotency_key,
                       attempts, status, body_ref
                FROM jobs WHERE project_id = %s AND idempotency_key = %s
                """,
                (project_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                job = self._hydrate(_job_from_idem(project_id, existing))
                if job.kb_id != kb_id or (job.document_id, job.version, job.text, job.acl) != (
                    document_id,
                    version,
                    text,
                    acl,
                ):
                    raise DocumentConflict("idempotency conflict")
                return job
            ceiling = self._ceiling_conn(conn, project_id, kb_id, document_id)
            if version < ceiling or (ceiling > 0 and version == ceiling):
                raise DocumentConflict("version is not newer")
            body_ref = self.blobs.put(
                project_id,
                f"{kb_id}/{document_id}/v{version}/{idempotency_key}",
                text.encode("utf-8"),
            )
            job = Job(
                id=new_id("job"),
                project_id=project_id,
                kb_id=kb_id,
                document_id=document_id,
                version=version,
                text=text,
                acl=acl,
                idempotency_key=idempotency_key,
                body_ref=body_ref,
            )
            try:
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO jobs (
                            id, project_id, kb_id, document_id, version, body, acl,
                            idempotency_key, attempts, status, body_ref
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 'queued', %s)
                        """,
                        (
                            job.id,
                            job.project_id,
                            job.kb_id,
                            job.document_id,
                            job.version,
                            "",
                            Json(list(job.acl)),
                            job.idempotency_key,
                            job.body_ref,
                        ),
                    )
            except UniqueViolation:
                self.blobs.delete(body_ref)
                raced = conn.execute(
                    """
                    SELECT id, kb_id, document_id, version, body, acl,
                           idempotency_key, attempts, status, body_ref
                    FROM jobs WHERE project_id = %s AND idempotency_key = %s
                    """,
                    (project_id, idempotency_key),
                ).fetchone()
                if raced is None:
                    raise DocumentConflict("version is not newer") from None
                raced_job = self._hydrate(_job_from_idem(project_id, raced))
                if raced_job.kb_id != kb_id or (
                    raced_job.document_id,
                    raced_job.version,
                    raced_job.text,
                    raced_job.acl,
                ) != (document_id, version, text, acl):
                    raise DocumentConflict("idempotency conflict") from None
                return raced_job
        return job

    def process_next(self) -> Job | None:
        job = self._claim_job()
        if job is None:
            return None
        try:
            if self.fail_remaining > 0:
                self.fail_remaining -= 1
                raise RuntimeError("index failed")
            latest = self._latest_version(job.project_id, job.kb_id, job.document_id)
            if job.version < latest:
                self._set_job(job, "rejected", job.attempts)
                return job
            built = build_chunks(job)
            if self.vectors is not None:
                old = self._children(job)
                children = [chunk for chunk in built if chunk.kind == "child"]
                try:
                    self.vectors.replace(job.project_id, job.kb_id, job.document_id, children)
                    self._commit_chunks(job, built)
                except (OSError, PgError, VectorError):
                    self._restore_vectors(job, old)
                    raise
            else:
                self._commit_chunks(job, built)
        except (RuntimeError, ValueError, OSError, PgError, VectorError, KeyError):
            job.attempts += 1
            job.status = attempt_status(job.attempts)
            self._set_job(job, job.status, job.attempts)
            return job
        job.status = JOB_INDEXED
        if job.body_ref:
            self.blobs.delete(job.body_ref)
            job.body_ref = ""
        self._drop_cache(job.project_id, job.kb_id)
        return job

    def job(self, project_id: str, kb_id: str, job_id: str) -> Job | None:
        self._owned(project_id, kb_id)
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT id, kb_id, document_id, version, body, acl, idempotency_key,
                       attempts, status, body_ref
                FROM jobs WHERE id = %s AND project_id = %s AND kb_id = %s
                """,
                (job_id, project_id, kb_id),
            ).fetchone()
        if row is None:
            return None
        return self._hydrate(_job_from_idem(project_id, row))

    def queued_job_count(self, project_id: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM jobs
                WHERE project_id = %s AND status = 'queued'
                """,
                (project_id,),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def _claim_job(self) -> Job | None:
        """Prefer the project with the fewest running jobs so one flood cannot starve others."""
        now = int(time.time() * 1000)
        stale = now - _JOB_LEASE_MS
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE jobs SET status = 'running', claimed_ms = %s
                WHERE id = (
                    SELECT j.id
                    FROM jobs j
                    WHERE j.status = 'queued'
                       OR (j.status = 'running' AND j.claimed_ms <= %s)
                    ORDER BY (
                        SELECT COUNT(*) FROM jobs r
                        WHERE r.project_id = j.project_id
                          AND r.status = 'running'
                          AND r.claimed_ms > %s
                    ), j.id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, project_id, kb_id, document_id, version, body, acl,
                          idempotency_key, attempts, status, body_ref
                """,
                (now, stale, stale),
            ).fetchone()
        if row is None:
            return None
        return self._hydrate(_job_from_queue(row))

    def _hydrate(self, job: Job) -> Job:
        if job.text:
            return job
        if not job.body_ref:
            return job
        job.text = self.blobs.get(job.body_ref).decode("utf-8")
        return job

    def _latest_version(self, project_id: str, kb_id: str, document_id: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(version), 0) FROM chunks
                WHERE project_id = %s AND kb_id = %s AND document_id = %s AND kind = 'parent'
                """,
                (project_id, kb_id, document_id),
            ).fetchone()
        return int(row[0] or 0)

    def _children(self, job: Job) -> list[Chunk]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, project_id, kb_id, document_id, version, parent_id, kind, body, acl
                FROM chunks
                WHERE project_id = %s AND kb_id = %s AND document_id = %s AND kind = 'child'
                """,
                (job.project_id, job.kb_id, job.document_id),
            ).fetchall()
        return [_chunk_row(row) for row in rows]

    def _restore_vectors(self, job: Job, old: list[Chunk]) -> None:
        if self.vectors is None:
            return
        try:
            self.vectors.replace(job.project_id, job.kb_id, job.document_id, old)
        except (OSError, VectorError) as exc:
            raise VectorError("qdrant restore failed") from exc

    def _commit_chunks(self, job: Job, built: list[Chunk]) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                DELETE FROM chunks
                WHERE project_id = %s AND kb_id = %s AND document_id = %s
                """,
                (job.project_id, job.kb_id, job.document_id),
            )
            for chunk in built:
                conn.execute(
                    """
                    INSERT INTO chunks (
                        id, project_id, kb_id, document_id, version, parent_id, kind, body, acl
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chunk.id,
                        chunk.project_id,
                        chunk.kb_id,
                        chunk.document_id,
                        chunk.version,
                        chunk.parent_id,
                        chunk.kind,
                        chunk.text,
                        Json(list(chunk.acl)),
                    ),
                )
            conn.execute(
                """
                UPDATE knowledge_bases SET corpus_version = corpus_version + 1 WHERE id = %s
                """,
                (job.kb_id,),
            )
            conn.execute(
                """
                UPDATE jobs
                SET status = 'indexed', claimed_ms = 0, body = '', body_ref = ''
                WHERE id = %s
                """,
                (job.id,),
            )

    def _set_job(self, job: Job, status: str, attempts: int) -> None:
        job.status = status
        job.attempts = attempts
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE jobs SET attempts = %s, status = %s, claimed_ms = 0 WHERE id = %s
                """,
                (attempts, status, job.id),
            )

    def delete(self, project_id: str, kb_id: str, document_id: str) -> None:
        self._owned(project_id, kb_id)
        with self.pool.connection() as conn:
            conn.execute(
                """
                DELETE FROM chunks
                WHERE project_id = %s AND kb_id = %s AND document_id = %s
                """,
                (project_id, kb_id, document_id),
            )
            conn.execute(
                """
                UPDATE jobs SET status = 'cancelled'
                WHERE project_id = %s AND kb_id = %s AND document_id = %s AND status = 'queued'
                """,
                (project_id, kb_id, document_id),
            )
            conn.execute(
                """
                UPDATE knowledge_bases
                SET corpus_version = corpus_version + 1
                WHERE id = %s
                """,
                (kb_id,),
            )
        if self.vectors is not None:
            self.vectors.delete_document(project_id, kb_id, document_id)
        self._drop_cache(project_id, kb_id)

    def corpus_version(self, project_id: str, kb_id: str) -> int:
        self._owned(project_id, kb_id)
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT corpus_version FROM knowledge_bases WHERE id = %s",
                (kb_id,),
            ).fetchone()
        if row is None:
            return 0
        return int(row[0])

    def parent(
        self, project_id: str, kb_id: str, parent_id: str, groups: tuple[str, ...]
    ) -> Chunk | None:
        self._owned(project_id, kb_id)
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT id, project_id, kb_id, document_id, version, parent_id, kind, body, acl
                FROM chunks
                WHERE project_id = %s AND kb_id = %s AND id = %s AND kind = 'parent'
                """,
                (project_id, kb_id, parent_id),
            ).fetchone()
        if row is None:
            return None
        chunk = Chunk(
            id=row[0],
            project_id=row[1],
            kb_id=row[2],
            document_id=row[3],
            version=row[4],
            parent_id=row[5],
            kind=row[6],
            text=row[7],
            acl=tuple(row[8] or ()),
        )
        return find_parent([chunk], parent_id, groups)

    def _chunks(self, project_id: str, kb_id: str) -> list[Chunk]:
        with self.pool.connection() as conn:
            return self._chunks_conn(conn, project_id, kb_id)

    def _owned(self, project_id: str, kb_id: str) -> None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT project_id FROM knowledge_bases WHERE id = %s",
                (kb_id,),
            ).fetchone()
        if row is None or row[0] != project_id:
            raise KeyError(kb_id)

    def _chunks_conn(self, conn, project_id: str, kb_id: str) -> list[Chunk]:
        rows = conn.execute(
            """
            SELECT id, project_id, kb_id, document_id, version, parent_id, kind, body, acl
            FROM chunks WHERE project_id = %s AND kb_id = %s
            """,
            (project_id, kb_id),
        ).fetchall()
        return [
            Chunk(
                id=row[0],
                project_id=row[1],
                kb_id=row[2],
                document_id=row[3],
                version=row[4],
                parent_id=row[5],
                kind=row[6],
                text=row[7],
                acl=tuple(row[8] or ()),
            )
            for row in rows
        ]

    def _ceiling_conn(self, conn, project_id: str, kb_id: str, document_id: str) -> int:
        indexed = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) FROM chunks
            WHERE project_id = %s AND kb_id = %s AND document_id = %s AND kind = 'parent'
            """,
            (project_id, kb_id, document_id),
        ).fetchone()
        queued = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) FROM jobs
            WHERE project_id = %s AND kb_id = %s AND document_id = %s AND status = 'queued'
            """,
            (project_id, kb_id, document_id),
        ).fetchone()
        return max(int(indexed[0] or 0), int(queued[0] or 0))


def _chunk_row(row: tuple) -> Chunk:
    return Chunk(
        id=row[0],
        project_id=row[1],
        kb_id=row[2],
        document_id=row[3],
        version=row[4],
        parent_id=row[5],
        kind=row[6],
        text=row[7],
        acl=tuple(row[8] or ()),
    )


def _job_from_idem(project_id: str, row: tuple) -> Job:
    body_ref = str(row[9]) if len(row) > 9 else ""
    return Job(
        id=row[0],
        project_id=project_id,
        kb_id=row[1],
        document_id=row[2],
        version=row[3],
        text=row[4] or "",
        acl=tuple(row[5] or ()),
        idempotency_key=row[6],
        attempts=row[7],
        status=row[8],
        body_ref=body_ref,
    )


def _job_from_queue(row: tuple) -> Job:
    body_ref = str(row[10]) if len(row) > 10 else ""
    return Job(
        id=row[0],
        project_id=row[1],
        kb_id=row[2],
        document_id=row[3],
        version=row[4],
        text=row[5] or "",
        acl=tuple(row[6] or ()),
        idempotency_key=row[7],
        attempts=row[8],
        status=row[9],
        body_ref=body_ref,
    )
