"""iter33 Phase B tests — engine-owned booking chain (hermetic: FakeLLM + mock webhooks).

Covers the master plan U7 list:
  1. end-to-end chain with ZERO extra LLM rounds
  2. resumable skips (model already verified)
  3. no chain when booking_intent is set (model keeps control)
  4. no chain / no double-book when the model books everything itself
  5. abort -> abort dvs surface -> model repair speech clears it
  6. fillers streamed via tts_token (distinct, from the pool)
  7. PHASEB_CHAIN=0 -> iter32 behavior (model walks the whole chain)
  8. trigger guard (missing selected_time -> engine stays out)
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
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())

PERSONA = {"first_name": "Maria", "last_name": "Gonzales",
           "company_name": "Bright Smile Dental", "prospect_timezone": "America/Chicago",
           "callback_number": "+13124001234"}


def make_rt(fake: FakeLLM, http: httpx.AsyncClient | None = None,
            settings: Settings | None = None) -> CallRuntime:
    s = settings or Settings(openai_api_key="test", retell_api_key="test",
                             langfuse_enabled=False, first_turn_lite=False)
    return CallRuntime(s, LLM_JSON, tracer=None, llm=fake, http_client=http or mock_client())


async def run_turn(rt: CallRuntime, call_id: str, user_text: str, first: bool = True,
                   modes: list[str] | None = None):
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": user_text}
    if first:
        payload.update(rt.initial_state(call_id))
    custom: list[dict] = []
    async for mode, data in rt.graph.astream(payload, config=config,
                                             stream_mode=modes or ["updates"]):
        if mode == "custom":
            custom.append(data)
    snap = await rt.graph.aget_state(config)
    return snap.values, custom


def abort_client() -> httpx.AsyncClient:
    """Mock transport with verify-lead-data forced to an unfixable failure."""
    def h(request: httpx.Request) -> httpx.Response:
        if "/validator-function/verify-lead-data" in str(request.url):
            return httpx.Response(200, json={
                "ok": True, "status": "incomplete", "data_verified": False,
                "problems": [{"field": "selected_time", "issue": "unfixable_format"}],
                "actions": ["Say: 'Sorry, I lost track - which slot did you pick?'"]})
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(h))


# --------------------------------------------------------------------------- #
def test_chain_end_to_end_zero_llm():
    """Model confirms the slot (extract + validate + transition); the engine
    runs verify -> book -> record -> Closing with ZERO further LLM rounds."""
    fake = FakeLLM()
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_confirm_details", {"selected_time": "2026-09-04T14:30:00"}),
        tool_call("validate_lead", {"first_name": "Maria", "last_name": "Gonzales",
                                     "company_name": "Bright Smile Dental",
                                     "callback_number": "+13124001234",
                                     "prospect_timezone": "America/Chicago",
                                     "selected_time": "2026-09-04T14:30:00"}),
        tool_call("transition_to_VerifyLead", {"slot_verified": True}, "t1"),
    ]})
    fake.add_round({"tokens": ["You", "'re", " booked", "."]})
    fake.add_round({"tokens": ["Bye", "!"], "tool_calls": [tool_call("end_call", {})]})

    rt = make_rt(fake)

    async def go_all():
        config = {"configurable": {"thread_id": "pb1"}}
        texts = ["tomorrow works", "thanks"]
        for i, text in enumerate(texts):
            payload = {"user_text": text}
            if i == 0:
                payload.update(rt.initial_state("pb1", persona_dvs=PERSONA))
                payload["state_name"] = "ConfirmSlots"
            async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
                pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go_all())
    assert state["state_name"] == "Closing"
    assert state["ended"] is True
    assert state["dvs"]["booking_uid"] == BOOKING_UID
    assert state["dvs"]["booking_verified"] is True
    assert state["dvs"]["chain_done"] is True
    assert state["dvs"]["data_verified"] is True
    # ZERO engine-owned LLM rounds: exactly 3 FakeLLM trips for 2 turns
    assert len(fake.seen_systems) == 3
    # the engine's chain tools all ran (verify, book, record, transitions)
    names = [e["tool"] for e in rt.executor.trace]
    for step in ("verify_lead_data", "create_livecall_booking",
                 "record_booking_uid", "transition_to_Closing"):
        assert step in names


def test_chain_resumable_skips():
    """data_verified already true (model verified earlier) -> the engine skips
    verify_lead_data and still completes the chain; no gate rejections."""
    fake = FakeLLM()
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_confirm_details", {"selected_time": "2026-09-04T14:30:00"}),
        tool_call("transition_to_VerifyLead", {"slot_verified": True}, "t1"),
    ]})
    fake.add_round({"tokens": ["All", " set", "."]})

    rt = make_rt(fake)

    async def go():
        config = {"configurable": {"thread_id": "pb2"}}
        payload = {"user_text": "perfect"}
        payload.update(rt.initial_state(
            "pb2", persona_dvs={**PERSONA, "slot_verified": True, "data_verified": True}))
        payload["state_name"] = "ConfirmSlots"
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert state["state_name"] == "Closing"
    assert state["dvs"]["chain_done"] is True
    assert state["dvs"]["booking_verified"] is True
    assert rt.executor.gate_rejections == []
    # the engine NEVER ran verify (flag was already set — resumable skip)
    engine_verify = [e for e in rt.executor.trace
                     if e["tool"] == "verify_lead_data" and e["resp"].get("status") == "ok"]
    assert engine_verify == []


def test_chain_not_fired_when_intent_set():
    """booking_intent='reschedule' -> the engine stays out; the model keeps
    control of the booking tool."""
    fake = FakeLLM()
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("create_livecall_booking", {
            "account_id": "diallux_live", "timezone": "America/Chicago",
            "time": "2026-09-04T14:30:00", "name": "Maria Gonzales",
            "email": "jaydiallux@gmail.com", "attendeePhoneNumber": "+13124001234",
            "title": "Dialux Live call for Bright Smile Dental",
            "notes": "Industry: dental", "slot_reservation_uids": "u1,u2",
            "booking_intent": "reschedule", "preferred_time": ""}),
    ]})
    fake.add_round({"tokens": ["How", " about", " 1", " pm", "?"]})

    rt = make_rt(fake)

    async def go():
        config = {"configurable": {"thread_id": "pb3"}}
        payload = {"user_text": "actually I need to move it"}
        payload.update(rt.initial_state(
            "pb3", persona_dvs={**PERSONA, "slot_verified": True, "data_verified": True,
                                 "booking_intent": "reschedule"}))
        payload["state_name"] = "Booking"
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert state["dvs"].get("chain_done") is False
    assert state["state_name"] == "Booking"          # engine never took over
    assert "phaseb_chain" not in state.get("metrics", {})


def test_chain_not_fired_when_model_books_all():
    """Model walks the whole Booking round itself -> after the round the state
    is Closing, the engine trigger can't fire, no double booking."""
    fake = FakeLLM()
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("create_livecall_booking", {
            "account_id": "diallux_live", "timezone": "America/Chicago",
            "time": "2026-09-04T14:30:00", "name": "Maria Gonzales",
            "email": "jaydiallux@gmail.com", "attendeePhoneNumber": "+13124001234",
            "title": "Dialux Live call for Bright Smile Dental",
            "notes": "Industry: dental", "slot_reservation_uids": "u1,u2",
            "booking_intent": "", "preferred_time": ""}),
        tool_call("record_booking_uid", {"booking_uid": BOOKING_UID}, "c2"),
        tool_call("transition_to_Closing", {"booking_verified": True}, "t1"),
    ]})
    fake.add_round({"tokens": ["Booked", "."]})

    rt = make_rt(fake)

    async def go():
        config = {"configurable": {"thread_id": "pb4"}}
        payload = {"user_text": "go ahead"}
        payload.update(rt.initial_state(
            "pb4", persona_dvs={**PERSONA, "slot_verified": True, "data_verified": True}))
        payload["state_name"] = "Booking"
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert state["state_name"] == "Closing"
    assert state["dvs"]["booking_verified"] is True
    assert state["dvs"]["booking_uid"] == BOOKING_UID
    assert state["dvs"].get("chain_done") is False   # the ENGINE never ran
    # exactly one booking webhook call (no engine double-book)
    books = [e for e in rt.executor.trace if e["tool"] == "create_livecall_booking"]
    assert len(books) == 1


