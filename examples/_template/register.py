"""Copy-me register script. Rename the package when you copy the folder."""

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
    meta: dict = {"base_url": base, "app": manifest["name"]}

    if api_key:
        client = PlatformClient(base, api_key)
        meta["api_key_source"] = "PLATFORM_API_KEY"
    else:
        admin = PlatformClient(base, env_admin_token())
        org_id, project_id, api_key = bootstrap_project(
            admin,
            org_name="examples",
            project_name=manifest["name"],
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
        ensure_cheap_route(client, name=f"{manifest['name']}-small")
        prompt_id = publish_prompt(
            client,
            manifest["prompt"]["name"],
            manifest["prompt"]["template"],
        )
        body = {
            "name": manifest["name"],
            "mode": manifest["mode"],
            "prompt_id": prompt_id,
            "model_profile": manifest["model_profile"],
            "allowed_tools": manifest.get("allowed_tools") or [],
        }
        if manifest.get("steps"):
            body["steps"] = manifest["steps"]
        if manifest.get("members"):
            body["members"] = manifest["members"]
            body["pattern"] = manifest.get("pattern") or "handoff"
            body["entrypoint"] = manifest.get("entrypoint") or manifest["members"][0]["id"]
        agent = create_and_publish_agent(client, body)
        meta.update(
            {
                "prompt_id": prompt_id,
                "agent_id": agent["id"],
                "sample_query": manifest.get("sample_query", "ping"),
            }
        )
        save_state(STATE, meta)
    finally:
        client.close()

    print(f"registered agent_id={meta['agent_id']}")
    if meta.get("api_key"):
        print("export PLATFORM_API_KEY=" + meta["api_key"])


if __name__ == "__main__":
    main()
