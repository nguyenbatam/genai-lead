from __future__ import annotations

from core.blobs import LocalBlobStore, MemoryBlobStore


def test_memory_blob_round_trip():
    store = MemoryBlobStore()
    ref = store.put("prj", "doc/v1", b"hello")
    assert store.get(ref) == b"hello"
    store.delete(ref)
    try:
        store.get(ref)
        raised = False
    except KeyError:
        raised = True
    assert raised


def test_local_blob_stays_under_root(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    ref = store.put("prj", "kb/doc", b"body")
    assert store.get(ref) == b"body"
    try:
        store.get("file:../outside")
        raised = False
    except KeyError:
        raised = True
    assert raised
