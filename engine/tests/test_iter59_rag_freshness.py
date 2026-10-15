"""iter59 T3 — RAG freshness pins (hermetic: no Postgres, no model).

The freshness bug: _fire_live_retrieve fired at EOT/EagerEOT — the SAME tick
the round consumed it — so the bounded await (60 ms) could never cover the
~123 ms batched embed and 23/24 live spans degraded. The fix, per fire mode
(rag_fire_mode): "speech-window" (fire at first flux Update, skip the EOT
fire), "round-sync" (inline retrieval), "hybrid" (lane A at first Update,
lane B single-query at EOT), "eot" (iter58 exact surface, revert).

Flux Update semantics (docs, resolved 2026-09-20): ~every 0.25 s of audio;
transcript CUMULATIVE within the turn; NO is_final field. The once-per-turn
fire guard is a BOOLEAN (clocks swap at EOT, not StartOfTurn — C8).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.graph.builder import CallRuntime
from diallux.media.session import CallSession
from tests.test_iter49_rag_parity import (
    _LLM_JSON, _chunk, _run, _t4_settings, LanesStore, mock_client,
)


def _chunk_ret(i, kb, content, score=0.9):
    return [_chunk(i, kb, content, score)]


def _settings(**kw) -> Settings:
    return _t4_settings(**kw)


def _rt(store, fake=None, **kw):
    return CallRuntime(_settings(**kw), _LLM_JSON, tracer=None,
                       llm=fake, http_client=mock_client(), kb_store=store)


# --------------------------------------------------------------------------- #
# 1. deepgram_stt: Update handler wired, fire-safe
# --------------------------------------------------------------------------- #
def test_stt_update_handler_fires_only_with_transcript():
    """flux Update = ~every 0.25 s (often empty transcript) — the handler
    fires ONLY on a non-empty transcript, and is fire-safe when absent."""
    from diallux.media.deepgram_stt import DeepgramSTT

    class _S:
        deepgram_mode = "flux"

    seen: list[str] = []
    stt = DeepgramSTT.__new__(DeepgramSTT)
    stt.settings = _S()
    stt.on_update = lambda t: _mark(seen, t)
    asyncio.run(stt._handle({"type": "TurnInfo", "event": "Update",
                             "transcript": ""}))
    asyncio.run(stt._handle({"type": "TurnInfo", "event": "Update",
                             "transcript": "Hi I need to"}))
    assert seen == ["Hi I need to"], seen


def test_stt_no_update_callback_is_silent():
    """on_update=None (default ctor) — the Update branch is a no-op."""
    from diallux.media.deepgram_stt import DeepgramSTT
    stt = DeepgramSTT.__new__(DeepgramSTT)
    stt.settings = type("S", (), {"deepgram_mode": "flux"})()
    stt.on_update = None
    asyncio.run(stt._handle({"type": "TurnInfo", "event": "Update",
                             "transcript": "hello"}))   # must not raise


async def _mark(seen, t):
    seen.append(t)


# --------------------------------------------------------------------------- #
# 2. session: first-Update fire once per turn + EOT skip (speech-window)
# --------------------------------------------------------------------------- #
def _session(settings, store):
    """A bare CallSession (no start()) with the runtime + fire-mode wiring
    the fire decision touches — object.__new__ pattern (iter44 test)."""
    from diallux.media.session import CallSession
    s = object.__new__(CallSession)
    s.settings = settings
    s.call_sid = "c59"
    s.stream_sid = "s59"
    s.transport = "browser"
    s.tracer = None
    s._stopped = False
    s._ended = False
    s._turn_state = "Intake"
    s._turn_dvs = {}
    s._update_fired = False
    s.runtime = CallRuntime(settings, {"default_dynamic_variables": {},
                                       "starting_state": "Intake"},
                            tracer=None, llm=None, http_client=mock_client(),
                            kb_store=store)
    return s


def test_speech_window_first_update_fires_once_per_turn():
    """Update #1 fires the full retrieval; Updates 2..N are ignored (boolean
    guard); StartOfTurn re-arms."""
    calls: list[tuple[str, str]] = []

    rt_calls = []

    class FakeRuntime:
        def spawn_live_retrieve(self, state, dvs, msg, lane="full"):
            calls.append((state, msg, lane))

    settings = _settings(rag_fire_mode="speech-window")
    sess = object.__new__(CallSession)
    sess.settings = settings
    sess._stopped = False
    sess._ended = False
    sess._update_fired = False
    sess._turn_state = "Intake"
    sess._turn_dvs = {}
    sess.runtime = FakeRuntime()
    sess.call_sid = "c59"

    sess._turn_task = None
    sess._clock = None
    sess.gate = None
    sess.tts = None
    sess.stream_sid = "s59"
    sess.tracer = None
    sess.settings.metrics_enabled = False

    async def up(t):
        await sess._on_stt_update(t)

    asyncio.run(up("Hi"))
    asyncio.run(up("Hi I need"))
    asyncio.run(up("Hi I need to cancel"))
    assert calls == [("Intake", "Hi", "full")], calls
    assert sess._update_fired is True
    # StartOfTurn re-arms
    asyncio.run(sess._on_start_of_turn())
    assert sess._update_fired is False


def test_speech_window_update_fired_skips_eot_fire():
    """Update landed ⇒ the EOT fire is SKIPPED (a re-spawn would cancel the
    landed task → the iter58 degrade)."""
    from diallux.media.session import CallSession
    calls: list = []

    class FakeRuntime:
        def spawn_live_retrieve(self, state, dvs, msg, lane="full"):
            calls.append((state, msg, lane))

    sess = object.__new__(CallSession)
    sess.settings = _settings(rag_fire_mode="speech-window")
    sess._stopped = False
    sess._ended = False
    sess._update_fired = True
    sess.runtime = FakeRuntime()
    sess._clock = type("C", (), {"turn_index": 0, "extra": {}, "barge_in": False})()
    sess._turn_state = "Intake"
    sess._turn_dvs = {}
    sess._turn_task = None
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

    asyncio.run(sess._on_eot("Hi I need to cancel my subscription"))
    assert calls == [], calls     # the EOT fire was SKIPPED
    assert sess._update_fired is False
    assert sess._turn_task is not None


def test_eot_mode_fires_at_eot_as_iter58():
    """regression: fire_mode="eot" keeps the iter58 surface — fire at EOT,
    never on Update."""
    from diallux.media.session import CallSession
    calls: list = []

    class FakeRuntime:
        def spawn_live_retrieve(self, state, dvs, msg, lane="full"):
            calls.append((state, msg, lane))

    sess = object.__new__(CallSession)
    sess.settings = _settings(rag_fire_mode="eot")
    sess._stopped = False
    sess._ended = False
    sess._update_fired = False
    sess.runtime = FakeRuntime()
    sess._turn_state = "Intake"
    sess._turn_dvs = {}

    sess._turn_task = None
    sess._clock = None
    sess.gate = None
    sess.tts = None
    sess.stream_sid = "s59"
    sess.tracer = None
    sess.settings.metrics_enabled = False

    async def up(t):
        await sess._on_stt_update(t)

    asyncio.run(up("partial"))
    assert calls == [], "eot mode must ignore Updates"
    sess._clock = type("C", (), {"turn_index": 0, "extra": {}, "barge_in": False})()
    sess._turn_task = None
    sess._real_turns = 0
    sess._call_start = 0.0
    sess._cap_closed = False
    sess._turn_reports = []
    sess.stream_sid = "s59"
    sess.call_sid = "c59"
    sess.tracer = None
    sess.tts = None
    sess.gate = None
    sess._thread_config = {"configurable": {"thread_id": "c59"}}
    sess._speak_clock = None
    sess._tts_context = None
    sess._run_turn = _fake_run
    sess._graceful_cap_close = _async_noop
    asyncio.run(sess._on_eot("the full transcript"))
    assert calls == [("Intake", "the full transcript", "full")]


async def _fake_run(transcript, epoch=0):
    return None


async def _async_noop():
    pass


# --------------------------------------------------------------------------- #
# 3. builder: turn-keyed supersede / landed-fresh / _live_consumed degrade
# --------------------------------------------------------------------------- #
def test_same_text_rerun_bumps_seq_not_msg_key():
    """Same text, changed dvs (the same-text-rerun edge): the second spawn
    supersedes the first and bumps the seq — the consumer can never serve
    the stale fire as fresh."""
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "chunk a")]]])
    rt = CallRuntime(_settings(), _LLM_JSON, tracer=None,
                     llm=None, http_client=mock_client(), kb_store=store)

    async def go():
        rt.spawn_live_retrieve("Intake", {}, "same text twice")
        t1 = rt._live_tasks["Intake|full"]
        seq1 = rt._live_task_seq["Intake|full"]
        rt.spawn_live_retrieve("Intake", {"industry": "hvac"}, "same text twice")
        t2 = rt._live_tasks["Intake|full"]
        assert t1 is not t2
        await asyncio.sleep(0.05)          # cancellation propagates
        assert t1.cancelled() or t1.done()
        assert rt._live_task_seq["Intake|full"] == seq1 + 1
        # consume: fresh fire not yet consumed -> lands fresh (degraded=False)
        d, k, ms, ch, lq, deg, aw = await rt._consume_live_retrieve(
            "Intake", {"industry": "hvac"}, "same text twice", store)
        assert deg is False
        assert "pain-points" in k
        await rt.aclose()
    seq1 = 0
    _run(go())


def test_landed_fresh_preferred_and_consumed_recorded():
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "fresh chunk")]]])
    rt = CallRuntime(_settings(rag_fire_mode="speech-window"), _LLM_JSON,
                     tracer=None, llm=None,
                     http_client=mock_client(), kb_store=store)

    async def go():
        # fire at "Update" (partial msg), land, then consume at EOT with the
        # FINAL transcript (msg differs — seq matching, not msg matching)
        rt.spawn_live_retrieve("Intake", {}, "partial", lane="full")
        await asyncio.shield(rt._live_tasks["Intake|full"])
        d, k, ms, ch, lq, deg, aw = await rt._consume_live_retrieve(
            "Intake", {}, "the complete final transcript", store)
        assert deg is False
        assert aw < 10
        assert "pain-points" in k
        assert rt._live_consumed_res["Intake"]["chunks"], "C4 stash written"
        await rt.aclose()

    _run(go())


def test_timeout_degrades_to_live_consumed_not_unconsumed_prev():
    """C4: the timeout fallback degrades to the last CONSUMED fresh set —
    never _live_prev's unconsumed overwrite."""
    # call 1: a fresh set lands and is consumed
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "consumed set")]]],
                       delay=0.0)
    rt = CallRuntime(_settings(rag_fire_mode="speech-window"), _LLM_JSON,
                     tracer=None, llm=None,
                     http_client=mock_client(), kb_store=store)

    async def go():
        rt.spawn_live_retrieve("Intake", {}, "first message lands")
        await asyncio.shield(rt._live_tasks["Intake|full"])
        await rt._consume_live_retrieve("Intake", {}, "first message lands", store)
        consumed = rt._live_consumed_res["Intake"]
        # call 2: a fire that will NEVER land within the cap (slow store),
        # while _live_prev gets overwritten by an UNCONSUMED slow result
        rt.settings.rag_live_await_ms = 5
        slow = LanesStore(sets=[[[_chunk(9, "sales-language", "slow set")]]],
                          delay=2.0)
        rt._resolve_kb_store = lambda: _async_ret(slow)
        rt.spawn_live_retrieve("Intake", {}, "slow second fire")
        d, k, ms, ch, lq, deg, aw = await rt._consume_live_retrieve(
            "Intake", {}, "slow second fire", slow)
        assert deg is True and aw < 200
        assert "pain-points" in k and d != ""      # the CONSUMED set, not the slow one
        assert "consumed set" in (d or "")
        await rt.aclose()

    _run(go())


