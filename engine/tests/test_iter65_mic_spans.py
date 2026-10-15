"""iter65 T4 — mic-span pins: eot_silence_wait + tts_first_byte.

Stub-tracer unit pins on a CallSession built via __new__ (class-level
anchor defaults; no __init__ hazards). Spans are observability-only.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.session import CallSession
from diallux.observability.latency import TurnClock

SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, rag_fire_mode="round-sync")


class StubTracer:
    def __init__(self):
        self.spans: list[tuple[str, dict]] = []

    def span(self, name, **kw):
        self.spans.append((name, kw.get("metadata") or {}))


def _bare_session(tracer: StubTracer, settings=SETTINGS) -> CallSession:
    """CallSession without __init__ — only the attributes the handlers touch."""
    s = CallSession.__new__(CallSession)
    s.settings = settings
    s.tracer = tracer
    s._stopped = False
    s._ended = False
    s._real_turns = 0
    s._call_start = 0.0
    s._cap_closed = False
    s._turn_task = None
    s._clock = TurnClock(turn_index=0)
    s._speak_clock = None
    s._update_fired = False
    s._eager_transcript = None
    s._t_speech_growth = None          # instance default (class attr exists too)
    s._last_update_len = 0
    s.tts = None
    s._tts_context = None
    s.transport = "browser"
    return s


# --------------------------------------------------------------------------- #
# eot_silence_wait
# --------------------------------------------------------------------------- #
def test_eot_silence_wait_span_fires():
    """Growing updates then an eager EOT → ONE eot_silence_wait span, sane ms."""
    tracer = StubTracer()
    s = _bare_session(tracer)
    async def go():
        # simulates flux updates while the caller speaks
        await s._on_stt_update("hey")
        time.sleep(0.02)
        await s._on_stt_update("hey there how")
        time.sleep(0.03)
        # stub the turn runner: the eager path spawns it as a task
        async def _noop_turn(transcript, epoch=0):
            pass
        s._run_turn = _noop_turn
        await s._on_eager_eot("hey there how are you")
        # drain the spawned turn task
        task = s._turn_task
        if task:
            await task
    asyncio.run(go())
    spans = [m for name, m in tracer.spans if name == "eot_silence_wait"]
    assert len(spans) == 1
    assert spans[0]["turn"] == 1
    assert spans[0]["ms"] > 0          # trailing-silence window measured
    assert spans[0]["ms"] < 5000       # sane bound for a unit run


def test_eot_silence_wait_not_fired_when_no_growth():
    """No update ever landed → no span (None-guard), never crashes."""
    tracer = StubTracer()
    s = _bare_session(tracer)
    async def go():
        async def _noop_turn(transcript, epoch=0):
            pass
        s._run_turn = _noop_turn
        await s._on_eager_eot("whatever")
        task = s._turn_task
        if task:
            await task
    asyncio.run(go())
    assert not any(name == "eot_silence_wait" for name, _ in tracer.spans)


# --------------------------------------------------------------------------- #
# tts_first_byte
# --------------------------------------------------------------------------- #
def test_tts_first_byte_span_once_per_turn():
    """speak → first audio: ONE tts_first_byte span; ms = t_tts_first − t_tts_req."""
    tracer = StubTracer()
    s = _bare_session(tracer)
    sent: list[dict] = []

    async def fake_speak(text, continue_):
        pass

    async def go():
        clock = TurnClock(turn_index=3)
        s._clock = clock
        s._speak_clock = clock
        # first TTS request of the turn stamps the anchor
        await s._speak_chunk("Hello there.", False)
        assert clock.extra["t_tts_req"] is not None
        time.sleep(0.01)
        await s._on_tts_audio("AAAA", "ctx-1")       # first audio byte
        await s._on_tts_audio("BBBB", "ctx-1")       # second chunk — must NOT re-span
    asyncio.run(go())
    spans = [m for name, m in tracer.spans if name == "tts_first_byte"]
    assert len(spans) == 1
    assert spans[0]["turn"] == 3
    assert spans[0]["ms"] > 0 and spans[0]["ms"] < 5000


def test_tts_first_byte_requires_request_stamp():
    """Audio without a prior _speak_chunk → no span (None-guard)."""
    tracer = StubTracer()
    s = _bare_session(tracer)
    clock = TurnClock(turn_index=1)
    s._clock = clock
    s._speak_clock = clock
    asyncio.run(s._on_tts_audio("AAAA", "ctx-1"))
    assert not any(name == "tts_first_byte" for name, _ in tracer.spans)
