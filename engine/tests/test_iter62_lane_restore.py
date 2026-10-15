"""iter62 — lane-restore pins (hermetic: no Postgres, no model).

The four proven retrieval failure classes (67 rag spans, deep dive §7):
lane B ran EMPTY on 25/67 rounds (history re-extraction diverged from the
fired transcript under eager/barge-in → empty respawn that passed the
12-char gate); zero-chunk rounds hid behind degraded=False; await misses
served stale sets; the solar vertical was pinless for lack of an alias;
record_reach_details(true) with no callback_number was a dead end.

iter62 fixes: ingest keeps the turn transcript (single source of truth),
hybrid respawn DELETED (fail clean, mismatch reject KEPT), empty set is
always degraded, zero_hit in both span emitters, solar alias, need_digits
recovery. Helpers cribbed from test_iter49_rag_parity / test_iter59.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.graph.builder import CallRuntime
from diallux.graph.tools import ToolExecutor
from tests.fake_llm import FakeLLM
from tests.test_iter49_rag_parity import (
    _LLM_JSON, _chunk, _run, _t4_settings, LanesStore, mock_client,
)

_UTTERANCE = "how much do you guys charge for HVAC"


def _rt(store=None, fake=None, **kw):
    return CallRuntime(_t4_settings(**kw), _LLM_JSON, tracer=None,
                       llm=fake or FakeLLM(), http_client=mock_client(),
                       kb_store=store)


# ---- 1. ingest keeps the turn transcript ---------------------------------- #
def test_ingest_keeps_user_text_across_rounds():
    """iter62 E1: ingest no longer wipes user_text — state_node consumes it
    as the single source of truth; the idempotent guard still holds."""
    fake = FakeLLM([{"tokens": ["Sure", ",", " how", " can", " I", " help", "?"]},
                    {"tokens": ["Thanks", " for", " asking", "."]}])
    rt = CallRuntime(_t4_settings(), _LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)

    async def go():
        config = {"configurable": {"thread_id": "c62-ingest"}}
        payload = {"user_text": _UTTERANCE, **rt.initial_state("c62-ingest")}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
        st = (await rt.graph.aget_state(config)).values
        assert st["user_text"] == _UTTERANCE, \
            f"iter62 E1 regressed: user_text wiped: {st.get('user_text')!r}"
        assert sum(1 for m in st.get("history", [])
                   if m.get("role") == "user") == 1
        # turn 2, SAME transcript: idempotent guard — still ONE user msg,
        # user_text unchanged (the next turn's payload overwrites it).
        async for _m, _d in rt.graph.astream(
                {"user_text": _UTTERANCE}, config=config,
                stream_mode=["updates"]):
            pass
        st2 = (await rt.graph.aget_state(config)).values
        assert st2["user_text"] == _UTTERANCE
        assert sum(1 for m in st2.get("history", [])
                   if m.get("role") == "user") == 1
        await rt.aclose()

    _run(go())


# ---- 2. hybrid consume uses the session transcript ------------------------- #
def test_consume_uses_session_transcript_not_history():
    """The consume query IS the fired transcript; no history re-extraction,
    no respawn (lane_calls stays 1)."""
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "charge chunk", 0.8)]]])
    rt = CallRuntime(_t4_settings(rag_fire_mode="hybrid"), _LLM_JSON,
                     tracer=None, llm=None, http_client=mock_client(),
                     kb_store=store)

    async def go():
        rt.spawn_live_retrieve("Intake", {}, _UTTERANCE, lane="laneB")
        await asyncio.shield(rt._live_tasks["Intake|laneB"])
        _d, _k, _ms, _ch, lq, deg, _aw = await rt._consume_live_retrieve(
            "Intake", {}, _UTTERANCE, store)
        assert deg is False
        assert any(_UTTERANCE in q for q in lq), lq
        assert len(store.lane_calls) == 1      # NO respawn (iter62 E4)
        await rt.aclose()

    _run(go())


# ---- 3. lane A shape: refer-tag lanes, owned KBs --------------------------- #
def test_lane_a_shape_refer_tags_owned():
    rt = CallRuntime(_t4_settings(rag_fire_mode="hybrid"), _LLM_JSON,
                     tracer=None, llm=None, http_client=mock_client(),
                     kb_store=None)
    lanes, owned = rt.build_lanes("Offer", {"monthly_leak": "60,000"}, "")
    assert len(lanes) == 1, lanes
    assert "re-anchoring" in lanes[0]["query"]
    assert "$60,000 a month" in lanes[0]["query"], lanes[0]
    assert lanes[0]["scope"] == ["sales-psychology"]
    assert owned[0] == {"sales-psychology"}
    # contact_details has NO refer tags → NO lane-A lanes (tags only)
    lanes2, owned2 = rt.build_lanes("contact_details",
                                    {"industry": "solar company"}, "")
    assert lanes2 == [] and owned2 == []


# ---- 4. zero chunks is an honest degraded round ----------------------------- #
def test_zero_chunks_is_degraded():
    """iter62 E3/E6: both lanes land, store returns empty sets → the round
    is degraded=True (was hard-coded False; 38/67 spans hid behind it)."""
    store = LanesStore(sets=[[[]]])
    rt = CallRuntime(_t4_settings(rag_fire_mode="hybrid"), _LLM_JSON,
                     tracer=None, llm=None, http_client=mock_client(),
                     kb_store=store)

    async def go():
        rt.spawn_live_retrieve("Intake", {}, "", lane="laneA")
        await asyncio.shield(rt._live_tasks["Intake|laneA"])
        rt.spawn_live_retrieve("Intake", {}, _UTTERANCE, lane="laneB")
        await asyncio.shield(rt._live_tasks["Intake|laneB"])
        _d, _k, _ms, ch, _lq, deg, _aw = await rt._consume_live_retrieve(
            "Intake", {}, _UTTERANCE, store)
        assert deg is True and ch == []
        await rt.aclose()

    _run(go())


# ---- 5. zero_hit in the rag span -------------------------------------------- #
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


def test_span_zero_hit_field():
    """iter62 E8: the live rag span carries zero_hit matching chunk emptiness
    (two one-turn graph runs: chunk store vs empty store)."""

    async def one_turn(store, sid):
        fake = FakeLLM([{"tokens": ["We", " can", " help", " with", " that",
                                    "."]}])
        spy = _SpanSpy()
        rt = CallRuntime(_t4_settings(), _LLM_JSON, tracer=spy, llm=fake,
                         http_client=mock_client(), kb_store=store)
        config = {"configurable": {"thread_id": sid}}
        payload = {"user_text": _UTTERANCE, **rt.initial_state(sid)}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
        await rt.aclose()
        return spy

    spy_full = _run(one_turn(
        LanesStore(sets=[[[_chunk(1, "pain-points", "charge chunk", 0.8)]]]),
        "c62-span-full"))
    spy_empty = _run(one_turn(LanesStore(), "c62-span-empty"))
    rag_full = [o for n, o in spy_full.spans if n == "rag"]
    rag_empty = [o for n, o in spy_empty.spans if n == "rag"]
    assert rag_full and rag_full[0]["zero_hit"] is False
    assert rag_empty and rag_empty[0]["zero_hit"] is True


# ---- 6. await miss fails clean (no stale injection) -------------------------- #
def test_await_miss_fails_clean():
    """iter62 E5: an await miss returns an EMPTY set — the previous fresh
    set is NOT served (owner rejected landed-late injection)."""
    store = LanesStore(sets=[
        [[_chunk(1, "pain-points", "first utterance chunk", 0.8)]],
        [[_chunk(2, "pain-points", "second utterance chunk", 0.8)]],
    ], delay=2.0)
    rt = CallRuntime(_t4_settings(rag_fire_mode="hybrid",
                                  rag_live_await_ms=5), _LLM_JSON,
                     tracer=None, llm=None, http_client=mock_client(),
                     kb_store=store)

    async def go():
        rt.spawn_live_retrieve("Intake", {}, _UTTERANCE, lane="laneB")
        await asyncio.shield(rt._live_tasks["Intake|laneB"])
        _d1, _k1, _ms1, _ch1, _lq1, deg1, _aw1 = await rt._consume_live_retrieve(
            "Intake", {}, _UTTERANCE, store)
        assert deg1 is False
        assert rt._live_consumed_res.get("Intake")     # round 1 was fresh
        msg2 = "and what about the warranty anyway"
        rt.spawn_live_retrieve("Intake", {}, msg2, lane="laneB")
        d2, _k2, _ms2, ch2, _lq2, deg2, _aw2 = await rt._consume_live_retrieve(
            "Intake", {}, msg2, store)
        assert deg2 is True and d2 == "" and ch2 == [], \
            f"iter62 E5 regressed: stale set served on await miss: {d2!r}"
        await rt.aclose()

    _run(go())


# ---- 7. solar pin alias ------------------------------------------------------ #
def test_solar_alias_resolves_vertical():
    """iter62 E10: 'solar company' resolves via the alias table (zero
    retrieve calls — no embed fallback) and pins the vertical once."""

    class PinStore:
        def __init__(self):
            self.retrieves = 0
            self.pin_calls: list[str] = []

        async def retrieve(self, query, slugs, vec=None):
            self.retrieves += 1
            return []

        async def pinned_by_tag(self, tag):
            self.pin_calls.append(tag)
            return [_chunk(i, "solar-objections", f"c{i}", 0.9)
                    for i in (1, 2, 3)]

    store = PinStore()
    rt = CallRuntime(_t4_settings(rag_fire_mode="hybrid",
                                  rag_pin_industry=True), _LLM_JSON,
                     tracer=None, llm=None, http_client=mock_client(),
                     kb_store=store)

    async def go():
        tag = await rt._resolve_industry_tag("solar company", store)
        assert tag == "Residential Roofing & Solar Installers", tag
        assert store.retrieves == 0            # alias path, no embed fallback
        await rt._ensure_pinned_industry({"industry": "solar company"}, store)
        assert rt._pinned_industry["tag"] == "Residential Roofing & Solar Installers"
        assert len(rt._pinned_industry["chunks"]) == 3
        assert store.pin_calls == ["Residential Roofing & Solar Installers"]
        await rt.aclose()

    _run(go())


# ---- 8. lane drive, no LLM (the chat-surface logic check) --------------------- #
def test_lane_drive_no_llm():
    """Both lanes fire, land, and merge through ONE consume — zero harness
    edits; the lane logic itself is proven hermetically."""
    store = LanesStore(sets=[
        [[_chunk(1, "sales-psychology", "laneA chunk", 0.95)]],
        [[_chunk(2, "pain-points", "laneB utterance chunk", 0.8)]],
    ])
    rt = CallRuntime(_t4_settings(rag_fire_mode="hybrid"), _LLM_JSON,
                     tracer=None, llm=None, http_client=mock_client(),
                     kb_store=store)

    async def go():
        rt.spawn_live_retrieve("Intake", {}, "", lane="laneA")
        await asyncio.shield(rt._live_tasks["Intake|laneA"])
        rt.spawn_live_retrieve("Intake", {}, _UTTERANCE, lane="laneB")
        await asyncio.shield(rt._live_tasks["Intake|laneB"])
        _d, _k, _ms, ch, lq, deg, _aw = await rt._consume_live_retrieve(
            "Intake", {}, _UTTERANCE, store)
        assert deg is False
        assert len(ch) >= 2                    # BOTH lanes in the merged set
        kbs = {c["kb"] for c in ch}
        assert "sales-psychology" in kbs and "pain-points" in kbs
        assert lq, "lane_qs must be non-empty"
        await rt.aclose()

    _run(go())


# ---- 9. record_reach_details need_digits recovery ----------------------------- #
def test_record_reach_details_need_digits_recovery():
    """iter62 E11: YES ∧ empty callback_number = need_digits + the next step;
    non-empty number or NO answer = byte-identical."""

    async def go():
        # (a) YES ∧ empty callback_number → need_digits recovery; the
        # webhook's phone_confirmed patch stands.
        ex = ToolExecutor(_t4_settings(), _LLM_JSON, http_client=mock_client())
        out = await ex.execute("record_reach_details",
                               {"is_calling_best_number": True},
                               {"callback_number": ""}, "contact_details")
        assert out.response["status"] == "need_digits"
        assert "set_callback_number" in out.response["instruction"]
        assert out.dvs_patch["phone_confirmed"] is True
        await ex.aclose()
        # (b) non-empty callback_number → byte-identical ok, NO instruction
        ex2 = ToolExecutor(_t4_settings(), _LLM_JSON, http_client=mock_client())
        out2 = await ex2.execute("record_reach_details",
                                 {"is_calling_best_number": True},
                                 {"callback_number": "+13125551234"},
                                 "contact_details")
        assert out2.response["status"] == "ok"
        assert "instruction" not in out2.response
        await ex2.aclose()
        # (c) NO answer → untouched
        ex3 = ToolExecutor(_t4_settings(), _LLM_JSON, http_client=mock_client())
        out3 = await ex3.execute("record_reach_details",
                                 {"is_calling_best_number": False},
                                 {"callback_number": ""}, "contact_details")
        assert out3.response["status"] == "ok"
        await ex3.aclose()

    _run(go())
