from __future__ import annotations

import time

import pytest

from control.errors import PlatformError
from control.records import LimitPolicy, ModelCard, ProviderCredential
from control.store import new_id
from core.credentials import MemoryCredentialPool, load_credentials
from core.model import ModelOutput
from tests.conftest import project


def _card(
    svc,
    principal,
    *,
    name: str = "llama-free",
    provider: str = "openrouter",
    affinity: str = "any",
    price: float = 0.0,
    tier_order: str = "free_paid_local",
) -> ModelCard:
    return svc.register_model(
        principal,
        ModelCard(
            id=new_id("mdl"),
            project_id=principal.project_id,
            name=name,
            profile="cheap",
            capabilities=("cheap",),
            input_price=price,
            output_price=price,
            pricing_version="2026-11",
            provider=provider,
            tier_affinity=affinity,
            tier_order=tier_order,
        ),
    )


def _cred(
    cred_id: str,
    *,
    tier: str = "free",
    provider: str = "openrouter",
    limits: tuple[LimitPolicy, ...] = (),
    models_allow: tuple[str, ...] = ("*",),
    weight: int = 1,
    default_cooldown_s: int = 60,
) -> ProviderCredential:
    return ProviderCredential(
        id=cred_id,
        provider=provider,
        tier=tier,
        api_key=f"sk-{cred_id}",
        base_url="http://llm.test/v1",
        weight=weight,
        limits=limits,
        models_allow=models_allow,
        default_cooldown_s=default_cooldown_s,
    )


def test_weighted_round_robin_spreads_free_keys(svc):
    principal, _ = project(svc)
    card = _card(svc, principal)
    svc.set_route(principal, "cheap", card.id, None)
    svc.add_credential(_cred("free-a"))
    svc.add_credential(_cred("free-b"))
    for _ in range(4):
        svc.model.queue.append(ModelOutput(text="ok", finish=True))
        svc.chat(principal, "cheap", None, ["q"])
    rows = svc.usage(principal)
    assert [row.credential_id for row in rows] == ["free-a", "free-b", "free-a", "free-b"]
    assert {row.tier for row in rows} == {"free"}


def test_free_before_paid_when_affinity_any(svc):
    principal, _ = project(svc)
    card = _card(svc, principal, affinity="any", price=1.0)
    svc.set_route(principal, "cheap", card.id, None)
    svc.add_credential(_cred("free-1"))
    svc.add_credential(_cred("paid-1", tier="paid"))
    svc.model.queue.append(ModelOutput(text="ok", finish=True, input_tokens=1, output_tokens=1))
    svc.chat(principal, "cheap", None, ["q"])
    assert svc.usage(principal)[0].credential_id == "free-1"
    assert svc.usage(principal)[0].tier == "free"


def test_daily_limit_rotates_to_next_free_then_paid(svc):
    principal, _ = project(svc)
    card = _card(svc, principal, affinity="any", price=2.0)
    svc.set_route(principal, "cheap", card.id, None)
    daily = (LimitPolicy(period="1d", unit="requests", max=1, window="calendar"),)
    svc.add_credential(_cred("free-1", limits=daily))
    svc.add_credential(_cred("free-2", limits=daily))
    svc.add_credential(_cred("paid-1", tier="paid"))
    for _ in range(3):
        svc.model.queue.append(ModelOutput(text="ok", finish=True, input_tokens=1, output_tokens=1))
        svc.chat(principal, "cheap", None, ["q"])
    assert [row.credential_id for row in svc.usage(principal)] == [
        "free-1",
        "free-2",
        "paid-1",
    ]


def test_free_only_never_uses_paid(svc):
    principal, _ = project(svc)
    card = _card(svc, principal, affinity="free_only")
    svc.set_route(principal, "cheap", card.id, None)
    svc.add_credential(_cred("free-1", limits=(LimitPolicy(period="1d", unit="requests", max=1),)))
    svc.add_credential(_cred("paid-1", tier="paid"))
    svc.model.queue.append(ModelOutput(text="ok", finish=True))
    svc.chat(principal, "cheap", None, ["q"])
    with pytest.raises(PlatformError) as caught:
        svc.chat(principal, "cheap", None, ["again"])
    assert caught.value.status == 502
    assert all(row.tier != "paid" for row in svc.usage(principal))


