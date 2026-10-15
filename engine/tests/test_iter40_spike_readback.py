"""iter40 — spike-kill (L2/L3/C3) + slot-readback regression tests.

Commit 1 (L2 — slots prefetch) pins:
  1. warm-hit: the model's first slots call (dated, empty uids, freeze-mode —
     the t40 OTEL shape) answers from the warm store with source=prefetch,
     response_variables → dvs, ZERO HTTP
  2. fallbacks: stale warm / mismatched date / non-empty uids / preferred_time
     / bad warm payload / empty slots / kill switch → live webhook
  3. warm fires on ok transition into ConfirmSlots + ConfirmSlots entry, per
     date (today + tomorrow), freeze-mode parity with the model's call shape
  (L3/C3 tests join in their own commits.)
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.graph.tools import ToolExecutor
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, rag_min_query_chars=0,
                    first_turn_lite=False,
                    # iter56: the C3 freeze pins below exercise the pre-history
                    # tail layout — the iter56 default (state rides the delta)
                    # makes the freeze write inert. Revert-mode pin.
                    state_in_delta=False)
# iter105 T4: the legacy dead-air filler pins below (L3) are now the ROLLBACK
# path — ASK_HOLD=false. With ask_hold ON the slots filler is a >=400 ms
# in-flight timer (single producer) and the empty-text round emits nothing.
LEGACY_SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                           langfuse_enabled=False, rag_min_query_chars=0,
                           first_turn_lite=False, state_in_delta=False,
                           ask_hold=False)
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())

TODAY = "2026-09-04"
WARM_PAYLOAD = {
    "ok": True,
    "slots": [{"day": TODAY, "time": f"{TODAY}T13:00:00",
               "reservation_uid": "2c4b75b4-546f-46fc-bb36-b3a600eb4c65",
               "day_human": "today", "time_human": "1 pm", "human": "today at 1 pm"}],
    "slot_reservation_uids": "2c4b75b4-546f-46fc-bb36-b3a600eb4c65",
    "slots_human": "today at 1 pm"}


def warm_store(today: str = TODAY) -> dict:
    return {today: {"payload": dict(WARM_PAYLOAD), "ts": time.monotonic(),
                    "tz": "America/New_York"}}


class NoHttp(Exception):
    pass


class ExplodingClient:
    """Any HTTP call fails the test — prefetch must answer without the wire."""

    async def post(self, *a, **k):
        raise NoHttp("prefetch must not hit the webhook on a warm hit")

    async def get(self, *a, **k):
        raise NoHttp("prefetch must not hit the webhook on a warm hit")


class RecordingClient:
    """Counts POSTs; serves a canned JSON body like MockTransport would."""

    def __init__(self, body: dict):
        self.body = body
        self.posts: list[dict] = []

    async def post(self, url=None, content=None, headers=None, **k):
        self.posts.append({"url": url, "content": content})
        return _FakeResp(self.body)

    async def get(self, *a, **k):
        raise AssertionError("GET not expected in these tests")

    async def aclose(self):
        pass


class _FakeResp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def make_executor(slots_warm, http_client=None, settings=None) -> ToolExecutor:
    return ToolExecutor(settings or SETTINGS, LLM_JSON, tracer=None,
                        http_client=http_client or ExplodingClient(),
                        slots_warm=slots_warm)


def run_slots_call(ex: ToolExecutor, args: dict):
    return asyncio.run(ex.execute("query_livecall_slots", args, {}, "ConfirmSlots"))


def run_turn(rt: CallRuntime, call_id: str, user_text: str, state: str | None = None,
             persona_dvs: dict | None = None, first: bool = True) -> dict:
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": user_text}
    if first:
        initial = rt.initial_state(call_id, persona_dvs)
        if state:
            initial["state_name"] = state
        payload.update(initial)

    async def go():
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    return asyncio.run(go())


# --------------------------------------------------------------------------- #
# L2 — slots prefetch (date-keyed, freeze-mode parity)
# --------------------------------------------------------------------------- #
def test_l2_warm_hit_no_http_and_patch():
    """Warm-hit answers without HTTP; response_variables land in dvs."""
    ex = make_executor(warm_store())
    out = run_slots_call(ex, {"account_id": "diallux_live",
                              "timezone": "America/New_York",
                              "slot_target_date": TODAY,
                              "current_reservation_uids": "",
                              "preferred_time": None})
    assert out.response.get("source") == "prefetch"
    assert out.response.get("ok") is True
    assert out.dvs_patch.get("requested_slot") == "today at 1 pm"
    assert out.dvs_patch.get("slot_reservation_uids") == \
        "2c4b75b4-546f-46fc-bb36-b3a600eb4c65"


def test_l2_stale_warm_goes_live():
    """>120s old → live webhook called, warm body NOT served."""
    client = RecordingClient({"ok": True, "slots": [{"day": TODAY}],
                              "slots_human": "today at 1 pm"})
    ex = make_executor({TODAY: {"payload": dict(WARM_PAYLOAD),
                                "ts": time.monotonic() - 200.0,
                                "tz": "America/New_York"}},
                       http_client=client)
    out = run_slots_call(ex, {"account_id": "diallux_live",
                              "timezone": "America/New_York",
                              "slot_target_date": TODAY})
    assert len(client.posts) == 1                       # live webhook fired
    assert out.response.get("source") is None           # not a prefetch answer


def test_l2_other_date_goes_live():
    """A date with no warm entry → live webhook."""
    client = RecordingClient({"ok": True, "slots": [{"day": "2026-09-05"}],
                              "slots_human": "tomorrow at 1 pm"})
    ex = make_executor(warm_store(), http_client=client)
    run_slots_call(ex, {"account_id": "diallux_live", "timezone": "America/New_York",
                        "slot_target_date": "2026-09-05"})
    assert len(client.posts) == 1


def test_l2_no_date_goes_live():
    """A no-date call (endpoint answers no_start_time) → live webhook."""
    client = RecordingClient({"ok": False, "error": "no_start_time",
                              "message": "slot_target_date is required."})
    ex = make_executor(warm_store(), http_client=client)
    run_slots_call(ex, {"account_id": "diallux_live", "timezone": "America/New_York"})
    assert len(client.posts) == 1


def test_l2_nonempty_uids_goes_live():
    """current_reservation_uids non-empty → release-and-refreeze must go live."""
    client = RecordingClient({"ok": True, "slots": [{"day": TODAY}],
                              "slots_human": "today at 1 pm"})
    ex = make_executor(warm_store(), http_client=client)
    run_slots_call(ex, {"account_id": "diallux_live", "timezone": "America/New_York",
                        "slot_target_date": TODAY,
                        "current_reservation_uids": "uid1,uid2"})
    assert len(client.posts) == 1


def test_l2_preferred_time_goes_live():
    """A named-time ask must NOT be served stale warm data."""
    client = RecordingClient({"ok": True, "slots": [{"day": TODAY}],
                              "slots_human": "today at 1 pm"})
    ex = make_executor(warm_store(), http_client=client)
    run_slots_call(ex, {"account_id": "diallux_live", "timezone": "America/New_York",
                        "slot_target_date": TODAY, "preferred_time": "today at 1 pm"})
    assert len(client.posts) == 1


def test_l2_bad_warm_payload_goes_live():
    """Warm store holding an error body (ok=False) → live webhook."""
    client = RecordingClient({"ok": True, "slots": [{"day": TODAY}],
                              "slots_human": "today at 1 pm"})
    ex = make_executor({TODAY: {"payload": {"ok": False, "error": "x"},
                                "ts": time.monotonic(),
                                "tz": "America/New_York"}},
                       http_client=client)
    run_slots_call(ex, {"account_id": "diallux_live", "timezone": "America/New_York",
                        "slot_target_date": TODAY})
    assert len(client.posts) == 1


def test_l2_empty_warm_slots_goes_live():
    """ok=True but zero slots ('everything taken') must not be served stale."""
    client = RecordingClient({"ok": True, "slots": [{"day": TODAY}],
                              "slots_human": "today at 1 pm"})
    ex = make_executor({TODAY: {"payload": {"ok": True, "slots": [], "slots_human": ""},
                                "ts": time.monotonic(),
                                "tz": "America/New_York"}},
                       http_client=client)
    run_slots_call(ex, {"account_id": "diallux_live", "timezone": "America/New_York",
                        "slot_target_date": TODAY})
    assert len(client.posts) == 1


def test_l2_kill_switch_restores_live():
    """slots_prefetch=False → the warm store is ignored, always live."""
    client = RecordingClient({"ok": True, "slots": [{"day": TODAY}],
                              "slots_human": "today at 1 pm"})
    settings = Settings(openai_api_key="test", retell_api_key="test",
                        langfuse_enabled=False, rag_min_query_chars=0,
                        slots_prefetch=False, first_turn_lite=False)
    ex = make_executor(warm_store(), http_client=client, settings=settings)
    run_slots_call(ex, {"account_id": "diallux_live", "timezone": "America/New_York",
                        "slot_target_date": TODAY})
    assert len(client.posts) == 1


def test_l2_transition_into_confirmslots_warms():
    """An ok transition_to_ConfirmSlots fires the bg warm for today+tomorrow."""
    fake = FakeLLM([
        {"tokens": ["Perfect", ",", " let", "'s", " check", "."], "tool_calls": [
            tool_call("transition_to_ConfirmSlots", {}),
        ]},
        {"tokens": ["What", " day", " works", "?"],
         "match_system": "query_livecall_slots"},          # ConfirmSlots prompt
    ])
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    persona = {"contact_details_completed": True, "first_name": "Maria",
               "company_name": "RoofCo", "prospect_timezone": "America/New_York",
               "callback_number": "+15123120001", "phone_confirmed": True,
               "is_calling_best_number": True, "today_date": TODAY}
    state = run_turn(rt, "l2-warm", "yes, book me in", state="contact_details",
                     persona_dvs=persona)

    async def drain():
        for entry in list(rt._slots_warm.values()):
            task = entry.get("task")
            if task is not None:
                await asyncio.wait_for(task, timeout=2)
    asyncio.run(drain())
    assert state["state_name"] == "ConfirmSlots"        # transition taken
    tomorrow = (_date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()
    assert set(rt._slots_warm) == {TODAY, tomorrow}     # both days warmed
    for entry in rt._slots_warm.values():
        assert entry.get("payload", {}).get("ok") is True
        assert entry.get("payload", {}).get("slots")
        assert entry.get("tz") == "America/New_York"


def test_l2_deterministic_entry_skips_fresh_warm():
    """ConfirmSlots entry with fresh warm entries → no new warm tasks fired."""
    fake = FakeLLM([
        {"tokens": ["Let", "'s", " find", " a", " time", "."],
         "tool_calls": [tool_call("query_livecall_slots",
                                  {"account_id": "diallux_live",
                                   "timezone": "America/New_York",
                                   "slot_target_date": TODAY})]},
        {"tokens": ["I", " can", " do", " today", " at", " 1 pm", "."]},
    ])
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    tomorrow = (_date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()
    for d in (TODAY, tomorrow):
        rt._slots_warm[d] = {"payload": dict(WARM_PAYLOAD), "ts": time.monotonic(),
                             "tz": "America/New_York"}  # mutate in place (executor alias)
    state = run_turn(rt, "l2-entry", "as soon as possible", state="ConfirmSlots",
                     persona_dvs={"today_date": TODAY})
    for entry in rt._slots_warm.values():               # fresh warm → no task key
        assert "task" not in entry
    assert state["dvs"].get("requested_slot") == "today at 1 pm"   # served warm


# --------------------------------------------------------------------------- #
# T6 commit 5 — slot_target_date auto-fill
# --------------------------------------------------------------------------- #
def test_t6_prefill_from_last_successful_date():
    """Omitted date + a prior successful dated call → prefilled, live webhook
    receives the date, response tagged source=prefill (no no_start_time round)."""
    client = RecordingClient({"ok": True, "slots": [{"day": TODAY}],
                              "slots_human": "today at 1 pm",
                              "slot_reservation_uids": "u1,u2"})
    ex = make_executor({}, http_client=client)          # empty warm store
    # first: successful DATED call → _last_slots_date set
    run_slots_call(ex, {"account_id": "diallux_live", "timezone": "America/New_York",
                        "slot_target_date": TODAY})
    # then: the t55 shape — date omitted after the error round
    out = run_slots_call(ex, {"account_id": "diallux_live", "timezone": "America/New_York"})
    assert len(client.posts) == 2
    sent = json.loads(client.posts[-1]["content"])
    assert sent["args"]["slot_target_date"] == TODAY    # prefilled into the live call
    assert out.response.get("source") == "prefill"
    assert out.response.get("ok") is True               # no no_start_time


def test_t6_prefill_from_requested_slot_day_word():
    """No prior call but {{requested_slot}} starts with a day word → date via
    {{today_date}} (tomorrow→today+1)."""
    client = RecordingClient({"ok": True, "slots": [{"day": TODAY}],
                              "slots_human": "today at 1 pm",
                              "slot_reservation_uids": "u1,u2"})
    ex = make_executor({}, http_client=client)
    dvs = {"requested_slot": "tomorrow at 1 pm or 3 pm", "today_date": TODAY,
           "prospect_timezone": "America/New_York"}
    out = asyncio.run(ex.execute("query_livecall_slots",
                                 {"account_id": "diallux_live",
                                  "timezone": "America/New_York"}, dvs, "ConfirmSlots"))
    sent = json.loads(client.posts[0]["content"])
    assert sent["args"]["slot_target_date"] == (_date.fromisoformat(TODAY)
                                                + timedelta(days=1)).isoformat()
    assert out.response.get("source") == "prefill"


def test_t6_no_known_date_unchanged():
    """Nothing known → live webhook receives NO date (endpoint no_start_time
    path unchanged — correctness fallback)."""
    client = RecordingClient({"ok": False, "error": "no_start_time",
                              "message": "slot_target_date is required."})
    ex = make_executor({}, http_client=client)
    out = run_slots_call(ex, {"account_id": "diallux_live",
                              "timezone": "America/New_York"})
    sent = json.loads(client.posts[0]["content"])
    assert "slot_target_date" not in sent["args"]
    assert out.response.get("source") is None
    assert out.response.get("error") == "no_start_time"


def test_t6_prefill_only_after_ok_slots():
    """A FAILED dated call does not become the prefill source."""
    client = RecordingClient({"ok": False, "error": "no_availability_in_window",
                              "message": "No free slots in that window."})
    ex = make_executor({}, http_client=client)
    run_slots_call(ex, {"account_id": "diallux_live", "timezone": "America/New_York",
                        "slot_target_date": TODAY})
    assert ex._last_slots_date is None                  # failed call ≠ prefill source


# --------------------------------------------------------------------------- #
# L3 — one-trip transitions + deterministic filler (commit 2)
# --------------------------------------------------------------------------- #
def test_l3_one_trip_ok_transition():
    """Speech + transition_to_* ok → ONE LLM trip, turn done in the new state."""
    fake = FakeLLM([
        {"tokens": ["Perfect", ",", " moving", " on", "."], "tool_calls": [
            tool_call("transition_to_Booking", {}),
        ]},
        {"tokens": ["never", " reached"], "match_system": "NEVERMatchThis"},
    ])
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    persona = {"data_verified": True, "selected_time": "today at 1 pm",
               "prospect_timezone": "America/New_York"}
    state = run_turn(rt, "l3-1", "yes that's correct", state="VerifyLead",
                     persona_dvs=persona)
    assert len(fake.seen_systems) == 1                  # single trip
    assert state["turn_active"] is False
    assert state["state_name"] == "Booking"             # transition still taken


def test_l3_gate_failed_still_loops():
    """gate_failed transition → repair round preserved (2 trips, state holds)."""
    fake = FakeLLM([
        {"tokens": ["Moving", " on", "."], "tool_calls": [
            tool_call("transition_to_Booking", {}),
        ]},
        {"tokens": ["Fixing", " the", " data", " first", "."]},
    ])
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    persona = {"selected_time": "today at 1 pm",
               "prospect_timezone": "America/New_York"}   # data_verified MISSING
    state = run_turn(rt, "l3-2", "ok", state="VerifyLead", persona_dvs=persona)
    assert len(fake.seen_systems) == 2                  # looped into repair round
    assert state["state_name"] == "VerifyLead"          # gate held
    assert rt.executor.gate_rejections                  # the refusal is observable


def test_l3_empty_text_slots_round_speaks_filler():
    """Slots call with EMPTY text → engine speaks the prescribed filler via TTS
    while the webhook runs; the offer round still follows (result-dependent).
    iter105 T4: rollback path (ASK_HOLD=false) — see LEGACY_SETTINGS."""
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [
            tool_call("query_livecall_slots", {"account_id": "diallux_live",
                                               "timezone": "America/New_York"}),
        ]},
        {"tokens": ["I", " can", " do", " today", " at", " 1 pm", "."]},
    ])
    rt = CallRuntime(LEGACY_SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    tokens: list[str] = []

    async def go():
        config = {"configurable": {"thread_id": "l3-3"}}
        payload = rt.initial_state("l3-3")
        payload["state_name"] = "ConfirmSlots"
        payload["user_text"] = "as soon as possible"
        async for mode, d in rt.graph.astream(payload, config=config,
                                              stream_mode=["updates", "custom"]):
            if mode == "custom" and isinstance(d, dict) and "tts_token" in d:
                tokens.append(d["tts_token"])
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    spoken = "".join(tokens)
    assert "One moment while I check availability." in spoken
    assert len(fake.seen_systems) == 2                  # offer round still happens
    assert state["turn_active"] is False
    assert state["dvs"].get("requested_slot")           # slot data landed via mock


def test_l3_filler_no_repeat_within_call():
    """A second empty-text slots round in the same call does NOT repeat the line.
    iter105 T4: rollback path (ASK_HOLD=false) — see LEGACY_SETTINGS."""
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [
            tool_call("query_livecall_slots", {"account_id": "diallux_live",
                                               "timezone": "America/New_York"})]},
        {"tokens": ["I", " can", " do", " today", " at", " 1 pm", "."]},
        {"tokens": [], "tool_calls": [
            tool_call("query_livecall_slots", {"account_id": "diallux_live",
                                               "timezone": "America/New_York",
                                               "slot_target_date": "2026-09-05"})]},
        {"tokens": ["Tomorrow", " has", " 1 pm", " too", "."]},
    ])
    rt = CallRuntime(LEGACY_SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    tokens: list[str] = []

    async def go():
        config = {"configurable": {"thread_id": "l3-4"}}
        payload = rt.initial_state("l3-4")
        payload["state_name"] = "ConfirmSlots"
        payload["user_text"] = "as soon as possible"
        async for mode, d in rt.graph.astream(payload, config=config,
                                              stream_mode=["updates", "custom"]):
            if mode == "custom" and isinstance(d, dict) and "tts_token" in d:
                tokens.append(d["tts_token"])
        return (await rt.graph.aget_state(config)).values

    asyncio.run(go())
    spoken = "".join(tokens)
    assert spoken.count("One moment while I check availability.") == 1


# --------------------------------------------------------------------------- #
# C3 — frozen state block per turn (commit 3)
# iter56: these pins exercise the REVERT layout (state_in_delta=False) — the
# frozen pre-history tail is exactly what the freeze machinery serves there.
# Under the iter56 default the state rides the post-history delta and the
# prefix never carries it (pinned in test_iter56_state_delta.py).
# --------------------------------------------------------------------------- #
def test_c3_state_block_frozen_within_tool_round():
    """Tool round: round-2 tail bytes EQUAL round-1's even though the slots
    webhook wrote dvs between rounds (t28-class cache-kill). Steady-state
    slots turn (today_date known → no engine-fired prefetch unfreezes it)."""
    fake = FakeLLM([
        {"tokens": ["One", " moment", "."], "tool_calls": [
            tool_call("query_livecall_slots", {"account_id": "diallux_live",
                                               "timezone": "America/New_York"})]},
        {"tokens": ["I", " can", " do", " today", " at", " 1 pm", "."]},
    ])
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    persona = {"today_date": "2026-09-04"}
    run_turn(rt, "c3-1", "as soon as possible", state="ConfirmSlots",
             persona_dvs=persona)
    assert len(fake.seen_system_msgs) == 2
    t1 = fake.seen_system_msgs[0][-1]                   # trailing system msg = tail
    t2 = fake.seen_system_msgs[1][-1]
    assert t1 == t2                                     # byte-frozen across rounds
    assert "requested_slot" not in t1                   # mid-turn write did NOT leak


def test_c3_dvs_visible_next_turn():
    """Frozen within the turn; the NEXT turn's state block sees the new dvs."""
    fake = FakeLLM([
        {"tokens": ["One", " moment", "."], "tool_calls": [
            tool_call("query_livecall_slots", {"account_id": "diallux_live",
                                               "timezone": "America/New_York"})]},
        {"tokens": ["I", " can", " do", " today", " at", " 1 pm", "."]},
        {"tokens": ["Great", " —", " which", " works", "?"]},
    ])
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    persona = {"today_date": "2026-09-04"}
    run_turn(rt, "c3-2", "as soon as possible", state="ConfirmSlots",
             persona_dvs=persona)
    state = run_turn(rt, "c3-2", "sounds good", first=False)
    tail2 = fake.seen_system_msgs[-1][-1]
    assert "requested_slot: today at 1 pm or 2:30 pm" in tail2
    assert state["state_name"] == "ConfirmSlots"


