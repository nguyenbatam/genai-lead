"""Process settings. Safe for the API and the worker to import."""

from __future__ import annotations

import os


def load_settings() -> dict[str, str]:
    """Host environment only. Callers must not log these values."""
    return {
        "llm_base_url": os.environ.get("LLM_BASE_URL", ""),
        "llm_api_key": os.environ.get("LLM_API_KEY", ""),
        "llm_credentials": os.environ.get("LLM_CREDENTIALS", ""),
        "llm_credentials_file": os.environ.get(
            "LLM_CREDENTIALS_FILE", "config/llm_credentials.yaml"
        ),
        "admin_token": os.environ.get("PLATFORM_ADMIN_TOKEN", ""),
        "key_pepper": os.environ.get("PLATFORM_KEY_PEPPER", ""),
        "database_url": os.environ.get("DATABASE_URL", ""),
        "redis_url": os.environ.get("REDIS_URL", ""),
        "qdrant_url": os.environ.get("QDRANT_URL", ""),
        "metrics_port": os.environ.get("METRICS_PORT", ""),
        "object_storage_dir": os.environ.get("OBJECT_STORAGE_DIR", ""),
        "shutdown_drain_s": os.environ.get("SHUTDOWN_DRAIN_S", "15"),
        "embedding_backend": os.environ.get("EMBEDDING_BACKEND", "hash"),
        "embedding_model": os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
        "qdrant_memory": os.environ.get("QDRANT_MEMORY", ""),
        "tools_url": os.environ.get("TOOLS_URL", ""),
        "tools_token": os.environ.get("TOOLS_TOKEN", ""),
    }