def test_free_local_paid_order_prefers_local_before_paid(svc):
    principal, _ = project(svc)
    card = _card(svc, principal, tier_order="free_local_paid")
    svc.set_route(principal, "cheap", card.id, None)
    daily = (LimitPolicy(period="1d", unit="requests", max=1, window="calendar"),)
    svc.add_credential(_cred("free-1", limits=daily))
    svc.add_credential(_cred("local-1", tier="local"))
    svc.add_credential(_cred("paid-1", tier="paid"))
    for _ in range(2):
        svc.model.queue.append(ModelOutput(text="ok", finish=True, input_tokens=1, output_tokens=1))
        svc.chat(principal, "cheap", None, ["q"])
    assert [row.credential_id for row in svc.usage(principal)] == ["free-1", "local-1"]


def test_route_tier_order_overrides_card(svc):
    principal, _ = project(svc)
    card = _card(svc, principal, tier_order="free_paid_local")
    svc.set_route(
        principal,
        "cheap",
        card.id,
        None,
        tier_order="free_local_paid",
    )
    daily = (LimitPolicy(period="1d", unit="requests", max=1, window="calendar"),)
    svc.add_credential(_cred("free-1", limits=daily))
    svc.add_credential(_cred("local-1", tier="local"))
    svc.add_credential(_cred("paid-1", tier="paid"))
    for _ in range(2):
        svc.model.queue.append(ModelOutput(text="ok", finish=True, input_tokens=1, output_tokens=1))
        svc.chat(principal, "cheap", None, ["q"])
    assert [row.tier for row in svc.usage(principal)] == ["free", "local"]


def test_default_order_uses_paid_before_local(svc):
    principal, _ = project(svc)
    card = _card(svc, principal, tier_order="free_paid_local")
    svc.set_route(principal, "cheap", card.id, None)
    daily = (LimitPolicy(period="1d", unit="requests", max=1, window="calendar"),)
    svc.add_credential(_cred("free-1", limits=daily))
    svc.add_credential(_cred("local-1", tier="local"))
    svc.add_credential(_cred("paid-1", tier="paid"))
    for _ in range(2):
        svc.model.queue.append(ModelOutput(text="ok", finish=True, input_tokens=1, output_tokens=1))
        svc.chat(principal, "cheap", None, ["q"])
    assert [row.credential_id for row in svc.usage(principal)] == ["free-1", "paid-1"]


def test_limit_hit_cools_key_and_rotates(svc):
    principal, _ = project(svc)
    card = _card(svc, principal)
    svc.set_route(principal, "cheap", card.id, None)
    svc.add_credential(_cred("free-a", default_cooldown_s=30))
    svc.add_credential(_cred("free-b", default_cooldown_s=30))
    svc.model.down.add("free-a")
    svc.model.queue.append(ModelOutput(text="from-b", finish=True))
    output = svc.chat(principal, "cheap", None, ["q"])
    assert output.text == "from-b"
    rows = svc.usage(principal)
    assert [row.credential_id for row in rows] == ["free-a", "free-b"]
    assert [row.status for row in rows] == ["failed", "success"]
    assert rows[0].tier == "free"
    text = svc.metrics.render()
    assert 'genai_provider_attempts_total{model="llama-free",outcome="limit",tier="free"}' in text
    assert 'genai_provider_attempts_total{model="llama-free",outcome="ok",tier="free"}' in text


def test_all_keys_limited_does_not_open_model_breaker(svc):
    principal, _ = project(svc)
    model = _card(svc, principal)
    svc.set_route(principal, "cheap", model.id, None)
    svc.add_credential(_cred("free-a", default_cooldown_s=30))
    svc.add_credential(_cred("free-b", default_cooldown_s=30))
    svc.model.down.update({"free-a", "free-b"})
    # Three attempts: first burns both keys; later ones find the pool cooled.
    for _ in range(3):
        with pytest.raises(PlatformError) as caught:
            svc.chat(principal, "cheap", None, ["q"])
        assert caught.value.status == 502
    rows = svc.usage(principal)
    # First request ledgers both limited keys; later requests find a cooled pool.
    assert [row.credential_id for row in rows] == ["free-a", "free-b"]
    assert all(row.status == "failed" for row in rows)
    assert svc.gateway.breaker.allow(model.id, time.monotonic()) is True


