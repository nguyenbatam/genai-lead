"""Object-storage port for ingest bodies. Postgres holds the pointer, not the bytes."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path


class BlobStore:
    def put(self, project_id: str, name: str, data: bytes) -> str:
        raise NotImplementedError

    def get(self, ref: str) -> bytes:
        raise NotImplementedError

    def delete(self, ref: str) -> None:
        raise NotImplementedError


class MemoryBlobStore(BlobStore):
    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, project_id: str, name: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()[:24]
        ref = f"mem:{project_id}/{name}/{digest}"
        with self._lock:
            self._items[ref] = data
        return ref

    def get(self, ref: str) -> bytes:
        with self._lock:
            found = self._items.get(ref)
        if found is None:
            raise KeyError(ref)
        return found

    def delete(self, ref: str) -> None:
        with self._lock:
            self._items.pop(ref, None)


class LocalBlobStore(BlobStore):
    """Directory-backed store. Swap for MinIO/S3 behind the same port later."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, project_id: str, name: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()[:24]
        safe = name.replace("..", "_").replace("\\", "/")
        path = self.root / project_id / safe / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"file:{project_id}/{safe}/{digest}"

    def get(self, ref: str) -> bytes:
        path = self._path(ref)
        if not path.is_file():
            raise KeyError(ref)
        return path.read_bytes()

    def delete(self, ref: str) -> None:
        path = self._path(ref)
        if path.is_file():
            path.unlink()

    def _path(self, ref: str) -> Path:
        if not ref.startswith("file:"):
            raise KeyError(ref)
        relative = ref.removeprefix("file:")
        path = (self.root / relative).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise KeyError(ref)
        return path


def build_blobs(settings: dict[str, str]) -> BlobStore:
    root = settings.get("object_storage_dir", "").strip()
    if root:
        return LocalBlobStore(root)
    return MemoryBlobStore()
