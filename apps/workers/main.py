"""Ingest worker. Separate process from the API. Shares Postgres and Redis."""

from __future__ import annotations

import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor

from apps.settings import load_settings
from core.services import build_services

_STOP = False
_DRAIN_S = 15.0


def _request_stop(signum: int, _frame: object) -> None:
    del signum
    global _STOP
    _STOP = True


def run_once(services) -> bool:
    job = services.work()
    eval_job = services.work_eval()
    chat_done = services.work_chat()
    swept = services.sweep_runs(limit=1)
    return job is not None or eval_job is not None or chat_done or swept > 0


def run_batch(services, workers: int) -> bool:
    """Claim up to `workers` ingest jobs at once. Runs are swept in the same pass."""
    if workers < 2:
        return run_once(services)

    def _one(_index: int) -> bool:
        del _index
        return services.work() is not None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        found = any(pool.map(_one, range(workers)))
    eval_done = services.work_eval() is not None
    chat_done = services.work_chat()
    return found or eval_done or chat_done or services.sweep_runs(limit=workers) > 0


def worker_count() -> int:
    raw = os.environ.get("WORKER_CONCURRENCY", "4")
    try:
        return max(1, int(raw))
    except ValueError:
        return 4


def main() -> None:
    settings = load_settings()
    if not settings["database_url"] or not settings["redis_url"]:
        raise SystemExit("worker requires DATABASE_URL and REDIS_URL")
    services = build_services(settings)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    workers = worker_count()
    idle = 0.05
    deadline = 0.0
    while True:
        if _STOP and deadline == 0.0:
            deadline = time.monotonic() + _DRAIN_S
        if deadline and time.monotonic() >= deadline:
            break
        if run_batch(services, workers):
            idle = 0.05
            continue
        if _STOP:
            break
        time.sleep(idle)
        idle = min(0.5, idle * 1.5)


if __name__ == "__main__":
    main()
