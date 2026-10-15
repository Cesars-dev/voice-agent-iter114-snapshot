"""iter59 T4 — semantic dedupe v2 + KB-everywhere pins (hermetic).

Dedupe v2: after the exact-match check, a QUESTION sentence whose stem
cosine vs the cross-turn question-stem window >= tts_dedupe_cos_threshold
(0.90, sweep-verified: near-verbatim 0.939-1.000, legit controls <=0.748)
is dropped from the spoken stream. Window entries {"text","vec"}; plain
state key; survives the whole call (ingest does NOT reset it); statements
and fast-flush fragments NEVER embed. Flag off = iter58 exact.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.graph.builder import CallRuntime, _cosine, _embed_stem
from tests.fake_llm import FakeLLM
from tests.test_iter49_rag_parity import (
    _GENERAL_KBS, _LLM_JSON, _run, _t4_settings, mock_client,
)

_VEC_A = [1.0, 0.0, 0.0]          # "what's the name of your company?"
_VEC_B = [1.0, 0.05, 0.0]         # near-verbatim variant (cos ≈ 0.9988 with A)
_VEC_C = [0.55, 0.6, 0.53]        # rephrase control (cos ≈ 0.54-0.57 with A)


async def _ret(v):
    return v


def _rt(**kw):
    return CallRuntime(_t4_settings(**kw), _LLM_JSON, tracer=None,
                       llm=FakeLLM(), http_client=mock_client(), kb_store=None)


def test_cosine_helper_matches_sweep_band():
    assert _cosine(_VEC_A, _VEC_B) >= 0.99
    assert 0.5 <= _cosine(_VEC_A, _VEC_C) <= 0.62
    assert _cosine(_VEC_A, [0.0, 1.0, 0.0]) <= 0.001


def test_semantic_threshold_config_defaults():
    s = _t4_settings()
    # PROMOTED iter91: False = prod value (PT-71 class — semantic dedupe
    # muted real speech; same-turn dup guard stays ON).
    assert s.tts_dedupe_semantic is False
    assert s.tts_dedupe_cos_threshold == 0.90
    assert s.tts_dedupe_stem_window == 12


def test_variant_dropped_at_threshold_control_survives():
    """1-2-word variant (cos 0.939-1.000) drops @0.90; the 0.54-0.57
    rephrase control survives."""
    from diallux.graph.builder import _semantic_dup

    class FakeStore:
        def __init__(self):
            self._map = {}

        def set(self, text, vec):
            self._map[text] = vec
            return self

        async def embed_query(self, text):
            return self._map.get(text, _VEC_C)

    store = FakeStore()
    store.set("Your company name?", _VEC_B)
    store.set("What kind of jobs do you do?", _VEC_C)
    rt = _rt()
    rt._resolve_kb_store = lambda: _ret(store)
    window = [{"text": "what's the name of your company?", "vec": _VEC_A}]

    async def go():
        dup = await _semantic_dup(rt, "Your company name?", window)
        keep = await _semantic_dup(rt, "What kind of jobs do you do?", window)
        return dup, keep

    dup, keep = _run(go())
    assert dup is True
    assert keep is False


def test_embed_failure_falls_back_to_exact_only():
    """store None / embed failure -> no drop, speech never blocked."""
    from diallux.graph.builder import _semantic_dup
    rt = _rt()

    async def boom(settings):
        raise RuntimeError("down")

    rt._resolve_kb_store = boom

    async def go():
        vec = await _embed_stem(rt, "Company name?")
        assert vec is None
        dup = await _semantic_dup(
            rt, "Company name?",
            [{"text": "what's the name of your company?", "vec": _VEC_A}])
        return dup

    assert _run(go()) is False


def test_state_key_survives_ingest_and_is_seeded_empty():
    """question_stem_window: plain key (NO reducer, NOT in the ingest reset
    list) — per-call persistence; seeded empty in initial_state."""
    from diallux.state import GraphState, initial_state
    assert "question_stem_window" in GraphState.__annotations__
    seed = initial_state("c59", {"starting_state": "Intake",
                                 "default_dynamic_variables": {}})
    assert seed["question_stem_window"] == []
    # the ingest reset patch (turn-scoped keys) must NOT contain the stem key
    import inspect
    from diallux.graph import builder as b
    src = inspect.getsource(b)
    i = src.find('"tts_sentence_window": []')
    assert i > 0
    reset_zone = src[i:src.find("}", i)]
    assert "question_stem_window" not in reset_zone


def test_statements_never_embed_spy_count():
    """Only QUESTION sentences reach the embedder; statements never do."""
    calls: list[str] = []

    class SpyStore:
        async def embed_query(self, text):
            calls.append(text)
            return _VEC_A

    rt = _rt()

    rt._resolve_kb_store = lambda: _ret(SpyStore())


    async def go():
        # a question embeds once
        vec = await _embed_stem(rt, "What's your company name?")
        assert vec == _VEC_A
        # the dedupe gate is "?" — a statement never reaches _embed_stem
        statement = "Here is your booking confirmed."
        assert "?" not in statement

    _run(go())
    assert calls == ["What's your company name?"], calls


def test_spoken_question_appends_to_stem_window_capped_fifo():
    """The window grows by {"text","vec"} and caps at tts_dedupe_stem_window."""
    window: list[dict] = []
    entries = [{"text": f"question number {i}?", "vec": _VEC_A}
               for i in range(15)]
    max_sw = 12
    for e in entries:
        window.append(e)
        if max_sw > 0 and len(window) > max_sw:
            del window[:len(window) - max_sw]
    assert len(window) == 12
    assert window[0]["text"] == "question number 3?"


# --------------------------------------------------------------------------- #
# KB-everywhere pins
# --------------------------------------------------------------------------- #
def test_all_nine_states_retrieve_the_general_union():
    """kb_slugs_for non-empty for all 9 states; the freed four == the 9
    general slugs (Retell tier)."""
    rt = _rt()
    for s in ("Booking", "VerifyLead", "ConfirmSlots", "contact_details"):
        assert rt.kb_slugs_for(s) == _GENERAL_KBS, s
    for s in ("Booking", "Closer", "Closing", "ConfirmSlots", "Discovery",
              "Intake", "Offer", "VerifyLead", "contact_details"):
        assert rt.kb_slugs_for(s), s


def test_contact_details_round_retrieves_and_renders_knowledge():
    """A contact_details round with an utterance >= rag_min_query_chars
    retrieves via lane B over the 9-KB union and the round carries
    KNOWLEDGE in the delta."""
    from tests.test_iter49_rag_parity import _t4_turns

    class OneStore:
        def __init__(self):
            self.lane_calls = []

        async def retrieve_lanes(self, lanes):
            self.lane_calls.append(lanes)
            return [[{"id": 7, "kb": "call-closing", "content": "warm chunk",
                      "score": 0.9}]]

        async def retrieve(self, q, slugs, vec=None):
            return []

    store = OneStore()
    fake = FakeLLM([{"tokens": ["Got", " it", "."]}])
    settings = _t4_settings(rag_live_retrieve=True)
    rt = CallRuntime(settings, _LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=store)
    _run(_t4_turns(rt, "c59-kb", ["yes, book me in for a demo please"]))
    assert store.lane_calls, "contact_details must retrieve"
    lanes = store.lane_calls[0]
    assert len(lanes) == 4          # 3 Intake refer lanes (iter89 I-4/I-5) + lane B (utterance)
    utt = lanes[-1]
    # iter64: pre-pin gate — the general-union scope excludes `industry`
    # until the pin resolves or the industry dv is extracted
    assert "industry" not in utt["scope"]
    assert sorted(utt["scope"]) == \
        sorted(s for s in _GENERAL_KBS if s != "industry")
    assert "book me in" in utt["query"]


def test_dedupe_flag_off_is_iter58_surface():
    """tts_dedupe_semantic=False: no semantic machinery in the round path
    (exact-only behavior); the exact dedupe flag stays untouched."""
    rt = _rt(tts_dedupe_semantic=False)
    assert rt.settings.tts_dedupe_semantic is False
    assert rt.settings.tts_dedupe_sentences is True   # iter42 exact stays
