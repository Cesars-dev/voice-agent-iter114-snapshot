"""iter43 — async prompt-cache prewarm.

Pins:
  a. warm_prompt_cache("Intake", history) sends a request whose system head is
     the FULL Intake static head (KB markers stripped when a store is
     attached) and whose tools are the FULL Intake tool array
  b. LATCH: a second warm_prompt_cache for the same state fires NO new request
  c. an LLM that raises on warm() never propagates (best-effort, swallowed)
  d. aclose() cancels pending warm tasks without raising
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux import rag as ragmod
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())


class WarmSpyLLM(FakeLLM):
    """Records warm() calls separately from the streaming hot path."""

    def __init__(self, rounds=None, fail=False):
        super().__init__(rounds)
        self.warm_calls: list[tuple[list[dict], list[dict]]] = []
        self._fail = fail

    async def warm(self, messages, tools):
        if self._fail:
            raise RuntimeError("warm boom")
        self.warm_calls.append((list(messages), [t["function"]["name"] for t in tools]))


def _settings() -> Settings:
    # fallback_inline_kb=True: the first two tests pin the legacy expand path
    # (kb_store=None → whole-KB head) — the iter44 guard default (False) is
    # covered in test_iter44_prewarm_bytes.py / test_iter44_cache_floor.py.
    return Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, fallback_inline_kb=True)


def test_warm_sends_full_head_and_tools():
    """The warm request replicates the state_node payload shape: full Intake
    head (strip_kb_markers applies with a store attached) + full tool array."""
    fake = WarmSpyLLM()
    rt = CallRuntime(_settings(), LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)   # no store: expand path

    async def go():
        rt.warm_prompt_cache("Intake", [])
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)

    asyncio.run(go())
    assert len(fake.warm_calls) == 1
    messages, tool_names = fake.warm_calls[0]
    # head = general_prompt + Intake state prompt + VOICE_OUTPUT_RULES
    intake_head = rt.static_head("Intake", expand_kb=True)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == intake_head
    assert tool_names, "full Intake tool array must be present"
    assert "extract_intake_details" in tool_names


def test_warm_strips_kb_markers_when_store_present():
    """With a kb_store, the warm head is the STRIPPED head — byte-identical to
    the state_node path."""
    fake = WarmSpyLLM()

    class FakeStore:
        async def retrieve(self, query, scope):
            return []

    rt = CallRuntime(_settings(), LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=FakeStore())

    async def go():
        rt.warm_prompt_cache("Intake", [])
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)

    asyncio.run(go())
    messages, _tools = fake.warm_calls[0]
    stripped = ragmod.strip_kb_markers(rt.static_head("Intake", expand_kb=False))
    assert messages[0]["content"] == stripped
    assert "##pain-points-kb##" not in messages[0]["content"]   # KB marker stripped


def test_warm_latched_once_per_state():
    """Second warm_prompt_cache for the same state = no new request."""
    fake = WarmSpyLLM()
    rt = CallRuntime(_settings(), LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)

    async def go():
        rt.warm_prompt_cache("Intake", [])
        rt.warm_prompt_cache("Intake", [])
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)

    asyncio.run(go())
    assert len(fake.warm_calls) == 1


def test_warm_exceptions_swallowed():
    """An LLM that raises on warm() never propagates (fire-and-forget)."""
    fake = WarmSpyLLM(fail=True)
    rt = CallRuntime(_settings(), LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)

    async def go():
        rt.warm_prompt_cache("Intake", [])
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)  # no raise

    asyncio.run(go())


def test_aclose_cancels_warm_tasks():
    """aclose() cancels pending warm tasks and never raises."""
    fake = WarmSpyLLM()

    async def slow_warm(messages, tools):
        await asyncio.sleep(30)

    fake.warm = slow_warm
    rt = CallRuntime(_settings(), LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)

    async def go():
        rt.warm_prompt_cache("Intake", [])
        assert rt._warm_tasks
        await rt.aclose()          # must cancel + await-suppress, not hang

    asyncio.run(go())
    assert not rt._warm_tasks