def test_c3_kill_switch_restores_re_render():
    """frozen_state_block=False → old behavior: round-2 tail re-renders and
    DOES contain the mid-turn dvs write."""
    settings = Settings(openai_api_key="test", retell_api_key="test",
                        langfuse_enabled=False, rag_min_query_chars=0,
                        frozen_state_block=False, first_turn_lite=False)
    fake = FakeLLM([
        {"tokens": [], "tool_calls": [                   # silence guard → loop
            tool_call("extract_intake_details", {"inbound_channel": "referral"})]},
        {"tokens": ["Got", " it", "."]},
    ])
    rt = CallRuntime(settings, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    run_turn(rt, "c3-3", "saw your referral")
    t1 = fake.seen_system_msgs[0][-1]
    t2 = fake.seen_system_msgs[1][-1]
    assert t1 != t2                                     # re-rendered per round
    assert "inbound_channel: referral" in t2            # live dvs visible round 2


# --------------------------------------------------------------------------- #
# T6b v2 — Closer-scoped skip gate (closer_completed; commit 7)
# --------------------------------------------------------------------------- #
def test_closer_skip_gate_maria_replay():
    """Maria-replay: spoken end_call from CLOSER with closer_completed unset →
    refused once with repair; second attempt accepted (dampener escape)."""
    ex = make_executor({})
    out1 = asyncio.run(ex.execute("end_call", {}, {"livecall_agreed": None}, "Closer",
                                  spoken_text="You'll get a confirmation text. Anything else?"))
    assert out1.response.get("status") == "end_call_blocked"
    assert "closer_completed" in out1.response["message"]
    out2 = asyncio.run(ex.execute("end_call", {}, {"livecall_agreed": None}, "Closer",
                                  spoken_text="You'll get a confirmation text. Anything else?"))
    assert out2.response.get("status") == "call_ended"
    assert out2.ended


def test_closer_skip_gate_completed_flag_passes():
    """closer_completed set (the state did its job) → end_call NOT gated."""
    ex = make_executor({})
    out = asyncio.run(ex.execute("end_call", {}, {"livecall_agreed": None,
                                                  "closer_completed": True}, "Closer",
                                  spoken_text="Great — talk soon, bye!"))
    assert out.response.get("status") == "call_ended"
    assert out.ended


def test_closer_skip_gate_other_states_untouched():
    """Intake hostile exit + Closing goodbye contract: NOT gated (iter16/30/31
    contracts preserved — the three tests commit-7-v1 broke now pass)."""
    ex = make_executor({})
    hostile = asyncio.run(ex.execute("end_call", {}, {}, "Intake",
                                     spoken_text="Fine, whatever. Bye."))
    assert hostile.response.get("status") == "call_ended"
    closing = asyncio.run(ex.execute("end_call", {}, {}, "Closing",
                                     spoken_text="Thanks, goodbye!"))
    assert closing.ended
