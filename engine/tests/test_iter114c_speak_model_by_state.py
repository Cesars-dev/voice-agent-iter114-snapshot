"""iter114c — per-state speak-model routing (owner design call 2026-10-13).

Plan: plans/plan_v5_iter114c_speak_model_by_state.md. The SPEAK stream runs
`settings.speak_model` (luna, fast speech) ONLY for the states listed in
`settings.speak_model_states` (default "Intake,Discovery,Closer,Offer" —
iter114d added Offer); every
other state's speak stream rides `settings.openai_model` (gpt-5.4, tool
accuracy). The TOOLS stream always stays `openai_model`.

Modes (zero ambiguity):
  - speak_model=""                  -> same model both streams (E1 mode)
  - speak_model_states=""           -> every state speaks `speak_model`
                                       (legacy all-luna, E7 mode)
  - default                         -> luna for Intake/Discovery/Closer/Offer,
                                       5.4 for the rest

Pins:
  1. `_speak_model_for` routing: fast states -> speak_model; every other
     state -> openai_model; unknown/empty -> openai_model (5.4).
  2. legacy `speak_model_states=""` -> every state rides speak_model.
  3. `speak_model=""` -> "" (no twin; same-model speech — E1 pin stays).
  4. e2e split arm passes the RESOLVED model as `speak_model` to astream
     (Intake -> luna, ConfirmSlots -> 5.4); the tools stream never carries one.
  5. trace label names the resolved model per state (T7 truth).
  6. per-model twin cache: one ChatOpenAI per model, reasoning_effort "none"
     on gpt-5.x twins, no verbosity/service_tier; cache hit on second ask.
  7. pipeline OFF -> byte-exact single astream, no speak_model kwarg use.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph import builder as B
from diallux.graph.llm import StreamingLLM
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client
from tests.test_iter112_speak_first import (
    PERSONA, SpeakFirstFakeLLM, LLM_JSON, _make_rt, _run, _tts_tokens,
)
from tests.test_iter114_speech_auth import _TracerStub


def _settings(**kw) -> Settings:
    base = dict(openai_api_key="test", retell_api_key="test",
                langfuse_enabled=False, first_turn_lite=False)
    base.update(kw)
    return Settings(**base)


LUNA = "gpt-5.6-luna"


# --------------------------------------------------------------------------- #
# 1/2/3 — routing table (unit)
# --------------------------------------------------------------------------- #
def test_speak_model_for_routing_table():
    rt = _make_rt(_settings(speak_first_pipeline=True, speak_model=LUNA),
                  FakeLLM([]))
    for s in ("Intake", "Discovery", "Closer", "Offer"):
        assert rt._speak_model_for(s) == LUNA, s
    for s in ("contact_details", "ConfirmSlots", "VerifyLead", "Booking",
              "Closing"):
        assert rt._speak_model_for(s) == "gpt-5.4", s
    assert rt._speak_model_for("") == "gpt-5.4"       # unknown -> 5.4


def test_speak_model_for_custom_state_list():
    rt = _make_rt(_settings(speak_first_pipeline=True, speak_model=LUNA,
                            speak_model_states="Intake"), FakeLLM([]))
    assert rt._speak_model_for("Intake") == LUNA
    assert rt._speak_model_for("Discovery") == "gpt-5.4"
    assert rt._speak_model_for("Closer") == "gpt-5.4"


def test_speak_model_for_legacy_all_luna():
    rt = _make_rt(_settings(speak_first_pipeline=True, speak_model=LUNA,
                            speak_model_states=""), FakeLLM([]))
    for s in ("Intake", "Discovery", "Closer", "contact_details",
              "ConfirmSlots", "Offer"):
        assert rt._speak_model_for(s) == LUNA, s


def test_speak_model_for_unset_is_e1_mode():
    rt = _make_rt(_settings(speak_first_pipeline=True), FakeLLM([]))
    for s in ("Intake", "ConfirmSlots", ""):
        assert rt._speak_model_for(s) == ""


# --------------------------------------------------------------------------- #
# 4 — e2e: the split arm passes the resolved model (fast vs heavy)
# --------------------------------------------------------------------------- #
def test_split_arm_fast_state_passes_speak_model():
    fake = SpeakFirstFakeLLM(
        speak_rounds=[{"tokens": ["Hi there."]}],
        tools_rounds=[{"tokens": []}],
    )
    rt = _make_rt(_settings(speak_first_pipeline=True, speak_model=LUNA), fake)
    _run(rt, "s114c-fast", "hello", persona=dict(PERSONA), state="Intake")
    kinds = [(c["kind"], c["speak_model"]) for c in fake.calls]
    assert kinds == [("speak", LUNA), ("tools", "")]


def test_split_arm_heavy_state_passes_openai_model():
    fake = SpeakFirstFakeLLM(
        speak_rounds=[{"tokens": ["One moment."]}],
        tools_rounds=[{"tokens": []}],
    )
    rt = _make_rt(_settings(speak_first_pipeline=True, speak_model=LUNA), fake)
    _run(rt, "s114c-heavy", "tomorrow works", persona=dict(PERSONA),
         state="ConfirmSlots")
    kinds = [(c["kind"], c["speak_model"]) for c in fake.calls]
    assert kinds == [("speak", "gpt-5.4"), ("tools", "")]


# --------------------------------------------------------------------------- #
# 5 — trace label names the resolved model per state (T7 truth)
# --------------------------------------------------------------------------- #
def test_trace_label_per_state():
    for state, want in (("Intake", LUNA), ("ConfirmSlots", "gpt-5.4")):
        tracer = _TracerStub()
        fake = SpeakFirstFakeLLM(
            speak_rounds=[{"tokens": ["Hi there."]}],
            tools_rounds=[{"tokens": []}],
        )
        rt = B.CallRuntime(_settings(speak_first_pipeline=True,
                                     speak_model=LUNA),
                           LLM_JSON, tracer=tracer, llm=fake,
                           http_client=mock_client())
        _run(rt, f"s114c-trace-{state}", "hello", persona=dict(PERSONA),
             state=state)
        assert tracer.starts[0]["model"] == want, state


# --------------------------------------------------------------------------- #
# 6 — per-model twin cache
# --------------------------------------------------------------------------- #
def test_speak_twin_cache_per_model():
    llm = StreamingLLM(_settings(speak_model=LUNA,
                                 openai_service_tier="fast"))
    a = llm.speak_llm("gpt-5.4")
    assert a.model_name == "gpt-5.4"
    assert getattr(a, "reasoning_effort", None) == "none"   # gpt-5.x twin
    assert a.verbosity is None and a.service_tier is None
    b = llm.speak_llm(LUNA)
    assert b.model_name == LUNA
    assert b.reasoning_effort == "none"
    assert llm.speak_llm("gpt-5.4") is a                    # cache hit
    assert llm.speak_llm() is b                             # default = speak_model
    assert set(llm._speak_llms) == {"gpt-5.4", LUNA}
    # the hot LLM is untouched
    assert llm._llm.model_name == "gpt-5.4"


# --------------------------------------------------------------------------- #
# 7 — pipeline OFF: byte-exact single arm (no split, no speak_model)
# --------------------------------------------------------------------------- #
def test_pipeline_off_single_arm_unchanged():
    fake = SpeakFirstFakeLLM(
        speak_rounds=[],
        tools_rounds=[{"tokens": ["Hi there."]}],
    )
    rt = _make_rt(_settings(speak_model=LUNA), fake)   # pipeline OFF
    _run(rt, "s114c-off", "hello", persona=dict(PERSONA))
    assert [c["kind"] for c in fake.calls] == ["tools"]
    assert fake.calls[0]["speak"] is False
    assert fake.calls[0]["speak_model"] == ""
