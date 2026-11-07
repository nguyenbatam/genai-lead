from __future__ import annotations

import json

import httpx
import pytest

from control.records import ModelCard
from core.model import MAX_OUTPUT_TOKENS, HttpModel, ProviderDown, ProviderLimitHit


def _card() -> ModelCard:
    return ModelCard(
        id="mdl_http",
        project_id="prj_http",
        name="small",
        profile="cheap",
        capabilities=("cheap",),
        input_price=0,
        output_price=0,
        pricing_version="2026-11",
    )


def _sse(*chunks: dict) -> httpx.Response:
    parts = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    parts.append("data: [DONE]\n\n")
    return httpx.Response(
        200,
        content="".join(parts),
        headers={"Content-Type": "text/event-stream"},
    )


def _text_chunks(text: str, *, prompt_tokens: int = 1, completion_tokens: int = 1) -> tuple:
    return (
        {"choices": [{"delta": {"role": "assistant", "content": text}}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        },
    )


def test_http_model_speaks_the_runtime_protocol():
    seen: dict = {}
    content = json.dumps(
        {
            "text": "answer",
            "handoff": "writer",
            "parallel": ["analyst"],
            "claims": [{"text": "answer", "cite": "p1"}],
            "finish": True,
            "tool_calls": [{"name": "note", "args": {"title": "t"}}],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _sse(*_text_chunks(content, prompt_tokens=3, completion_tokens=4))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        output = HttpModel("http://llm.test/v1", "sk", client=client).complete(
            _card(), ["prompt", "question"], ("note",)
        )
    body = seen["body"]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1] == {"role": "system", "content": "prompt"}
    assert body["messages"][2] == {"role": "user", "content": "question"}
    assert body["tools"][0]["function"]["name"] == "note"
    assert body["max_tokens"] == MAX_OUTPUT_TOKENS
    assert output.text == "answer"
    assert output.handoff == "writer"
    assert output.parallel == ("analyst",)
    assert output.tool_calls[0].args == {"title": "t"}
    assert output.claims[0].cite == "p1"
    assert output.input_tokens == 3
    assert output.output_tokens == 4


def test_plain_provider_text_stays_text():
    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse(*_text_chunks("hello"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        output = HttpModel("http://llm.test/v1", "", client=client).complete(_card(), ["question"])
    assert output.text == "hello"
    assert output.finish is True
    assert output.tool_calls == ()


def test_http_model_retries_transient_provider_errors():
    hits = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if hits["n"] < 2:
            return httpx.Response(503, json={"error": "busy"})
        return _sse(*_text_chunks("ok"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = HttpModel("http://llm.test/v1", "", client=client, retries=3)
        output = model.complete(_card(), ["question"])
    assert hits["n"] == 2
    assert output.text == "ok"
    assert model.last_http_retries == 1
    assert model.last_wire_ms >= 0


def test_http_model_429_is_a_credential_limit():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "12"}, json={"error": "rate"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderLimitHit) as caught:
            HttpModel("http://llm.test/v1", "sk", client=client).complete(_card(), ["q"])
    assert caught.value.retry_after_s == 12.0


def test_http_model_uses_per_call_credential():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["portkey"] = request.headers.get("x-portkey-config")
        return _sse(*_text_chunks("ok"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        HttpModel("http://llm.test/v1", "sk-default", client=client).complete(
            _card(),
            ["q"],
            api_key="sk-pool",
            base_url="http://pool.test/v1",
            headers={"x-portkey-config": "pc-demo"},
        )
    assert seen["auth"] == "Bearer sk-pool"
    assert seen["url"].startswith("http://pool.test/v1/")
    assert seen["portkey"] == "pc-demo"


def test_http_model_aggregates_native_tool_call_deltas():
    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "note", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '{"title":'}}]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"t"}'}}]}}
                ]
            },
            {
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 5},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        output = HttpModel("http://llm.test/v1", "sk", client=client).complete(
            _card(), ["q"], ("note",)
        )
    assert output.tool_calls[0].name == "note"
    assert output.tool_calls[0].args == {"title": "t"}
    assert output.input_tokens == 2
    assert output.output_tokens == 5


def test_stream_progress_marks_data_before_midstream_failure():
    from core.model import _read_chat_stream

    class _Cut:
        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'
            raise httpx.ReadError("cut")

    progress = {"got_data": False}
    with pytest.raises(httpx.ReadError):
        _read_chat_stream(_Cut(), progress)
    assert progress["got_data"] is True


def test_http_model_does_not_retry_after_stream_data():
    hits = {"n": 0}

    class _OnceThenCut:
        def stream(self, method, url, **kwargs):
            del method, url, kwargs
            hits["n"] += 1
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @property
        def status_code(self) -> int:
            return 200

        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'
            raise httpx.ReadError("cut")

    client = _OnceThenCut()
    with pytest.raises(ProviderDown):
        HttpModel("http://llm.test/v1", "", client=client, retries=3).complete(_card(), ["q"])
    assert hits["n"] == 1
