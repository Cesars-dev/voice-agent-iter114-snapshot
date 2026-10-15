"""iter105 T2+T3 (ask-hold) — the defer gate + hold-release.

T2 (defer gate): a DEFER_SET call (query_livecall_slots,
create_livecall_booking, calculate_monthly_leak) never executes before the
caller's answer. S1 = the round's spoken text ended on a question; S2 = the
round entered ConfirmSlots and the caller has not named a day/time (the
day-guard executes answer-first flows immediately). Deferred != executed:
the tool message says "wait / re-call"; spoke rounds finalize, silent rounds
loop once. The phase-B engine chain is exempt by construction.

T3 (hold-release): engine continuation speech never happens without new
caller input — mid-turn news/results are parked as held_facts and released
with an ENGINE note on the next caller turn.

Pins:
  T2: test_question_round_defers_slots · test_entry_round_defers_slots ·
      test_entry_round_no_defer_when_caller_gave_day ·
      test_answered_round_executes · test_phaseb_chain_never_deferred
  T3: test_engine_news_held_on_question_round · test_release_note_next_ingest ·
      test_result_tool_loop_held · test_normal_news_speaks
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
from tests.mock_webhooks import mock_client, BOOKING_UID

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())

SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, first_turn_lite=False)

PERSONA = {"first_name": "Maria", "last_name": "Gonzales",
           "company_name": "Bright Smile Dental", "prospect_timezone": "America/Chicago",
           "callback_number": "+13124001234"}
CONTACT_DVS = {**PERSONA, "contact_details_completed": True,
               "phone_confirmed": True, "is_calling_best_number": True}

DAY_Q = ["Would", " you", " prefer", " later", " today", " or", " tomorrow", "?"]


def make_rt(rounds: list[dict], settings: Settings | None = None):
    fake = FakeLLM(rounds)
    rt = CallRuntime(settings or SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    return rt, fake


def run_turn(rt: CallRuntime, call_id: str, user_text: str,
             state: str = "ConfirmSlots", persona: dict | None = None,
             first: bool = True):
    """One caller turn; returns (final_state, custom_events)."""
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


def tool_msgs(vals: dict) -> list[dict]:
    return [m for m in vals.get("history", []) if m.get("role") == "tool"]


def trace_names(rt: CallRuntime) -> list[str]:
    return [e["tool"] for e in rt.executor.trace]


# ---- T2.1 S1: a question round never pulls -------------------------------- #
def test_question_round_defers_slots():
    rt, fake = make_rt([{"tokens": DAY_Q,
                         "tool_calls": [tool_call("query_livecall_slots", {})]}])
    vals, custom = run_turn(rt, "ah-t2-1", "sure", state="ConfirmSlots",
                            persona=PERSONA)
    assert spoken(custom).rstrip() == "Would you prefer later today or tomorrow?"
    assert "query_livecall_slots" not in trace_names(rt), \
        "the pull must NOT execute on a question round"
    msgs = [m for m in tool_msgs(vals) if '"deferred"' in m["content"]]
    assert len(msgs) == 1
    assert "You asked the caller a question in this same response." in msgs[0]["content"]
    assert "query_livecall_slots" in msgs[0]["content"]
    calls = vals.get("last_round_tool_calls") or []
    assert calls and calls[-1]["status"] == "deferred"
    # spoke -> finalize: the single scripted FakeLLM round was the whole turn
    assert fake.rounds == []


# ---- T2.2 S2: ConfirmSlots entry before a day -> defer; silent loops once -- #
def test_entry_round_defers_slots():
    rt, fake = make_rt([
        {"tokens": [], "tool_calls": [
            tool_call("transition_to_ConfirmSlots", {"contact_details_completed": True}, "t1"),
            tool_call("query_livecall_slots", {}, "t2")]},
        {"tokens": DAY_Q},
    ])
    vals, custom = run_turn(rt, "ah-t2-2", "okay, sounds good",
                            state="contact_details", persona=CONTACT_DVS)
    assert vals["state_name"] == "ConfirmSlots"
    assert "query_livecall_slots" not in trace_names(rt), \
        "the pull must NOT execute before the caller names a day"
    msgs = [m for m in tool_msgs(vals) if '"deferred"' in m["content"]]
    assert len(msgs) == 1
    assert "Slot pull deferred" in msgs[0]["content"]
    assert "Would you prefer later today or tomorrow" in msgs[0]["content"]
    # silent round -> loop ONCE: round 2 (the day question) was consumed+spoken
    assert fake.rounds == []
    assert "Would you prefer later today or tomorrow?" in spoken(custom)
    assert vals.get("round_asked") is True


# ---- T2.3 S2 day-guard: caller already named the day -> pull EXECUTES ------ #
def test_entry_round_no_defer_when_caller_gave_day():
    # The pull is resolvable in ConfirmSlots (where the entry round runs —
    # the corpus shape: transition landed round 1, pull round runs in the
    # new state); the S2 pre-scan still sees the co-fired transition name.
    rt, fake = make_rt([
        {"tokens": ["Perfect", ",", " checking", " tomorrow", " for", " you", "."],
         "tool_calls": [
             tool_call("transition_to_ConfirmSlots", {"contact_details_completed": True}, "t1"),
             tool_call("query_livecall_slots", {"slot_target_date": "2026-09-05"}, "t2")]},
        {"tokens": ["I", " can", " get", " you", " in", " tomorrow", " at",
                    " 1", " or", " 2:30", "."]},
    ])
    vals, custom = run_turn(rt, "ah-t2-3", "tomorrow afternoon works",
                            state="ConfirmSlots", persona=PERSONA)
    assert "query_livecall_slots" in trace_names(rt), \
        "the T5 day-guard: answer-first flows must pull immediately"
    msgs = tool_msgs(vals)
    assert msgs and not any('"deferred"' in m["content"] for m in msgs)
    assert any("slots_human" in m["content"] for m in msgs)
    # the readout continuation ran (round 2 consumed)
    assert fake.rounds == []
    assert "1" in spoken(custom)


# ---- T2.4 answered round (no question) executes + readout flows ------------ #
def test_answered_round_executes():
    rt, fake = make_rt([
        {"tokens": ["Let", " me", " check", " that", " for", " you", "."],
         "tool_calls": [tool_call("query_livecall_slots", {"slot_target_date": "2026-09-05"})]},
        {"tokens": ["I", " can", " do", " tomorrow", " at", " 1", " pm", " or",
                    " 2:30", " pm", "."]},
    ])
    vals, custom = run_turn(rt, "ah-t2-4", "tomorrow works", state="ConfirmSlots",
                            persona=PERSONA)
    assert "query_livecall_slots" in trace_names(rt)
    assert not any('"deferred"' in m["content"] for m in tool_msgs(vals))
    assert fake.rounds == []
    assert "2:30" in spoken(custom), "the readout continuation must speak"


# ---- T2.5 the phase-B chain is exempt (books regardless of ask_hold) ------- #
def test_phaseb_chain_never_deferred():
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

    config = {"configurable": {"thread_id": "ah-t2-5"}}

    async def go_all():
        for i, text in enumerate(["tomorrow works", "thanks"]):
            payload = {"user_text": text}
            if i == 0:
                payload.update(rt.initial_state("ah-t2-5", persona_dvs=PERSONA))
                payload["state_name"] = "ConfirmSlots"
            async for _m, _d in rt.graph.astream(payload, config=config,
                                                 stream_mode=["updates"]):
                pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go_all())
    assert state["state_name"] == "Closing"
    assert state["dvs"]["booking_uid"] == BOOKING_UID
    assert state["dvs"]["booking_verified"] is True
    names = trace_names(rt)
    for step in ("verify_lead_data", "create_livecall_booking",
                 "record_booking_uid", "transition_to_Closing"):
        assert step in names
    for m in tool_msgs(state):
        assert '"deferred"' not in m["content"], "engine chain calls are never deferred"


# --------------------------------------------------------------------------- #
# T3 — hold-release: continuation speech waits for the caller
# --------------------------------------------------------------------------- #
LEAK_DVS = {"missed_calls_weekly": "8", "close_rate_pct": "30", "avg_job_value": "2500"}
LEAK_Q = ["How", " much", " revenue", " are", " you", " leaving", " on", " the",
          " table", " each", " month", "?"]


# ---- T3.1 engine news never speaks on a question round -------------------- #
def test_engine_news_held_on_question_round():
    rt, fake = make_rt([{"tokens": LEAK_Q,
                         "tool_calls": [tool_call("memory_note", {"note": "x"})]}])
    vals, custom = run_turn(rt, "ah-t3-1", "I miss about eight a week",
                            state="Discovery", persona=LEAK_DVS)
    assert vals.get("held_facts") == ["the leak math result is ready"], vals.get("held_facts")
    assert vals["dvs"].get("weekly_leak"), "the engine math still lands in dvs"
    assert fake.rounds == [], "no continuation round — the news is HELD"
    assert spoken(custom).rstrip() == "How much revenue are you leaving on the table each month?"


# ---- T3.2 the next round delivers and the parking lot is cleared ---------- #
def test_release_note_next_ingest():
    rt, fake = make_rt([{"tokens": LEAK_Q,
                         "tool_calls": [tool_call("memory_note", {"note": "x"})]},
                        {"tokens": ["That", " is", " a", " lot", "."]}])
    run_turn(rt, "ah-t3-2", "I miss about eight a week", state="Discovery",
             persona=LEAK_DVS)
    vals, custom = run_turn(rt, "ah-t3-2", "wow, okay", state="Discovery",
                            persona=None, first=False)
    note = ("ENGINE: the leak math result is ready. Deliver these now, "
            "then continue the flow.")
    assert note in fake.seen_systems[-1], "the release note must ride the next delta"
    assert vals.get("held_facts") == [], "held facts are consumed once"
    assert "That is a lot." in spoken(custom)


# ---- T3.3 the engine entry loop is held too (no auto-speak readout) ------- #
def test_result_tool_loop_held():
    rt, fake = make_rt([
        {"tokens": DAY_Q, "tool_calls": [
            tool_call("transition_to_ConfirmSlots",
                      {"contact_details_completed": True}, "t1")]},
    ])
    vals, custom = run_turn(rt, "ah-t3-3", "yes, that's all correct",
                            state="contact_details", persona=CONTACT_DVS)
    assert vals["state_name"] == "ConfirmSlots"
    # the deterministic date prefetch fired — and was held, not looped
    assert vals["dvs"].get("today_date"), "the engine prefetch still lands"
    assert vals.get("held_facts") == ["today's date is available"]
    assert fake.rounds == [], "no continuation round after the question"
    assert "Would you prefer later today or tomorrow?" in spoken(custom)


# ---- T3.4 non-question rounds speak the news exactly as today ------------- #
def test_normal_news_speaks():
    rt, fake = make_rt([{"tokens": ["Got", " it", "."],
                         "tool_calls": [tool_call("memory_note", {"note": "x"})]},
                        {"tokens": ["Your", " leak", " is", " 12", "k", "."]}])
    vals, custom = run_turn(rt, "ah-t3-4", "eight calls, thirty percent, 2500 a job",
                            state="Discovery", persona=LEAK_DVS)
    assert vals.get("held_facts") == []
    assert fake.rounds == [], "the news round was consumed and spoken"
    assert "Your leak is 12k." in spoken(custom)
