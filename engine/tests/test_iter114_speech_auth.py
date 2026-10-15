"""iter114 — speech authorization layer pins.

Design: tasks/surgeon/iter114-speech-auth/04_master_plan.md (owner GO 2026-10-13).
The mouth is a RENDERER: the engine derives facts (T1), resolves inputs (T2),
hands it a WIT card (T3), curates its payload (T4), and mechanically blocks
unauthorized figures/times/status claims (T5). Telemetry tells the truth (T7);
the sanitizer drops CJK glyph garbage (T6). All flags code-default ON.

Pins:
  1. T6 sanitizer: CJK punctuation drops the sentence; accents survive.
  2. T2 resolve_calls table: digits / word numbers / call_volume "…week" /
     daily ×5 / non-week call_volume ignored / normalize=False legacy.
  3. T2 model toolset: calculate_monthly_leak filtered from EVERY schema
     (engine-only); _DEFER_SET shrunk to the two booking-visible tools.
  4. T1 _derive: E5-shape inputs derive $3,375 (word-number path).
  5. T3 card: facts, NOT-COMPUTED marker, booking flag, sanctioned line.
  6. T4 speak view: tool frames stripped; user/assistant text kept; delta last.
  7. T5 guard unit table: fabrication cut reasons; authorized mirroring passes.
  8. T5 e2e: a fabricated figure never reaches the TTS stream; ONE recovery
     line; history keeps the model's original text (speech-only containment).
  9. T5 e2e: an authorized figure passes untouched.
 10. T7: the trace label names the SPEAK model on split rounds (and the
     openai model on single-call rounds); the delta rides the trace input.
 11. T3/T4 e2e: the entry-lite delta carries the card (replaces the raw
     state block) and still ends with the SPEECH-ONLY directive.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph import builder as B
from diallux.graph.llm import build_tool_schemas
from tests.fake_llm import FakeLLM
from tests.test_iter112_speak_first import (
    PERSONA, SpeakFirstFakeLLM, _make_rt, _run, _run_turns, _tts_tokens,
)

LLM_JSON = B.CallRuntime.__module__ and None  # placeholder to avoid import churn
from tests.test_iter112_speak_first import LLM_JSON  # noqa: E402


def _settings(**kw) -> Settings:
    base = dict(openai_api_key="test", retell_api_key="test",
                langfuse_enabled=False, first_turn_lite=False)
    base.update(kw)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# 1 — T6 sanitizer
# --------------------------------------------------------------------------- #
def test_sanitizer_drops_cjk_glyph_sentences():
    s = _settings()
    assert B._sanitize_spoken("Got it.】【。", s) is None
    assert B._sanitize_spoken("Ｈｅｌｌｏ.", s) is None          # fullwidth
    assert B._sanitize_spoken("Claro, ¿qué tal? Mañana a las dos.", s) \
        == "Claro, ¿qué tal? Mañana a las dos."                # accents kept


# --------------------------------------------------------------------------- #
# 2 — T2 resolve_calls
# --------------------------------------------------------------------------- #
def test_resolve_calls_table():
    from diallux.graph.tools import resolve_calls
    assert resolve_calls({"missed_calls_weekly": "20"}) == (20.0, "weekly")
    assert resolve_calls({"call_volume": "about fifteen missed calls a week"}) \
        == (15.0, "call_volume")
    assert resolve_calls({"call_volume": "twenty five a week"}) \
        == (25.0, "call_volume")
    assert resolve_calls({"call_volume": "a couple a week"}) \
        == (2.0, "call_volume")
    assert resolve_calls({"missed_calls_daily": "4"}) == (20.0, "daily")
    assert resolve_calls({"call_volume": "10-15 calls a day"})[0] != 10.0 \
        or resolve_calls({"call_volume": "10-15 calls a day"})[1] == ""
    import math
    v, src = resolve_calls({"call_volume": "10-15 calls a day"})
    assert src == "" and math.isnan(v)          # daily-phrased call_volume ignored
    v, src = resolve_calls({"missed_calls_weekly": "10", "call_volume": "about fifteen a week"})
    assert (v, src) == (10.0, "weekly")         # explicit weekly wins
    v, src = resolve_calls({"call_volume": "about fifteen a week"}, normalize=False)
    assert src == ""                            # rollback: legacy path


# --------------------------------------------------------------------------- #
# 3 — T2 engine-only tool + defers
# --------------------------------------------------------------------------- #
def test_compute_tool_filtered_from_every_schema():
    closer = [s for s in LLM_JSON["states"] if s["name"] == "Closer"][0]
    names = [t["name"] for t in closer.get("tools", [])]
    assert "calculate_monthly_leak" in names    # still in llm.json (JS parity)
    out = build_tool_schemas(closer, LLM_JSON.get("general_tools", []),
                             lambda x: x, {})
    got = [t["function"]["name"] for t in out]
    assert "calculate_monthly_leak" not in got  # never model-invoked
    assert "extract_leak_inputs" in got


def test_defer_set_shrunk_to_booking_visible():
    assert B._DEFER_SET == {"query_livecall_slots", "create_livecall_booking"}


# --------------------------------------------------------------------------- #
# 4 — T1 derive (the E5-shape)
# --------------------------------------------------------------------------- #
def test_derive_word_number_inputs_resolve():
    from diallux.graph.tools import leak_inputs_present
    rt = _make_rt(_settings(), FakeLLM([]))
    dvs = {"call_volume": "about fifteen missed calls a week",
           "close_rate_pct": 50, "avg_job_value": 450}
    assert leak_inputs_present(dvs) is True
    patch, fired = rt._derive(dvs)
    assert fired is True
    assert patch["weekly_leak"] == "3,375"      # 15 × 0.5 × 450
    assert patch["monthly_leak"] == "14,600"
    assert patch["missed_calls_weekly"] == 15


def test_derive_rollback_flag_ignores_volume_string():
    rt = _make_rt(_settings(leak_normalize=False), FakeLLM([]))
    dvs = {"call_volume": "about fifteen missed calls a week",
           "close_rate_pct": 50, "avg_job_value": 450}
    patch, fired = rt._derive(dvs)
    assert fired is False and patch == {}


# --------------------------------------------------------------------------- #
# 5 — T3 WIT card
# --------------------------------------------------------------------------- #
def test_wit_card_facts_and_flags():
    rt = _make_rt(_settings(), FakeLLM([]))
    dvs = {"first_name": "Danny", "company_name": "Precision Auto Care",
           "weekly_leak": "3,375", "monthly_leak": "14,600",
           "requested_slot": "tuesday, oct 20 at 2 pm or 2:30 pm",
           "selected_time": "2:30 pm", "close_rate_pct": 50}
    card = rt._wit_card("Closer", dvs, [])
    assert "# WHAT IS TRUE RIGHT NOW" in card
    assert "weekly $3,375" in card
    assert "slots offered: tuesday, oct 20 at 2 pm or 2:30 pm" in card
    assert "caller's chosen time: 2:30 pm (pending verification)" in card
    assert "booking: NOT CREATED yet" in card
    assert "current task: quantify the revenue leak" in card

    empty = rt._wit_card("Closer", {}, [])
    assert "NOT COMPUTED YET" in empty          # never estimate
    assert "booking: NOT CREATED yet" in empty

    booked = rt._wit_card("Closer", {"booking_uid": "u-1"}, [])
    assert "booking: CONFIRMED" in booked

    hist = [{"role": "tool",
             "content": '{"status":"ok","preferred_unavailable":true}'}]
    card2 = rt._wit_card("ConfirmSlots", {}, hist)
    assert "sanctioned line" in card2


# --------------------------------------------------------------------------- #
# 6 — T4 speak view
# --------------------------------------------------------------------------- #
def test_speak_view_strips_tool_frames():
    rt = _make_rt(_settings(), FakeLLM([]))
    hist = [
        {"role": "user", "content": "tomorrow works"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "query_livecall_slots",
                                      "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": '{"slots_human": "tomorrow at 1 pm or 2:30 pm"}'},
        {"role": "assistant", "content": "One moment while I check."},
    ]
    msgs = rt._speak_view("HEAD", hist, "CARD-DELTA")
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "system"]
    assert msgs[1]["content"] == "tomorrow works"
    assert msgs[2]["content"] == "One moment while I check."
    assert msgs[-1]["content"] == "CARD-DELTA"
    assert not any(m.get("role") == "tool" for m in msgs)


# --------------------------------------------------------------------------- #
# 7 — T5 guard unit table
# --------------------------------------------------------------------------- #
def test_speak_guard_cut_table():
    g = B._speak_guard_cut
    auth_t = {"$3375", "t230pm"}
    auth_n = {"3375", "450", "2", "30", "5123120001", "50"}
    dvs = {}
    # fabricated figure -> cut
    r = g("That's over $40,000 a year in recoverable jobs.", auth_t, auth_n,
          dvs, "")
    assert r is not None and r.startswith("number:") and "40000" in r
    # verified figure passes (cross-kind: dvs "3,375" authorizes "$3,375")
    assert g("that's about $3,375 walking out your door every week.",
             auth_t, auth_n, dvs, "") is None
    # caller's own number mirrors fine
    assert g("With a $450 average ticket, that's real money.",
             auth_t, auth_n, dvs, "") is None
    # small bare ints are exempt
    assert g("About two missed calls a day.", auth_t, auth_n, dvs, "") is None
    # phone readback authorized from the number
    assert g("5, 1, 2, 3, 1, 2, 0, 0, 0, 1 — correct?",
             auth_t, auth_n, dvs, "") is None
    # unauthorized phone-ish run -> cut
    assert g("9, 8, 7, 6, 5, 4, 3, 2, 1, 0 ok?",
             auth_t, auth_n, dvs, "") is not None
    # status claims gated on engine facts
    assert g("You're all set for the walkthrough.", auth_t, auth_n, {},
             "") == "booking-claim-without-booking"
    assert g("You're all set for the walkthrough.", auth_t, auth_n,
             {"booking_uid": "u1"}, "") is None
    assert g("2:30 pm was an available slot.", auth_t, auth_n, {},
             "not_offered:'tuesday, oct 20 at 2:30 pm'") \
        == "available-claim-over-not_offered"
    assert g("Is 2:30 pm still available?", auth_t, auth_n, {},
             "not_offered:'x'") is None          # 'is available' w/o subject+was
    assert g("2 pm ipm Central is the slot you chose.", auth_t, auth_n,
             {}, "") == "chose-claim-without-selection"
    assert g("the one pm slot just filled up.", auth_t, auth_n, {},
             "") == "unavailability-narrative-no-event"
    assert g("the one pm slot just filled up.", auth_t, auth_n, {},
             '{"preferred_unavailable": true}') is None


# --------------------------------------------------------------------------- #
# 8/9 — T5 e2e: cut + recovery / authorized pass
# --------------------------------------------------------------------------- #
def test_guard_e2e_fabrication_cut_and_recovery():
    fake = SpeakFirstFakeLLM(
        speak_rounds=[{"tokens": ["That's about $40,000 a year, easy."]}],
        tools_rounds=[{"tokens": []}],
    )
    rt = _make_rt(_settings(speak_first_pipeline=True), fake)
    custom, values = _run(rt, "s-g1", "sounds good", persona=dict(PERSONA))
    toks = _tts_tokens(custom)
    assert not any("$40,000" in t for t in toks)          # never spoken
    assert any("Let me run those numbers" in t for t in toks)   # recovery
    # history keeps the model's original text (speech-only containment)
    assert any("$40,000" in str(m.get("content") or "")
               for m in (values.get("history") or []))


def test_guard_e2e_authorized_passes_untouched():
    fake = SpeakFirstFakeLLM(
        speak_rounds=[{"tokens": ["That's about $3,375 a week."]}],
        tools_rounds=[{"tokens": []}],
    )
    rt = _make_rt(_settings(speak_first_pipeline=True), fake)
    persona = dict(PERSONA)
    persona["weekly_leak"] = "3,375"
    custom, _ = _run(rt, "s-g2", "show me", persona=persona)
    toks = _tts_tokens(custom)
    assert any("$3,375" in t for t in toks)
    assert not any("Let me run those numbers" in t for t in toks)


# --------------------------------------------------------------------------- #
# 10 — T7 telemetry truth
# --------------------------------------------------------------------------- #
class _TracerStub:
    def __init__(self):
        self.starts: list[dict] = []
        self.spans: list[dict] = []

    def start_llm(self, state, model, messages):
        self.starts.append({"state": state, "model": model,
                            "messages": list(messages)})
        return {"stub": True}

    def finish_llm(self, obs, **kw):
        pass

    def span(self, name, **kw):
        self.spans.append({"name": name, **kw})


def test_trace_label_names_the_speak_model():
    tracer = _TracerStub()
    fake = SpeakFirstFakeLLM(
        speak_rounds=[{"tokens": ["Hi there."]}],
        tools_rounds=[{"tokens": []}],
    )
    from tests.mock_webhooks import mock_client
    rt = B.CallRuntime(_settings(speak_first_pipeline=True,
                                 speak_model="gpt-4.1"),
                       LLM_JSON, tracer=tracer, llm=fake,
                       http_client=mock_client())
    _run(rt, "s-t1", "hello", persona=dict(PERSONA))
    assert tracer.starts, "start_llm must fire"
    assert tracer.starts[0]["model"] == "gpt-4.1"
    # the delta rides the trace input (the value the mouth saw)
    assert any(m.get("role") == "system" and "WHAT IS TRUE RIGHT NOW" in str(m.get("content"))
               for m in tracer.starts[0]["messages"]) \
        or any(m.get("role") == "system" and "CURRENT CALL STATE" in str(m.get("content"))
               for m in tracer.starts[0]["messages"])


def test_trace_label_single_call_uses_main_model():
    tracer = _TracerStub()
    fake = FakeLLM([{"tokens": ["Hi there."]}])
    from tests.mock_webhooks import mock_client
    rt = B.CallRuntime(_settings(speak_model="gpt-4.1"),
                       LLM_JSON, tracer=tracer, llm=fake,
                       http_client=mock_client())
    _run(rt, "s-t2", "hello", persona=dict(PERSONA))
    assert tracer.starts[0]["model"] == _settings().openai_model


# --------------------------------------------------------------------------- #
# 11 — T3/T4 entry-lite delta carries the card
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 12 — v1.1: word-sourced mirrors stay authorized (E7 false-cut class)
# --------------------------------------------------------------------------- #
def test_guard_word_mirror_authorization():
    rt = _make_rt(_settings(), FakeLLM([]))
    hist = [
        {"role": "user", "content": "Big jobs run four to five thousand."},
        {"role": "user", "content": "Average repair job is around four hundred fifty dollars."},
        {"role": "user", "content": "Tomorrow at one pm works. Yes, book it."},
    ]
    _, toks, nums = rt._speak_auth_sets({}, hist)
    assert "4000" in nums and "5000" in nums        # four/five thousand
    assert "450" in nums                            # four hundred fifty
    assert "t1pm" in toks                            # one pm
    # the E7 cut sentences now PASS
    assert B._speak_guard_cut(
        "Got it, your average repair is around $450, with larger jobs "
        "reaching $4,000 to $5,000.", toks, nums, {}, "") is None
    assert B._speak_guard_cut("I'll check tomorrow at 1 PM Central.",
                              toks, nums, {}, "") is None
    # a truly unspoken amount still bounces
    assert B._speak_guard_cut("That's $9,850 a month in lost work.",
                              toks, nums, {}, "") is not None


def test_card_carries_last_tool_outcome():
    rt = _make_rt(_settings(), FakeLLM([]))
    hist = [{"role": "tool", "content": json.dumps(
        {"ok": False, "error": "no_availability_in_window",
         "message": "No free slots in that window. Offer the next available day."})}]
    card = rt._wit_card("ConfirmSlots", {}, hist)
    assert "last tool result: No free slots in that window" in card


def test_entry_lite_delta_carries_card():
    fake = FakeLLM([
        {"tokens": ["Hi", " there", "."]},          # turn 1: first_turn_lite
        {"tokens": ["Got", " it", "."]},            # turn 2: state-entry lite
        {"tokens": ["And", " more", "."]},          # turn 3: heavy
    ])
    rt = _make_rt(_settings(first_turn_lite=True), fake)
    _run_turns(rt, "s-e1", ["hello?", "my phone rings off the hook", "tell me more"])
    ack_msgs = fake.seen_messages[1]
    last = ack_msgs[-1]
    assert last["role"] == "system"
    assert "# WHAT IS TRUE RIGHT NOW" in last["content"]    # the card rides
    assert "SPEECH ONLY" in last["content"]                 # directive still FINAL
    assert "CURRENT CALL STATE" not in last["content"]      # card REPLACED it
