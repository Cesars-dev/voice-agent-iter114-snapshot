"""iter63 — --rag-fire-sim chat-surface lane-fire pins (hermetic).

The sim fires BOTH lanes through the EXACT session contract
(runtime.spawn_live_retrieve(state, dvs, transcript, lane=...), keyed
"<state>|laneA"/"<state>|laneB", every fire bumping _live_task_seq) before
each graph turn — proving on chat what the mic session does in
CallSession._fire_live_retrieve. Helpers cribbed from
test_iter49_rag_parity / test_iter62; engine code untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fake_llm import FakeLLM                                  # noqa: E402
from tests.llm2llm.harness import GraphAgent                        # noqa: E402
from tests.test_iter49_rag_parity import (                          # noqa: E402
    _chunk, _run, _t4_settings, LanesStore, mock_client,
)

_UTTERANCE = "my receptionist just quit on me"      # >= rag_min_query_chars
_PERSONA = {"name": "Sim Pin", "dynvars": {"pain_frame": "missed calls"}}


def _agent(store=None, tracer=None, fake=None, **kw):
    return GraphAgent(_t4_settings(rag_fire_mode="hybrid", **kw),
                      http_client=mock_client(), tracer=tracer,
                      llm=fake or FakeLLM(
                          [{"tokens": ["Happy", " to", " help", " with",
                                       " that", "."]}]),
                      kb_store=store, rag_fire_sim=True,
                      fire_sim_tail_ms=kw.pop("fire_sim_tail_ms", 100))


# ---- 1. both lanes fire once per turn, in session order --------------------- #
def test_fire_sim_spawns_both_lanes_once_per_turn():
    """laneA first (refer-tag plan, utterance-independent), laneB second
    (query IS the caller utterance). NO third fire — iter62 E4 deleted the
    respawn, so a consumed turn must not re-fire."""
    store = LanesStore()
    agent = _agent(store=store)

    async def go():
        await agent.start(_PERSONA)
        turn = await agent.send(_UTTERANCE, first=True, persona=_PERSONA)
        assert turn.speech, "FakeLLM speech round produced no tokens"
        await agent.aclose()

    _run(go())
    assert len(store.lane_calls) == 2, store.lane_calls
    lane_a, lane_b = store.lane_calls
    # laneA: the refer-tag plan — dvs-driven, utterance-independent
    # iter89 Edit A: 3 refer lanes in Intake now (I-4/I-5).
    assert len(lane_a) == 3, lane_a
    assert [l["query"] for l in lane_a] == [
        "surface pain recognition and opening discovery questions",
        "probing questions below the surface symptom",
        "emotional anchoring and empathic response: missed calls",
    ], lane_a
    assert all(l["scope"] == ["pain-points"] for l in lane_a), lane_a
    # laneB: the caller utterance, verbatim (no industry dv → no suffix)
    assert len(lane_b) == 1, lane_b
    assert lane_b[0]["query"] == _UTTERANCE, lane_b
    assert lane_b[0]["scope"] != ["pain-points"], lane_b


# ---- 2. landed turn: honest rag span on chat -------------------------------- #
class _SpanSpy:
    def __init__(self):
        self.spans = []

    def span(self, name, output=None, **kw):
        self.spans.append((name, output))

    def start_llm(self, *a, **kw):
        return None

    def finish_llm(self, *a, **kw):
        return None

    def start_tool(self, *a, **kw):
        return None

    def finish_tool(self, *a, **kw):
        return None


def test_fire_sim_landed_span_semantics():
    """tail >= embed → both lanes land inside the speech window → the turn's
    rag span shows the landed semantics: right state, the utterance in the
    query, chunks back (zero_hit False, degraded False)."""
    store = LanesStore(sets=[
        [[_chunk(1, "pain-points", "laneA charge chunk", 0.9)]],
        [[_chunk(2, "pain-points", "laneB utterance chunk", 0.8)]],
    ])
    spy = _SpanSpy()
    agent = _agent(store=store, tracer=spy)

    async def go():
        await agent.start(_PERSONA)
        await agent.send(_UTTERANCE, first=True, persona=_PERSONA)
        await agent.aclose()

    _run(go())
    rag_spans = [o for n, o in spy.spans if n == "rag"]
    assert rag_spans, spy.spans
    out = rag_spans[0]
    assert out["state"] == "Intake", out
    assert _UTTERANCE in out["query"], out
    assert out["zero_hit"] is False, out
    assert out["degraded"] is False, out


# ---- 3. short window fails clean (documented chat semantics) ----------------- #
def test_fire_sim_short_window_fails_clean():
    """delay=2.0 store + rag_live_await_ms=5 + tail=0 → the embed cannot land
    inside the window → the span shows degraded=True / zero_hit=True. The
    same fail-clean shape the mic shows on a short EagerEOT window — visible
    on chat, expected, NOT a regression."""
    store = LanesStore(delay=2.0)
    spy = _SpanSpy()
    agent = _agent(store=store, tracer=spy, fire_sim_tail_ms=0,
                   rag_live_await_ms=5)

    async def go():
        await agent.start(_PERSONA)
        await agent.send(_UTTERANCE, first=True, persona=_PERSONA)
        await agent.aclose()

    _run(go())
    rag_spans = [o for n, o in spy.spans if n == "rag"]
    assert rag_spans, spy.spans
    out = rag_spans[0]
    assert out["degraded"] is True, out
    assert out["zero_hit"] is True, out
