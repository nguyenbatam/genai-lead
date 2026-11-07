"""Errors the API turns into problem+json. No runtime imports."""

from __future__ import annotations

import re

_CODE = re.compile(r"[^a-z0-9]+")


def error_code(detail: str) -> str:
    slug = _CODE.sub("_", detail.strip().lower()).strip("_")
    return slug or "error"


class PlatformError(Exception):
    def __init__(
        self,
        status: int,
        detail: str,
        retry_after: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.retry_after = retry_after
        self.code = code or error_code(detail)


class IdempotencyConflict(Exception):
    """Same key, different body."""


class IdempotencyPending(Exception):
    """Same key is already in flight."""
