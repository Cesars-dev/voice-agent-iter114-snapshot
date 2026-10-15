"""iter93 T1 observability pins (FIND-35 data-first commit — NO behavior change).

Pins:
  1. deepgram_raw_dump code default = False (env-gated, no log noise)
  2. dump OFF: raw TurnInfo never logged; ON: full payload logged
  3. adopt releases => released_epoch stamped on the turn clock extras
  4. held_swallow detector: held + no release + no audio => flagged on report
  5. rag fire probe: every fire attempt lands on clock extras (laneA probe)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.deepgram_stt import DeepgramSTT
from diallux.media.sentence_gate import SentenceGate
from diallux.media.session import CallSession
from diallux.observability.latency import TurnClock

BASE = dict(openai_api_key="t", retell_api_key="t",
            langfuse_enabled=False, metrics_enabled=False)
SETTINGS_ON = Settings(eager_hold_audio_until_confirm=True, **BASE)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class FakeClock:
    def __init__(self, extra=None):
        self.extra = extra or {}
        self.barge_in = False
        self.t_barge_in = 0.0


def _mk_session(settings, gate, tts=None, extra_clock=None, **extra):
    sent = []
    sess = object.__new__(CallSession)
    sess.settings = settings
    sess._stopped = False
    sess._ended = False
    sess.stream_sid = "x"
    sess.call_sid = "c1"
    sess.tts = tts
    sess.tracer = None
    sess.gate = gate
    sess._ladder_task = None
    sess._turn_task = None
    sess._clock = FakeClock(extra=extra_clock or {"eager": True})
    sess._tts_context = "ctx-0"
    sess._speak_clock = None
    sess._eager_transcript = None
    sess._resumed_count = 0
    sess._held = None
    sess._epoch = 0
    sess._turn_task_epoch = 0
    sess._tts_ctx_epoch = 0
    sess._real_turns = 0
    sess._call_start = None
    sess._update_fired = False
    sess._send = lambda payload: sent.append(payload)
    sess.sent = sent
    for k, v in extra.items():
        setattr(sess, k, v)
    return sess


def test_raw_dump_code_default_off():
    s = Settings(**BASE)
    assert s.deepgram_raw_dump is False


def test_dump_off_never_logs(caplog):
    async def scenario():
        eots = []
        stt = DeepgramSTT(
            SimpleNamespace(deepgram_raw_dump=False, deepgram_mode="flux"),
            on_eot=lambda t: eots.append(t) or asyncio.sleep(0),
            on_start_of_turn=lambda: asyncio.sleep(0), transport="twilio")
        with caplog.at_level(logging.INFO, logger="diallux.stt"):
            await stt._handle({"type": "TurnInfo", "event": "EndOfTurn",
                               "transcript": "hello there"})
        assert eots == ["hello there"]          # behavior unchanged
        assert not any("flux raw dump" in r.message for r in caplog.records)

    _run(scenario())


def test_dump_on_logs_payload(caplog):
    async def scenario():
        eots = []
        stt = DeepgramSTT(
            SimpleNamespace(deepgram_raw_dump=True, deepgram_mode="flux"),
            on_eot=lambda t: eots.append(t) or asyncio.sleep(0),
            on_start_of_turn=lambda: asyncio.sleep(0), transport="twilio")
        with caplog.at_level(logging.INFO, logger="diallux.stt"):
            await stt._handle({"type": "TurnInfo", "event": "EndOfTurn",
                               "transcript": "hello there", "confidence": 0.9})
        assert eots == ["hello there"]
        assert any("flux raw dump" in r.message and "confidence" in r.message
                   for r in caplog.records), "raw payload (confidence incl.) dumped"

    _run(scenario())


def test_adopt_stamps_released_epoch():
    async def scenario():
        calls = []
        gate = SentenceGate(
            lambda text, cont: calls.append((text, cont)) or asyncio.sleep(0))
        now = asyncio.get_event_loop().time()
        sess = _mk_session(SETTINGS_ON, gate,
                           extra_clock={"eager": True, "t_eager": now})
        sess._epoch = 2                          # eager fire bumped it
        sess._turn_task = asyncio.ensure_future(asyncio.sleep(5))
        sess._turn_task_epoch = 2
        sess._eager_transcript = "Friday works"
        gate.hold(2)
        sess._held = 2
        gate.add("Great, Friday works for me. ", 2)
        await sess._on_eot("Friday works")       # adopt branch
        assert sess._clock.extra.get("released_epoch") == 2, \
            "released_epoch stamped at adopt release"
        assert sess._held is None
        sess._turn_task.cancel()

    _run(scenario())


def test_held_swallow_detector_flags_and_clean_passes(caplog):
    async def aget_state(_config):
        return SimpleNamespace(values={"ended": False, "state_name": "Intake",
                                       "dvs": {}})

    def _sess(clock):
        sess = _mk_session(SimpleNamespace(
            metrics_enabled=False, hangup_mode="timer"), None)
        sess._clock = clock
        sess.tracer = SimpleNamespace(span=lambda *a, **k: None)
        sess.runtime = SimpleNamespace(graph=SimpleNamespace(
            aget_state=aget_state), _last_consume=None)
        sess._thread_config = {"configurable": {"thread_id": "t"}}
        sess._turn_mark_seq = 0
        sess._outbox = asyncio.Queue()
        sess.transport = "twilio"
        return sess

    async def scenario():
        # disease shape: CONFIRM ran, hold armed, no release, no audio
        clock = TurnClock(turn_index=7, extra={"held_epoch": 3,
                                               "eager_final_match": True})
        clock.t_user_end = 1.0
        clock.t_turn_done = 2.0
        clock.t_audio_out = 3.0                  # exit the drain loop instantly
        sess = _sess(clock)
        with caplog.at_level(logging.WARNING, logger="diallux.session"):
            await sess._late_turn_report(clock)
        # the flag rides the FINAL REPORT (span output + json log), not extra
        assert any("held_swallow anomaly" in r.message and "turn 7"
                   in r.message for r in caplog.records), \
            "swallow flagged on the turn report"

        # clean shape: released (or never held) => no flag
        n0 = len(caplog.records)
        clock2 = TurnClock(turn_index=8, extra={"held_epoch": 3,
                                                "eager_final_match": True,
                                                "released_epoch": 3})
        clock2.t_user_end = 1.0
        clock2.t_turn_done = 2.0
        clock2.t_audio_out = 3.0
        sess2 = _sess(clock2)
        with caplog.at_level(logging.WARNING, logger="diallux.session"):
            await sess2._late_turn_report(clock2)
        assert not any("held_swallow anomaly" in r.message
                       for r in caplog.records[n0:])

        # never-held shape (non-eager turn): no flag
        n1 = len(caplog.records)
        clock3 = TurnClock(turn_index=9)
        clock3.t_user_end = 1.0
        clock3.t_turn_done = 2.0
        clock3.t_audio_out = 3.0
        sess3 = _sess(clock3)
        await sess3._late_turn_report(clock3)
        assert not any("held_swallow anomaly" in r.message
                       for r in caplog.records[n1:])

    _run(scenario())


def test_rag_fire_probe_records_lanes():
    async def scenario():
        sess = _mk_session(SimpleNamespace(rag_fire_mode="hybrid"), None)
        sess.tracer = SimpleNamespace(span=lambda *a, **k: None)
        sess.runtime = None                      # probe must record even on no-runtime
        sess._fire_live_retrieve("hi", lane="laneB")
        sess._fire_live_retrieve("", lane="laneA")
        assert sess._clock.extra.get("rag_fires") == ["laneB", "laneA"], \
            "every fire attempt recorded with its lane (PT-79)"

    _run(scenario())