async def _async_ret(v):
    return v


def test_round_sync_retrieves_inline():
    """round-sync: NO task — one inline retrieval on the round path
    (span label live-sync), degraded 0% by construction."""
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "sync chunk")]]])
    rt = CallRuntime(_settings(rag_fire_mode="round-sync"), _LLM_JSON,
                     tracer=None, llm=None,
                     http_client=mock_client(), kb_store=store)

    async def go():
        d, k, ms, ch, lq, deg, aw = await rt._consume_live_retrieve(
            "Intake", {}, "an inline round sync message", store)
        assert deg is False
        assert "pain-points" in k
        assert rt._live_mode_label == "live-sync"
        assert len(store.lane_calls) == 1
        await rt.aclose()

    _run(go())


def test_hybrid_lane_split_and_merge():
    """hybrid: lane A (fired at Update, EMPTY utterance) + lane B (fired at
    EOT, single query) merge through ONE merge_lane_chunks; lane A's owned
    KB exemption survives."""
    # retrieve_lanes receives laneA's lanes first, then laneB's
    store = LanesStore(sets=[
        [[_chunk(1, "sales-psychology", "laneA owned chunk", 0.95)]],
        [[_chunk(2, "pain-points", "laneB utterance chunk", 0.8)]],
    ])
    rt = CallRuntime(_settings(rag_fire_mode="hybrid"), _LLM_JSON,
                     tracer=None, llm=None,
                     http_client=mock_client(), kb_store=store)

    async def go():
        rt.spawn_live_retrieve("Intake", {}, "", lane="laneA")
        await asyncio.shield(rt._live_tasks["Intake|laneA"])
        rt.spawn_live_retrieve("Intake", {}, "my calls go to voicemail",
                               lane="laneB")
        await asyncio.shield(rt._live_tasks["Intake|laneB"])
        d, k, ms, ch, lq, deg, aw = await rt._consume_live_retrieve(
            "Intake", {}, "my calls go to voicemail", store)
        assert deg is False
        assert len(ch) >= 2                       # both lanes in the merged set
        kbs = {c["kb"] for c in ch}
        assert "sales-psychology" in kbs and "pain-points" in kbs
        # lane A landed -> consumed floor recorded
        assert rt._live_consumed["Intake"] >= 1
        await rt.aclose()

    _run(go())


