"""iter65 T1 — OpenAI boot-prewarm + shared httpx pool pins.

Pins:
  1. llm_boot_prewarm sends ONE warm request through the SHARED client:
     model from settings, max_completion_tokens=16, message "HI".
  2. StreamingLLM twins (hot + warm + verbosity twin) carry
     http_async_client=shared_llm_http_client() when the knob is on.
  3. Knob off = per-instance clients (iter64 exact, no kwarg).
  4. Settings default llm_boot_prewarm=True.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph import llm as llm_mod
from diallux.graph.llm import StreamingLLM, llm_boot_prewarm, shared_llm_http_client


def _settings(**kw) -> Settings:
    base = dict(openai_api_key="test", langfuse_enabled=False)
    base.update(kw)
    return Settings(**base)


class _StubChat:
    """Captures constructor kwargs; ainvoke returns a trivial message."""
    last_kwargs: dict = {}
    calls: list[list] = []

    def __init__(self, **kwargs):
        _StubChat.last_kwargs = kwargs

    async def ainvoke(self, messages):
        _StubChat.calls.append(messages)
        return {"content": "ok"}


def test_knob_default_true():
    assert Settings(openai_api_key="test").llm_boot_prewarm is True


def test_boot_prewarm_request_shape(monkeypatch):
    """Warm request: model from settings, max_completion_tokens=16, "HI",
    through the SHARED client."""
    _StubChat.last_kwargs = {}
    _StubChat.calls = []
    monkeypatch.setattr(llm_mod, "ChatOpenAI", _StubChat)
    s = Settings(openai_api_key="test", openai_model="gpt-5.4")
    ms = asyncio.run(llm_boot_prewarm(s))
    assert isinstance(ms, float) and ms >= 0
    kw = _StubChat.last_kwargs
    assert kw["model"] == "gpt-5.4"
    assert kw["max_completion_tokens"] == 16
    assert kw["stream_usage"] is False
    assert kw["http_async_client"] is shared_llm_http_client()
    assert _StubChat.calls == [[{"role": "user", "content": "HI"}]]


def test_boot_prewarm_reasoning_kwargs(monkeypatch):
    """Reasoning-family model -> reasoning_effort/verbosity ride model_kwargs."""
    _StubChat.last_kwargs = {}
    monkeypatch.setattr(llm_mod, "ChatOpenAI", _StubChat)
    s = Settings(openai_api_key="test", openai_model="gpt-5.4",
                 openai_reasoning_effort="low", openai_verbosity="medium")
    asyncio.run(llm_boot_prewarm(s))
    assert _StubChat.last_kwargs["model_kwargs"] == {
        "reasoning_effort": "low", "verbosity": "medium"}


def test_streaming_llm_twins_share_client():
    """Knob on: hot LLM, warm twin and verbosity twin share the pool."""
    s = Settings(openai_api_key="test", openai_model="gpt-5.4")
    session = StreamingLLM(s)
    shared = shared_llm_http_client()
    assert getattr(session._llm, "http_async_client", None) is shared
    assert getattr(session._warm_llm, "http_async_client", None) is shared
    twin = session.llm_for_verbosity("high")
    assert getattr(twin, "http_async_client", None) is shared


def test_streaming_llm_knob_off_per_instance():
    """Knob off = iter64 exact: NO http_async_client kwarg at all."""
    s = Settings(openai_api_key="test", openai_model="gpt-5.4",
                 llm_boot_prewarm=False)
    session = StreamingLLM(s)
    assert getattr(session._llm, "http_async_client", None) is None
    assert getattr(session._warm_llm, "http_async_client", None) is None
    twin = session.llm_for_verbosity("high")
    assert getattr(twin, "http_async_client", None) is None
