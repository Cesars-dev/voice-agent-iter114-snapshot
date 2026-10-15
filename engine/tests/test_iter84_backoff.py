"""iter84 FIX 5 tests (post-interrupt backoff, owner 500ms variant).

Laws under test (fixes MD §6.3, adapted: real-barge stamp point is
_on_start_of_turn's interrupted branch at fa62162 — _barge_resolve is a
FIX 4 primitive that does not exist in this tree):
  1. first chunk after a real barge-in waits out the remaining window
  2. one-shot: second chunk does NOT wait (window read+cleared)
  3. no barge-in -> no wait
  4. knob 0 -> no stamp, no wait ever
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.session import CallSession

SETTINGS_ON = Settings(openai_api_key="t", retell_api_key="t",
                       langfuse_enabled=False, metrics_enabled=False,
                       post_interrupt_backoff_ms=500)
SETTINGS_OFF = Settings(openai_api_key="t", retell_api_key="t",
                        langfuse_enabled=False, metrics_enabled=False,
                        post_interrupt_backoff_ms=0)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class FakeClock:
    def __init__(self):
        self.extra = {"t_tts_req": None}
        self.barge_in = False
        self.t_barge_in = 0.0


def _mk_session(settings, **extra) -> CallSession:
    sess = object.__new__(CallSession)
    sess.settings = settings
    sess._stopped = False
    sess._ended = False
    sess.stream_sid = "x"
    sess.call_sid = "c1"
    sess.tts = None
    sess.tracer = None
    sess.gate = None
    sess._ladder_task = None
    sess._turn_task = None
    sess._clock = FakeClock()
    sess._tts_context = None
    sess._send = lambda payload: None
    sess._update_fired = False
    for k, v in extra.items():
        setattr(sess, k, v)
    return sess


def test_first_chunk_waits_out_remaining_window(monkeypatch):
    sess = _mk_session(SETTINGS_ON)

    async def scenario():
        waited = []
        real_sleep = asyncio.sleep

        async def fake_sleep(s, *a, **k):
            waited.append(s)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        # real barge-in: a turn task is running when StartOfTurn arrives
        task = asyncio.ensure_future(real_sleep(5))
        sess._turn_task = task
        await sess._on_start_of_turn()
        assert sess._backoff_until is not None       # window stamped
        assert task.cancelling() >= 1                # turn cancel was requested
        assert sess._turn_task is None               # iter93: slot cleared sync
        await sess._speak_chunk("hello", True)       # first chunk of next turn
        assert len(waited) == 1 and waited[0] > 0    # slept out the remainder
        assert waited[0] <= 0.5
        assert sess._backoff_until is None           # cleared
        assert sess._clock.extra["t_tts_req"] is not None

    _run(scenario())


def test_one_shot_second_chunk_no_wait(monkeypatch):
    sess = _mk_session(SETTINGS_ON)

    async def scenario():
        waits = []
        real_sleep = asyncio.sleep

        async def fake_sleep(s, *a, **k):
            waits.append(s)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        sess._turn_task = asyncio.ensure_future(real_sleep(5))
        await sess._on_start_of_turn()
        await sess._speak_chunk("one", True)
        await sess._speak_chunk("two", True)
        await sess._speak_chunk("three", True)
        assert len(waits) == 1                       # ONE-SHOT: only the first
        assert all(w > 0 for w in waits)

    _run(scenario())


def test_no_barge_no_wait(monkeypatch):
    sess = _mk_session(SETTINGS_ON)

    async def scenario():
        waited = []
        real_sleep = asyncio.sleep

        async def fake_sleep(s, *a, **k):
            waited.append(s)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        # turn task already done -> NOT a real barge-in
        sess._turn_task = asyncio.ensure_future(real_sleep(0))
        await real_sleep(0.05)
        await sess._on_start_of_turn()
        assert sess._backoff_until is None
        await sess._speak_chunk("hi", True)
        assert waited == []                          # never slept

    _run(scenario())


def test_knob_zero_no_stamp_no_wait(monkeypatch):
    sess = _mk_session(SETTINGS_OFF)

    async def scenario():
        waited = []
        real_sleep = asyncio.sleep

        async def fake_sleep(s, *a, **k):
            waited.append(s)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        sess._turn_task = asyncio.ensure_future(real_sleep(5))
        await sess._on_start_of_turn()               # real barge-in, knob is 0
        assert sess._backoff_until is None           # never stamped
        await sess._speak_chunk("hi", True)
        assert waited == []
        assert sess._clock.extra["t_tts_req"] is not None

    _run(scenario())
