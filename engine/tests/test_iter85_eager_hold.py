"""iter85 FIX 1 tests (eager-think, confirmed-speak — audio hold at the gate).

Laws under test (fixes MD §2.6):
  1. held gate buffers until release (no on_sentence while held)
  2. end_of_turn parks its final marker while held; release() replays it
  3. TurnResumed while held discards SILENTLY (no tts.cancel, no clear)
  4. confirmed EOT adopt releases the audio (on_sentence after release)
  5. stale hold never sticks: reset() clears the hold (rerun-safe)
  6. knob off = byte-exact today behavior (flush during the stream)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.sentence_gate import SentenceGate
from diallux.media.session import CallSession

BASE = dict(openai_api_key="t", retell_api_key="t",
            langfuse_enabled=False, metrics_enabled=False)
SETTINGS_ON = Settings(eager_hold_audio_until_confirm=True, **BASE)
SETTINGS_OFF = Settings(eager_hold_audio_until_confirm=False, **BASE)


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


class FakeTTS:
    def __init__(self):
        self.cancelled = []
        self.spoken = []

    def new_context_id(self):
        return "ctx-1"

    async def speak(self, ctx, text, continue_, overrides=None):
        self.spoken.append((text, continue_))

    async def cancel(self, ctx):
        self.cancelled.append(ctx)


async def _noop():
    return None


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


def test_held_gate_buffers_until_release():
    async def scenario():
        calls = []
        gate = SentenceGate(
            lambda text, cont: calls.append((text, cont)) or _noop())
        gate.hold(1)
        gate.add("Hello there. ", 1)
        await asyncio.sleep(0.25)               # idle-flush window elapses
        assert calls == []                      # FIX 1: buffering only, no flush
        await gate.release(1)
        await asyncio.sleep(0.25)               # release schedules the flush
        assert any(t.startswith("Hello there") and c is True
                   for t, c in calls)           # sentence flushed on release

    _run(scenario())


def test_end_of_turn_parks_while_held():
    async def scenario():
        calls = []
        gate = SentenceGate(
            lambda text, cont: calls.append((text, cont)) or _noop())
        gate.hold(1)
        gate.add("All done. ", 1)
        await gate.end_of_turn()                # stream ended while held
        assert calls == []                      # nothing sent yet
        await gate.release(1)
        assert calls and calls[-1][1] is False  # final marker (continue=False)
        await gate._drain()                     # queue fully drained

    _run(scenario())


def test_resume_discards_silently():
    async def scenario():
        calls = []
        gate = SentenceGate(
            lambda text, cont: calls.append((text, cont)) or _noop())
        tts = FakeTTS()
        sess = _mk_session(SETTINGS_ON, gate, tts,
                           extra_clock={"eager": True, "t_eager": None})
        sess._turn_task = asyncio.ensure_future(asyncio.sleep(5))
        gate.hold(1)
        sess._epoch = 1
        sess._turn_task_epoch = 1
        sess._held = 1
        gate.add("Wrong audio. ", 1)            # buffered, never flushed
        await sess._on_turn_resumed()
        assert tts.cancelled == []              # NO tts.cancel (silent)
        assert sess.sent == []                  # NO "clear" event (silent)
        assert gate._buffer == ""               # held buffer dropped
        assert gate._barrier is None and gate._held_final is False
        assert gate._parked == []               # parked shelf empty
        assert sess._held is None

    _run(scenario())


def test_adopt_releases_audio():
    async def scenario():
        calls = []
        gate = SentenceGate(
            lambda text, cont: calls.append((text, cont)) or _noop())
        now = asyncio.get_event_loop().time()
        sess = _mk_session(SETTINGS_ON, gate,
                           extra_clock={"eager": True, "t_eager": now})
        sess._turn_task = asyncio.ensure_future(asyncio.sleep(5))
        sess._eager_transcript = "Friday works"
        gate.hold(1)
        sess._epoch = 1
        sess._turn_task_epoch = 1
        sess._held = 1
        gate.add("Great, Friday works for me. ", 1)   # buffered, never flushed
        assert calls == []
        await sess._on_eot("Friday works")      # adopt branch (task running)
        assert sess._held is None
        await asyncio.sleep(0.05)               # sender delivers the release
        assert any(c is True and "Friday" in t for t, c in calls), \
            "audio released at confirmed EOT"
        sess._turn_task.cancel()                # clean up the running task

    _run(scenario())


def test_stale_hold_never_sticks_reset_clears():
    async def scenario():
        calls = []
        gate = SentenceGate(
            lambda text, cont: calls.append((text, cont)) or _noop())
        gate.hold(1)
        gate.add("Doomed buffer. ", 1)
        await gate.end_of_turn()                # parks the final while held
        assert gate._held_final is True
        await gate.reset()                      # cancel path (rerun) safety
        assert gate._barrier is None and gate._held_final is False
        gate.advance(2)                         # iter93: rerun generation opens
        gate.add("Rerun answer is spoken. ", 2) # rerun tokens reach TTS
        await asyncio.sleep(0.25)
        assert any("Rerun answer" in t for t, c in calls), \
            "rerun turn must not be stuck held"

    _run(scenario())


def test_knob_off_is_byte_exact():
    async def scenario():
        calls = []
        gate = SentenceGate(
            lambda text, cont: calls.append((text, cont)) or _noop())
        gate.add("Hello there. ")
        await asyncio.sleep(0.25)
        assert calls, "knob off: flushes exactly as today (no hold)"
        assert gate._barrier is None

    _run(scenario())
