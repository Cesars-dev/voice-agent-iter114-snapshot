"""Graph tests: the Retell runtime replica semantics on LangGraph.

Covers (engine.py parity):
  1. one full turn with tool calls: extract writes dvs, tool msgs land in history
  2. mid-turn transition: next LLM round runs in the NEW state (prompt + toolset swap)
  3. turn ends on a pure-speech round (no tool calls -> finalize)
  4. state persists across invocations (MemorySaver, thread_id = call_id)
  5. end_call ends the turn
  6. repeat-call guard nudges instead of re-executing
  7. the full happy-path walk: Intake -> ... -> Closing -> end_call
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.observability.tracer import Tracer
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client

SETTINGS = Settings(
    openai_api_key="test", retell_api_key="test", langfuse_enabled=False,
    first_turn_lite=False
)
LLM_JSON = json.loads((Path(__file__).resolve().parents[1] / "agent" / "llm.json").read_text())


def make_runtime(fake: FakeLLM) -> CallRuntime:
    return CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake, http_client=mock_client())


async def run_turn(runtime: CallRuntime, call_id: str, user_text: str, first: bool, initial=None):
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": user_text}
    if first:
        payload.update(initial or runtime.initial_state(call_id))
    tokens = []
    async for mode, data in runtime.graph.astream(payload, config=config, stream_mode=["custom", "updates"]):
        if mode == "custom" and isinstance(data, dict) and "tts_token" in data:
            tokens.append(data["tts_token"])
    return tokens


# --------------------------------------------------------------------------- #
def test_tool_round_writes_dvs_and_history():
    fake = FakeLLM([
        {  # round 1: intake extract + completion flag
            "tokens": [],
            "tool_calls": [
                tool_call("extract_intake_details", {
                    "inbound_channel": "voicemail", "interest_topic": "missed calls",
                    "pain_frame": "losing customers"}),
                tool_call("intake_completed", {"intake_completed": True}, call_id="call_flag"),
            ],
        },
        {"tokens": ["Got", " it", "."], "match_system": "This is the start of the call"},
    ])
    rt = make_runtime(fake)
    tokens = run_turn_sync(rt, "call-1", "I got a voicemail from Jay about missed calls.")
    assert tokens == ["Got", " it", "."]

    state = get_state_sync(rt, "call-1")
    assert state["dvs"]["inbound_channel"] == "voicemail"
    assert state["dvs"]["intake_completed"] is True
    roles = [m["role"] for m in state["history"]]
    assert "tool" in roles and "user" in roles
    # tools available in Intake include its extract tool + end_call + transition
    assert "extract_intake_details" in fake.seen_tools[0]
    assert "transition_to_Discovery" in fake.seen_tools[0]
    assert "end_call" in fake.seen_tools[0]


def test_midturn_transition_swaps_state():
    fake = FakeLLM([
        {   # Intake round: capture + flag + transition to Discovery (V2 gates)
            "tokens": [],
            "tool_calls": [
                tool_call("extract_intake_details", {"inbound_channel": "ad", "interest_topic": "x",
                                                      "pain_frame": "y"}),
                tool_call("intake_completed", {"intake_completed": True}, "flag_2"),
                tool_call("transition_to_Discovery", {"intake_completed": True, "inbound_channel": "ad",
                                                       "interest_topic": "x", "pain_frame": "y"},
                          call_id="call_2"),
            ],
        },
        # next round must ALREADY run Discovery's prompt + toolset
        {"tokens": ["So", " you", " saw", " the", " ad", "."], "match_system": "Bridge what they came for"},
    ])
    rt = make_runtime(fake)
    run_turn_sync(rt, "call-2", "saw your ad")
    # round 2 toolset: Discovery tools, NOT Intake's
    assert "extract_discovery_details" in fake.seen_tools[1]
    assert "extract_intake_details" not in fake.seen_tools[1]
    state = get_state_sync(rt, "call-2")
    assert state["state_name"] == "Discovery"


def test_state_persists_across_invocations():
    fake = FakeLLM([
        {"tokens": ["Hello", " there", "."], "match_system": "This is the start of the call"},
        {"tokens": ["Makes", " sense", "."], "match_system": "This is the start of the call"},
    ])
    rt = make_runtime(fake)
    run_turn_sync(rt, "call-3", "hi", first=True)
    run_turn_sync(rt, "call-3", "what is this about?")
    state = get_state_sync(rt, "call-3")
    user_msgs = [m for m in state["history"] if m["role"] == "user"]
    assert len(user_msgs) == 2
    # begin_message seeded exactly once (first invocation)
    assert state["history"][0]["content"].startswith("Hello! You have reached Dialux")


def test_end_call_ends_turn():
    fake = FakeLLM([
        {"tokens": ["Good", "bye", "."], "tool_calls": [tool_call("end_call", {})]},
        {"tokens": ["Take", " care", "."], "match_system": "send them off warm"},
    ])
    rt = make_runtime(fake)
    run_turn_sync(rt, "call-4", "that is all, bye")
    state = get_state_sync(rt, "call-4")
    assert state["ended"] is True


def test_repeat_guard_nudges():
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [tool_call("extract_intake_details", {"inbound_channel": "ad"})]},
        # second round: model repeats the IDENTICAL call -> guard nudges, loop continues
        {"tokens": [], "tool_calls": [tool_call("extract_intake_details", {"inbound_channel": "ad"})]},
        {"tokens": ["Moving", " on", "."], "match_system": "This is the start of the call"},
    ])
    rt = make_runtime(fake)
    run_turn_sync(rt, "call-5", "hello")
    tool_msgs = [m for m in get_state_sync(rt, "call-5")["history"] if m["role"] == "tool"]
    guard = json.loads(tool_msgs[-1]["content"])
    assert guard["status"] == "already_captured"


# (the full happy-path walk for V2 lives in tests/test_v2.py — gated, with
# mocked production webhooks: test_gated_full_happy_path)


# --------------------------------------------------------------------------- #
# helpers: run the async turn on a fresh event loop per test (pytest-sync style)
def run_turn_sync(runtime: CallRuntime, call_id: str, user_text: str, first: bool = False, initial=None):
    import asyncio
    return asyncio.run(_run(runtime, call_id, user_text, first, initial))


async def _run(runtime: CallRuntime, call_id: str, user_text: str, first: bool, initial):
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": user_text}
    if first:
        payload.update(initial or runtime.initial_state(call_id))
    tokens = []
    async for mode, data in runtime.graph.astream(payload, config=config, stream_mode=["custom", "updates"]):
        if mode == "custom" and isinstance(data, dict) and "tts_token" in data:
            tokens.append(data["tts_token"])
    return tokens


def get_state_sync(runtime: CallRuntime, call_id: str) -> dict:
    import asyncio

    async def _get():
        snap = await runtime.graph.aget_state({"configurable": {"thread_id": call_id}})
        return snap.values

    return asyncio.run(_get())
