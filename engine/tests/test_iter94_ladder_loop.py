"""iter94 pins — the silence-stretch ladder guard (Fix B).

Disease (iter93b evidence, research/surgeon/iter93b-call-pollution-assess/):
_ladder_arm() ran on EVERY agent turn (Twilio mark echo :434 + browser
late-report :851), cancel-restarting the ladder — so any fragment reply
("Absolutely, go ahead") reset the 8s nudge clock and pushed the 45s goodbye
out of reach: the "Hey, are you still there?" every-8-seconds nag.

Fix: one guard in _ladder_arm — a ladder already armed mid-stretch must NOT
be restarted by an agent turn. Stretch = caller speech (disarm :559/:695)
→ next agent turn end → arm. ONE nudge per stretch, close anchored at the
stretch start.

Fake clock: asyncio.sleep is patched to wait on a virtual clock the test
advances explicitly; settings use SPEC values (8000/1/45000).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.session import CallSession

REAL_SLEEP = asyncio.sleep

SPEC_SETTINGS = Settings(
    openai_api_key="t", retell_api_key="t",
    langfuse_enabled=False, metrics_enabled=False,
    pause_ladder_mode="reminder",
    pause_reask_ms=8000,          # SPEC value — virtual clock makes it instant
    pause_ladder_max=1,
    pause_close_ms=45000,
)
NUDGE = "Hey, are you still there?"
GOODBYE = SPEC_SETTINGS.pause_close_text


class VClock:
    def __init__(self):
        self.now = 0.0


class FakeWS:
    def __init__(self, clock):
        self.clock = clock
        self.closed = []

    async def close(self, code=1000):
        self.closed.append((code, self.clock.now))


class FakeTTS:
    def __init__(self, clock):
        self.clock = clock
        self.spoken = []

    def new_context_id(self) -> int:
        return len(self.spoken) + 1

    async def speak(self, context_id, text, continue_):
        self.spoken.append((text, self.clock.now))

    async def cancel(self, ctx):
        pass

    async def close(self):
        pass


class FakeSTT:
    async def close(self):
        pass


class FakeTracer:
    def __init__(self):
        self.spans = []

    def span(self, name, **k):
        self.spans.append(name)

    def finish(self, output=None):
        pass


def _mk_session(settings=SPEC_SETTINGS):
    clock = VClock()
    sess = object.__new__(CallSession)
    sess.settings = settings
    sess._stopped = False
    sess._ended = False
    sess.stream_sid = "x"
    sess.call_sid = "c94"
    sess.ws = FakeWS(clock)
    sess.tts = FakeTTS(clock)
    sess.stt = FakeSTT()
    sess.tracer = FakeTracer()
    sess._ladder_task = None
    sess._ladder_step = 0
    sess._liveness_task = None
    sess._turn_task = None
    sess.gate = None
    sess.runtime = None
    sess.turn_reports = []
    sess._outbox = asyncio.Queue()
    sess._clock = type("C", (), {"barge_in": False, "t_barge_in": 0.0})()
    return sess, clock


async def _drain(n: int = 4):
    for _ in range(n):
        await REAL_SLEEP(0)


async def _end(sess):
    if sess._ladder_task is not None and not sess._ladder_task.done():
        sess._ladder_disarm()
        await _drain(2)


def _clocked(scenario):
    clock_holder = {}

    async def _fake_sleep(d):
        wake = clock_holder["clock"].now + max(float(d), 0.0)
        while clock_holder["clock"].now < wake:
            await REAL_SLEEP(0)

    def _fake_perf_counter():
        return clock_holder["clock"].now

    import time as _time
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    saved_sleep, saved_pc = asyncio.sleep, _time.perf_counter
    asyncio.sleep = _fake_sleep
    _time.perf_counter = _fake_perf_counter
    try:
        loop.run_until_complete(scenario(clock_holder))
    finally:
        asyncio.sleep = saved_sleep
        _time.perf_counter = saved_pc
        loop.close()
        asyncio.set_event_loop(None)


def test_guard_midstretch_arm_keeps_task():
    """THE FIX: an agent turn while the ladder is armed must NOT restart it."""
    sess, clock = _mk_session()

    async def scenario(h):
        h["clock"] = clock
        sess._ladder_arm()
        await _drain(2)                     # ladder starts, waits at the 8s mark
        t = sess._ladder_task
        assert t is not None and not t.done()
        sess._ladder_arm()                  # fragment reply turn ends NOW
        sess._ladder_arm()                  # ...and another one
        assert sess._ladder_task is t       # same task — not cancelled/restarted
        assert not t.done()
        await _end(sess)

    _clocked(scenario)


def test_one_nudge_per_stretch_despite_agent_turns():
    """Fragment replies inside a stretch must not re-nudge or re-anchor."""
    sess, clock = _mk_session()

    async def scenario(h):
        h["clock"] = clock
        sess._ladder_arm()
        await _drain(2)                     # armed at stretch start
        clock.now += 8.0                    # nudge deadline
        await _drain(3)
        assert [t for t, _ in sess.tts.spoken] == [NUDGE]
        assert sess._ladder_step == 1
        sess._ladder_arm()                  # fragment-triggered agent turns
        sess._ladder_arm()
        assert sess._ladder_step == 1       # step state not reset
        clock.now += 32.0                   # t=40: FOUR more nudge windows pass
        await _drain(3)
        assert [t for t, _ in sess.tts.spoken] == [NUDGE]   # still exactly ONE
        await _end(sess)

    _clocked(scenario)


def test_close_fires_at_45s_despite_agent_turns():
    """Close anchors at stretch START (t0+45s), not at the last agent turn."""
    sess, clock = _mk_session()

    async def scenario(h):
        h["clock"] = clock
        sess._ladder_arm()
        await _drain(2)
        clock.now += 8.0                    # t=8: nudge
        await _drain(3)
        sess._ladder_arm()                  # agent turn mid-stretch
        clock.now += 20.0                   # t=28: nothing more may fire
        await _drain(3)
        assert [t for t, _ in sess.tts.spoken] == [NUDGE]
        clock.now += 17.0                   # t=45: close anchored at stretch start
        await _drain(3)
        texts = [t for t, _ in sess.tts.spoken]
        assert texts == [NUDGE, GOODBYE]
        _, goodbye_at = sess.tts.spoken[-1]
        assert goodbye_at == pytest.approx(45.0, abs=0.5)
        clock.now += 1.2                    # close grace elapses
        await _drain(4)
        assert sess.ws.closed and sess.ws.closed[0][0] == 1000
        assert sess._stopped
        await _end(sess)

    _clocked(scenario)


def test_caller_speech_resets_stretch():
    """Disarm on caller speech → next agent turn end arms a FRESH stretch
    with exactly one nudge of its own."""
    sess, clock = _mk_session()

    async def scenario(h):
        h["clock"] = clock
        sess._ladder_arm()
        await _drain(2)
        clock.now += 8.0                    # nudge #1
        await _drain(3)
        sess._ladder_disarm()               # caller speech (:559/:695)
        assert sess._ladder_task is None
        sess._ladder_arm()                  # next agent turn end
        await _drain(2)
        clock.now += 8.0                    # nudge #2 — exactly one more
        await _drain(3)
        texts = [t for t, _ in sess.tts.spoken]
        assert texts.count(NUDGE) == 2
        assert texts == [NUDGE, NUDGE]
        await _end(sess)

    _clocked(scenario)


def test_nudge_never_re_arms_itself():
    """_speak_exact sends no turn mark/report — the nudge must not restart
    the ladder (structural post-fix; pinned regardless)."""
    sess, clock = _mk_session()

    async def scenario(h):
        h["clock"] = clock
        sess._ladder_arm()
        await _drain(2)
        t = sess._ladder_task
        clock.now += 8.0                    # nudge fires
        await _drain(3)
        assert sess._ladder_task is t       # same task, not re-armed
        assert sess._ladder_step == 1
        clock.now += 8.0                    # another window passes
        await _drain(3)
        assert [t2 for t2, _ in sess.tts.spoken] == [NUDGE]
        await _end(sess)

    _clocked(scenario)
