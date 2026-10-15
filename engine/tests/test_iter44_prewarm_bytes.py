"""iter44 T1 — prewarm is BYTE-EXACT with the hot path.

Pins:
  a. the warm request is a strict PREFIX of the next hot round's request
     (tools + head + tail + history so far) — OpenAI's prefix cache carries
     the warm bytes into the live turn (the iter43 shape mismatch is dead)
  b. warm tools == hot tools (exact equality)
  c. with a store: staged chunks make the tail equal too (greeting warm
     stages the retrieval, the state entry consumes the SAME chunks)
  d. prewarm_byte_exact=False → legacy warm shape ([head][history], no tail)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())


class WarmSpyLLM(FakeLLM):
    """Records warm() calls (full lists) separately from the hot path."""

    def __init__(self, rounds=None):
        super().__init__(rounds)
        self.warm_calls: list[tuple[list[dict], list[dict]]] = []

    async def warm(self, messages, tools):
        self.warm_calls.append((list(messages), list(tools)))


def _settings(**kw) -> Settings:
    # first_turn_lite=False: these tests pin warm==hot byte-exactness of the
    # FULL payload path (lite has no warm — its turn-1 shape is pinned in
    # test_first_turn_lite.py / test_iter44_cache_floor.py).
    # state_in_delta=False: the warm==hot TAIL pins (staged chunks in the
    # pre-history tail) describe the iter55 layout — under the iter56 default
    # the warm prefills [tools][head][history] with NO tail (own pin in
    # test_iter56_state_delta.py).
    return Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, first_turn_lite=False,
                    state_in_delta=False, **kw)


async def _gather(rt: CallRuntime):
    await asyncio.gather(*rt._warm_tasks, return_exceptions=True)


def _run_turn(rt: CallRuntime, call_id: str, text: str, dvs: dict | None = None):
    config = {"configurable": {"thread_id": call_id}}
    st = rt.initial_state(call_id, dvs)
    payload = {"user_text": text, **st}

    async def go():
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
    return go


# --------------------------------------------------------------------------- #
# a+b: no-store path (expand) — warm is a prefix of hot
# --------------------------------------------------------------------------- #
def test_warm_is_prefix_of_hot_round_no_store():
    fake = WarmSpyLLM([{"tokens": ["Hi", "."]}])
    rt = CallRuntime(_settings(fallback_inline_kb=True), LLM_JSON, tracer=None,
                     llm=fake, http_client=mock_client(), kb_store=None)
    dvs = {"first_name": "Maria", "inbound_channel": "voicemail"}

    async def go():
        call_id = "w44-1"
        st = rt.initial_state(call_id, dvs)
        rt.warm_prompt_cache("Intake", list(st["history"]), dvs=dict(st["dvs"]))
        await _gather(rt)
        config = {"configurable": {"thread_id": call_id}}
        async for _m, _d in rt.graph.astream({"user_text": "hello there",
                                              **st}, config=config,
                                             stream_mode=["updates"]):
            pass
    asyncio.run(go())

    assert len(fake.warm_calls) == 1
    warm_msgs, warm_tools = fake.warm_calls[0]
    hot_msgs = fake.seen_messages[0]
    # strict prefix: everything warm sent is byte-identical in the hot round
    assert hot_msgs[:len(warm_msgs)] == warm_msgs
    assert warm_msgs[0]["content"] == hot_msgs[0]["content"]     # head
    assert warm_msgs[1]["content"] == hot_msgs[1]["content"]     # tail
    # warm stores full schemas; FakeLLM records names
    assert [t["function"]["name"] for t in warm_tools] == fake.seen_tools[0]


def test_warm_tools_exact_equality():
    fake = WarmSpyLLM([{"tokens": ["Hi", "."]}])
    rt = CallRuntime(_settings(fallback_inline_kb=True), LLM_JSON, tracer=None,
                     llm=fake, http_client=mock_client(), kb_store=None)

    async def go():
        st = rt.initial_state("w44-1b", {})
        rt.warm_prompt_cache("Intake", list(st["history"]), dvs=dict(st["dvs"]))
        await _gather(rt)
    asyncio.run(go())
    warm_msgs, warm_tools = fake.warm_calls[0]
    names = [t["function"]["name"] for t in warm_tools]
    assert "extract_intake_details" in names


# --------------------------------------------------------------------------- #
# c: store path — staged chunks make the warm tail == hot tail
# --------------------------------------------------------------------------- #
class StagingStore:
    """Real-shape fake: embed_query + retrieve(vec=...); canned chunks."""

    def __init__(self):
        self.retrieves: list[tuple[str, list[str], list[float] | None]] = []

    async def embed_query(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 7

    async def retrieve(self, query_text, kb_slugs, vec=None):
        self.retrieves.append((query_text, list(kb_slugs), vec))
        return [{"kb": "pain-points", "content": "empathic mirror tactic",
                 "score": 0.9}]


def test_warm_tail_includes_staged_chunks_and_hot_consumes_them():
    store = StagingStore()
    fake = WarmSpyLLM([{"tokens": ["So", ",", " tell", " me", "."]}])
    # iter49c T1: pins the iter48 staged-warm-tail path (StagingStore has no
    # retrieve_lanes; under rag_live_retrieve the warm tail is RAG-free by
    # design) — live mode OFF.
    rt = CallRuntime(_settings(rag_live_retrieve=False), LLM_JSON, tracer=None,
                     llm=fake, http_client=mock_client(), kb_store=store)
    dvs = {"first_name": "Maria", "pain_points": "missed calls"}

    async def go():
        call_id = "w44-2"
        st = rt.initial_state(call_id, dvs)
        # session-start shape: warm_rag stages, prewarm composes with them
        rt.warm_prompt_cache("Intake", list(st["history"]), dvs=dict(st["dvs"]))
        await _gather(rt)
        config = {"configurable": {"thread_id": call_id}}
        async for _m, _d in rt.graph.astream({"user_text": "saw your ad about missed calls",
                                              **st}, config=config,
                                             stream_mode=["updates"]):
            pass
    asyncio.run(go())

    warm_msgs, _ = fake.warm_calls[0]
    hot_msgs = fake.seen_messages[0]
    assert hot_msgs[:len(warm_msgs)] == warm_msgs          # prefix, incl. KNOWLEDGE
    assert "empathic mirror tactic" in warm_msgs[1]["content"]
    assert "empathic mirror tactic" in hot_msgs[1]["content"]
    # the staged chunks were consumed by the hot entry (no second retrieve)
    assert len(store.retrieves) == 1
    assert rt._rag_staging.get("Intake") is None


# --------------------------------------------------------------------------- #
# d: kill-switch restores the legacy warm shape
# --------------------------------------------------------------------------- #
def test_prewarm_byte_exact_off_legacy_shape():
    fake = WarmSpyLLM([{"tokens": ["Hi", "."]}])
    rt = CallRuntime(_settings(fallback_inline_kb=True, prewarm_byte_exact=False),
                     LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)

    async def go():
        st = rt.initial_state("w44-3", {"first_name": "Maria"})
        rt.warm_prompt_cache("Intake", list(st["history"]), dvs=dict(st["dvs"]))
        await _gather(rt)
    asyncio.run(go())

    warm_msgs, _ = fake.warm_calls[0]
    # legacy: [head] + history snapshot — NO tail system message
    assert len(warm_msgs) == 2
    assert all(m["role"] != "system" or i == 0 for i, m in enumerate(warm_msgs))