def test_chain_aborts_to_repair():
    """verify-lead-data fails unfixable -> chain aborts, abort dvs surface set,
    the next round's tail SHOWS it, and the model's repair speech clears it."""
    fake = FakeLLM()
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_confirm_details", {"selected_time": "2026-09-04T14:30:00"}),
        tool_call("validate_lead", {"selected_time": "2026-09-04T14:30:00"}),
        tool_call("transition_to_VerifyLead", {"slot_verified": True}, "t1"),
    ]})
    fake.add_round({"tokens": ["Sorry", ",", " which", " slot", " did", " you", " pick", "?"]})

    rt = make_rt(fake, http=abort_client())

    async def go():
        config = {"configurable": {"thread_id": "pb5"}}
        payload = {"user_text": "tomorrow works"}
        payload.update(rt.initial_state("pb5", persona_dvs=PERSONA))
        payload["state_name"] = "ConfirmSlots"
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    # aborted at verify_lead_data, stayed in VerifyLead, latch popped — the
    # final dvs shows it CLEARED (the repair speech ran U5); the mid-turn tail
    # check below proves the surface was live when the model read it.
    assert state["state_name"] == "VerifyLead"
    assert state["ended"] is not True
    assert "incomplete" in state["dvs"]["chain_aborted_msg"] or \
        state["dvs"]["chain_aborted_msg"] == ""       # cleared by repair speech
    assert state["dvs"].get("chain_done") is False
    # the repair round READ the abort surface (tail rendered it mid-turn)
    assert "chain_aborted_step: verify_lead_data" in fake.seen_systems[1]
    # and the repair speech CLEARED it (U5)
    assert state["dvs"]["chain_aborted_step"] == ""


