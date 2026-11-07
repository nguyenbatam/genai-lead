"""Fair pick across tenants. Prefer under-served projects over FIFO floods."""

from __future__ import annotations

from collections.abc import Callable, Sequence

# At most this many items per project in one claim batch before serving others.
PER_PROJECT = 1


def fair_take[T](
    items: Sequence[T],
    project_of: Callable[[T], str],
    *,
    limit: int,
    per_project: int = PER_PROJECT,
) -> list[T]:
    """Take up to `limit` items, round-robin by project, `per_project` each round."""
    if limit < 1 or not items:
        return []
    by_project: dict[str, list[T]] = {}
    order: list[str] = []
    for item in items:
        project_id = project_of(item)
        if project_id not in by_project:
            by_project[project_id] = []
            order.append(project_id)
        by_project[project_id].append(item)
    picked: list[T] = []
    indexes = dict.fromkeys(order, 0)
    while len(picked) < limit:
        progressed = False
        for project_id in order:
            bucket = by_project[project_id]
            taken = 0
            while taken < per_project and len(picked) < limit:
                idx = indexes[project_id]
                if idx >= len(bucket):
                    break
                picked.append(bucket[idx])
                indexes[project_id] = idx + 1
                taken += 1
                progressed = True
        if not progressed:
            break
    return picked
