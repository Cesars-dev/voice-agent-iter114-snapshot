"""iter112 — speak-first staggered pipeline + clock port.

plan_v5_iter112_speak_first_pipeline.md (owner design 2026-10-13): every
tool-eligible heavy round fires a BARE speech-only astream first (tools=[] —
"we don't need tools to stream audio we already decided we're going to
speak") while a gpt-5.4 TOOLS astream runs concurrently as a BACKGROUND task
(text discarded; its final's tool_calls are the only tool source). Merge =
await-on-arrival, NO caps, at three points:
  M1  tools land while the turn is still open -> execute EXACTLY as today;
      the natural follow-up round speaks the tool-informed answer.
  M2  caller EOT first -> the next turn's ingest AWAITS the tools future
      before the build; the next SPEAK payload carries the utterance + the
      tool results in one shot.
  M3  end_call/close -> awaited/cancelled cleanly; no orphan tasks.
Flag `speak_first_pipeline` (default False = byte-exact original call site).
E2 (speak_model="gpt-4.1") is a config-only edit; the speak twin carries NO
verbosity/reasoning/service_tier kwargs (MODEL LAW exception, owner order).

Pins:
  1. flag OFF -> exactly ONE astream call per round, full tool array
     (the ORIGINAL call site shape).
  2. flag ON  -> the SPEAK call is BARE (tools=[]); the background TOOLS
     task carries the full array; SAME messages both.
  3. M1: tools future lands while the turn is open -> its tool_calls execute
     exactly as today (executed/history/transition), the follow-up round's
     payload carries the tool results.
  4. M2: caller EOT first -> the next turn's build awaits the pending future;
     the next payload carries the utterance + tool results in one shot.
  5. M3: close with a pending future -> cancelled, no orphan task, no hang.
  6. history assistant text = the SPEAK stream only (tools-stream text never
     lands).
  7. static: NO asyncio.wait_for / timeout caps bound the tools merges.
  8. speak_model="" -> both streams ride the same LLM.
  9. speak_model="gpt-4.1" -> the speak LLM is 4.1 WITHOUT verbosity (no
     reasoning/service_tier either); the tools LLM is unchanged.
  10. divergence WARNs: speak-empty+tools; booked-claim speech with no tools.
  11. clock: llm_dispatch once per round before first_event; llm_first_event
     from the SPEAK stream only (never the tools consumer); TurnClock report
     carries the ported keys.
  12. lite / entry-lite / channel-closed (tools=[]) rounds never enter the
     speak-first pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.graph.llm import StreamingLLM
from diallux.observability.latency import TurnClock
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())

PERSONA = {"first_name": "Maria", "last_name": "Gonzales",
           "company_name": "Bright Smile Dental",
           "prospect_timezone": "America/Chicago",
           "callback_number": "+13124001234"}


def _settings(first_turn_lite=False, **kw) -> Settings:
    base = dict(openai_api_key="test", retell_api_key="test",
                langfuse_enabled=False, first_turn_lite=first_turn_lite)
    base.update(kw)
    return Settings(**base)


class CountingFakeLLM(FakeLLM):
    """FakeLLM + a record of every astream call (tools names + messages)."""

    def __init__(self, rounds=None):
        super().__init__(rounds)
        self.calls: list[dict] = []

    async def astream(self, messages, tools, verbosity="", speak=False,
                      speak_model: str = ""):
        self.calls.append({
            "tools": [t["function"]["name"] for t in tools],
            "messages": list(messages),
            "speak": speak,
            "speak_model": speak_model,
        })
        async for ev in super().astream(messages, tools, verbosity=verbosity):
            yield ev


class SpeakFirstFakeLLM:
    """Routes astream by the `speak` kwarg: speak calls pop `speak_rounds`,
    everything else pops `tools_rounds`. Each script may carry `delay`
    (seconds before the first yield) — used to control the race between the
    foreground SPEAK loop and the background TOOLS task:
      speak delay > tools delay  -> tools land mid-round  (M1)
      tools delay > speak delay  -> tools pending at round close (M2/M3)."""

    def __init__(self, speak_rounds, tools_rounds):
        self.speak_rounds = list(speak_rounds)
        self.tools_rounds = list(tools_rounds)
        self.calls: list[dict] = []

    async def astream(self, messages, tools, verbosity="", speak=False,
                      speak_model: str = ""):
        kind = "speak" if speak else "tools"
        queue = self.speak_rounds if speak else self.tools_rounds
        script = queue.pop(0) if queue else {"tokens": [], "tool_calls": []}
        self.calls.append({"kind": kind, "speak": speak,
                           "speak_model": speak_model,
                           "tools": [t["function"]["name"] for t in tools],
                           "messages": list(messages)})
        delay = float(script.get("delay") or 0.0)
        if delay:
            await asyncio.sleep(delay)
        for tok in script.get("tokens", []):
            yield {"type": "token", "text": tok}
        for tc in script.get("tool_calls", []):
            yield {"type": "tool_call_start", "name": tc["name"]}
        usage = script.get("usage") or {"input_tokens": 100, "output_tokens": 20,
                                        "total_tokens": 120}
        yield {"type": "final", "content": "".join(script.get("tokens", [])),
               "tool_calls": script.get("tool_calls", []), "usage": usage}


async def _run_turn(rt: CallRuntime, call_id: str, text: str,
                    first: bool = False, persona: dict | None = None,
                    state: str | None = None):
    """One user turn on ONE shared event loop session; returns (custom, values)."""
    config = {"configurable": {"thread_id": call_id}}
    payload: dict = {"user_text": text}
    if first:
        payload.update(rt.initial_state(call_id, persona))
    if state:
        payload["state_name"] = state
    custom: list[dict] = []
    async for mode, data in rt.graph.astream(payload, config=config,
                                             stream_mode=["custom", "updates"]):
        if mode == "custom" and isinstance(data, dict):
            custom.append(data)
    values = (await rt.graph.aget_state(config)).values
    return custom, values


def _run(rt: CallRuntime, call_id: str, text: str, persona: dict | None = None,
         state: str | None = None):
    async def go():
        return await _run_turn(rt, call_id, text, first=True, persona=persona,
                               state=state)
    return asyncio.run(go())


def _run_turns(rt: CallRuntime, call_id: str, texts: list[str],
               persona: dict | None = None, state: str | None = None):
    async def go():
        out = []
        for i, t in enumerate(texts):
            out.append(await _run_turn(rt, call_id, t, first=(i == 0),
                                       persona=persona,
                                       state=state if i == 0 else None))
        return out
    return asyncio.run(go())


def _trace_names(rt: CallRuntime) -> list[str]:
    return [e["tool"] for e in rt.executor.trace]


def _tts_tokens(custom: list[dict]) -> list[str]:
    return [e["tts_token"] for e in custom if "tts_token" in e]


def _spy_warm(rt: CallRuntime) -> list[dict]:
    calls: list[dict] = []

    def spy(dest, history=None, dvs=None, lite=False, force=False):
        calls.append({"dest": dest, "force": force, "lite": lite})

    rt.warm_prompt_cache = spy
    return calls


def _assistant_tool_names(msgs: list[dict]) -> list[str]:
    return [tc["function"]["name"]
            for m in msgs
            if m.get("role") == "assistant" and m.get("tool_calls")
            for tc in m["tool_calls"]]


def _make_rt(settings: Settings, fake) -> CallRuntime:
    return CallRuntime(settings, LLM_JSON, tracer=None, llm=fake,
                       http_client=mock_client())


# --------------------------------------------------------------------------- #
# 1 — flag OFF: the ORIGINAL single-astream call site
# --------------------------------------------------------------------------- #
def test_flag_off_single_astream_full_tools():
    fake = CountingFakeLLM([
        {"tokens": ["Hi", " there", "."],
         "tool_calls": [tool_call("extract_intake_details", {"caller_name": "Kim"})]},
    ])
    rt = _make_rt(_settings(), fake)
    _run(rt, "s-off-1", "this is Kim")
    assert len(fake.calls) == 1                      # ONE stream (flag off)
    assert fake.calls[0]["speak"] is False
    assert "extract_intake_details" in fake.calls[0]["tools"]   # FULL array


# --------------------------------------------------------------------------- #
# 2/6 — flag ON: bare speak + full tools, same messages, speak-only history
# --------------------------------------------------------------------------- #
def test_flag_on_bare_speak_full_tools_same_messages():
    fake = SpeakFirstFakeLLM(
        speak_rounds=[{"tokens": ["Hi", " there", "."], "delay": 0.01}],
        tools_rounds=[{"tokens": ["TOOLSIDE-NEVER-SPOKEN"],
                       "tool_calls": [tool_call("extract_intake_details",
                                                {"caller_name": "Kim"})]}],
    )
    rt = _make_rt(_settings(speak_first_pipeline=True), fake)
    custom, values = _run(rt, "s-on-1", "this is Kim")

    kinds = [c["kind"] for c in fake.calls]
    assert kinds == ["speak", "tools"]               # two streams, one round
    speak_call = fake.calls[0]
    tools_call = fake.calls[1]
    assert speak_call["tools"] == []                 # BARE (owner order)
    assert "extract_intake_details" in tools_call["tools"]   # FULL array
    # iter114 T4 (speak diet): the two views share the system head but NOT the
    # full payload — the mouth gets conversation + the WIT card delta; the
    # TOOLS stream keeps full fidelity. With speak_diet/speak_card OFF the
    # messages are byte-identical (the original iter112 contract).
    assert speak_call["messages"][0] == tools_call["messages"][0]   # same head
    assert speak_call["messages"] != tools_call["messages"]         # diet ON

    # 6 — speech pipeline got ONLY the speak tokens; tools text never lands
    toks = _tts_tokens(custom)
    assert toks == ["Hi", " there", "."]
    assert not any("TOOLSIDE" in t for t in toks)
    assert "TOOLSIDE" not in json.dumps(values.get("history") or [])

    # M1 adopted (tools done while the turn was open): executed exactly as today
    assert [t["name"] for t in (values.get("last_round_tool_calls") or [])] == \
        ["extract_intake_details"]
    assistant = next(m for m in reversed(values.get("history") or [])
                     if m.get("role") == "assistant" and m.get("tool_calls"))
    assert assistant["content"] == "Hi there."       # SPEAK content wins


# --------------------------------------------------------------------------- #
# 3 — M1: the follow-up round's payload carries the tool results
# --------------------------------------------------------------------------- #
def test_m1_tools_land_in_round_followup():
    fake = SpeakFirstFakeLLM(
        speak_rounds=[
            {"tokens": ["Let me check that for you."], "delay": 0.01},
            {"tokens": ["I can do tomorrow at 1 pm or 2:30 pm."], "delay": 0.01},
        ],
        tools_rounds=[
            {"tool_calls": [tool_call("query_livecall_slots",
                                      {"slot_target_date": "2026-09-05"})]},
            {"tool_calls": []},
        ],
    )
    rt = _make_rt(_settings(speak_first_pipeline=True), fake)
    custom, values = _run(rt, "s-m1-1", "tomorrow works", persona=PERSONA,
                          state="ConfirmSlots")

    kinds = [c["kind"] for c in fake.calls]
    assert kinds == ["speak", "tools", "speak", "tools"]   # follow-up round ran
    # iter114 T4: the SPEAK view never carries raw tool frames (the mouth
    # cannot argue with machine verdicts); the TOOLS view still does.
    msgs2 = fake.calls[2]["messages"]                 # round-2 SPEAK payload
    assert not any(m.get("role") == "tool" for m in msgs2)
    msgs2_tools = fake.calls[3]["messages"]           # round-2 TOOLS payload
    assert any(m.get("role") == "tool" for m in msgs2_tools)
    assert "query_livecall_slots" in _assistant_tool_names(msgs2_tools)
    # executed in-round (exactly as today) — the slot pull really ran
    assert "query_livecall_slots" in _trace_names(rt)
    hist = values.get("history") or []
    assert "query_livecall_slots" in _assistant_tool_names(hist)
    # the follow-up round spoke the TOOL-INFORMED answer
    assert any("2:30" in t for t in _tts_tokens(custom))

    # clock (pin 11): one dispatch + one first_event PER ROUND, speak stream only
    # (the tools consumers never stamped -> counts equal the round count)
    dispatched = [e for e in custom if e.get("llm_dispatch")]
    first_events = [e for e in custom if e.get("llm_first_event")]
    assert len(dispatched) == 2 and len(first_events) == 2


# --------------------------------------------------------------------------- #
# 4 — M2: caller EOT first -> next turn's ingest awaits the future
# --------------------------------------------------------------------------- #
def test_m2_next_turn_build_awaits_tools(caplog):
    fake = SpeakFirstFakeLLM(
        speak_rounds=[
            {"tokens": ["Got it."]},                              # turn 1
            {"tokens": ["After-hours calls are the leak."], "delay": 0.01},
        ],
        tools_rounds=[
            {"tool_calls": [tool_call("transition_to_Discovery", {}, "t1")],
             "delay": 0.3},                                       # outlives turn 1
            {"tool_calls": []},
        ],
    )
    rt = _make_rt(_settings(speak_first_pipeline=True), fake)
    warms = _spy_warm(rt)
    with caplog.at_level(logging.INFO, logger="diallux.graph"):
        (_, v1), (_, v2) = _run_turns(
            rt, "s-m2-1", ["this is Kim", "sounds fine"],
            persona={"inbound_channel": "Facebook/Meta ad",
                     "interest_topic": "missed calls",
                     "pain_frame": "losing callers",
                     "intake_completed": True})

    # turn 1 deferred: no tools executed, future kept pending
    assert not [t for t in (v1.get("last_round_tool_calls") or [])]
    assert rt._pending_tools is None                  # consumed by M2
    # M2 ran before the build: state moved + the deferred calls executed
    assert v2.get("state_name") == "Discovery"
    assert any("split M2:" in r.message for r in caplog.records)
    # destination warm fired from M2 (transition side effect)
    assert any(w["dest"] == "Discovery" and w["force"] for w in warms)

    # turn-2 payloads carry the user utterance + the tool results in one shot
    assert len(fake.calls) >= 3                       # turn 2 ran
    msgs = fake.calls[2]["messages"]                  # turn-2 SPEAK payload
    user_idx = max(i for i, m in enumerate(msgs) if m.get("role") == "user")
    tool_idx = max(i for i, m in enumerate(msgs) if m.get("role") == "tool")
    assert tool_idx < user_idx                        # tools append before user
    assert "transition_to_Discovery" in _assistant_tool_names(msgs)
    # the deferred tool RESULT rides the same payload as the utterance
    defer_hist = v2.get("history") or []
    assert any(m.get("role") == "tool"
               and '"state": "Discovery"' in (m.get("content") or "")
               for m in defer_hist)


# --------------------------------------------------------------------------- #
# 5 — M3: close with a pending future -> cancelled, no orphan, no hang
# --------------------------------------------------------------------------- #
def test_m3_close_cancels_pending():
    fake = SpeakFirstFakeLLM(
        speak_rounds=[{"tokens": ["Hello."]}],
        tools_rounds=[{"tool_calls": [tool_call("query_livecall_slots", {})],
                       "delay": 5.0}],                # still pending at close
    )
    rt = _make_rt(_settings(speak_first_pipeline=True), fake)

    async def go():
        await _run_turn(rt, "s-m3-1", "hi", first=True)
        assert rt._pending_tools is not None and not rt._pending_tools.task.done()
        task = rt._pending_tools.task
        await rt.aclose()
        await asyncio.sleep(0.01)
        assert task.cancelled()
        assert rt._pending_tools is None

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# 7 — static: no wait_for / timeout caps on the tools merge paths
# --------------------------------------------------------------------------- #
def test_no_wait_for_in_split_merge_paths():
    import inspect
    from diallux.graph import builder as B
    for fn in (B.CallRuntime._fire_split_tools,
               B.CallRuntime._execute_deferred_tools,
               B.CallRuntime._split_close_pending):
        src = inspect.getsource(fn)
        assert "wait_for" not in src, fn.__name__
        assert "timeout" not in src, fn.__name__


# --------------------------------------------------------------------------- #
# 8/9 — speak-model routing (pin 8: "" same LLM; pin 9: gpt-4.1 no verbosity)
# --------------------------------------------------------------------------- #
class _ProbeLLM:
    def __init__(self):
        self.called = 0

    async def astream(self, messages):
        self.called += 1
        if False:
            yield None


def _drain(aiter):
    async def go():
        async for _ in aiter:
            pass
    asyncio.run(go())


def test_speak_model_empty_rides_same_llm():
    llm = StreamingLLM(_settings())
    assert llm._speak_llms == {}                     # no twin ever built
    probe = _ProbeLLM()
    llm._llm = probe
    _drain(llm.astream([], [], speak=True))
    assert probe.called == 1                         # speak rode the hot LLM


def test_speak_model_gpt41_no_verbosity_twin():
    llm = StreamingLLM(_settings(speak_model="gpt-4.1",
                                 openai_service_tier="fast"))
    sp = llm.speak_llm()
    assert sp.model_name == "gpt-4.1"
    assert sp.verbosity is None                      # MODEL LAW exception
    assert sp.reasoning_effort is None
    assert sp.service_tier is None                   # owner order: not on speak
    # tools LLM unchanged (gpt-5.4, verbosity + tier as configured)
    assert llm._llm.model_name == "gpt-5.4"
    assert llm._llm.verbosity == "medium"
    assert llm._llm.service_tier == "fast"
    # routing: speak=True rides the twin when set
    probe = _ProbeLLM()
    llm._speak_llms["gpt-4.1"] = probe
    _drain(llm.astream([], [], speak=True))
    assert probe.called == 1


def test_speak_model_luna_reasoning_effort_none():
    """Owner order 2026-10-13: luna (gpt-5.6) is the mouth. A gpt-5.x speak
    twin must carry reasoning_effort (default "none") — luna reasons by
    default and starves the speech stream on hidden reasoning."""
    llm = StreamingLLM(_settings(speak_model="gpt-5.6-luna",
                                 openai_service_tier="fast"))
    sp = llm.speak_llm()
    assert sp.model_name == "gpt-5.6-luna"
    assert sp.reasoning_effort == "none"             # visible text, fast
    assert sp.verbosity is None                      # stays off the twin
    assert sp.service_tier is None                   # owner order
    # tools LLM untouched
    assert llm._llm.model_name == "gpt-5.4"


# --------------------------------------------------------------------------- #
# 10 — divergence monitor rows
# --------------------------------------------------------------------------- #
def test_divergence_warn_mouth_empty(caplog):
    fake = SpeakFirstFakeLLM(
        speak_rounds=[{"tokens": [], "delay": 0.01},
                      {"tokens": ["Got it."]}],
        tools_rounds=[{"tool_calls": [tool_call("extract_intake_details",
                                                {"caller_name": "Kim"})]},
                      {"tool_calls": []}],
    )
    rt = _make_rt(_settings(speak_first_pipeline=True), fake)
    with caplog.at_level(logging.WARNING, logger="diallux.graph"):
        _run(rt, "s-div-1", "this is Kim")
    assert any("split_divergence" in r.message and "mouth-empty" in r.message
               for r in caplog.records)


def test_divergence_warn_booked_claim_no_tools(caplog):
    fake = SpeakFirstFakeLLM(
        speak_rounds=[{"tokens": ["Great,", " you're all booked",
                                  " for Thursday."], "delay": 0.01}],
        tools_rounds=[{"tool_calls": []}],
    )
    rt = _make_rt(_settings(speak_first_pipeline=True), fake)
    with caplog.at_level(logging.WARNING, logger="diallux.graph"):
        _run(rt, "s-div-2", "sounds good")
    assert any("split_divergence" in r.message
               and "booked-claim-no-tools" in r.message
               for r in caplog.records)


# --------------------------------------------------------------------------- #
# 12 — lite / entry-lite / channel-closed rounds never enter the pipeline
# --------------------------------------------------------------------------- #
def test_lite_and_entry_lite_never_split():
    fake = SpeakFirstFakeLLM(
        speak_rounds=[],
        tools_rounds=[{"tokens": ["Hello", " there", "."]},   # turn-1 lite
                      {"tokens": ["Of course."]}],            # turn-2 entry_lite
    )
    rt = _make_rt(_settings(speak_first_pipeline=True, first_turn_lite=True),
                  fake)
    _run_turns(rt, "s-lite-1", ["hello?", "yes please"])
    assert len(fake.calls) == 2                      # 1 stream per lite round
    assert all(c["kind"] == "tools" for c in fake.calls)   # never a SPEAK arm


def test_channel_closed_round_never_splits():
    fake = SpeakFirstFakeLLM(
        speak_rounds=[{"tokens": [], "delay": 0.01}],         # silent mouth
        tools_rounds=[{"tool_calls": [tool_call("extract_intake_details",
                                                {"caller_name": "Kim"})]},
                      {"tokens": ["Okay, got it."]}],         # forced-speech rd
    )
    rt = _make_rt(_settings(speak_first_pipeline=True,
                            force_speech_keep_tools=False), fake)
    custom, _ = _run(rt, "s-cc-1", "this is Kim")
    assert [c["kind"] for c in fake.calls] == ["speak", "tools", "tools"]
    assert fake.calls[2]["tools"] == []              # channel closed -> single
    assert fake.calls[2]["speak"] is False
    assert "Okay, got it." in "".join(_tts_tokens(custom))


# --------------------------------------------------------------------------- #
# 13 — repeat guard across cancelled generations (E3 booking-killer)
# --------------------------------------------------------------------------- #
def test_repeat_guard_reset_releases_reexecution():
    rt = _make_rt(_settings(), CountingFakeLLM([]))
    ex = rt.executor
    args = {"timezone": "America/Chicago", "account_id": "diallux_live",
            "slot_target_date": "2026-10-14", "current_reservation_uids": "",
            "preferred_time": ""}

    async def go():
        dvs: dict = {}
        a = await ex.execute("query_livecall_slots", dict(args), dvs,
                             "ConfirmSlots")
        assert a.response.get("ok") is True              # first: live mock slots
        b = await ex.execute("query_livecall_slots", dict(args), dvs,
                             "ConfirmSlots")
        assert b.response.get("ok") is True, \
            "no requested_slot stored -> the guard must release and re-execute"
        ex.reset_repeat_guard()
        c = await ex.execute("query_livecall_slots", dict(args), dvs,
                             "ConfirmSlots")
        assert c.response.get("ok") is True              # reset -> live again
        # values REALLY stored -> the dedupe still holds
        dvs2 = {"requested_slot": "friday at 1 pm or 1:30 pm"}
        await ex.execute("query_livecall_slots", dict(args), dvs2, "ConfirmSlots")
        e = await ex.execute("query_livecall_slots", dict(args), dvs2,
                             "ConfirmSlots")
        assert e.response.get("status") == "already_captured"

    asyncio.run(go())


def test_session_cancel_resets_repeat_guard():
    src = (ROOT / "diallux" / "media" / "session.py").read_text()
    # the turn-runner CancelledError path (cancelled generation) must release
    # the executor guard for the retry turn
    assert "reset_repeat_guard" in src
    i_cancel = src.index("except asyncio.CancelledError:")
    i_reset = src.index("reset_repeat_guard", i_cancel)
    i_return = src.index("return", i_reset)
    assert i_cancel < i_reset < i_return


# --------------------------------------------------------------------------- #
# 11 — clock port: TurnClock keys + session stamp helper + builder events
# --------------------------------------------------------------------------- #
def test_health_carries_speak_first_flag():
    src = (ROOT / "diallux" / "app.py").read_text()
    assert '"speak_first_pipeline"' in src     # lane verification surface
    assert '"speak_model"' in src


def test_turnclock_report_carries_port_keys():
    c = TurnClock()
    c.mark_user_end()
    c.mark_llm_dispatch()
    c.mark_llm_first_event()
    c.mark_llm_first()
    r = c.report()
    assert "stt_eot_to_llm_dispatch_ms" in r
    assert "llm_dispatch_to_first_event_ms" in r
    assert "llm_first_event_to_llm_first_ms" in r
    assert r["llm_first_event_to_llm_first_ms"] >= 0.0
    assert r["llm_dispatch_to_first_event_ms"] >= 0.0


def test_stamp_clock_event_first_only():
    from diallux.media.session import _stamp_clock_event
    c = TurnClock()
    _stamp_clock_event(c, {"llm_dispatch": True})
    first = c.t_llm_dispatch
    _stamp_clock_event(c, {"llm_dispatch": True})
    assert c.t_llm_dispatch == first              # first-only per turn
    _stamp_clock_event(c, {"llm_first_event": True})
    firste = c.t_llm_first_event
    _stamp_clock_event(c, {"llm_first_event": True})
    assert c.t_llm_first_event == firste
    _stamp_clock_event(c, {"tts_token": "hi"})    # never stamps from tts
    assert c.t_llm_first is None


def test_builder_clock_events_order():
    fake = CountingFakeLLM([
        {"tokens": ["Hi", " there", "."], "tool_calls": []},
    ])
    rt = _make_rt(_settings(), fake)
    custom, _ = _run(rt, "s-clock-1", "hello?")
    dispatched = [i for i, e in enumerate(custom) if e.get("llm_dispatch")]
    first_events = [i for i, e in enumerate(custom) if e.get("llm_first_event")]
    assert len(dispatched) == 1 and len(first_events) == 1
    assert dispatched[0] < first_events[0]
    first_tok = next(i for i, e in enumerate(custom) if "tts_token" in e)
    assert first_events[0] < first_tok
