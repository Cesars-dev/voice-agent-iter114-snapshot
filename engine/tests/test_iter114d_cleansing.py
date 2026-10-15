"""iter114d — cleansing pins (owner GO 2026-10-14; Offer confirmed into the
luna states).

Plan: tasks/surgeon/iter114d-cleansing/04_master_plan.md. Three fixes:
  1. word->digits guard auth (FIND-114C): word-spoken digit RUNS authorize
     the digit readback; unspoken figures still cut.
  2. speak-arm prewarm: fires ONLY for the states listed in
     `speak_model_states` (default now Intake,Discovery,Closer,Offer), on
     the resolved speak twin, parallel to the full warm; heavy states and
     every flag-off configuration stay cold (intended no-op).
  3. driver close-guard (F-114-3/4): an engine close mid-send stops
     `stream_pcm` quietly (open=False + ws_closed stamp), never raises —
     the driver journal and --out JSON must always land.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph import builder as B
from tests.fake_llm import FakeLLM, tool_call
from tests.test_iter112_speak_first import (
    PERSONA, LLM_JSON, SpeakFirstFakeLLM, _make_rt, _run_turns,
)

LUNA = "gpt-5.6-luna"


def _settings(**kw) -> Settings:
    base = dict(openai_api_key="test", retell_api_key="test",
                langfuse_enabled=False, first_turn_lite=False)
    base.update(kw)
    return Settings(**base)


async def _none_store():
    return None


# --------------------------------------------------------------------------- #
# 1/2 — word->digits guard auth (FIND-114C)
# --------------------------------------------------------------------------- #
def test_word_digits_authorize_e9_readback():
    rt = _make_rt(_settings(), FakeLLM([]))
    hist = [{"role": "user", "content":
             "Best number is five one two, three one two, zero zero zero one. "
             "We're in central time, Texas."}]
    _, toks, nums = rt._speak_auth_sets({}, hist)
    assert "5123120001" in nums
    # the E9 false-positive sentence — faithful readback passes now
    assert B._speak_guard_cut(
        "Perfect, and just to confirm, that's 5, 1, 2, 3, 1, 2, 0, 0, 0, 1, "
        "right?", toks, nums, {}, "") is None
    # an UNSPOKEN figure still cuts (token may be $9850 or #9850 — set order)
    cut = B._speak_guard_cut("That's $9,850 a month in lost work.",
                             toks, nums, {}, "")
    assert cut is not None and cut.startswith("number:")


def test_word_digits_small_talk_safe():
    assert B._spk_word_digits("I have two trucks") == set()
    assert B._spk_word_digits("one moment") == set()
    assert B._spk_word_digits("oh wow, just one thing") == set()
    assert B._spk_word_digits("nine one one") == {"911"}
    assert B._spk_word_digits(
        "five one two, three one two, zero zero zero one") == {"5123120001"}


# --------------------------------------------------------------------------- #
# 3 — speak warm: main states only; flag-off configurations stay cold
# --------------------------------------------------------------------------- #
class _StubSpeakLLM:
    """Records speak_llm(model) asks + the bound request kwargs + invokes."""

    def __init__(self):
        self.models: list[str] = []
        self.bind_kwargs: list[dict] = []
        self.invoked = 0

    def factory(self):
        def _get(model=None):
            self.models.append(model)
            stub = self

            class _Bound:
                def bind(self, **kw):
                    stub.bind_kwargs.append(kw)
                    return self

                async def ainvoke(self, messages):
                    stub.invoked += 1

            return _Bound()

        return _get


def test_speak_warm_fires_for_main_states_only():
    main = ("Intake", "Discovery", "Closer", "Offer")
    heavy = ("contact_details", "ConfirmSlots", "Closing")
    stub = _StubSpeakLLM()
    rt = _make_rt(_settings(speak_first_pipeline=True, speak_diet=True,
                            speak_model=LUNA,
                            speak_model_states="Intake,Discovery,Closer,Offer"),
                  FakeLLM([]))
    rt.llm.speak_llm = stub.factory()
    rt._resolve_kb_store = _none_store

    async def go():
        for s in main + heavy:
            rt.warm_speak_cache(s, [], {})
        await asyncio.gather(*rt._warm_tasks)

    asyncio.run(go())
    assert set(rt._prewarmed) == {f"speak:{s}" for s in main}
    assert stub.models == [LUNA] * 4
    assert stub.invoked == 4
    assert stub.bind_kwargs == [{"max_completion_tokens": 64}] * 4

    # flag-off configurations: no schedule, no latch, no twin ask (X0/F-A4)
    cases = [
        dict(speak_first_pipeline=False, speak_diet=True,
             speak_model_states="Intake"),                       # pipeline off
        dict(speak_first_pipeline=True, speak_diet=False,
             speak_model_states="Intake"),                       # diet rollback
        dict(speak_first_pipeline=True, speak_diet=True,
             speak_model_states=""),                             # legacy all-luna
    ]
    for kw in cases:
        stub2 = _StubSpeakLLM()
        rt2 = _make_rt(_settings(speak_model=LUNA, **kw), FakeLLM([]))
        rt2.llm.speak_llm = stub2.factory()
        rt2._resolve_kb_store = _none_store

        async def go2(rt=rt2):
            rt.warm_speak_cache("Intake", [], {})
            if rt._warm_tasks:
                await asyncio.gather(*rt._warm_tasks)

        asyncio.run(go2())
        assert not rt2._prewarmed, kw
        assert stub2.models == [], kw


# --------------------------------------------------------------------------- #
# 4 — hook parity: full + speak warm fire from the SAME transition moment;
#     the gate keeps heavy destinations cold
# --------------------------------------------------------------------------- #
def test_speak_warm_parallel_with_full_warm():
    persona = dict(PERSONA)
    for dest, scheduled in (("Discovery", True), ("contact_details", False)):
        stub = _StubSpeakLLM()
        fake = SpeakFirstFakeLLM(
            speak_rounds=[
                {"tokens": ["Got it."]},
                {"tokens": ["Alright, noted."], "delay": 0.01},
            ],
            tools_rounds=[
                {"tool_calls": [tool_call(f"transition_to_{dest}", {}, "t1")],
                 "delay": 0.3},           # outlives turn 1 -> M2
                {"tool_calls": []},
            ],
        )
        rt = _make_rt(_settings(speak_first_pipeline=True, speak_diet=True,
                                speak_model=LUNA,
                                speak_model_states="Intake,Discovery,Closer,Offer"),
                      fake)
        rt.llm.speak_llm = stub.factory()
        rt._resolve_kb_store = _none_store
        full_warms: list[str] = []
        speak_calls: list[str] = []
        rt.warm_prompt_cache = lambda d, history=None, dvs=None, lite=False, \
            force=False: full_warms.append(d)
        orig = rt.warm_speak_cache

        def spy(state_name, history=None, dvs=None):
            speak_calls.append(state_name)
            return orig(state_name, history, dvs)

        rt.warm_speak_cache = spy
        _run_turns(rt, f"s114d-par-{dest}", ["this is Kim", "sounds fine"],
                   persona=persona)
        assert dest in full_warms
        assert speak_calls == [dest]        # same hook, same moment
        if scheduled:
            assert f"speak:{dest}" in rt._prewarmed
        else:
            assert f"speak:{dest}" not in rt._prewarmed


# --------------------------------------------------------------------------- #
# 5 — driver close-guard (F-114-3/4)
# --------------------------------------------------------------------------- #
def _load_driver():
    path = Path(__file__).resolve().parents[1] / "scripts" / "llm_mic_caller.py"
    spec = importlib.util.spec_from_file_location(
        "llm_mic_caller_iter114d", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_driver_stream_pcm_close_guarded():
    mc = _load_driver()
    drv = mc.MicDriver("ws://stub", ear=None)

    class _ClosedWS:
        async def send(self, data):
            raise websockets.ConnectionClosedOK(None, None)

    drv.ws = _ClosedWS()
    asyncio.run(drv.stream_pcm(b"\x00" * 960))          # must not raise
    assert drv.open is False
    kinds = [e["kind"] for e in drv.events]
    assert "ws_closed" in kinds
    assert "caller_stream_end" not in kinds
