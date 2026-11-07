"""Provider credential pool. Free before paid; limits and cooling are per key."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from control.records import LimitPolicy, ModelCard, ProviderCredential
from control.vocab import (
    AFFINITY_FREE_ONLY,
    AFFINITY_PAID_ONLY,
    CREDENTIAL_ACTIVE,
    CREDENTIAL_STATUSES,
    CREDENTIAL_TIERS,
    LIMIT_UNITS,
    LIMIT_WINDOW_CALENDAR,
    LIMIT_WINDOW_ROLLING,
    LIMIT_WINDOWS,
    TIER_FREE,
    TIER_LOCAL,
    TIER_ORDER_DEFAULT,
    TIER_ORDER_FREE_LOCAL_PAID,
    TIER_ORDER_FREE_PAID_LOCAL,
    TIER_ORDERS,
    TIER_PAID,
)

_PERIOD_SECONDS = {
    "1m": 60,
    "1h": 3_600,
    "1d": 86_400,
    "7d": 604_800,
    "30d": 2_592_000,
}


class CredentialPool(Protocol):
    def add(self, credential: ProviderCredential) -> None: ...

    def has_provider(self, provider: str) -> bool: ...

    def pick(
        self,
        card: ModelCard,
        *,
        skip: set[str] | None = None,
        now: float | None = None,
        tier_order: str | None = None,
    ) -> ProviderCredential | None: ...

    def commit(
        self,
        credential: ProviderCredential,
        *,
        requests: int = 1,
        tokens: int = 0,
        spend_usd: float = 0.0,
        now: float | None = None,
    ) -> None: ...

    def cool(
        self,
        credential: ProviderCredential,
        retry_after_s: float | None,
        now: float | None = None,
    ) -> None: ...


class MemoryCredentialPool:
    """In-process pool. Unit tests and single-replica demos."""

    def __init__(self, credentials: list[ProviderCredential] | None = None) -> None:
        self._lock = threading.Lock()
        self._creds: dict[str, ProviderCredential] = {}
        self._used: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._cooling_until: dict[str, float] = {}
        self._rr: dict[str, int] = defaultdict(int)
        for credential in credentials or ():
            self.add(credential)

    def add(self, credential: ProviderCredential) -> None:
        if credential.weight < 1:
            raise ValueError("credential weight must be at least 1")
        if credential.tier not in CREDENTIAL_TIERS:
            raise ValueError("credential tier must be free or paid")
        if credential.status not in CREDENTIAL_STATUSES:
            raise ValueError("credential status must be active or disabled")
        for policy in credential.limits:
            _validate_policy(policy)
        with self._lock:
            self._creds[credential.id] = credential

    def has_provider(self, provider: str) -> bool:
        name = _provider_name(provider)
        with self._lock:
            return any(_provider_name(item.provider) == name for item in self._creds.values())

    def pick(
        self,
        card: ModelCard,
        *,
        skip: set[str] | None = None,
        now: float | None = None,
        tier_order: str | None = None,
    ) -> ProviderCredential | None:
        moment = time.time() if now is None else now
        ignored = skip or set()
        provider = _provider_name(card.provider)
        order = _resolve_tier_order(card, tier_order)
        with self._lock:
            for tier in _tier_order(card.tier_affinity, order):
                eligible = [
                    cred
                    for cred in self._creds.values()
                    if cred.id not in ignored
                    and cred.status == CREDENTIAL_ACTIVE
                    and cred.tier == tier
                    and _provider_name(cred.provider) == provider
                    and _allows_model(cred, card.name)
                    and self._cooling_until.get(cred.id, 0.0) <= moment
                    and self._under_limits(cred, moment)
                ]
                chosen = self._weighted_rr(provider, tier, eligible)
                if chosen is not None:
                    return chosen
        return None

    def commit(
        self,
        credential: ProviderCredential,
        *,
        requests: int = 1,
        tokens: int = 0,
        spend_usd: float = 0.0,
        now: float | None = None,
    ) -> None:
        moment = time.time() if now is None else now
        amounts = {
            "requests": float(requests),
            "tokens": float(tokens),
            "spend_usd": float(spend_usd),
        }
        with self._lock:
            for policy in credential.limits:
                key = _counter_key(credential.id, policy, moment)
                self._used[credential.id][key] += amounts.get(policy.unit, 0.0)

    def cool(
        self,
        credential: ProviderCredential,
        retry_after_s: float | None,
        now: float | None = None,
    ) -> None:
        moment = time.time() if now is None else now
        if retry_after_s is not None and credential.cooldown_from_header:
            delay = max(0.0, float(retry_after_s))
        else:
            delay = float(credential.default_cooldown_s)
        with self._lock:
            until = moment + delay
            previous = self._cooling_until.get(credential.id, 0.0)
            if until > previous:
                self._cooling_until[credential.id] = until

    def _under_limits(self, credential: ProviderCredential, now: float) -> bool:
        for policy in credential.limits:
            key = _counter_key(credential.id, policy, now)
            if self._used[credential.id].get(key, 0.0) >= policy.max:
                return False
        return True

    def _weighted_rr(
        self, provider: str, tier: str, eligible: list[ProviderCredential]
    ) -> ProviderCredential | None:
        if not eligible:
            return None
        slot = f"{provider}:{tier}"
        total = sum(item.weight for item in eligible)
        index = self._rr[slot] % total
        self._rr[slot] += 1
        cursor = 0
        for item in sorted(eligible, key=lambda cred: cred.id):
            cursor += item.weight
            if index < cursor:
                return item
        return eligible[0]


class RedisCredentialPool:
    """Shared counters and cooling. Credential secrets stay in process memory."""

    def __init__(
        self,
        client: object,
        credentials: list[ProviderCredential] | None = None,
        memory: MemoryCredentialPool | None = None,
    ) -> None:
        self.client = client
        self.memory = memory or MemoryCredentialPool(credentials)

    def add(self, credential: ProviderCredential) -> None:
        self.memory.add(credential)

    def has_provider(self, provider: str) -> bool:
        return self.memory.has_provider(provider)

    def pick(
        self,
        card: ModelCard,
        *,
        skip: set[str] | None = None,
        now: float | None = None,
        tier_order: str | None = None,
    ) -> ProviderCredential | None:
        moment = time.time() if now is None else now
        ignored = skip or set()
        provider = _provider_name(card.provider)
        order = _resolve_tier_order(card, tier_order)
        for tier in _tier_order(card.tier_affinity, order):
            eligible: list[ProviderCredential] = []
            for cred in list(self.memory._creds.values()):
                if cred.id in ignored or cred.status != CREDENTIAL_ACTIVE or cred.tier != tier:
                    continue
                if _provider_name(cred.provider) != provider or not _allows_model(cred, card.name):
                    continue
                if self._cooling(cred.id, moment):
                    continue
                if not self._under_limits(cred, moment):
                    continue
                eligible.append(cred)
            chosen = self._weighted_rr(provider, tier, eligible)
            if chosen is not None:
                return chosen
        return None

    def commit(
        self,
        credential: ProviderCredential,
        *,
        requests: int = 1,
        tokens: int = 0,
        spend_usd: float = 0.0,
        now: float | None = None,
    ) -> None:
        moment = time.time() if now is None else now
        amounts = {
            "requests": float(requests),
            "tokens": float(tokens),
            "spend_usd": float(spend_usd),
        }
        try:
            for policy in credential.limits:
                amount = amounts.get(policy.unit, 0.0)
                if amount <= 0:
                    continue
                key = f"cred:used:{_counter_key(credential.id, policy, moment)}"
                pipe = self.client.pipeline()
                pipe.incrbyfloat(key, amount)
                pipe.expire(key, _ttl_seconds(policy))
                pipe.execute()
        except OSError:
            self.memory.commit(
                credential, requests=requests, tokens=tokens, spend_usd=spend_usd, now=moment
            )

    def cool(
        self,
        credential: ProviderCredential,
        retry_after_s: float | None,
        now: float | None = None,
    ) -> None:
        moment = time.time() if now is None else now
        if retry_after_s is not None and credential.cooldown_from_header:
            delay = max(1, int(float(retry_after_s)))
        else:
            delay = max(1, int(credential.default_cooldown_s))
        try:
            self.client.set(f"cred:cooling:{credential.id}", "1", ex=delay)
        except OSError:
            self.memory.cool(credential, float(delay), now=moment)

    def _cooling(self, credential_id: str, now: float) -> bool:
        del now
        try:
            return bool(self.client.exists(f"cred:cooling:{credential_id}"))
        except OSError:
            return self.memory._cooling_until.get(credential_id, 0.0) > time.time()

    def _under_limits(self, credential: ProviderCredential, now: float) -> bool:
        try:
            for policy in credential.limits:
                key = f"cred:used:{_counter_key(credential.id, policy, now)}"
                raw = self.client.get(key)
                used = float(raw or 0)
                if used >= policy.max:
                    return False
            return True
        except (OSError, TypeError, ValueError):
            return self.memory._under_limits(credential, now)

    def _weighted_rr(
        self, provider: str, tier: str, eligible: list[ProviderCredential]
    ) -> ProviderCredential | None:
        if not eligible:
            return None
        total = sum(item.weight for item in eligible)
        slot = f"cred:rr:{provider}:{tier}"
        try:
            index = int(self.client.incr(slot)) - 1
            self.client.expire(slot, 86_400)
            index %= total
        except OSError:
            return self.memory._weighted_rr(provider, tier, eligible)
        cursor = 0
        for item in sorted(eligible, key=lambda cred: cred.id):
            cursor += item.weight
            if index < cursor:
                return item
        return eligible[0]


def load_credentials(settings: dict[str, str]) -> list[ProviderCredential]:
    """Build the pool from env or a YAML/JSON file. Secrets are not logged.

    Entries that point at an unset `api_key_env` are skipped so a checked-in
    inventory file does not break API import or CI without a local secret file.
    """
    base_url = settings.get("llm_base_url", "")
    raw = settings.get("llm_credentials", "").strip()
    if raw:
        return _load_entries(_parse_credentials_payload(raw, "env"), base_url)
    path = settings.get("llm_credentials_file", "").strip()
    if path and os.path.isfile(path):
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read credentials file {path}") from exc
        return _load_entries(_parse_credentials_file(path, text), base_url)
    api_key = settings.get("llm_api_key", "")
    if api_key:
        return [
            ProviderCredential(
                id="default",
                provider="default",
                tier=TIER_PAID,
                api_key=api_key,
                base_url=base_url,
            )
        ]
    return []


def _load_entries(items: list[object], base_url: str) -> list[ProviderCredential]:
    import logging

    loaded: list[ProviderCredential] = []
    skipped = 0
    for item in items:
        try:
            loaded.append(_from_dict(item, base_url))
        except ValueError as exc:
            if "has no api key" not in str(exc):
                raise
            skipped += 1
    if skipped:
        logging.getLogger("genai").info('{"message":"credentials skipped","count":%s}', skipped)
    return loaded


def _parse_credentials_file(path: str, text: str) -> list[object]:
    lowered = path.lower()
    if lowered.endswith((".yaml", ".yml")):
        import yaml

        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"credentials file {path} must be YAML") from exc
        return _as_credential_list(payload, path)
    if lowered.endswith(".json"):
        return _parse_credentials_payload(text, path)
    raise ValueError(f"credentials file {path} must be .yaml, .yml, or .json")


def _parse_credentials_payload(raw: str, source: str) -> list[object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM_CREDENTIALS ({source}) must be JSON") from exc
    return _as_credential_list(payload, source)


def _as_credential_list(payload: object, source: str) -> list[object]:
    if isinstance(payload, dict) and "credentials" in payload:
        payload = payload["credentials"]
    if not isinstance(payload, list):
        raise ValueError(f"credentials in {source} must be a list")
    return payload


def _from_dict(raw: object, default_base: str) -> ProviderCredential:
    if not isinstance(raw, dict):
        raise ValueError("credential entry must be an object")
    env_name = str(raw.get("api_key_env") or "")
    api_key = str(raw.get("api_key") or "")
    if env_name:
        api_key = os.environ.get(env_name, "")
    if not api_key:
        raise ValueError(f"credential {raw.get('id')} has no api key")
    limits_raw = raw.get("limits") or []
    if not isinstance(limits_raw, list):
        raise ValueError("limits must be a list")
    limits = tuple(_policy_from_dict(item) for item in limits_raw)
    allow = raw.get("models_allow") or ["*"]
    if not isinstance(allow, list) or not allow:
        raise ValueError("models_allow must be a non-empty list")
    headers_raw = raw.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise ValueError("headers must be an object")
    headers = {str(key): str(value) for key, value in headers_raw.items()}
    return ProviderCredential(
        id=str(raw["id"]),
        provider=str(raw.get("provider") or "default"),
        tier=str(raw.get("tier") or TIER_PAID),
        api_key=api_key,
        base_url=str(raw.get("base_url") or default_base),
        weight=max(1, int(raw.get("weight") or 1)),
        status=str(raw.get("status") or CREDENTIAL_ACTIVE),
        models_allow=tuple(str(item) for item in allow),
        limits=limits,
        cooldown_from_header=bool(raw.get("cooldown_from_header", True)),
        default_cooldown_s=max(1, int(raw.get("default_cooldown_s") or 60)),
        headers=headers,
    )


def _policy_from_dict(raw: object) -> LimitPolicy:
    if not isinstance(raw, dict):
        raise ValueError("limit entry must be an object")
    policy = LimitPolicy(
        period=str(raw["period"]),
        unit=str(raw["unit"]),
        max=float(raw["max"]),
        window=str(raw.get("window") or LIMIT_WINDOW_CALENDAR),
    )
    _validate_policy(policy)
    return policy


def _validate_policy(policy: LimitPolicy) -> None:
    if policy.period not in _PERIOD_SECONDS:
        raise ValueError(f"unsupported limit period {policy.period}")
    if policy.unit not in LIMIT_UNITS:
        raise ValueError(f"unsupported limit unit {policy.unit}")
    if policy.window not in LIMIT_WINDOWS:
        raise ValueError(f"unsupported limit window {policy.window}")
    if policy.max <= 0:
        raise ValueError("limit max must be positive")


def _resolve_tier_order(card: ModelCard, tier_order: str | None) -> str:
    raw = (tier_order or card.tier_order or TIER_ORDER_DEFAULT).strip()
    if raw not in TIER_ORDERS:
        return TIER_ORDER_DEFAULT
    return raw


def _tier_order(affinity: str, order: str = TIER_ORDER_DEFAULT) -> tuple[str, ...]:
    if affinity == AFFINITY_FREE_ONLY:
        return (TIER_FREE,)
    if affinity == AFFINITY_PAID_ONLY:
        return (TIER_PAID,)
    if order == TIER_ORDER_FREE_LOCAL_PAID:
        return (TIER_FREE, TIER_LOCAL, TIER_PAID)
    if order == TIER_ORDER_FREE_PAID_LOCAL:
        return (TIER_FREE, TIER_PAID, TIER_LOCAL)
    return (TIER_FREE, TIER_PAID, TIER_LOCAL)


def _provider_name(provider: str) -> str:
    return provider.strip() or "default"


def _allows_model(credential: ProviderCredential, model_name: str) -> bool:
    if "*" in credential.models_allow:
        return True
    return model_name in credential.models_allow


def _counter_key(credential_id: str, policy: LimitPolicy, now: float) -> str:
    stamp = _window_stamp(policy, now)
    return f"{credential_id}:{policy.unit}:{policy.period}:{policy.window}:{stamp}"


def _window_stamp(policy: LimitPolicy, now: float) -> str:
    if policy.window == LIMIT_WINDOW_ROLLING:
        seconds = _PERIOD_SECONDS[policy.period]
        return str(int(now) // seconds)
    instant = datetime.fromtimestamp(now, tz=UTC)
    if policy.period == "1m":
        return instant.strftime("%Y%m%d%H%M")
    if policy.period == "1h":
        return instant.strftime("%Y%m%d%H")
    if policy.period == "1d":
        return instant.strftime("%Y%m%d")
    if policy.period == "7d":
        iso = instant.isocalendar()
        return f"{iso.year}W{iso.week:02d}"
    return instant.strftime("%Y%m")


def _ttl_seconds(policy: LimitPolicy) -> int:
    return _PERIOD_SECONDS[policy.period] * 2