def test_weekly_calendar_limit(svc):
    pool = MemoryCredentialPool()
    policy = LimitPolicy(period="7d", unit="requests", max=2, window="calendar")
    cred = _cred("week-1", limits=(policy,))
    pool.add(cred)
    card = ModelCard(
        id="m1",
        project_id="p",
        name="m",
        profile="cheap",
        capabilities=("cheap",),
        input_price=0,
        output_price=0,
        pricing_version="v",
        provider="openrouter",
    )
    now = 1_700_000_000.0
    assert pool.pick(card, now=now) is not None
    pool.commit(cred, requests=1, now=now)
    pool.commit(cred, requests=1, now=now)
    assert pool.pick(card, now=now) is None
    assert pool.pick(card, now=now + 8 * 86_400) is not None


def test_models_allow_filters_keys(svc):
    principal, _ = project(svc)
    card = _card(svc, principal, name="only-on-b")
    svc.set_route(principal, "cheap", card.id, None)
    svc.add_credential(_cred("free-a", models_allow=("other",)))
    svc.add_credential(_cred("free-b", models_allow=("only-on-b",)))
    svc.model.queue.append(ModelOutput(text="ok", finish=True))
    svc.chat(principal, "cheap", None, ["q"])
    assert svc.usage(principal)[0].credential_id == "free-b"


def test_load_credentials_from_json(monkeypatch):
    monkeypatch.setenv("OR_KEY_01", "secret-one")
    settings = {
        "llm_base_url": "https://openrouter.ai/api/v1",
        "llm_credentials": (
            '[{"id":"or-free-01","provider":"openrouter","tier":"free",'
            '"api_key_env":"OR_KEY_01","limits":[{"period":"1d","unit":"requests","max":50}]}]'
        ),
    }
    loaded = load_credentials(settings)
    assert len(loaded) == 1
    assert loaded[0].api_key == "secret-one"
    assert loaded[0].limits[0].max == 50
    assert "secret-one" not in settings["llm_credentials"]


def test_load_credentials_from_yaml_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OR_KEY_01", "secret-one")
    path = tmp_path / "llm_credentials.yaml"
    path.write_text(
        """
credentials:
  - id: or-free-01
    provider: openrouter
    tier: free
    api_key_env: OR_KEY_01
    limits:
      - { period: 1d, unit: requests, max: 50 }
""",
        encoding="utf-8",
    )
    loaded = load_credentials(
        {
            "llm_base_url": "https://openrouter.ai/api/v1",
            "llm_credentials": "",
            "llm_credentials_file": str(path),
        }
    )
    assert loaded[0].id == "or-free-01"
    assert loaded[0].api_key == "secret-one"


def test_load_credentials_skips_missing_env_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    monkeypatch.setenv("PRESENT_KEY", "secret-ok")
    path = tmp_path / "llm_credentials.yaml"
    path.write_text(
        """
credentials:
  - id: missing
    provider: openrouter
    tier: free
    api_key_env: MISSING_KEY
  - id: present
    provider: openrouter
    tier: free
    api_key_env: PRESENT_KEY
""",
        encoding="utf-8",
    )
    loaded = load_credentials(
        {"llm_credentials": "", "llm_credentials_file": str(path), "llm_base_url": ""}
    )
    assert [item.id for item in loaded] == ["present"]


def test_load_portkey_headers_from_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTKEY_PAID_01", "pk-test")
    path = tmp_path / "llm_credentials.yaml"
    path.write_text(
        """
credentials:
  - id: portkey-paid-01
    provider: portkey
    tier: paid
    api_key_env: PORTKEY_PAID_01
    base_url: https://api.portkey.ai/v1
    headers:
      x-portkey-config: pc-demo
""",
        encoding="utf-8",
    )
    loaded = load_credentials(
        {"llm_credentials": "", "llm_credentials_file": str(path), "llm_base_url": ""}
    )
    assert loaded[0].headers["x-portkey-config"] == "pc-demo"
    assert loaded[0].base_url.endswith("/v1")


def test_inline_credentials_override_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OR_KEY_01", "from-file")
    monkeypatch.setenv("OR_KEY_02", "from-env")
    path = tmp_path / "llm_credentials.yaml"
    path.write_text(
        """
credentials:
  - id: from-file
    provider: openrouter
    tier: free
    api_key_env: OR_KEY_01
""",
        encoding="utf-8",
    )
    loaded = load_credentials(
        {
            "llm_base_url": "https://openrouter.ai/api/v1",
            "llm_credentials": (
                '[{"id":"from-env","provider":"openrouter","tier":"free",'
                '"api_key_env":"OR_KEY_02"}]'
            ),
            "llm_credentials_file": str(path),
        }
    )
    assert [item.id for item in loaded] == ["from-env"]
