"""iter105 T4 (single filler producer) — one hold-line source, latency-gated.

With ask_hold=True the filler emitters are exactly two: (a) the slots
in-flight timer — "One moment while I check availability" fires IFF the
slots webhook is genuinely in flight >= 400 ms, nothing spoken yet this
turn, no tts_token emitted this round; and (b) iter109's transition hold —
ONE _ENGINE_FILLERS line when a successful hop round spoke nothing (the
T4-era silence it cures). The legacy arms (dead-air killer + chain
fillers, iter40/iter41) survive ONLY on the ASK_HOLD=false rollback path
(pinned here via LEGACY-style settings). The pull-first prompt lines are
gone: ConfirmSlots.md no longer starts with a hold phrase nor mandates the
SAME-response co-fire; Booking.md/VerifyLead.md drop their hold examples.

Pins:
  1. test_chain_emits_no_filler           — phase-B chain, ask_hold on: only the iter109 hop-hold fires; chain steps emit none
  2. test_slots_filler_latency_gated      — slow webhook -> one filler; fast -> none
  3. test_no_filler_when_spoke            — a round that already spoke never fillers
  4. test_prompt_hold_phrases_absent      — the three deleted strings gone from prompts/
  5. test_prompt_no_same_response_mandate — ConfirmSlots.md reworded to ask-wait-call
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime, _ENGINE_FILLERS
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client, handler, BOOKING_UID

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "diallux" / "prompts"
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())

SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, first_turn_lite=False)

PERSONA = {"first_name": "Maria", "last_name": "Gonzales",
           "company_name": "Bright Smile Dental", "prospect_timezone": "America/Chicago",
           "callback_number": "+13124001234"}

SLOTS_FILLER = "One moment while I check availability."


def slow_client(delay_s: float) -> httpx.AsyncClient:
    async def h(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay_s)
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(h))


def make_rt(rounds: list[dict], http: httpx.AsyncClient | None = None):
    fake = FakeLLM(rounds)
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=http or mock_client(), kb_store=None)
    return rt, fake


def run_turn(rt: CallRuntime, call_id: str, user_text: str,
             state: str = "ConfirmSlots", persona: dict | None = None,
             first: bool = True):
    config = {"configurable": {"thread_id": call_id}}
    payload: dict = {}
    if first:
        payload.update(rt.initial_state(call_id, persona))
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


# ---- 1. the chain emits no filler under ask_hold -------------------------- #
def test_chain_emits_no_filler():
    rt, fake = make_rt([
        {"tokens": [], "tool_calls": [
            tool_call("extract_confirm_details", {"selected_time": "2026-09-04T14:30:00"}),
            tool_call("validate_lead", {"first_name": "Maria", "last_name": "Gonzales",
                                        "company_name": "Bright Smile Dental",
                                        "callback_number": "+13124001234",
                                        "prospect_timezone": "America/Chicago",
                                        "selected_time": "2026-09-04T14:30:00"}),
            tool_call("transition_to_VerifyLead", {"slot_verified": True}, "t1"),
        ]},
        {"tokens": ["You", "'re", " booked", "."]},
        {"tokens": ["Bye", "!"], "tool_calls": [tool_call("end_call", {})]},
    ])
    config = {"configurable": {"thread_id": "t4-1"}}
    custom: list[dict] = []

    async def go_all():
        for i, text in enumerate(["tomorrow works", "thanks"]):
            payload = {"user_text": text}
            if i == 0:
                payload.update(rt.initial_state("t4-1", persona_dvs=PERSONA))
                payload["state_name"] = "ConfirmSlots"
            async for mode, d in rt.graph.astream(payload, config=config,
                                                  stream_mode=["custom", "updates"]):
                if mode == "custom":
                    custom.append(d)
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go_all())
    assert state["dvs"]["booking_uid"] == BOOKING_UID, "the chain still books"
    out = spoken(custom)
    hits = [line for line in _ENGINE_FILLERS if line in out]
    assert len(hits) == 1, \
        f"ask_hold=True: only the iter109 hop-hold may fire (chain emits none), got {out!r}"
    assert SLOTS_FILLER not in out


# ---- 2. the slots filler is a real-in-flight timer ------------------------ #
def test_slots_filler_latency_gated():
    rounds = [
        {"tokens": [], "tool_calls": [
            tool_call("query_livecall_slots", {"slot_target_date": "2026-09-05",
                                               "account_id": "diallux_live",
                                               "timezone": "America/Chicago"})]},
        {"tokens": ["I", " can", " do", " tomorrow", " at", " 1", " pm", "."]},
    ]
    # slow webhook (600 ms > 400 ms gate) -> exactly one filler
    rt, fake = make_rt(list(rounds), http=slow_client(0.6))
    _, custom = run_turn(rt, "t4-2a", "tomorrow works", persona=PERSONA)
    out = spoken(custom)
    assert out.count(SLOTS_FILLER) == 1, f"slow webhook must filler once: {out!r}"
    assert fake.rounds == []
    # fast webhook (50 ms < gate) -> zero fillers
    rt2, fake2 = make_rt(list(rounds), http=slow_client(0.05))
    _, custom2 = run_turn(rt2, "t4-2b", "tomorrow works", persona=PERSONA)
    assert SLOTS_FILLER not in spoken(custom2)
    assert fake2.rounds == []


# ---- 3. a round that already spoke never fillers -------------------------- #
def test_no_filler_when_spoke():
    rt, fake = make_rt([
        {"tokens": ["Let", " me", " check", " tomorrow", " for", " you", "."],
         "tool_calls": [tool_call("query_livecall_slots", {"slot_target_date": "2026-09-05"})]},
        {"tokens": ["I", " can", " do", " tomorrow", " at", " 1", " pm", "."]},
    ], http=slow_client(0.6))
    _, custom = run_turn(rt, "t4-3", "tomorrow works", persona=PERSONA)
    out = spoken(custom)
    assert SLOTS_FILLER not in out, "the round spoke — no filler may fire"
    assert "Let me check tomorrow for you." in out
    assert fake.rounds == []


# ---- 4. the pull-first prompt lines are gone ------------------------------ #
def test_prompt_hold_phrases_absent():
    confirm = (PROMPTS / "ConfirmSlots.md").read_text()
    booking = (PROMPTS / "Booking.md").read_text()
    verify = (PROMPTS / "VerifyLead.md").read_text()
    assert "One moment while I check availability" not in confirm
    assert "One moment while I confirm that" not in booking
    assert "One moment while I grab that slot for you" not in verify


# ---- 5. no SAME-response mandate; the ask-wait-call wording landed -------- #
def test_prompt_no_same_response_mandate():
    confirm = (PROMPTS / "ConfirmSlots.md").read_text()
    assert "SAME response" not in confirm
    assert "wait for the caller's answer" in confirm
    assert "call the tool with what they said" in confirm
