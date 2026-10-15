"""V2-specific tests: the production iterations.

  1. hard gates — a transition with unsatisfied `required` params is REFUSED;
     non-adjacent transitions are INVALID
  2. server-owned dvs — extract tools cannot write slot_verified / today_date
  3. deterministic leak math — fires from the graph when inputs exist,
     even if the LLM never calls calculate_monthly_leak (open item #1)
  4. deterministic today prefetch in ConfirmSlots (open item #3)
  5. typed state — numeric coercion, unknown keys dropped, no null passthrough
  6. full gated happy path — every transition passes a REAL gate fed by the
     mocked production webhooks (validate_lead, verify-lead-data, book-livecall,
     record-booking-uid, record-reach-details)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.schema import DynamicVariables, SERVER_OWNED_DVS
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client, BOOKING_UID

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())
SETTINGS = Settings(openai_api_key="test", retell_api_key="test", langfuse_enabled=False)


def make_runtime(fake: FakeLLM, http: httpx.AsyncClient | None = None) -> CallRuntime:
    return CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake, http_client=http or mock_client())


async def run_turn(rt: CallRuntime, call_id: str, user_text: str, first: bool = True):
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": user_text}
    if first:
        payload.update(rt.initial_state(call_id))
    async for _mode, _data in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
        pass
    snap = await rt.graph.aget_state(config)
    return snap.values


def run(rt, call_id, text, first=True):
    return asyncio.run(run_turn(rt, call_id, text, first))


# --------------------------------------------------------------------------- #
def test_hard_gate_refuses_transition():
    """contact_details -> ConfirmSlots requires the server-written phone_confirmed;
    a premature transition is refused and the model sees the refusal."""
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [
            tool_call("extract_person_details", {"first_name": "Maria"}),
            tool_call("transition_to_ConfirmSlots", {"contact_details_completed": True,
                                                      "phone_confirmed": True}, "c1"),
        ]},
        {"tokens": ["Let", "'s", " finish", " your", " details", "."]},
    ])
    rt = make_runtime(fake)
    initial = rt.initial_state("g1")
    initial["state_name"] = "contact_details"          # jump straight to contact_details

    async def go():
        config = {"configurable": {"thread_id": "g1"}}
        payload = {**initial, "user_text": "hi"}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert state["state_name"] == "contact_details"     # refused -> stayed
    rejections = rt.executor.gate_rejections
    assert rejections and "phone_confirmed" in rejections[0]["missing"]
    tool_msgs = [m for m in state["history"] if m["role"] == "tool"]
    refusal = json.loads(next(m["content"] for m in tool_msgs if "gate_failed" in m["content"]))
    assert refusal["status"] == "gate_failed" and "phone_confirmed" in refusal["missing"]


def test_gate_passes_with_is_calling_best_number_false():
    """iter14: a legitimate NO answer (is_calling_best_number=false) no longer
    dead-ends the gate — non-null (answered), not truthy, is the semantics."""
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [
            tool_call("extract_person_details", {"first_name": "Maria", "last_name": "Gonzales",
                                                   "company_name": "Bright Smile Dental"}),
            tool_call("extract_contact_timezone", {"prospect_timezone": "America/Chicago"}),
            tool_call("record_reach_details", {"is_calling_best_number": False}),
            tool_call("set_callback_number", {"callback_number": "+13124001234"}),
            tool_call("contact_details_completed", {"contact_details_completed": True}),
            tool_call("transition_to_ConfirmSlots", {
                "contact_details_completed": True, "first_name": "Maria",
                "last_name": "Gonzales", "company_name": "Bright Smile Dental",
                "prospect_timezone": "America/Chicago", "callback_number": "+13124001234",
                "is_calling_best_number": False, "phone_confirmed": True}, "c9"),
        ]},
        {"tokens": ["Perfect", "."]},
    ])
    rt = make_runtime(fake)
    initial = rt.initial_state("g14a", persona_dvs={"callback_number": "+13124001234"})
    initial["state_name"] = "contact_details"

    async def go():
        config = {"configurable": {"thread_id": "g14a"}}
        payload = {**initial, "user_text": "call my cell instead"}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert state["state_name"] == "ConfirmSlots"
    assert state["dvs"]["is_calling_best_number"] is False
    assert state["dvs"]["phone_confirmed"] is True
    assert rt.executor.gate_rejections == []


def test_gate_passes_with_empty_last_name():
    """iter15: last_name is no longer required — a caller who declines to give
    a last name can still complete contact_details and transition to ConfirmSlots."""
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [
            tool_call("extract_person_details", {"first_name": "Maria",
                                                   "company_name": "Bright Smile Dental"}),
            tool_call("extract_contact_timezone", {"prospect_timezone": "America/Chicago"}),
            tool_call("record_reach_details", {"is_calling_best_number": True}),
            tool_call("contact_details_completed", {"contact_details_completed": True}),
            tool_call("transition_to_ConfirmSlots", {
                "contact_details_completed": True, "first_name": "Maria",
                "last_name": "", "company_name": "Bright Smile Dental",
                "prospect_timezone": "America/Chicago", "callback_number": "+13124001234",
                "is_calling_best_number": True, "phone_confirmed": True}, "c15"),
        ]},
        {"tokens": ["Perfect", "."]},
    ])
    rt = make_runtime(fake)
    initial = rt.initial_state("g15", persona_dvs={"callback_number": "+13124001234"})
    initial["state_name"] = "contact_details"

    async def go():
        config = {"configurable": {"thread_id": "g15"}}
        payload = {**initial, "user_text": "I'd rather not give a last name"}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert state["state_name"] == "ConfirmSlots"
    assert state["dvs"]["last_name"] == ""
    assert rt.executor.gate_rejections == []


def test_gate_blocks_when_is_calling_best_number_unanswered():
    """iter14: is_calling_best_number=None (question never asked) BLOCKS the
    gate and the message names it as unanswered."""
    from diallux.graph.tools import ToolExecutor
    ex = ToolExecutor(SETTINGS, LLM_JSON, http_client=mock_client())
    dvs = {"contact_details_completed": True, "first_name": "Maria", "last_name": "Gonzales",
           "company_name": "Bright Smile Dental", "prospect_timezone": "America/Chicago",
           "callback_number": "+13124001234", "phone_confirmed": True}
    out = asyncio.run(ex.execute("transition_to_ConfirmSlots", dvs, dict(dvs), "contact_details"))
    assert out.new_state is None
    assert out.response["status"] == "gate_failed"
    assert "is_calling_best_number" in out.response["missing"]
    assert "is_calling_best_number is unanswered" in out.response["message"]


def test_non_adjacent_transition_is_invalid():
    """The state machine topology itself is enforced: Intake cannot jump to Booking."""
    from diallux.graph.tools import ToolExecutor
    ex = ToolExecutor(SETTINGS, LLM_JSON, http_client=mock_client())
    out = asyncio.run(ex.execute("transition_to_Booking", {"data_verified": True}, {}, "Intake"))
    assert out.new_state is None
    assert out.response["status"] == "invalid_transition"


def test_hard_gate_allows_transition_when_webhook_wrote_gate():
    """record-reach-details (server) writes phone_confirmed -> the gate opens."""
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [
            tool_call("extract_person_details", {"first_name": "Maria", "last_name": "Gonzales",
                                                   "company_name": "Bright Smile Dental"}),
            tool_call("extract_contact_timezone", {"prospect_timezone": "America/Chicago"}),
            tool_call("record_reach_details", {"is_calling_best_number": True}),
            tool_call("contact_details_completed", {"contact_details_completed": True}),
            tool_call("transition_to_ConfirmSlots", {
                "contact_details_completed": True, "first_name": "Maria",
                "last_name": "Gonzales", "company_name": "Bright Smile Dental",
                "prospect_timezone": "America/Chicago", "callback_number": "+13124001234",
                "is_calling_best_number": True, "phone_confirmed": True}, "c9"),
        ]},
        {"tokens": ["Perfect", "."]},
    ])
    rt = make_runtime(fake)
    initial = rt.initial_state("g2", persona_dvs={"callback_number": "+13124001234"})
    initial["state_name"] = "contact_details"

    async def go():
        config = {"configurable": {"thread_id": "g2"}}
        payload = {**initial, "user_text": "here are my details"}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    # webhook wrote phone_confirmed=True -> every required param satisfied -> gate opens
    assert state["state_name"] == "ConfirmSlots"
    assert state["dvs"]["phone_confirmed"] is True
    assert rt.executor.gate_rejections == []


def test_server_owned_dvs_reject_extract_writes():
    from diallux.graph.tools import ToolExecutor
    ex = ToolExecutor(SETTINGS, LLM_JSON, http_client=mock_client())
    # synthetic tool declaring a server-owned variable (defense in depth)
    t = {"name": "evil_extract", "type": "extract_dynamic_variable",
         "variables": [{"name": "slot_verified", "type": "boolean"}]}
    out = ex._exec_extract(t, {"slot_verified": True})
    assert out.dvs_patch == {}
    assert out.response.get("rejected_server_owned") == ["slot_verified"]


def test_deterministic_leak_math_fires_without_llm_call():
    """Open item #1: the Closer stochastically skipped calculate_monthly_leak.
    V2: the deterministic node computes it when inputs exist."""
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [
            tool_call("extract_leak_inputs", {"missed_calls_weekly": "20",
                                               "close_rate_pct": "50",
                                               "avg_job_value": "650"}),
        ]},
        {"tokens": ["Rough", " numbers", "."]},
    ])
    rt = make_runtime(fake)
    initial = rt.initial_state("d1")
    initial["state_name"] = "Closer"

    async def go():
        config = {"configurable": {"thread_id": "d1"}}
        payload = {**initial, "user_text": "about 20 calls, half would book, 650 average"}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert state["dvs"]["weekly_leak"] == "6,500"       # deterministic layer fired
    assert state["dvs"]["monthly_leak"] == "28,100"


def test_typed_state_coercion_and_unknown_keys():
    m = DynamicVariables.from_flat({"missed_calls_weekly": "20", "pattern_matched": True,
                                     "hacker_key": "x", "first_name": None})
    flat = m.to_flat()
    assert flat["missed_calls_weekly"] == 20
    assert flat["pattern_matched"] is True
    assert "hacker_key" not in flat
    assert flat["first_name"] == ""
    assert "slot_verified" in SERVER_OWNED_DVS


def test_today_prefetch_in_confirm_slots():
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [
            tool_call("extract_confirm_details", {"selected_time": "2026-09-05T13:00:00"}),
        ]},
        {"tokens": ["Got", " it", "."]},
    ])
    rt = make_runtime(fake)
    initial = rt.initial_state("t1")
    initial["state_name"] = "ConfirmSlots"

    async def go():
        config = {"configurable": {"thread_id": "t1"}}
        payload = {**initial, "user_text": "tomorrow please"}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert state["dvs"]["today_date"] == "2026-09-04"


# --------------------------------------------------------------------------- #
def test_gated_full_happy_path():
    """The whole 9-state machine with REAL gates: every transition only passes
    because the mocked production webhooks wrote the gate booleans."""
    fake = FakeLLM()
    # turn 1: Intake -> Discovery (flag tool + transition; gate checks dvs)
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_intake_details", {"inbound_channel": "voicemail",
                                             "interest_topic": "missed calls",
                                             "pain_frame": "losing jobs"}),
        tool_call("intake_completed", {"intake_completed": True}, "f1"),
        tool_call("transition_to_Discovery", {"intake_completed": True,
                                               "inbound_channel": "voicemail",
                                               "interest_topic": "missed calls",
                                               "pain_frame": "losing jobs"}, "t1"),
    ]})
    fake.add_round({"tokens": ["Thanks", " for", " sharing", "."]})
    # turn 2: Discovery -> Closer
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_discovery_details", {"industry": "dental", "pain_points": "after-hours",
                                                  "interest_signal": True, "pattern_matched": True}),
        tool_call("discovery_completed", {"discovery_completed": True}, "f2"),
        tool_call("transition_to_Closer", {"industry": "dental", "interest_level": "high",
                                            "closer_completed": True, "pain_points": "after-hours"}, "t2"),
    ]})
    fake.add_round({"tokens": ["Ouch", "."]})
    # turn 3: Closer -> Offer (leak inputs; deterministic layer computes the leak)
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_leak_inputs", {"missed_calls_weekly": 20, "close_rate_pct": 50,
                                           "avg_job_value": 650}),
        tool_call("extract_closer_details", {"objection_type": "", "interest_level": "high",
                                              "last_name": "Gonzales"}),
        tool_call("closer_completed", {"closer_completed": True}, "f3"),
        tool_call("transition_to_Offer", {"industry": "dental", "interest_level": "high",
                                           "closer_completed": True, "pain_points": "after-hours"}, "t3"),
    ]})
    fake.add_round({"tokens": ["That", "'s", " $6,500", " a", " week", "."]})
    # turn 4: Offer -> contact_details
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_offer_details", {"livecall_agreed": True}),
        tool_call("offer_completed", {"offer_completed": True}, "f4"),
        tool_call("transition_to_contact_details", {"offer_completed": True, "livecall_agreed": True}, "t4"),
    ]})
    fake.add_round({"tokens": ["Perfect", "."]})
    # turn 5: contact_details -> ConfirmSlots (server writes phone_confirmed)
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_person_details", {"first_name": "Maria", "last_name": "Gonzales",
                                               "company_name": "Bright Smile Dental"}),
        tool_call("extract_contact_timezone", {"prospect_timezone": "America/Chicago"}),
        tool_call("record_reach_details", {"is_calling_best_number": True}),
        tool_call("contact_details_completed", {"contact_details_completed": True}, "f5"),
        tool_call("transition_to_ConfirmSlots", {"contact_details_completed": True, "first_name": "Maria",
                                                   "last_name": "Gonzales", "company_name": "Bright Smile Dental",
                                                   "prospect_timezone": "America/Chicago",
                                                   "callback_number": "+13124001234",
                                                   "is_calling_best_number": True,
                                                   "phone_confirmed": True}, "t5"),
    ]})
    fake.add_round({"tokens": ["Great", "."]})
    # turn 6: ConfirmSlots -> VerifyLead (validate_lead writes slot_verified)
    fake.add_round({"tokens": [], "tool_calls": [
        tool_call("extract_confirm_details", {"selected_time": "2026-09-04T14:30:00"}),
        tool_call("validate_lead", {"first_name": "Maria", "last_name": "Gonzales",
                                     "company_name": "Bright Smile Dental",
                                     "callback_number": "+13124001234",
                                     "prospect_timezone": "America/Chicago",
                                     "selected_time": "2026-09-04T14:30:00",
                                     "missed_calls_per_week": 20, "close_rate_percent": 50,
                                     "avg_job_value": 650}),
        tool_call("transition_to_VerifyLead", {"slot_verified": True,
                                                "selected_time": "2026-09-04T14:30:00",
                                                "prospect_timezone": "America/Chicago"}, "t6"),
    ]})
    fake.add_round({"tokens": ["Locked", " in", "."]})
    # iter33 Phase B: the ENGINE owns everything past slot_verified — after
    # turn 6's tool round the deterministic node runs verify→book→record→
    # Closing in ONE pass (0 LLM rounds), then "Locked in." is spoken from
    # Closing. The old turn-7/turn-8 tool rounds are gone (F12).
    # turn 7: Closing speech (post-chain close-out)
    fake.add_round({"tokens": ["You", "'re", " booked", "."]})
    # turn 8: Closing -> end_call (iter16: goodbye must be spoken WITH end_call)
    fake.add_round({"tokens": ["Bye", "!"], "tool_calls": [tool_call("end_call", {})]})

    rt = make_runtime(fake)
    texts = ["got a voicemail", "dental office", "20 calls a week",
             "sounds good", "Maria Gonzales", "tomorrow works", "thanks", "bye"]

    async def go_all():
        config = {"configurable": {"thread_id": "g-full"}}
        for i, text in enumerate(texts):
            payload = {"user_text": text}
            if i == 0:
                # real inbound calls seed callback_number from Twilio's From
                payload.update(rt.initial_state("g-full", persona_dvs={"callback_number": "+13124001234"}))
            async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
                pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go_all())
    assert state["state_name"] == "Closing"
    assert state["ended"] is True
    # gates opened only via server writes:
    assert state["dvs"]["slot_verified"] is True
    assert state["dvs"]["data_verified"] is True
    assert state["dvs"]["booking_verified"] is True
    assert state["dvs"]["booking_uid"] == BOOKING_UID
    assert state["dvs"]["phone_confirmed"] is True
    assert state["dvs"]["selected_time"] == "2026-09-04T14:30:00"
    # deterministic leak math fired ("6,500"), then validate_lead's server values
    # OVERWROTE it with "$6,500" — documented precedence: server values are final
    assert state["dvs"]["weekly_leak"] == "$6,500"
    assert state["dvs"]["monthly_leak"] == "$28,000"
    assert state["dvs"]["today_date"] == "2026-09-04"
    assert rt.executor.gate_rejections == []
