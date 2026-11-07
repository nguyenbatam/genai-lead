"""Model port. The scripted provider is a queue, not a keyword router."""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import ClassVar

from control.records import ModelCard

PROVIDER_TIMEOUT_S = 30
PROVIDER_CONNECT_TIMEOUT_S = 10
MAX_OUTPUT_TOKENS = 1024
PROVIDER_RETRIES = 3
_PROTOCOL = (
    "Reply with one JSON object and no other text. "
    "Keys: text, tool_calls as a list of {name, args}, handoff, parallel, "
    "claims as a list of {text, cite}, finish, model_hint. "
    "model_hint is optional and must be an allowed model id or name when provided. "
    "Use only tool names listed in this request."
)


class ProviderDown(Exception):
    """Transport failure, 429, or 5xx. Fallback is allowed when the route names one."""

    def __init__(
        self,
        model_id: str,
        *,
        ledgered: bool = False,
        trip_breaker: bool = True,
        wire_ms: int = 0,
    ) -> None:
        super().__init__(model_id)
        self.model_id = model_id
        # True when gateway already wrote usage for the credential hops that failed.
        self.ledgered = ledgered
        # False when every hop was a credential limit — rotate keys, do not open breaker.
        self.trip_breaker = trip_breaker
        self.wire_ms = wire_ms


class ProviderRejected(Exception):
    """The provider answered 4xx, or the tool payload was not JSON. Do not fall over."""

    def __init__(self, model_id: str, *, wire_ms: int = 0) -> None:
        super().__init__(model_id)
        self.model_id = model_id
        self.wire_ms = wire_ms


class ProviderLimitHit(Exception):
    """This credential hit a rate or quota limit. Rotate key; do not open the model breaker."""

    def __init__(
        self, model_id: str, retry_after_s: float | None = None, *, wire_ms: int = 0
    ) -> None:
        super().__init__(model_id)
        self.model_id = model_id
        self.retry_after_s = retry_after_s
        self.wire_ms = wire_ms


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict


@dataclass(frozen=True)
class Claim:
    text: str
    cite: str


@dataclass(frozen=True)
class ModelOutput:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    handoff: str | None = None
    parallel: tuple[str, ...] = ()
    finish: bool = False
    claims: tuple[Claim, ...] = ()
    model_hint: str | None = None
    input_tokens: int = 1
    output_tokens: int = 1


