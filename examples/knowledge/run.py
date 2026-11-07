"""Run the registered knowledge agent once."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from examples.lib.client import PlatformClient, load_state, wait_run

HERE = Path(__file__).resolve().parent
STATE = HERE / ".local_state.json"


def main() -> None:
    state = load_state(STATE)
    query = sys.argv[1] if len(sys.argv) > 1 else state.get("sample_query", "phê duyệt ngân sách")
    api_key = state.get("api_key") or os.environ.get("PLATFORM_API_KEY", "")
    if not api_key:
        raise SystemExit("Set PLATFORM_API_KEY or re-run register.py")

    with PlatformClient(state["base_url"], api_key) as client:
        started = client.post(
            f"/v1/agents/{state['agent_id']}/runs",
            {"query": query},
        )
        run = started
        if started.get("status") not in {
            "completed",
            "failed",
            "cancelled",
            "waiting_approval",
        }:
            run = wait_run(client, started["id"])

    print(
        f"run_id={run['id']} status={run['status']} outcome={run.get('outcome')} "
        f"citations={run.get('citations')}"
    )
    if run.get("answer"):
        print(f"answer={run['answer']}")


if __name__ == "__main__":
    main()