def test_fillers_streamed():
    """Chain emits tts_token fillers between the slow webhooks — distinct,
    from the pool, surfaced via the custom stream and fillers_said.
    iter105 T4: this is now the ROLLBACK path (ASK_HOLD=false) — with the
    ask-hold knob ON the chain emits no filler (single producer: the slots
    in-flight timer); the legacy arm stays pinned here for byte-parity."""
    fake = FakeLLM()
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_confirm_details", {"selected_time": "2026-09-04T14:30:00"}),
        tool_call("validate_lead", {"selected_time": "2026-09-04T14:30:00"}),
        tool_call("transition_to_VerifyLead", {"slot_verified": True}, "t1"),
    ]})
    fake.add_round({"tokens": ["Booked", "."]})

    rt = make_rt(fake, settings=Settings(openai_api_key="test", retell_api_key="test",
                                         langfuse_enabled=False, first_turn_lite=False,
                                         ask_hold=False))

    async def go():
        config = {"configurable": {"thread_id": "pb6"}}
        payload = {"user_text": "tomorrow works"}
        payload.update(rt.initial_state("pb6", persona_dvs=PERSONA))
        payload["state_name"] = "ConfirmSlots"
        custom = []
        async for mode, data in rt.graph.astream(payload, config=config,
                                                 stream_mode=["custom", "updates"]):
            if mode == "custom":
                custom.append(data)
        snap = await rt.graph.aget_state(config)
        return snap.values, custom

    state, custom = asyncio.run(go())
    toks = [c["tts_token"].strip() for c in custom if "tts_token" in c]
    fillers = [t for t in toks if t in _ENGINE_FILLERS]
    # iter41 T2: 3 slow webhooks (verify, create, record_uid) -> ONE filler
    # (max 1 engine filler per deterministic pass — the 3-filler stack was the
    # R5.4 robotic defect on booked Closing turns)
    assert len(fillers) == 1
    assert fillers[0] in _ENGINE_FILLERS
    assert set(fillers) == set(state["fillers_said"])      # complete cumulative list (F8)


def test_chain_flag_off():
    """PHASEB_CHAIN=0 -> iter32 behavior: the model walks verify/book itself;
    the engine never sets chain_done."""
    s = Settings(openai_api_key="test", retell_api_key="test",
                 langfuse_enabled=False, phaseb_chain=False,
                 first_turn_lite=False)
    fake = FakeLLM()
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_confirm_details", {"selected_time": "2026-09-04T14:30:00"}),
        tool_call("validate_lead", {"selected_time": "2026-09-04T14:30:00"}),
        tool_call("transition_to_VerifyLead", {"slot_verified": True}, "t1"),
    ]})
    fake.add_round({"tokens": ["Locked", " in", "."]})
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("verify_lead_data", {"selected_time": "2026-09-04T14:30:00"}),
        tool_call("transition_to_Booking", {"data_verified": True}, "t2"),
    ]})
    fake.add_round({"tokens": ["One", " moment", "."]})
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("create_livecall_booking", {
            "account_id": "diallux_live", "timezone": "America/Chicago",
            "time": "2026-09-04T14:30:00", "name": "Maria Gonzales",
            "email": "jaydiallux@gmail.com", "attendeePhoneNumber": "+13124001234",
            "title": "Dialux Live call for Bright Smile Dental",
            "notes": "Industry: dental", "slot_reservation_uids": "u1,u2",
            "booking_intent": "", "preferred_time": ""}),
        tool_call("record_booking_uid", {"booking_uid": BOOKING_UID}, "c3"),
        tool_call("transition_to_Closing", {"booking_verified": True}, "t3"),
    ]})
    fake.add_round({"tokens": ["Booked", "."]})
    fake.add_round({"tokens": ["Bye", "!"], "tool_calls": [tool_call("end_call", {})]})

    rt = make_rt(fake, settings=s)

    async def go_all():
        config = {"configurable": {"thread_id": "pb7"}}
        texts = ["tomorrow works", "yes confirm", "go ahead", "thanks"]
        for i, text in enumerate(texts):
            payload = {"user_text": text}
            if i == 0:
                payload.update(rt.initial_state("pb7", persona_dvs=PERSONA))
                payload["state_name"] = "ConfirmSlots"
            async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
                pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go_all())
    assert state["state_name"] == "Closing"
    assert state["dvs"]["booking_verified"] is True
    assert state["dvs"].get("chain_done") is False
    assert "phaseb_chain" not in state.get("metrics", {})


def test_trigger_guard():
    """Direct _deterministic_node: trigger requires selected_time — missing ->
    engine stays out entirely."""
    fake = FakeLLM()
    rt = make_rt(fake)
    dvs = {**PERSONA, "slot_verified": True, "selected_time": ""}
    state = {"state_name": "VerifyLead", "dvs": dvs, "fillers_said": []}
    out = asyncio.run(rt._deterministic_node(state))
    assert out.get("engine_fired") is False
    assert not (out.get("dvs") or {}).get("chain_done")
