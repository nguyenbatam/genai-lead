"""Policy, fallback, and the usage ledger. Providers are adapters behind this."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from control.records import ModelCard, Principal, ProviderCredential, Usage
from control.store import Store
from control.vocab import (
    MAX_ROUTE_FALLBACKS,
    TIER_ORDER_DEFAULT,
    TIER_ORDERS,
    USAGE_FAILED,
    USAGE_SUCCESS,
)
from core.credentials import CredentialPool
from core.metrics import Metrics
from core.model import (
    MAX_OUTPUT_TOKENS,
    ModelOutput,
    ProviderDown,
    ProviderLimitHit,
    ProviderRejected,
    ScriptedModel,
)


def token_micros(card: ModelCard, input_tokens: int, output_tokens: int) -> int:
    in_rate = round(card.input_price * 1_000_000)
    out_rate = round(card.output_price * 1_000_000)
    return (input_tokens * in_rate + output_tokens * out_rate) // 1_000_000


def cost_of(card: ModelCard, output: ModelOutput) -> float:
    """USD from the pricing version on the card. Integer micros, then scaled."""
    return token_micros(card, output.input_tokens, output.output_tokens) / 1_000_000


def ceiling_micros(card: ModelCard, messages: list[str]) -> int:
    """Worst case before the provider answers. Protocol overhead is included.

    Char/2 covers CJK denser than ASCII char/4 so the hold rarely undershoots actual.
    """
    estimated = max(1, sum(len(part) for part in messages) // 2) + 384
    return token_micros(card, estimated, MAX_OUTPUT_TOKENS)


class SpendExceeded(Exception):
    """Project USD cap is already spent. The provider is not called."""


class ProviderBusy(Exception):
    """Project already holds its share of provider calls. The provider is not called."""


class Breaker:
    """Per-model fail-fast. Half-open lets a single probe through."""

    def __init__(self, limit: int = 3, cooldown_s: float = 30) -> None:
        self.limit = limit
        self.cooldown_s = cooldown_s
        self.failures: dict[str, int] = {}
        self.open_until: dict[str, float] = {}
        self.probing: set[str] = set()
        self._lock = threading.Lock()

    def allow(self, model_id: str, now: float) -> bool:
        with self._lock:
            until = self.open_until.get(model_id, 0.0)
            if until and now < until:
                return False
            if until:
                if model_id in self.probing:
                    return False
                self.probing.add(model_id)
                return True
            return True

    def success(self, model_id: str) -> None:
        with self._lock:
            self.failures[model_id] = 0
            self.open_until.pop(model_id, None)
            self.probing.discard(model_id)

    def failure(self, model_id: str, now: float) -> None:
        with self._lock:
            self.probing.discard(model_id)
            count = self.failures.get(model_id, 0) + 1
            self.failures[model_id] = count
            if count >= self.limit:
                self.open_until[model_id] = now + self.cooldown_s

    def forget_probe(self, model_id: str) -> None:
        with self._lock:
            self.probing.discard(model_id)


class RedisBreaker:
    """Shared breaker for multi-replica. Keys expire; a missing key is closed."""

    def __init__(self, client: object, limit: int = 3, cooldown_s: float = 30) -> None:
        self.client = client
        self.limit = limit
        self.cooldown_s = int(cooldown_s)

    def allow(self, model_id: str, now: float) -> bool:
        del now
        open_key = f"breaker:open:{model_id}"
        probe_key = f"breaker:probe:{model_id}"
        try:
            if not self.client.exists(open_key):
                return True
            return bool(self.client.set(probe_key, "1", nx=True, ex=self.cooldown_s))
        except OSError:
            return True

    def success(self, model_id: str) -> None:
        try:
            pipe = self.client.pipeline()
            pipe.delete(f"breaker:fail:{model_id}")
            pipe.delete(f"breaker:open:{model_id}")
            pipe.delete(f"breaker:probe:{model_id}")
            pipe.execute()
        except OSError:
            return

    def failure(self, model_id: str, now: float) -> None:
        del now
        fail_key = f"breaker:fail:{model_id}"
        try:
            self.client.delete(f"breaker:probe:{model_id}")
            count = int(self.client.incr(fail_key))
            if count == 1:
                self.client.expire(fail_key, self.cooldown_s * 4)
            if count >= self.limit:
                self.client.set(f"breaker:open:{model_id}", "1", ex=self.cooldown_s)
        except OSError:
            return

    def forget_probe(self, model_id: str) -> None:
        try:
            self.client.delete(f"breaker:probe:{model_id}")
        except OSError:
            return


class Gateway:
    def __init__(
        self,
        store: Store,
        model: ScriptedModel,
        observe: Callable[[int], None] | None = None,
        breaker: Breaker | RedisBreaker | None = None,
        limiter: object | None = None,
        pool: CredentialPool | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.store = store
        self.model = model
        self.breaker = breaker or Breaker()
        self.observe = observe
        self.limiter = limiter
        self.pool = pool
        self.metrics = metrics

    def complete(
        self,
        principal: Principal,
        *,
        task: str,
        model_id: str | None,
        messages: list[str],
        tools: tuple[str, ...] = (),
    ) -> tuple[ModelOutput, ModelCard, float]:
        from core.trace import span

        with span("gateway.complete", project_id=principal.project_id, task=task):
            return self._complete(
                principal, task=task, model_id=model_id, messages=messages, tools=tools
            )

    def _complete(
        self,
        principal: Principal,
        *,
        task: str,
        model_id: str | None,
        messages: list[str],
        tools: tuple[str, ...] = (),
    ) -> tuple[ModelOutput, ModelCard, float]:
        primary = self._resolve(principal, task, model_id)
        chain = self._chain(principal, task, primary)
        ceiling = max(ceiling_micros(card, messages) for card in chain)
        self._provider_slot(principal)
        settled = False
        held = 0
        try:
            now_ms = int(time.time() * 1000)
            reserved = self.store.reserve_spend(principal.project_id, ceiling, now_ms)
            if reserved is None:
                raise SpendExceeded()
            held = reserved
            output, card, spent = self._walk(principal, chain, messages, task, tools, held)
            settled = True
            return output, card, spent
        finally:
            self._release_provider(principal)
            if held and not settled:
                self.store.release_spend(principal.project_id, held)

    def _walk(
        self,
        principal: Principal,
        chain: list[ModelCard],
        messages: list[str],
        task: str,
        tools: tuple[str, ...],
        settle_held: int,
    ) -> tuple[ModelOutput, ModelCard, float]:
        previous_id: str | None = None
        last_down: ProviderDown | None = None
        for card in chain:
            if not self.breaker.allow(card.id, time.monotonic()):
                # Open breaker: no call, but still attribute the next hop as fallback.
                last_down = ProviderDown(card.id)
                previous_id = card.id
                continue
            tier_order = self._tier_order_for(principal, task, card)
            try:
                output, latency, credential = self._call(
                    principal, card, messages, tools, tier_order=tier_order
                )
            except ProviderRejected:
                self.breaker.forget_probe(card.id)
                raise
            except ProviderDown as exc:
                if exc.trip_breaker:
                    self.breaker.failure(card.id, time.monotonic())
                if not exc.ledgered:
                    self._usage(
                        principal,
                        card,
                        _empty(),
                        USAGE_FAILED,
                        previous_id,
                        exc.wire_ms,
                        0,
                    )
                previous_id = card.id
                last_down = exc
                continue
            self.breaker.success(card.id)
            if previous_id is not None and self.metrics is not None:
                self.metrics.fallbacks.inc(
                    model=card.name,
                    tier="none" if credential is None else (credential.tier or "none"),
                )
            spent = cost_of(card, output)
            self._usage(
                principal,
                card,
                output,
                USAGE_SUCCESS,
                previous_id,
                latency,
                settle_held,
                credential,
            )
            return output, card, spent
        if last_down is not None:
            raise last_down
        raise ProviderDown(chain[-1].id)

    def _provider_slot(self, principal: Principal) -> None:
        if self.limiter is None:
            return
        project = self.store.get_project(principal.project_id)
        if project is None:
            raise ProviderBusy()
        if not self.limiter.acquire(
            principal.project_id, "provider", project.max_provider_inflight
        ):
            raise ProviderBusy()

    def _release_provider(self, principal: Principal) -> None:
        if self.limiter is None:
            return
        self.limiter.release(principal.project_id, "provider")

    def _call(
        self,
        principal: Principal,
        card: ModelCard,
        messages: list[str],
        tools: tuple[str, ...],
        *,
        tier_order: str,
    ) -> tuple[ModelOutput, int, ProviderCredential | None]:
        if self.pool is None or not self.pool.has_provider(card.provider):
            output, latency = self._invoke(card, messages, tools, None)
            self._note_attempt(card.name, "none", "ok")
            return output, latency, None
        skip: set[str] = set()
        last_down: ProviderDown | None = None
        ledgered = False
        trip_breaker = False
        while True:
            credential = self.pool.pick(card, skip=skip, tier_order=tier_order)
            if credential is None:
                if last_down is not None:
                    last_down.ledgered = ledgered
                    last_down.trip_breaker = trip_breaker
                    raise last_down
                # Nothing to try (cooled/empty pool) — no phantom usage row.
                raise ProviderDown(card.id, ledgered=True, trip_breaker=False)
            skip.add(credential.id)
            try:
                output, latency = self._invoke(card, messages, tools, credential)
            except ProviderLimitHit as exc:
                self._note_attempt(card.name, credential.tier, "limit")
                self._usage(
                    principal,
                    card,
                    _empty(),
                    USAGE_FAILED,
                    None,
                    exc.wire_ms,
                    0,
                    credential,
                )
                ledgered = True
                self.pool.cool(credential, exc.retry_after_s)
                self.pool.commit(credential, requests=1)
                last_down = ProviderDown(card.id, wire_ms=exc.wire_ms, trip_breaker=False)
                continue
            except ProviderDown as exc:
                self._note_attempt(card.name, credential.tier, "down")
                self._usage(
                    principal,
                    card,
                    _empty(),
                    USAGE_FAILED,
                    None,
                    exc.wire_ms,
                    0,
                    credential,
                )
                ledgered = True
                trip_breaker = True
                self.pool.cool(credential, None)
                self.pool.commit(credential, requests=1)
                last_down = exc
                continue
            except ProviderRejected:
                self._note_attempt(card.name, credential.tier, "rejected")
                self.pool.commit(credential, requests=1)
                raise
            self._note_attempt(card.name, credential.tier, "ok")
            spent = cost_of(card, output)
            self.pool.commit(
                credential,
                requests=1,
                tokens=output.input_tokens + output.output_tokens,
                spend_usd=spent,
            )
            return output, latency, credential

    def _invoke(
        self,
        card: ModelCard,
        messages: list[str],
        tools: tuple[str, ...],
        credential: ProviderCredential | None,
    ) -> tuple[ModelOutput, int]:
        started = time.perf_counter()
        tier = "none" if credential is None else (credential.tier or "none")
        model = card.name or "unknown"
        output: ModelOutput | None = None
        error: BaseException | None = None
        try:
            if credential is None:
                output = self.model.complete(card, messages, tools)
            else:
                output = self.model.complete(
                    card,
                    messages,
                    tools,
                    api_key=credential.api_key,
                    base_url=credential.base_url or None,
                    credential_id=credential.id,
                    headers=credential.headers or None,
                )
        except BaseException as exc:
            error = exc
        wire_ms = self._finish_invoke(started, model, tier)
        if error is not None:
            if isinstance(error, (ProviderDown, ProviderLimitHit, ProviderRejected)):
                error.wire_ms = wire_ms
            raise error
        assert output is not None
        return output, wire_ms

    def _finish_invoke(self, started: float, model: str, tier: str) -> int:
        wall_ms = int((time.perf_counter() - started) * 1000)
        raw_wire = getattr(self.model, "last_wire_ms", None)
        wire_ms = wall_ms if raw_wire is None else int(raw_wire)
        retries = int(getattr(self.model, "last_http_retries", 0) or 0)
        if self.observe is not None:
            self.observe(wire_ms)
        if self.metrics is not None and retries:
            self.metrics.retries.inc(retries, model=model, tier=tier)
        return wire_ms

    def _note_attempt(self, model: str, tier: str, outcome: str) -> None:
        if self.metrics is None:
            return
        self.metrics.attempts.inc(model=model or "unknown", tier=tier or "none", outcome=outcome)

    def _resolve(self, principal: Principal, task: str, model_id: str | None) -> ModelCard:
        if model_id:
            return self.store.model_for(principal, model_id)
        route = self.store.route_for(principal.project_id, task)
        if route is None:
            raise KeyError(task)
        return self.store.model_for(principal, route.model_id)

    def _chain(self, principal: Principal, task: str, primary: ModelCard) -> list[ModelCard]:
        cards = [primary]
        route = self.store.route_for(principal.project_id, task)
        if route is None or route.model_id != primary.id:
            return cards
        for model_id in route.fallback_model_ids[:MAX_ROUTE_FALLBACKS]:
            try:
                card = self.store.model_for(principal, model_id)
            except KeyError:
                continue
            if not set(primary.capabilities).issubset(card.capabilities):
                continue
            cards.append(card)
        return cards

    def _tier_order_for(self, principal: Principal, task: str, card: ModelCard) -> str:
        route = self.store.route_for(principal.project_id, task)
        if route is not None and route.tier_order in TIER_ORDERS:
            return route.tier_order
        if card.tier_order in TIER_ORDERS:
            return card.tier_order
        return TIER_ORDER_DEFAULT

    def _usage(
        self,
        principal: Principal,
        card: ModelCard,
        output: ModelOutput,
        status: str,
        fallback_from: str | None,
        latency_ms: int,
        release_held: int,
        credential: ProviderCredential | None = None,
    ) -> None:
        cost = 0.0 if status != USAGE_SUCCESS else cost_of(card, output)
        tier = "" if credential is None else credential.tier
        self.store.record_usage(
            Usage(
                project_id=principal.project_id,
                model_id=card.id,
                model_name=card.name,
                pricing_version=card.pricing_version,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
                cost=cost,
                status=status,
                fallback_from=fallback_from,
                org_id=principal.org_id,
                latency_ms=latency_ms,
                credential_id="" if credential is None else credential.id,
                tier=tier,
            ),
            release_held=release_held,
        )
        if self.metrics is not None and status == USAGE_SUCCESS and cost > 0:
            self.metrics.cost_micros.inc(
                int(round(cost * 1_000_000)),
                model=card.name,
                tier=tier or "none",
            )


def _empty() -> ModelOutput:
    return ModelOutput(input_tokens=0, output_tokens=0)
