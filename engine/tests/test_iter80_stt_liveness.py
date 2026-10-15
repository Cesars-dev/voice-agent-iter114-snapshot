"""iter83 FIX 7 tests (STT liveness: adoption age gate + recv-heartbeat watchdog).

Laws under test (fixes MD §8.5):
  1. stale pooled socket refused (age > gate) -> None, caller falls back
  2. young socket adopted as today
  3. zombie: last_rx old + mic flowing -> watchdog fires _on_stt_disconnect
  4. silent caller (last_rx recent) -> no premature fire
  5. knob 0 -> watchdog exits immediately
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.session import CallSession
import diallux.media.prewarm as pw

SETTINGS = Settings(openai_api_key="t", retell_api_key="t",
                    langfuse_enabled=False, metrics_enabled=False,
                    stt_adopt_max_age_s=120.0,
                    stt_rx_zombie_ms=15000)


class FakeSTT:
    def __init__(self, last_rx=0.0):
        self._last_rx = last_rx
        self.closed = 0

    @property
    def last_rx(self) -> float:
        return self._last_rx

    async def close(self):
        self.closed += 1


class FakeTracer:
    def __init__(self):
        self.spans = []

    def span(self, name, **k):
        self.spans.append(name)


def _mk_session(**extra) -> CallSession:
    sess = object.__new__(CallSession)
    sess.settings = SETTINGS
    sess._stopped = False
    sess._ended = False
    sess.stt = FakeSTT()
    sess.tts = None
    sess.tracer = FakeTracer()
    sess._mic_chunks = 0
    sess._liveness_task = None
    for k, v in extra.items():
        setattr(sess, k, v)
    return sess


def test_stale_socket_refused(monkeypatch):
    monkeypatch.setitem(pw.pool, "stt", object())
    monkeypatch.setitem(pw._spawn_ts, "stt", time.monotonic() - 130)
    got = pw.get("stt", max_age_s=120.0)
    assert got is None                       # corpse refused
    assert "stt" not in pw.pool              # evicted


def test_fresh_socket_adopted(monkeypatch):
    ws = object()
    monkeypatch.setitem(pw.pool, "stt", ws)
    monkeypatch.setitem(pw._spawn_ts, "stt", time.monotonic() - 10)
    got = pw.get("stt", max_age_s=120.0)
    assert got is ws                         # young socket adopts as today


def test_zombie_fires_reconnect():
    reconnects = []
    sess = _mk_session()
    sess.stt = FakeSTT(last_rx=time.monotonic() - 20)
    sess._mic_chunks = 100

    async def fake_reconnect():
        reconnects.append(1)
        sess.stt = FakeSTT(last_rx=time.monotonic())

    sess._on_stt_disconnect = fake_reconnect

    async def run():
        task = asyncio.ensure_future(sess._stt_liveness_watchdog())
        await asyncio.sleep(1.6)             # > 1 tick + margin
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert reconnects, "watchdog must fire the existing reconnect"
    assert "stt:zombie_reconnect" in sess.tracer.spans


def test_silent_caller_no_premature_fire():
    sess = _mk_session()
    sess.stt = FakeSTT(last_rx=time.monotonic() - 3)
    sess._mic_chunks = 0                     # no caller audio -> never fire

    async def run():
        task = asyncio.ensure_future(sess._stt_liveness_watchdog())
        await asyncio.sleep(1.6)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    # also: mic flowing but last_rx recent -> no fire
    sess2 = _mk_session()
    sess2.stt = FakeSTT(last_rx=time.monotonic() - 3)
    sess2._mic_chunks = 50

    async def run2():
        task = asyncio.ensure_future(sess2._stt_liveness_watchdog())
        await asyncio.sleep(1.6)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run2())
    assert sess.tracer.spans == []
    assert sess2.tracer.spans == []


def test_knob_zero_off():
    sess = _mk_session(settings=Settings(
        openai_api_key="t", retell_api_key="t",
        langfuse_enabled=False, metrics_enabled=False,
        stt_rx_zombie_ms=0))

    async def run():
        task = asyncio.ensure_future(sess._stt_liveness_watchdog())
        await asyncio.sleep(1.2)
        assert task.done()                   # exited immediately
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
