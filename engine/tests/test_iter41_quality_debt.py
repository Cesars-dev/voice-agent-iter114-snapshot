"""iter41 — quality-debt regression tests (double-speak, filler stacks, mock date).

Commit 1 (T1 — sentence-level TTS dedupe) pins:
  1. in-turn exact-duplicate consecutive sentence is spoken ONCE (token stream)
  2. two DIFFERENT sentences pass through in order
  3. newline-joined double (the observed "…\n…" shape) dropped too
  4. kill switch off → doubles pass through (old behavior)
  5. final content / history keep the model's RAW output (dedupe is spoken-only)
(T2 filler cap / T3 mock date-awareness join in their own commits.)
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
from tests.mock_webhooks import mock_client, handler as mock_handler
from tests.mock_webhooks import BOOKING_UID

import httpx

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())

SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, rag_min_query_chars=0)

# t1 Marcus-shape: same sentence twice inside ONE round's token stream
DOUBLE = ["Got it", " — after", "-hours", ".", " Got", " it — after-hours."]


def make_rt(rounds: list[dict], settings: Settings | None = None):
    fake = FakeLLM(rounds)
    rt = CallRuntime(settings or SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    return rt, fake


def run_round(rt: CallRuntime, call_id: str, user_text: str,
              persona_dvs: dict | None = None):
    """One speak-only turn; returns (final_state, tts_tokens)."""
    tokens: list[str] = []

    async def go():
        config = {"configurable": {"thread_id": call_id}}
        payload = rt.initial_state(call_id, persona_dvs)
        payload["state_name"] = "Intake"
        payload["user_text"] = user_text
        async for mode, d in rt.graph.astream(payload, config=config,
                                              stream_mode=["updates", "custom"]):
            if mode == "custom" and isinstance(d, dict) and "tts_token" in d:
                tokens.append(d["tts_token"])
        return (await rt.graph.aget_state(config)).values

    return asyncio.run(go()), tokens


def last_assistant_text(state: dict) -> str:
    return [m for m in state["history"] if m.get("role") == "assistant"][-1]["content"]


# --------------------------------------------------------------------------- #
# T1 — sentence-level TTS dedupe (commit 1)
# --------------------------------------------------------------------------- #
def test_t1_double_sentence_spoken_once():
    """Exact-duplicate consecutive sentence inside one turn → spoken once."""
    rt, fake = make_rt([{"tokens": DOUBLE}])
    state, tokens = run_round(rt, "t1-1", "yeah sure")
    spoken = "".join(tokens)
    assert spoken.count("Got it — after-hours.") == 1
    # raw model output is preserved in history (spoken stream ≠ record)
    assert last_assistant_text(state) == "Got it — after-hours. Got it — after-hours."
    assert len(fake.seen_systems) == 1                    # no extra trips


def test_t1_different_sentences_pass_through():
    """Two different sentences: both spoken, order preserved."""
    rt, _ = make_rt([{"tokens": ["First", " sentence", ".",
                                 " Second", " one", "."]}])
    _, tokens = run_round(rt, "t1-2", "ok")
    spoken = "".join(tokens)
    assert "First sentence." in spoken
    assert "Second one." in spoken
    assert spoken.index("First sentence.") < spoken.index("Second one.")


def test_t1_newline_joined_double_dropped():
    """The observed Langfuse shape ('Got it — after-hours…\\nGot it — …') —
    newline is a sentence boundary; the joined double is dropped."""
    rt, _ = make_rt([{"tokens": ["Got it — after-hours…\nGot it — after-hours…"]}])
    _, tokens = run_round(rt, "t1-3", "ok")
    assert "".join(tokens).count("Got it — after-hours…") == 1


def test_t1_kill_switch_off_passes_doubles():
    """tts_dedupe_sentences=False → raw stream (iter40 behavior)."""
    s = SETTINGS.__class__(openai_api_key="test", retell_api_key="test",
                           langfuse_enabled=False, rag_min_query_chars=0,
                           tts_dedupe_sentences=False)
    rt, _ = make_rt([{"tokens": DOUBLE}], settings=s)
    _, tokens = run_round(rt, "t1-4", "ok")
    assert "".join(tokens).count("Got it — after-hours.") == 2


def test_t1_final_content_unchanged():
    """The 'final' event content (what history/records see) keeps the double."""
    rt, _ = make_rt([{"tokens": DOUBLE}])
    state, _ = run_round(rt, "t1-5", "ok")
    assert last_assistant_text(state) == "Got it — after-hours. Got it — after-hours."
    assert state["last_spoken"] == "Got it — after-hours. Got it — after-hours."


def test_t1_cross_round_double_dropped():
    """The Danny t10 luna shape: round 2 of the SAME turn repeats round 1's
    closing sentence → spoken once (dedupe window carried via tts_sentence_window)."""
    SLOTS = {"account_id": "diallux_live", "timezone": "America/Chicago"}
    rt, fake = make_rt([
        {"tokens": ["I’ll check", " the openings", " with you."],
         "tool_calls": [ {"id": "c1", "name": "query_livecall_slots",
                          "arguments": json.dumps(SLOTS)} ]},
        {"tokens": ["I’ll check", " the openings", " with you.", " Sure", "."]},
    ])
    persona = {"prospect_timezone": "America/Chicago"}
    tokens: list[str] = []

    async def go():
        config = {"configurable": {"thread_id": "t1-6"}}
        payload = rt.initial_state("t1-6", persona)
        payload["state_name"] = "ConfirmSlots"
        payload["user_text"] = "as soon as possible"
        async for mode, d in rt.graph.astream(payload, config=config,
                                              stream_mode=["updates", "custom"]):
            if mode == "custom" and isinstance(d, dict) and "tts_token" in d:
                tokens.append(d["tts_token"])
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    spoken = "".join(tokens)
    assert spoken.count("I’ll check the openings with you.") == 1
    assert len(fake.seen_systems) == 2                 # two rounds happened
    assert state["tts_sentence_window"] == ["i’ll check the openings with you.",
                                            "sure."]   # window carried for next round


# --------------------------------------------------------------------------- #
# T2 — filler cap: max ONE engine filler per deterministic pass (commit 2)
# --------------------------------------------------------------------------- #
PERSONA = {"first_name": "Maria", "last_name": "Gonzales",
           "company_name": "Bright Smile Dental", "prospect_timezone": "America/Chicago",
           "callback_number": "+13124001234"}

CHAIN_ROUND = {"tokens": [], "tool_calls": [
    tool_call("extract_confirm_details", {"selected_time": "2026-09-04T14:30:00"}),
    tool_call("validate_lead", {"first_name": "Maria", "last_name": "Gonzales",
                                 "company_name": "Bright Smile Dental",
                                 "callback_number": "+13124001234",
                                 "prospect_timezone": "America/Chicago",
                                 "selected_time": "2026-09-04T14:30:00"}),
    tool_call("transition_to_VerifyLead", {"slot_verified": True}, "t1"),
]}


def run_phaseb(call_id: str, http: httpx.AsyncClient | None = None):
    """One ConfirmSlots turn that triggers the engine booking chain.
    iter105 T4: ROLLBACK lane (ASK_HOLD=false) — the filler pins below test
    the legacy iter41 T2 arm, which ask_hold=True replaces with the slots
    in-flight timer (single producer)."""
    fake = FakeLLM()
    fake.add_round(CHAIN_ROUND)
    fake.add_round({"tokens": ["Booked", "."]})
    rt = CallRuntime(Settings(openai_api_key="test", retell_api_key="test",
                              langfuse_enabled=False, rag_min_query_chars=0,
                              ask_hold=False),
                     LLM_JSON, tracer=None, llm=fake,
                     http_client=http or mock_client(), kb_store=None)
    custom: list[dict] = []

    async def go():
        config = {"configurable": {"thread_id": call_id}}
        payload = rt.initial_state(call_id, persona_dvs=PERSONA)
        payload["state_name"] = "ConfirmSlots"
        payload["user_text"] = "tomorrow works"
        async for mode, d in rt.graph.astream(payload, config=config,
                                              stream_mode=["custom", "updates"]):
            if mode == "custom":
                custom.append(d)
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    toks = [c["tts_token"].strip() for c in custom if "tts_token" in c]
    return state, [t for t in toks if t in _ENGINE_FILLERS]


def test_t2_multi_slow_step_chain_one_filler():
    """3 slow webhooks in ONE chain pass (verify, create, record_uid) →
    exactly ONE engine filler spoken (was 3 — the R5.4 robotic stack)."""
    state, fillers = run_phaseb("t2-1")
    assert len(fillers) == 1
    assert fillers[0] in _ENGINE_FILLERS
    assert state["dvs"]["booking_verified"] is True         # chain completed


def test_t2_abort_pass_at_most_one_filler():
    """A chain aborted at the first slow step speaks ≤1 filler."""
    def h(request: httpx.Request) -> httpx.Response:
        if "/validator-function/verify-lead-data" in str(request.url):
            return httpx.Response(200, json={
                "ok": True, "status": "incomplete", "data_verified": False,
                "problems": [{"field": "selected_time", "issue": "unfixable_format"}],
                "actions": ["Say: 'Sorry, I lost track - which slot did you pick?'"]})
        return mock_handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(h))
    state, fillers = run_phaseb("t2-2", http=http)
    assert len(fillers) <= 1
    assert state["dvs"].get("chain_done") is False          # latch popped on abort


# --------------------------------------------------------------------------- #
# T3 — mock date-aware check_availability (commit 3)
# --------------------------------------------------------------------------- #
async def _post(path: str, args: dict) -> dict:
    async with httpx.AsyncClient(
            transport=httpx.MockTransport(mock_handler)) as http:
        r = await http.post(path, content=json.dumps({"args": args}))
    return r.json()


def test_t3_tomorrow_query_serves_tomorrow():
    """slot_target_date=tomorrow → slots_human starts 'tomorrow at', day
    fields carry the DATE (live endpoint lockstep — the Brenda fix)."""
    body = asyncio.run(_post("https://x/check_availability", {
        "account_id": "diallux_live", "timezone": "America/Chicago",
        "slot_target_date": "2026-09-05", "current_reservation_uids": ""}))
    assert body["ok"] is True
    assert body["slots_human"].startswith("tomorrow at")
    assert body["slots_human"] == "tomorrow at 1 pm or 2:30 pm"
    assert all(s["day"] == "2026-09-05" for s in body["slots"])
    assert all(s["day_human"] == "tomorrow" for s in body["slots"])
    assert body["slots"][1]["human"] == "tomorrow at 2:30 pm"


def test_t3_no_date_serves_today_legacy():
    """No slot_target_date → today (unchanged legacy fixture shape)."""
    body = asyncio.run(_post("https://x/check_availability", {
        "account_id": "diallux_live", "timezone": "America/Chicago"}))
    assert body["slots_human"] == "today at 1 pm or 2:30 pm"
    assert all(s["day"] == "2026-09-04" for s in body["slots"])


def test_t3_weekday_word_beyond_tomorrow():
    """+4 days renders the weekday name (live endpoint day-word logic)."""
    body = asyncio.run(_post("https://x/check_availability", {
        "account_id": "diallux_live", "timezone": "America/Chicago",
        "slot_target_date": "2026-09-08", "current_reservation_uids": ""}))
    assert body["slots_human"] == "tuesday at 1 pm or 2:30 pm"


def test_t3_brenda_replay_contained():
    """The Brenda mechanism: agent re-queries (mock now serves tomorrow),
    model picks 'tomorrow at 2:30 pm' → verify_lead_data containment gate
    PASSES — no unfixable_format, no repair loop, conversion completes."""
    body = asyncio.run(_post("https://x/validator-function/verify-lead-data", {
        "selected_time": "tomorrow at 2:30 pm",
        "slot_options": "tomorrow at 1 pm or 2:30 pm",
        "first_name": "Brenda", "callback_number": "+15550001111",
        "prospect_timezone": "America/Chicago"}))
    assert body["status"] == "ok"
    assert body["data_verified"] is True
    assert not any(p.get("issue") == "unfixable_format" for p in body["problems"])
