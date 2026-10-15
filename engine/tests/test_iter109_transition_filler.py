"""iter109 (transition-hold) — a silent TRANSITION round speaks ONE engine
hold line while the destination state boots.

iter105 T4 left transition rounds fully silent (pull-first prose deleted +
chain fillers gated off under ask_hold): live callers heard nothing from
their EOT until the destination state spoke (3-4.6 s on hops). iter109
widens the single filler producer — a hop round that SUCCEEDED (new_state
set) and spoke nothing gets one line from _ENGINE_FILLERS, anti-repeat per
call; a round/turn that already spoke never fillers; ask_hold=False keeps
the legacy byte-exact path (no emission).

Pins:
  1. test_silent_transition_fires_one_hold  — silent successful hop -> exactly one hold line
  2. test_spoken_transition_no_hold         — a hop that spoke never fillers
  3. test_silent_extract_no_hold            — silent NON-transition round -> zero hold lines
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime, _ENGINE_FILLERS
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client, BOOKING_UID

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())

SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, first_turn_lite=False)

PERSONA = {"first_name": "Maria", "last_name": "Gonzales",
           "company_name": "Bright Smile Dental", "prospect_timezone": "America/Chicago",
           "callback_number": "+13124001234"}

_VALIDATE_ARGS = {"first_name": "Maria", "last_name": "Gonzales",
                  "company_name": "Bright Smile Dental",
                  "callback_number": "+13124001234",
                  "prospect_timezone": "America/Chicago",
                  "selected_time": "2026-09-04T14:30:00"}


def make_rt(rounds: list[dict]):
    fake = FakeLLM(rounds)
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    return rt, fake


def run_turn(rt: CallRuntime, call_id: str, user_text: str,
             state: str = "ConfirmSlots"):
    config = {"configurable": {"thread_id": call_id}}
    payload = dict(rt.initial_state(call_id, PERSONA))
    payload["state_name"] = state
    payload["user_text"] = user_text
    custom: list[dict] = []

    async def go():
        async for mode, data in rt.graph.astream(payload, config=config,
                                                 stream_mode=["updates", "custom"]):
            if mode == "custom":
                custom.append(data)
        return (await rt.graph.aget_state(config)).values

    return asyncio.run(go()), custom


def spoken(custom: list[dict]) -> str:
    return "".join(d["tts_token"] for d in custom
                   if isinstance(d, dict) and "tts_token" in d)


def hold_lines(custom: list[dict]) -> list[str]:
    out = spoken(custom)
    return [line for line in _ENGINE_FILLERS if line in out]


_HOP_ROUND = {"tokens": [], "tool_calls": [
    tool_call("extract_confirm_details", {"selected_time": "2026-09-04T14:30:00"}),
    tool_call("validate_lead", dict(_VALIDATE_ARGS)),
    tool_call("transition_to_VerifyLead", {"slot_verified": True}, "t1"),
]}


# ---- 1. a silent successful hop speaks one hold --------------------------- #
def test_silent_transition_fires_one_hold():
    rt, fake = make_rt([
        dict(_HOP_ROUND),
        {"tokens": ["Great", ",", " we", " are", " all", " set", "."]},
        {"tokens": ["Bye", "!"], "tool_calls": [tool_call("end_call", {})]},
    ])
    state, custom = run_turn(rt, "t109-1", "tomorrow works")
    assert state["dvs"]["booking_uid"] == BOOKING_UID, "the chain still books"
    hits = hold_lines(custom)
    assert len(hits) == 1, f"silent hop must speak exactly one hold line, got {hits}"


# ---- 2. a hop that spoke never fillers ------------------------------------ #
def test_spoken_transition_no_hold():
    spoken_round = dict(_HOP_ROUND)
    spoken_round["tokens"] = ["Got", " it", ",", " locking", " that", " in", "."]
    rt, fake = make_rt([
        spoken_round,
        {"tokens": ["Great", ",", " we", " are", " all", " set", "."]},
        {"tokens": ["Bye", "!"], "tool_calls": [tool_call("end_call", {})]},
    ])
    _, custom = run_turn(rt, "t109-2", "tomorrow works")
    assert hold_lines(custom) == [], "the hop spoke — no hold may fire"


# ---- 3. silent non-transition rounds never filler ------------------------- #
def test_silent_extract_no_hold():
    rt, fake = make_rt([
        {"tokens": [], "tool_calls": [
            tool_call("extract_confirm_details", {"selected_time": "2026-09-04T14:30:00"})]},
        {"tokens": ["Which", " day", " works", "?"]},
        {"tokens": ["Bye", "!"], "tool_calls": [tool_call("end_call", {})]},
    ])
    _, custom = run_turn(rt, "t109-3", "maybe friday")
    assert hold_lines(custom) == [], "extract-only rounds must not filler"
