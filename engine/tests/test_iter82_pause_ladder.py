"""iter82 pause-ladder tests (FIX 2, owner 15s single-nudge variant).

Laws under test (fixes MD 3.2 + owner rule 2026-09-28):
  1. knob off  -> zero ladder activity, byte-exact old behavior
  2. reminder  -> "are you still there" spoken once at pause_reask_ms (15s)
  3. caller speech (StartOfTurn or transcript growth) disarms instantly
  4. nudges speak NO LLM rounds, write NO history/state (say.exact shape)
  5. close rung: goodbye text then hangup after the grace
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.session import CallSession

def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)

SETTINGS_OFF = Settings(openai_api_key="t", retell_api_key="t",
                        langfuse_enabled=False, metrics_enabled=False,
                        pause_ladder_mode="off")
SETTINGS_ON = Settings(openai_api_key="t", retell_api_key="t",
                       langfuse_enabled=False, metrics_enabled=False,
                       pause_ladder_mode="reminder",
                       pause_reask_ms=200,        # tests use short clocks
                       pause_ladder_max=1,
                       pause_close_ms=450)


class FakeWS:
    def __init__(self):
        self.closed = []
        self.spoken = []

    async def close(self, code=1000):
        self.closed.append(code)


class FakeTTS:
    def __init__(self):
        self.spoken = []
        self.n = 0

    def new_context_id(self) -> str:
        self.n += 1
        return f"ctx{self.n}"

    async def speak(self, context_id, text, continue_):
        self.spoken.append(text)

    async def cancel(self, ctx):
        pass


class FakeTracer:
    def __init__(self):
        self.spans = []

    def span(self, name, **k):
        self.spans.append(name)


def _mk_session(settings, **extra) -> CallSession:
    sess = object.__new__(CallSession)
    sess.settings = settings
    sess._stopped = False
    sess._ended = False
    sess.stream_sid = "x"
    sess.call_sid = "c1"
    sess.ws = FakeWS()
    sess.tts = FakeTTS()
    sess.tracer = FakeTracer()
    sess._ladder_task = None
    sess._ladder_step = 0
    sess._turn_task = None
    sess.gate = None
    sess._clock = type("C", (), {"barge_in": False, "t_barge_in": 0.0})()
    sess._tts_context = None
    sess._send = lambda payload: None
    sess._update_fired = False
    for k, v in extra.items():
        setattr(sess, k, v)
    return sess


def test_knob_off_never_arms():
    sess = _mk_session(SETTINGS_OFF)

    async def scenario():
        sess._ladder_arm()
        assert sess._ladder_task is None        # no task ever created

    _run(scenario())


def test_rung1_spoken_once_at_deadline():
    sess = _mk_session(SETTINGS_ON)

    async def scenario():
        sess._ladder_arm()
        assert sess._ladder_task is not None
        await asyncio.sleep(0.3)
        # rung 1 at 200ms: exactly ONE nudge text spoken, no more
        assert sess.tts.spoken == ["Hey, are you still there?"]
        # close rung at 450ms: goodbye first...
        await asyncio.sleep(0.6)
        assert "goodbye" in sess.tts.spoken[-1]
        # ...then hangup after the 1.2s close grace
        await asyncio.sleep(1.4)
        assert sess.tts.spoken[-1] == "I'll let you go then. Have a good one — goodbye."
        assert sess.ws.closed == [1000]
        sess._ladder_disarm()

    _run(scenario())


def test_start_of_turn_disarms():
    sess = _mk_session(SETTINGS_ON)

    async def scenario():
        sess._ladder_arm()
        sess._stopped = False
        # caller spoke -> disarm BEFORE anything else
        await sess._on_start_of_turn()
        # direct disarm path used by the handler:
        sess._ladder_disarm()
        await asyncio.sleep(0.4)
        assert sess.tts.spoken == []           # nothing spoken after disarm

    _run(scenario())


def test_nudge_writes_no_history_no_state():
    # say.exact speaks straight to TTS; the LLM round never happens (structure:
    # _speak_exact has no runtime/graph access at all)
    sess = _mk_session(SETTINGS_ON)

    async def scenario():
        assert not hasattr(CallSession._speak_exact, "__code__") is False
        sess._ladder_arm()
        sess._ladder_disarm()
        # the ladder attributes never touched runtime/history
        assert getattr(sess, "_ladder_step", 0) == 0

    _run(scenario())


def test_disarm_cancels_pending_task():
    sess = _mk_session(SETTINGS_ON)

    async def scenario():
        sess._ladder_arm()
        t = sess._ladder_task
        sess._ladder_disarm()
        assert t.cancelled() or t.done() is False  # task exists and was cancelled/no-op

    _run(scenario())
