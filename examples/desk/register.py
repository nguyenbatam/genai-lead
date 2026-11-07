"""Register the desk agent against a running platform."""

from __future__ import annotations

from pathlib import Path

from examples.lib.client import (
    PlatformClient,
    bootstrap_project,
    create_and_publish_agent,
    ensure_cheap_route,
    env_admin_token,
    env_api_key,
    env_base_url,
    load_json,
    publish_prompt,
    save_state,
)

HERE = Path(__file__).resolve().parent
STATE = HERE / ".local_state.json"
MANIFEST = HERE / "agent.json"


def main() -> None:
    manifest = load_json(MANIFEST)
    base = env_base_url()
    api_key = env_api_key()
    meta: dict = {"base_url": base, "app": "desk"}

    if api_key:
        client = PlatformClient(base, api_key)
        meta["api_key_source"] = "PLATFORM_API_KEY"
    else:
        admin = PlatformClient(base, env_admin_token())
        org_id, project_id, api_key = bootstrap_project(
            admin,
            org_name="examples",
            project_name="desk",
            groups=[],
        )
        admin.close()
        client = PlatformClient(base, api_key)
        meta.update(
            {
                "org_id": org_id,
                "project_id": project_id,
                "api_key": api_key,
                "api_key_source": "bootstrap",
            }
        )

    try:
        ensure_cheap_route(client, name="desk-small")
        prompt_id = publish_prompt(
            client,
            manifest["prompt"]["name"],
            manifest["prompt"]["template"],
        )
        agent = create_and_publish_agent(
            client,
            {
                "name": manifest["name"],
                "mode": manifest["mode"],
                "prompt_id": prompt_id,
                "model_profile": manifest["model_profile"],
                "allowed_tools": manifest["allowed_tools"],
            },
        )
        meta.update(
            {
                "prompt_id": prompt_id,
                "agent_id": agent["id"],
                "agent_version": agent.get("version"),
                "sample_query": manifest["sample_query"],
            }
        )
        save_state(STATE, meta)
    finally:
        client.close()

    print(f"registered desk agent_id={meta['agent_id']}")
    print(f"state written to {STATE}")
    if meta.get("api_key"):
        print("export PLATFORM_API_KEY=" + meta["api_key"])
    print("next: .venv/bin/python -m examples.desk.run")


if __name__ == "__main__":
    main()
