"""iter103 PT-5 — RAG query memo pins (byte-identical per-call reuse, no sqlite).

The Oct-8 canned shape: the same query set fired round after round (25
identical fires, same top-3). The memo keys the normalized QUERY SET of one
fire (all lanes, byte-identical modulo case/whitespace) and reuses the stored
raw pieces on a hit — no embed, no pgvector, no sqlite, dies with the call.

Pins:
  1. test_three_identical_queries_one_embed  — 3 fires → 1 embed; hits byte-identical
  2. test_delta_query_new_fire               — changed query set fires + stores
  3. test_normalization_hit                  — case/whitespace-only diff = hit
  4. test_fresh_runtime_empty_memo           — per-call purge (initial_state)
  5. test_memo_flag_on_span                  — rag span carries memo true/false
  6. test_consumer_staleness_untouched       — msg-text mismatch still respawns
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.graph.builder import CallRuntime
from tests.fake_llm import FakeLLM
from tests.test_iter49_rag_parity import (
    _LLM_JSON, _chunk, _run, _t4_settings, LanesStore, mock_client,
)

_MSG = "how much do you charge"


def _rt(store, **kw):
    return CallRuntime(_t4_settings(**kw), _LLM_JSON, tracer=None,
                       llm=FakeLLM(), http_client=mock_client(),
                       kb_store=store)


async def _fire_and_consume(rt, store, state="Intake", dvs=None, msg=_MSG):
    dvs = dvs or {}
    rt.spawn_live_retrieve(state, dvs, msg, lane="full")
    await asyncio.shield(rt._live_tasks[f"{state}|full"])
    return await rt._consume_live_retrieve(state, dvs, msg, store)


def test_three_identical_queries_one_embed():
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "charge chunk", 0.8)]],
                             [[_chunk(99, "X", "SECOND FIRE", 0.1)]]])
    rt = _rt(store)

    async def go():
        outs = [await _fire_and_consume(rt, store) for _ in range(3)]
        assert len(store.lane_calls) == 1, \
            f"3 identical fires -> 1 embed, got {len(store.lane_calls)}"
        # rounds 2-3 serve the memo hit byte-identically (delta + chunks)
        for o in outs[1:]:
            assert o[0] == outs[0][0], "delta byte-identical on the hit"
            assert o[3] == outs[0][3], "chunks byte-identical on the hit"
        await rt.aclose()

    _run(go())


def test_delta_query_new_fire():
    store = LanesStore(sets=[[[]]])
    rt = _rt(store)

    async def go():
        await _fire_and_consume(rt, store, dvs={"pain_frame": "missed calls"})
        n1 = len(store.lane_calls)
        await _fire_and_consume(rt, store, dvs={"pain_frame": "overloaded line"})
        assert len(store.lane_calls) == n1 + 1, "changed query set fires fresh"
        assert len(rt._rag_query_memo) == 2, "both query sets stored"
        await rt.aclose()

    _run(go())


def test_normalization_hit():
    store = LanesStore(sets=[[[]]])
    rt = _rt(store)

    async def go():
        r1 = await rt._retrieve_raw("Intake", {}, "How Much Do You Charge",
                                    store, lane="laneB")
        r2 = await rt._retrieve_raw("Intake", {}, "how   much do you   charge",
                                    store, lane="laneB")
        assert len(store.lane_calls) == 1, \
            "case/whitespace-only diff = memo hit (no second embed)"
        assert r2 == r1, "the hit returns the stored tuple"
        await rt.aclose()

    _run(go())


def test_fresh_runtime_empty_memo():
    store = LanesStore(sets=[[[]]])
    rt = _rt(store)
    assert rt._rag_query_memo == {}, "new runtime -> empty memo"

    async def go():
        await rt._retrieve_raw("Intake", {}, _MSG + " today", store)
        assert len(rt._rag_query_memo) == 1
        rt.initial_state("c-memo")           # per-call purge proof
        assert rt._rag_query_memo == {}
        await rt.aclose()

    _run(go())


class _Tracer:
    def __init__(self):
        self.spans = []

    def span(self, name, output=None, metadata=None):
        self.spans.append((name, output or {}))

    def start_llm(self, *a, **k):
        return None

    def finish_llm(self, *a, **k):
        pass


def test_memo_flag_on_span():
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "charge chunk", 0.8)]]])
    fake = FakeLLM([{"tokens": ["Hi", " there", "."]},
                    {"tokens": ["Got", " it", "."]},
                    {"tokens": ["Sure", " thing", "."]}])
    rt = CallRuntime(_t4_settings(), _LLM_JSON, tracer=_Tracer(), llm=fake,
                     http_client=mock_client(), kb_store=store)

    async def go():
        config = {"configurable": {"thread_id": "memo-span"}}
        texts = ["hello there", "tell me more", "hello there"]
        payload = {"user_text": texts[0], **rt.initial_state("memo-span")}
        for i, text in enumerate(texts):
            if i:
                payload = {"user_text": text}
            async for _m, _d in rt.graph.astream(payload, config=config,
                                                 stream_mode=["updates"]):
                pass
        rag = [out for name, out in rt.tracer.spans if name == "rag"]
        assert len(rag) == 3, f"one live rag span per round, got {len(rag)}"
        assert rag[0].get("memo") is False, "first fire is a miss"
        assert rag[1].get("memo") is False, "changed text fires fresh"
        assert rag[2].get("memo") is True, "canned re-fire is served by memo"
        await rt.aclose()

    _run(go())


def test_consumer_staleness_untouched():
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "charge chunk", 0.8)]]])
    rt = _rt(store)

    async def go():
        _d, _k, _ms, _ch, _lq, deg, _aw = await _fire_and_consume(rt, store)
        assert deg is False
        seq = rt._live_task_seq["Intake|full"]
        # matching msg-text: the consume reuses the landed task (NO respawn)
        await rt._consume_live_retrieve("Intake", {}, _MSG, store)
        assert rt._live_task_seq["Intake|full"] == seq, "match -> no respawn"
        # mismatched msg-text: the staleness check still respawns — the memo
        # never bypasses it (the respawn's embed is absorbed by the memo)
        rt._live_msgs["Intake|full"] = "STALE OTHER TEXT"
        await rt._consume_live_retrieve("Intake", {}, _MSG, store)
        assert rt._live_task_seq["Intake|full"] == seq + 1, \
            "stale msg-text -> respawn (memo never bypasses the check)"
        assert len(store.lane_calls) == 1, "respawn absorbed by memo"
        await rt.aclose()

    _run(go())
