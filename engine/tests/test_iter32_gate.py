"""iter32 — silent-round payload gate (tool channel if/else) + strict end_call.

Pins:
  1. silent + all-mechanical previous round -> NEXT round is FORCED SPEECH
     (iter43: the FULL tool array is kept — tools render FIRST in the cached
     prefix, tools=[] would bust it — and the tail carries a SPEECH ONLY
     directive; force_speech_keep_tools=False restores the old tools=[]).
  2. speech + fire-and-forget co-emit -> one-trip finalize (unchanged)
  3. silent + result-dependent tool (query_livecall_slots) -> tools stay (the
     booking chain keeps its channel)
  4. forced-speech round that still answers with no tools/text -> turn
      finalizes (escape hatch; no infinite loop)
  5. the STATE BLOCK tail carries the recency SPEAK FIRST directive
  6. end_call gate stays STRICT at 48d6ad3 semantics (no dampener)
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
SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, first_turn_lite=False)  # hermetic: .env may set it true
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())


def make_runtime(fake: FakeLLM) -> CallRuntime:
    return CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake, http_client=mock_client())


async def _turns(rt: CallRuntime, call_id: str, texts: list[str]) -> dict:
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": texts[0], **rt.initial_state(call_id)}
    for i, text in enumerate(texts):
        if i > 0:
            payload = {"user_text": text}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
    return (await rt.graph.aget_state(config)).values


# --------------------------------------------------------------------------- #
def test_silent_mechanical_round_closes_tool_channel():
    """Round 1: SILENT extract_intake_details (mechanical). Round 2 is forced
    speech: iter43 keeps the FULL tool array (prefix stability) + SPEECH ONLY
    tail directive; its speech finalizes the turn."""
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [tool_call("extract_intake_details", {"caller_name": "Kim"})]},
        {"tokens": ["Got", " it", ",", " Kim", "."]},
    ])
    rt = make_runtime(fake)
    state = asyncio.run(_turns(rt, "g-1", ["this is Kim"]))
    # round 1 saw the full toolset; round 2 (forced speech) KEEPS the full set
    assert fake.seen_tools[0]                      # full set
    assert fake.seen_tools[1] == fake.seen_tools[0]   # iter43: tools kept, prefix stable
    assert any("SPEECH ONLY" in m for m in fake.seen_system_msgs[1])
    assert len(fake.seen_tools) == 2                # speech round finalizes: no round 3
    assert state.get("ended") is not True


def test_silent_mechanical_round_kill_switch_tools_empty():
    """Kill switch: force_speech_keep_tools=False restores the exact iter32
    behavior — the forced round gets tools=[] and NO SPEECH ONLY directive."""
    settings = Settings(openai_api_key="test", retell_api_key="test",
                        langfuse_enabled=False, force_speech_keep_tools=False)
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [tool_call("extract_intake_details", {"caller_name": "Kim"})]},
        {"tokens": ["Got", " it", ",", " Kim", "."]},
    ])
    rt = CallRuntime(settings, LLM_JSON, tracer=None, llm=fake, http_client=mock_client())
    asyncio.run(_turns(rt, "g-1k", ["this is Kim"]))
    assert fake.seen_tools[1] == []                 # old channel-closed behavior
    assert not any("SPEECH ONLY" in m for m in fake.seen_system_msgs[1])


def test_speech_round_keeps_tools():
    """Speech + fire-and-forget co-emit stays a ONE-TRIP turn (unchanged)."""
    fake = FakeLLM([
        {"tokens": ["Thanks", ",", " Kim", "."],
         "tool_calls": [tool_call("extract_intake_details", {"caller_name": "Kim"})]},
    ])
    rt = make_runtime(fake)
    state = asyncio.run(_turns(rt, "g-2", ["this is Kim"]))
    assert len(fake.seen_tools) == 1                # one LLM call total
    assert fake.seen_tools[0]                       # tools were available
    assert state.get("ended") is not True


def test_result_dep_silent_round_keeps_tools():
    """Silent query_livecall_slots (result-dependent, ConfirmSlots) -> the
    next round keeps the FULL toolset: the chain channel stays open."""
    seed = {"livecall_agreed": True, "slot_verified": False, "booking_verified": False}
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [tool_call("query_livecall_slots",
                                                {"slot_target_date": "2026-09-10"})],
         "match_system": "Find a live-call demo slot"},
        {"tokens": ["How", " does", " Thursday", " sound", "?"],
         "match_system": "Find a live-call demo slot"},
    ])
    rt = make_runtime(fake)
    init = rt.initial_state("g-3", persona_dvs=seed)
    init["state_name"] = "ConfirmSlots"            # start the call mid-chain
    payload = {"user_text": "what times work?", **init}

    async def go():
        config = {"configurable": {"thread_id": "g-3"}}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert fake.seen_tools[1]                       # still the full set
    assert state.get("ended") is not True


def test_forced_speech_empty_finalizes():
    """Forced-speech round answered with tokens:[] and no tools -> the turn
    finalizes (no loop, no second forced round). iter43: the forced round
    keeps the FULL tool array (prefix stability)."""
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [tool_call("extract_intake_details", {"caller_name": "Al"})]},
        {"tokens": []},                              # forced round: still says nothing
    ])
    rt = make_runtime(fake)
    state = asyncio.run(_turns(rt, "g-4", ["hey there"]))
    assert len(fake.seen_tools) == 2                # exactly two rounds, then done
    assert fake.seen_tools[1] == fake.seen_tools[0]  # iter43: full set kept
    assert state.get("ended") is not True


def test_state_block_has_directive():
    """The dynamic tail carries the recency SPEAK FIRST directive."""
    fake = FakeLLM([{"tokens": []}])
    rt = make_runtime(fake)
    block = rt._state_block({"caller_name": "Kim"})
    assert "SPEAK FIRST: every response starts with your spoken sentence" in block
    assert "# CURRENT CALL STATE (live values)" in block
    assert "caller_name: Kim" in block


# --------------------------------------------------------------------------- #
def test_end_call_strict_no_dampener():
    """Executor level, 48d6ad3 semantics verbatim (no dampener): silent
    end_call with NO goodbye -> blocked; WITH goodbye in last_spoken ->
    accepted; text+end_call -> accepted; iter8 livecall-agreed without
    verified booking -> blocked in ALL cases."""
    from diallux.graph.tools import ToolExecutor
    ex = ToolExecutor(SETTINGS, LLM_JSON, http_client=mock_client())

    # 1. silent end_call, no goodbye anywhere -> blocked
    out = ex_result = asyncio.run(ex.execute(
        "end_call", {}, {}, "Intake", spoken_text="", last_spoken="So what brought this on?"))
    assert ex_result.response["status"] == "end_call_blocked"
    assert out.ended is not True

    # 2. silent end_call after a spoken goodbye -> accepted
    out = asyncio.run(ex.execute(
        "end_call", {}, {}, "Closing",
        spoken_text="", last_spoken="Thanks for calling, have a great day, bye!"))
    assert out.ended is True
    assert out.response["status"] == "call_ended"

    # 3. end_call WITH text -> always accepted
    out = asyncio.run(ex.execute(
        "end_call", {}, {}, "Closing",
        spoken_text="Great, take care — goodbye!", last_spoken=""))
    assert out.ended is True

    # 4. iter8 absolute: livecall agreed, no verified slot/booking -> blocked
    dvs = {"livecall_agreed": True, "slot_verified": False, "booking_verified": False}
    out = asyncio.run(ex.execute(
        "end_call", {}, dvs, "ConfirmSlots",
        spoken_text="You're all set — bye!", last_spoken="You're all set — bye!"))
    assert out.response["status"] == "end_call_blocked"
    assert out.ended is not True

    # 5. ...even with a goodbye already spoken (no bail-out hangup)
    out = asyncio.run(ex.execute(
        "end_call", {}, dvs, "ConfirmSlots",
        spoken_text="", last_spoken="Thanks anyway, have a good one, bye!"))
    assert out.response["status"] == "end_call_blocked"
    assert out.ended is not True
