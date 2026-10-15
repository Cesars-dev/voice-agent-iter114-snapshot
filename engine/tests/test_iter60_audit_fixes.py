"""iter60 — audit fixes AUD-1..AUD-10 pins (hermetic).

Source of the findings: research/surgeon/iter59-kb-everywhere/04_code_audit.md.
Each pin name maps 1:1 to a fix in 05_fix_report.md. No existing test file
was edited (per-iteration pin convention).
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from pydantic import ValidationError

from diallux.graph.builder import CallRuntime
from diallux.media import prewarm
from tests.fake_llm import FakeLLM
from tests.test_iter49_rag_parity import (
    _LLM_JSON, _chunk, _run, _t4_settings, LanesStore, mock_client,
)

_VEC_A = [1.0, 0.0, 0.0]


async def _ret(v):
    return v


def _rt(store=None, fake=None, **kw):
    return CallRuntime(_t4_settings(**kw), _LLM_JSON, tracer=None,
                       llm=fake or FakeLLM(), http_client=mock_client(),
                       kb_store=store)


# --------------------------------------------------------------------------- #
# AUD-1: the idle TTS _drain companion task is cancelled on adoption
# --------------------------------------------------------------------------- #
def test_tts_drain_task_cancelled_on_get():
    class FakeWS:
        pass

    fake_ws = FakeWS()
    old_pool = dict(prewarm.pool)
    old_tasks = dict(prewarm._companion_tasks)
    task = None

    async def never():
        await asyncio.Event().wait()

    async def go():
        nonlocal task
        task = asyncio.get_running_loop().create_task(never())
        prewarm.pool["tts"] = fake_ws
        prewarm._companion_tasks["tts"] = task
        got = prewarm.get("tts")
        assert got is fake_ws, "adoption must still return the pooled ws"
        assert "tts" not in prewarm._companion_tasks
        await asyncio.sleep(0.05)          # let the cancellation land
        assert task.cancelled()

    try:
        _run(go())
    finally:
        if task is not None and not task.done():
            task.cancel()
        prewarm.pool.clear()
        prewarm.pool.update(old_pool)
        prewarm._companion_tasks.clear()
        prewarm._companion_tasks.update(old_tasks)


# --------------------------------------------------------------------------- #
# AUD-2: phrase-cache join decodes per chunk (mid-padding never truncates)
# --------------------------------------------------------------------------- #
def test_b64_join_padded_chunks_no_truncation():
    from diallux.media.cartesia_tts import _b64d, _b64e
    chunks = [_b64e(b"AB"), _b64e(b"ABCD"), _b64e(b"ABC")]
    # the audit's truncation case: string-join first = mid-string padding
    assert len(_b64d("".join(chunks))) < 9
    # the fix (the exact expression in prewarm._warm_phrase_cache): byte-join
    assert b"".join(_b64d(c) for c in chunks) == b"ABABCDABC"
    src = inspect.getsource(prewarm._warm_phrase_cache)
    assert 'b"".join(_b64d(c) for c in chunks)' in src
    assert '_b64d("".join(chunks))' not in src


# --------------------------------------------------------------------------- #
# AUD-3: round 2+ of the same turn reuses the round-1 merged set
# --------------------------------------------------------------------------- #
def test_hybrid_round2_reuses_full_merged_set():
    store = LanesStore(sets=[
        [[_chunk(1, "sales-psychology", "laneA chunk", 0.95)]],
        [[_chunk(2, "pain-points", "laneB chunk", 0.8)]],
    ])
    rt = CallRuntime(_t4_settings(rag_fire_mode="hybrid"), _LLM_JSON,
                     tracer=None, llm=None, http_client=mock_client(),
                     kb_store=store)

    async def go():
        rt.spawn_live_retrieve("Intake", {}, "", lane="laneA")
        await asyncio.shield(rt._live_tasks["Intake|laneA"])
        rt.spawn_live_retrieve("Intake", {}, "my calls go to voicemail",
                               lane="laneB")
        await asyncio.shield(rt._live_tasks["Intake|laneB"])
        _d1, _k1, _ms1, _c1, lq1, deg1, _aw1 = await rt._consume_live_retrieve(
            "Intake", {}, "my calls go to voicemail", store)
        assert deg1 is False
        assert any("emotional anchoring" in q for q in lq1), lq1
        # round 2 of the SAME turn (tool round): stash short-circuits
        _d2, _k2, _ms2, ch2, lq2, deg2, _aw2 = await rt._consume_live_retrieve(
            "Intake", {}, "my calls go to voicemail", store)
        assert deg2 is False
        assert any("emotional anchoring" in q for q in lq2), \
            f"AUD-3 regressed: laneA query lost on round 2: {lq2}"
        assert lq2 == lq1
        assert len(ch2) == 2
        await rt.aclose()

    _run(go())


# --------------------------------------------------------------------------- #
# AUD-4: a DONE laneB from an earlier turn is rejected on msg mismatch
# --------------------------------------------------------------------------- #
def test_hybrid_stale_laneb_respawned_with_current_msg():
    store = LanesStore(sets=[
        [[_chunk(1, "pain-points", "confirming chunk", 0.8)]],
        [[_chunk(2, "pain-points", "new utterance chunk", 0.8)]],
    ])
    rt = CallRuntime(_t4_settings(rag_fire_mode="hybrid"), _LLM_JSON,
                     tracer=None, llm=None, http_client=mock_client(),
                     kb_store=store)

    async def go():
        # turn 1: Booking laneB fires + lands + is consumed
        rt.spawn_live_retrieve("Booking", {}, "confirming the booking please",
                               lane="laneB")
        await asyncio.shield(rt._live_tasks["Booking|laneB"])
        _d, _k, _ms, _c, _lq, deg, _aw = await rt._consume_live_retrieve(
            "Booking", {}, "confirming the booking please", store)
        assert deg is False
        # turn 2: fresh fires for Intake (the stale Booking task stays DONE)
        rt.spawn_live_retrieve("Intake", {}, "and what about the warranty",
                               lane="laneA")
        await asyncio.shield(rt._live_tasks["Intake|laneA"])
        rt.spawn_live_retrieve("Intake", {}, "and what about the warranty",
                               lane="laneB")
        await asyncio.shield(rt._live_tasks["Intake|laneB"])
        # a Booking round whose utterance is the NEW message: the stale
        # turn-1 laneB must be rejected (msg mismatch) with NOTHING re-fired
        # iter62: the respawn is DELETED (fail-clean) — the stale DONE
        # laneB (fired for an earlier utterance) is REJECTED on msg
        # mismatch, NOTHING re-fires, and the round fails CLEAN.
        _d2, _k2, _ms2, _c2, lq2, deg2, _aw2 = await rt._consume_live_retrieve(
            "Booking", {}, "and what about the warranty", store)
        assert deg2 is True
        assert lq2 == [] and _c2 == [] and _d2 == ""
        assert len(store.lane_calls) == 3   # turn-1 laneB + turn-2 laneA/laneB — NO 4th fire
        await rt.aclose()

    _run(go())


# --------------------------------------------------------------------------- #
# AUD-5: round-sync fires NOTHING at EOT/EagerEOT (consume is inline)
# --------------------------------------------------------------------------- #
async def _fake_run(transcript, epoch=0):
    return None


async def _async_noop():
    pass


def test_round_sync_fires_nothing_at_eot():
    from diallux.media.session import CallSession
    calls: list = []

    class FakeRuntime:
        def spawn_live_retrieve(self, state, dvs, msg, lane="full"):
            calls.append((state, msg, lane))

    sess = object.__new__(CallSession)
    sess.settings = _t4_settings(rag_fire_mode="round-sync")
    sess._stopped = False
    sess._ended = False
    sess._update_fired = False
    sess.runtime = FakeRuntime()
    sess._turn_state = "Intake"
    sess._turn_dvs = {}
    sess._turn_task = None
    sess._clock = type("C", (), {"turn_index": 0, "extra": {},
                                 "barge_in": False})()
    sess._real_turns = 0
    sess._call_start = 0.0
    sess._cap_closed = False
    sess._turn_reports = []
    sess.stream_sid = "s59"
    sess.call_sid = "c59"
    sess.tracer = None
    sess.tts = None
    sess.gate = None
    sess._outbox = asyncio.Queue()
    sess._thread_config = {"configurable": {"thread_id": "c59"}}
    sess._speak_clock = None
    sess._tts_context = None
    sess._run_turn = _fake_run
    sess._graceful_cap_close = _async_noop
    sess.settings.max_call_turns = 64
    sess.settings.max_call_seconds = 0

    asyncio.run(sess._on_eot("the full transcript"))
    assert calls == [], \
        f"AUD-5 regressed: round-sync fired a wasted task at EOT: {calls}"
    assert sess._turn_task is not None


# --------------------------------------------------------------------------- #
# AUD-6: a turn-OPENING near-verbatim re-ask is dropped (no spoken_any gate)
# --------------------------------------------------------------------------- #
def test_turn_opening_reask_dropped_when_window_nonempty():
    class FakeStore:
        async def embed_query(self, text):
            return _VEC_A

        async def retrieve_lanes(self, lanes):
            return [[] for _ in lanes]

        async def retrieve(self, query, slugs, vec=None):
            return []

    Q = ["So", ",", " what", " is", " your", " company", " name", "?"]
    C = ["And", " I", " can", " definitely", " help", " with", " that", "."]
    fake = FakeLLM([{"tokens": Q + C}])
    # fast-first-flush OFF: fragments never embed by design (G5) — with the
    # 28-char fast flush the question would leak as a pre-"?" fragment
    # before the semantic gate ever saw it; this pin targets the gate.
    # iter91: eager EOT is ON by default now, and the re-ask drop routes
    # through the semantic-dup path (G5) which the pack promotes OFF in prod
    # (PT-71). Isolation knobs explicit: dedupe ON = the gate under test;
    # eager OFF = keep the pin on the gate, not on eager EOT timing.
    rt = _rt(fake=fake, rag_min_query_chars=0, tts_gate_fast_first_flush=False,
             tts_dedupe_semantic=True,
             deepgram_eager=False, deepgram_eager_eot=False,
             deepgram_eager_eot_threshold=None)
    rt._resolve_kb_store = lambda: _ret(FakeStore())

    tokens: list[str] = []

    async def go():
        config = {"configurable": {"thread_id": "c60-aud6"}}
        payload = rt.initial_state("c60-aud6")
        payload["state_name"] = "Intake"
        payload["user_text"] = "yeah sure go ahead"
        payload["question_stem_window"] = [
            {"text": "what's the name of your company?", "vec": _VEC_A}]
        async for mode, d in rt.graph.astream(
                payload, config=config,
                stream_mode=["updates", "custom"]):
            if mode == "custom" and isinstance(d, dict) and "tts_token" in d:
                tokens.append(d["tts_token"])
        return await rt.graph.aget_state(config)

    _run(go())
    spoken = "".join(tokens)
    assert "what is your company name" not in spoken.lower(), \
        "AUD-6 regressed: turn-OPENING re-ask was spoken"
    assert "And I can definitely help with that." in spoken, \
        "control statement must still speak"


# --------------------------------------------------------------------------- #
# AUD-8: rag_fire_mode is validated (typo fails at Settings load)
# --------------------------------------------------------------------------- #
def test_rag_fire_mode_rejects_invalid():
    with pytest.raises(ValidationError):
        _t4_settings(rag_fire_mode="spech-window")
    s = _t4_settings(rag_fire_mode="hybrid")          # valid modes construct
    assert s.rag_fire_mode == "hybrid"
    _t4_settings(rag_fire_mode="eot")
    _t4_settings(rag_fire_mode="speech-window")
    _t4_settings(rag_fire_mode="round-sync")


# --------------------------------------------------------------------------- #
# AUD-10: hybrid counts _kb_stats exactly once per consumed round
# --------------------------------------------------------------------------- #
def test_hybrid_kb_stats_count_once():
    store = LanesStore(sets=[
        [[_chunk(1, "sales-psychology", "laneA chunk", 0.95)]],
        [[_chunk(2, "pain-points", "laneB chunk", 0.8)]],
    ])
    rt = CallRuntime(_t4_settings(rag_fire_mode="hybrid"), _LLM_JSON,
                     tracer=None, llm=None, http_client=mock_client(),
                     kb_store=store)

    async def go():
        before = dict(rt._kb_stats)
        rt.spawn_live_retrieve("Intake", {}, "", lane="laneA")
        await asyncio.shield(rt._live_tasks["Intake|laneA"])
        rt.spawn_live_retrieve("Intake", {}, "my calls go to voicemail",
                               lane="laneB")
        await asyncio.shield(rt._live_tasks["Intake|laneB"])
        await rt._consume_live_retrieve("Intake", {}, "my calls go to voicemail",
                                        store)
        delta = rt._kb_stats["rag_turns"] - before["rag_turns"]
        assert delta == 1, \
            f"AUD-10 regressed: hybrid counted {delta} rag_turns (want 1)"
        await rt.aclose()

    _run(go())


# --------------------------------------------------------------------------- #
# AUD-7: the ledger rag table gains mode/degraded/await_ms (idempotent)
# --------------------------------------------------------------------------- #
_OLD_RAG_DDL = """
CREATE TABLE rag (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, run_id TEXT,
  ts TEXT, kind TEXT, state TEXT, ms REAL, chunks INTEGER, kbs TEXT,
  query TEXT, prefetched INTEGER, frozen_reuse INTEGER, drifted INTEGER,
  force_drift INTEGER, cosine REAL, delta_chunks INTEGER
)
"""


def _load_live_sql():
    script = Path(__file__).resolve().parents[1] / "scripts" / "live_sql.py"
    spec = importlib.util.spec_from_file_location("live_sql", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_live_sql_rag_migration_adds_columns():
    live_sql = _load_live_sql()
    with tempfile.TemporaryDirectory() as td:
        con = sqlite3.connect(str(Path(td) / "ledger.db"))
        con.execute(_OLD_RAG_DDL)
        con.commit()
        live_sql._ensure_rag_cols(con)
        cols = [r[1] for r in con.execute("PRAGMA table_info(rag)")]
        assert "mode" in cols and "degraded" in cols and "await_ms" in cols
        # idempotent: a second pass is a no-op
        live_sql._ensure_rag_cols(con)
        cols2 = [r[1] for r in con.execute("PRAGMA table_info(rag)")]
        assert cols2 == cols
        # the import-path INSERT accepts a span dict with the new fields
        con.execute(
            "INSERT INTO rag(trace_id,run_id,ts,kind,state,ms,chunks,query,"
            "prefetched,frozen_reuse,drifted,force_drift,cosine,delta_chunks,"
            "mode,degraded,await_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("t1", "r1", "2026-09-21T00:00:00", "rag", "Intake", 12.5, 3, "q",
             0, 0, 0, 0, None, 0, "hybrid", 0, 61.2))
        row = con.execute(
            "SELECT mode, degraded, await_ms FROM rag WHERE trace_id='t1'"
        ).fetchone()
        assert row == ("hybrid", 0, 61.2)
        con.close()
    # the CREATE-TABLE path builds the new columns too (fresh ledgers)
    src = inspect.getsource(live_sql)
    assert "mode TEXT, degraded INTEGER, await_ms REAL" in src
    assert 'out.get("mode")' in src
    assert '1 if out.get("degraded") else 0' in src
    assert 'out.get("await_ms")' in src
