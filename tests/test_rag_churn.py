"""Continuous corpus updates: version churn dataset must retire stale phrases."""

from __future__ import annotations

from scripts.bench_lib import churn_entries, churn_families, measure_churn


def test_churn_seed_expands_to_hundreds_of_version_steps():
    families = churn_families()
    steps = churn_entries()
    assert len(families) >= 200
    assert len(steps) >= 1000
    assert [item["step"] for item in steps] == list(range(1, len(steps) + 1))
    first = [item for item in steps if item["document_id"] == families[0]["document_id"]]
    assert [item["version"] for item in first] == list(range(1, int(families[0]["versions"]) + 1))


def test_churn_replay_drops_stale_and_serves_latest():
    # Full seed is ~1000 steps / ~2800 cases and still finishes under a second (lexical).
    report = measure_churn()
    assert report["status"] == "ok"
    assert report["steps"] >= 1000
    assert report["cases"] >= 2800
    assert report["success_rate"] == 1.0, [row for row in report["rows"] if not row["success"]][:5]