@dataclass
class ScriptedModel:
    """Test and demo provider. Outputs are pushed by the caller, never inferred from wording."""

    down: set[str] = field(default_factory=set)
    queue: list[ModelOutput] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    concurrent: ClassVar[bool] = False

    def complete(
        self,
        model: ModelCard,
        messages: list[str],
        tools: tuple[str, ...] = (),
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        credential_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ModelOutput:
        del tools, api_key, base_url, headers
        self.seen.extend(messages)
        if credential_id:
            self.seen.append(f"cred:{credential_id}")
        if credential_id and credential_id in self.down:
            raise ProviderLimitHit(model.id, retry_after_s=30.0)
        if model.id in self.down or model.name in self.down:
            raise ProviderDown(model.id)
        if not self.queue:
            return ModelOutput(text="[NO_ANSWER]", finish=True)
        return self.queue.pop(0)


def tool_calls_from(model_id: str, message: dict) -> tuple[ToolCall, ...]:
    calls: list[ToolCall] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_args = function.get("arguments") or {}
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError as exc:
                raise ProviderRejected(model_id) from exc
        if not isinstance(raw_args, dict):
            raise ProviderRejected(model_id)
        calls.append(ToolCall(str(function.get("name") or ""), dict(raw_args)))
    return tuple(calls)


def dump_output(output: ModelOutput) -> dict:
    return {
        "text": output.text,
        "tool_calls": [{"name": call.name, "args": call.args} for call in output.tool_calls],
        "handoff": output.handoff,
        "parallel": list(output.parallel),
        "finish": output.finish,
        "claims": [{"text": claim.text, "cite": claim.cite} for claim in output.claims],
        "model_hint": output.model_hint,
        "input_tokens": output.input_tokens,
        "output_tokens": output.output_tokens,
    }


def load_output(raw: dict) -> ModelOutput:
    hint = raw.get("model_hint")
    return ModelOutput(
        text=str(raw.get("text") or ""),
        tool_calls=tuple(
            ToolCall(str(item["name"]), dict(item.get("args") or {}))
            for item in raw.get("tool_calls") or []
        ),
        handoff=raw.get("handoff"),
        parallel=tuple(raw.get("parallel") or ()),
        finish=bool(raw.get("finish")),
        claims=tuple(
            Claim(str(item["text"]), str(item["cite"])) for item in raw.get("claims") or []
        ),
        model_hint=str(hint) if isinstance(hint, str) and hint else None,
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
    )


class HttpModel:
    """OpenAI-compatible adapter. Used only when LLM_BASE_URL is set.

    Always streams (`stream=true`). Idle read timeout resets per SSE chunk so a
    long completion does not die as long as the provider keeps sending tokens.
    Retries only when no stream data arrived yet.
    """

    concurrent = True

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_s: float = PROVIDER_TIMEOUT_S,
        client: object | None = None,
        retries: int = PROVIDER_RETRIES,
    ) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.retries = max(1, retries)
        self._owns_client = client is None
        # Global pool is a last-resort ceiling; per-project provider slots bound fairness.
        wire_timeout = httpx.Timeout(
            connect=min(PROVIDER_CONNECT_TIMEOUT_S, timeout_s),
            read=timeout_s,
            write=timeout_s,
            pool=timeout_s,
        )
        self.client = client or httpx.Client(
            timeout=wire_timeout,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def complete(
        self,
        model: ModelCard,
        messages: list[str],
        tools: tuple[str, ...] = (),
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        credential_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ModelOutput:
        import httpx

        del credential_id
        key = self.api_key if api_key is None else api_key
        root = self.base_url if not base_url else base_url.rstrip("/")
        wire_headers = {"Authorization": f"Bearer {key}"} if key else {}
        if headers:
            wire_headers.update(headers)
        payload: dict = {
            "model": model.name,
            "temperature": 0,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": _wire_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = [_tool_schema(name) for name in tools]
        last_error: Exception | None = None
        wire_ms = 0
        http_retries = 0
        self.last_wire_ms = 0
        self.last_http_retries = 0
        for attempt in range(self.retries):
            started = time.perf_counter()
            progress = {"got_data": False}
            try:
                with self.client.stream(
                    "POST",
                    f"{root}/chat/completions",
                    json=payload,
                    headers=wire_headers,
                    timeout=httpx.Timeout(
                        connect=min(PROVIDER_CONNECT_TIMEOUT_S, self.timeout_s),
                        read=self.timeout_s,
                        write=self.timeout_s,
                        pool=self.timeout_s,
                    ),
                ) as response:
                    if response.status_code == 429:
                        wire_ms += int((time.perf_counter() - started) * 1000)
                        self.last_wire_ms = wire_ms
                        self.last_http_retries = http_retries
                        raise ProviderLimitHit(
                            model.id,
                            _retry_after_seconds(response.headers.get("retry-after")),
                        )
                    if response.status_code >= 500:
                        wire_ms += int((time.perf_counter() - started) * 1000)
                        if attempt + 1 >= self.retries:
                            self.last_wire_ms = wire_ms
                            self.last_http_retries = http_retries
                            raise ProviderDown(model.id)
                        http_retries += 1
                        _backoff(attempt, response.headers.get("retry-after"))
                        continue
                    if response.status_code >= 400:
                        wire_ms += int((time.perf_counter() - started) * 1000)
                        self.last_wire_ms = wire_ms
                        self.last_http_retries = http_retries
                        raise ProviderRejected(model.id)
                    message, usage = _read_chat_stream(response, progress)
            except ProviderLimitHit:
                raise
            except ProviderRejected:
                raise
            except ProviderDown:
                raise
            except httpx.HTTPError as exc:
                wire_ms += int((time.perf_counter() - started) * 1000)
                last_error = exc
                if progress["got_data"] or attempt + 1 >= self.retries:
                    self.last_wire_ms = wire_ms
                    self.last_http_retries = http_retries
                    raise ProviderDown(model.id) from exc
                http_retries += 1
                _backoff(attempt)
                continue
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
                wire_ms += int((time.perf_counter() - started) * 1000)
                self.last_wire_ms = wire_ms
                self.last_http_retries = http_retries
                raise ProviderDown(model.id) from exc
            wire_ms += int((time.perf_counter() - started) * 1000)
            native = tool_calls_from(model.id, message)
            output = output_from_content(model.id, str(message.get("content") or ""), native)
            self.last_wire_ms = wire_ms
            self.last_http_retries = http_retries
            return ModelOutput(
                text=output.text,
                tool_calls=output.tool_calls,
                handoff=output.handoff,
                parallel=output.parallel,
                finish=output.finish,
                claims=output.claims,
                input_tokens=int(usage.get("prompt_tokens") or 1),
                output_tokens=int(usage.get("completion_tokens") or 1),
            )
        self.last_wire_ms = wire_ms
        self.last_http_retries = http_retries
        raise ProviderDown(model.id) from last_error


def _read_chat_stream(response: object, progress: dict) -> tuple[dict, dict]:
    """Consume OpenAI-compatible SSE into one message dict plus usage.

    Sets progress['got_data'] once any `data:` payload other than [DONE] arrives
    so mid-stream failure must not retry. Non-SSE JSON bodies (tests, proxies)
    are accepted as a single chat.completion object.
    """
    iterator = response.iter_lines()
    content_parts: list[str] = []
    tool_slots: dict[int, dict] = {}
    usage: dict = {}
    json_lines: list[str] = []
    mode = ""
    for line in iterator:
        if line is None or not line:
            continue
        text = str(line)
        if not mode:
            if text.startswith("data:") or text.startswith(":"):
                mode = "sse"
            else:
                mode = "json"
                json_lines.append(text)
                json_lines.extend(str(item) for item in iterator if item)
                break
        if mode == "sse":
            if text.startswith(":"):
                continue
            if not text.startswith("data:"):
                continue
            payload = text[5:].strip()
            if payload == "[DONE]":
                break
            progress["got_data"] = True
            chunk = json.loads(payload)
            if not isinstance(chunk, dict):
                raise ValueError("stream chunk is not an object")
            chunk_usage = chunk.get("usage")
            if isinstance(chunk_usage, dict) and chunk_usage:
                usage = chunk_usage
            for choice in chunk.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    continue
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    content_parts.append(piece)
                for call in delta.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    _merge_tool_delta(tool_slots, call)
    if mode == "json":
        body = json.loads("\n".join(json_lines))
        if not isinstance(body, dict):
            raise ValueError("completion body is not an object")
        progress["got_data"] = True
        choice = (body.get("choices") or [{}])[0]
        if not isinstance(choice, dict):
            raise ValueError("completion choice is not an object")
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            raise ValueError("completion message is not an object")
        usage_obj = body.get("usage") or {}
        if not isinstance(usage_obj, dict):
            usage_obj = {}
        return message, usage_obj
    message = {"content": "".join(content_parts)}
    if tool_slots:
        message["tool_calls"] = [tool_slots[index] for index in sorted(tool_slots)]
    return message, usage


def _merge_tool_delta(slots: dict[int, dict], call: dict) -> None:
    index = int(call.get("index") or 0)
    slot = slots.get(index)
    if slot is None:
        slot = {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        }
        slots[index] = slot
    call_id = call.get("id")
    if isinstance(call_id, str) and call_id:
        slot["id"] = call_id
    call_type = call.get("type")
    if isinstance(call_type, str) and call_type:
        slot["type"] = call_type
    function = call.get("function") or {}
    if not isinstance(function, dict):
        return
    name = function.get("name")
    if isinstance(name, str) and name:
        slot["function"]["name"] = name
    arguments = function.get("arguments")
    if isinstance(arguments, str) and arguments:
        slot["function"]["arguments"] += arguments


def _retry_after_seconds(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _backoff(attempt: int, retry_after: str | None = None) -> None:
    if retry_after:
        try:
            time.sleep(min(30.0, float(retry_after)))
            return
        except ValueError:
            pass
    delay = min(8.0, (2**attempt) * 0.05)
    time.sleep(delay * random.random())


def _wire_messages(messages: list[str]) -> list[dict[str, str]]:
    wired = [{"role": "system", "content": _PROTOCOL}]
    if not messages:
        return wired
    wired.append({"role": "system", "content": messages[0]})
    wired.extend({"role": "user", "content": message} for message in messages[1:])
    return wired


def _tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object", "additionalProperties": True},
        },
    }


def output_from_content(model_id: str, content: str, native: tuple[ToolCall, ...]) -> ModelOutput:
    parsed = _protocol_json(content)
    if parsed is None:
        return ModelOutput(text=content, tool_calls=native, finish=not native)
    calls = _calls_from_protocol(model_id, parsed.get("tool_calls"))
    if native and not calls:
        calls = native
    handoff = parsed.get("handoff")
    parallel = parsed.get("parallel") or []
    claims_raw = parsed.get("claims") or []
    if not isinstance(parallel, list) or not isinstance(claims_raw, list):
        raise ProviderRejected(model_id)
    claims: list[Claim] = []
    for item in claims_raw:
        if not isinstance(item, dict):
            raise ProviderRejected(model_id)
        claims.append(Claim(str(item.get("text") or ""), str(item.get("cite") or "")))
    finish = parsed.get("finish")
    hint = parsed.get("model_hint")
    return ModelOutput(
        text=str(parsed.get("text") or ""),
        tool_calls=calls,
        handoff=str(handoff) if isinstance(handoff, str) and handoff else None,
        parallel=tuple(str(item) for item in parallel),
        finish=bool(finish) if finish is not None else not calls,
        claims=tuple(claims),
        model_hint=str(hint) if isinstance(hint, str) and hint else None,
    )


def _protocol_json(content: str) -> dict | None:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    keys = {"text", "tool_calls", "handoff", "parallel", "claims", "finish", "model_hint"}
    if not keys & set(parsed):
        return None
    return parsed


def _calls_from_protocol(model_id: str, raw: object) -> tuple[ToolCall, ...]:
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise ProviderRejected(model_id)
    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("args", {}), dict):
            raise ProviderRejected(model_id)
        calls.append(ToolCall(str(item.get("name") or ""), dict(item.get("args") or {})))
    return tuple(calls)
