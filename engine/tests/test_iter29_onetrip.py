"""iter29 — one-trip-per-turn + RAG on-refer cache.

Pins (master plan 04 §A):
  1. per-round tool-call dedupe (3 identical end_call -> 1 executed)
  2. ONE TRIP: speech + fire-and-forget bookkeeping finalizes in 1 LLM round
  3. silence guard: tools + empty text loops back into the state node
  4. R2 guard: speech + result-dependent tool (query_livecall_slots) loops
  5. RAG cache: same (state, query) twice in one call -> 1 retrieve call
  6. Closing.md drift: no `end_call` micro-management left in the file
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
# rag_min_query_chars=0: iter21's filler-turn RAG skip is OFF here — short
# utterances ("hi") are the retrieval probes in the cache tests.
SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, rag_min_query_chars=0,
                    first_turn_lite=False, rag_live_retrieve=False)
# iter49c T1: the cache tests pin the iter48 freeze/cache path (CountingStore
# has no retrieve_lanes) — live multi-lane mode is OFF here by design.
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())


def make_runtime(fake: FakeLLM, kb_store=...) -> CallRuntime:
    return CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake,
                       http_client=mock_client(), kb_store=kb_store)


async def _turn(rt: CallRuntime, call_id: str, user_text: str, first: bool = True) -> dict:
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": user_text}
    if first:
        payload.update(rt.initial_state(call_id))
    async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
        pass
    return (await rt.graph.aget_state(config)).values


def run_turn(rt: CallRuntime, call_id: str, user_text: str) -> dict:
    return asyncio.run(_turn(rt, call_id, user_text))


# --------------------------------------------------------------------------- #
def test_dedupe_three_identical_end_calls():
    """A1: 3 identical end_call calls in one round -> exactly 1 executed."""
    fake = FakeLLM([
        {"tokens": ["Bye", "!"], "tool_calls": [
            tool_call("end_call", {}, "e1"),
            tool_call("end_call", {}, "e2"),
            tool_call("end_call", {}, "e3"),
        ]},
    ])
    rt = make_runtime(fake)
    state = run_turn(rt, "ot-1", "that's all, bye")
    assert state["ended"] is True
    assert state["metrics"]["calls"] == ["end_call"]          # deduped to one
    assistant = next(m for m in state["history"] if m.get("tool_calls"))
    assert len(assistant["tool_calls"]) == 1                   # history keeps one call only


def test_one_trip_speech_plus_bookkeeping_finalizes():
    """A2: speech + extract_* in the SAME round -> turn done in ONE LLM trip."""
    fake = FakeLLM([
        {"tokens": ["Got", " it", "."], "tool_calls": [
            tool_call("extract_intake_details", {"inbound_channel": "ad"}),
        ]},
        # MUST NOT be reached — the turn already finalized
        {"tokens": ["extra", " round"], "match_system": "NEVERMatchThis"},
    ])
    rt = make_runtime(fake)
    state = run_turn(rt, "ot-2", "I saw your ad about missed calls")
    assert state["turn_active"] is False
    assert state["round_spoke"] is True
    assert len(fake.seen_systems) == 1                         # exactly 1 LLM call


def test_silence_guard_loops_back():
    """A2: tools with EMPTY reply text -> no one-trip; the state node loops."""
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [
            tool_call("extract_intake_details", {"inbound_channel": "ad"}),
        ]},
        {"tokens": ["So", " you", " saw", " the", " ad", "."]},
    ])
    rt = make_runtime(fake)
    state = run_turn(rt, "ot-3", "saw your ad")
    assert state["turn_active"] is False
    assert len(fake.seen_systems) == 2                         # looped: round 2 spoke
    assert state["round_spoke"] is True


def test_result_dependent_tool_loops_back():
    """R2: speech + query_livecall_slots is NOT fire-and-forget -> loops."""
    fake = FakeLLM([
        {"tokens": ["One", " moment", "."], "tool_calls": [
            tool_call("query_livecall_slots", {"timezone": "America/Chicago"}),
        ]},
        {"tokens": ["I", " can", " offer", " you", " 1 pm", "."]},
    ])
    rt = make_runtime(fake)
    initial = rt.initial_state("ot-4", persona_dvs={"prospect_timezone": "America/Chicago"})
    initial["state_name"] = "ConfirmSlots"

    async def go():
        config = {"configurable": {"thread_id": "ot-4"}}
        payload = {**initial, "user_text": "as soon as possible"}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert len(fake.seen_systems) == 2                         # looped into the next round
    assert state["dvs"]["requested_slot"]                      # slot data landed


def test_rag_cache_one_retrieve_for_same_query():
    """A3: two rounds, same (state, query) -> kb_store.retrieve called ONCE."""
    class CountingStore:
        def __init__(self):
            self.calls = 0

        async def retrieve(self, query, slugs):
            self.calls += 1
            return [{"kb": "pain-points", "content": "mirror the pain", "score": 0.9}]

    kb = CountingStore()
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [                  # silence guard -> loop
            tool_call("extract_intake_details", {"inbound_channel": "ad"})]},
        {"tokens": ["Got", " it", "."]},                # speech -> finalize
    ])
    rt = make_runtime(fake, kb_store=kb)
    run_turn(rt, "ot-5", "saw your ad")
    assert kb.calls == 1                                       # cached on the 2nd round


def test_rag_cache_cleared_per_call():
    """A3: the cache never leaks across calls."""
    class CountingStore:
        def __init__(self):
            self.calls = 0

        async def retrieve(self, query, slugs):
            self.calls += 1
            return []

    kb = CountingStore()
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = make_runtime(fake, kb_store=kb)
    run_turn(rt, "ot-6a", "hello")
    fake.rounds = [{"tokens": ["Hi", "."]}]
    run_turn(rt, "ot-6b", "hello")
    assert kb.calls == 2                                       # 1 per call, cache reset


def test_closing_prompt_has_no_end_call_references():
    """A5: Closing.md no longer double-owns the call ending."""
    text = (ROOT / "diallux" / "prompts" / "Closing.md").read_text(encoding="utf-8")
    assert "end_call" not in text
    assert "MANDATORY GOODBYE" in text                         # the goodbye contract stays