def test_hybrid_lane_b_only_when_lane_a_absent():
    """iter62: an eager-off turn (no laneB fire) has NOTHING to consume —
    the round FAILS CLEAN (inline respawn deleted; fires are session-owned;
    chat surfaces show this exact shape — documented, not a regression)."""
    store = LanesStore(sets=[[[_chunk(3, "pain-points", "laneB only", 0.8)]]])
    rt = CallRuntime(_settings(rag_fire_mode="hybrid"), _LLM_JSON,
                     tracer=None, llm=None,
                     http_client=mock_client(), kb_store=store)

    async def go():
        d, k, ms, ch, lq, deg, aw = await rt._consume_live_retrieve(
            "Intake", {}, "spawning lane b inline", store)
        assert deg is True and d == "" and k == [] and ch == []
        assert store.lane_calls == []          # nothing fired inline
        await rt.aclose()

    _run(go())


def test_hybrid_degrades_never_raises():
    """No store / both lanes fail → clean degrade (C4 fallback), no raise."""
    rt = CallRuntime(_settings(rag_fire_mode="hybrid"),
                     {"default_dynamic_variables": {},
                      "starting_state": "Intake"}, tracer=None, llm=None,
                     http_client=mock_client(), kb_store=None)

    async def boom(settings):
        raise RuntimeError("down")

    rt._resolve_kb_store = boom

    async def go():
        d, k, ms, ch, lq, deg, aw = await rt._consume_live_retrieve(
            "Intake", {}, "store is down", None)
        assert deg is True and d == "" and k == []
        await rt.aclose()

    _run(go())


def test_retrieval_off_attribute_is_empty_set():
    """T4: the attribute STAYS (gate/tests reference it) — now EMPTY."""
    assert CallRuntime._RETRIEVAL_OFF == set()
