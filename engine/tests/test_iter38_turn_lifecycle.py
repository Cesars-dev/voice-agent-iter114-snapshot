"""iter38 turn-lifecycle regression tests (memory/latency fix).

Covers the 2026-09-09 micbridge disaster call (`baccf630adfd`) root causes:
  F1 eager-adopt on EOT (PT-33) + the TurnResumed race guard
  F2 _late_turn_report hangup (NameError regression)
  F3 hard turn cap with graceful close (PT-34)
  F4 idempotent user-append in ingest
  F5 barge_in carried across the clock swap
  F6 audio_out stamped on the real socket write
"""
from __future__ import annotations

import asyncio
import time
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.media.session import CallSession
from diallux.observability.latency import TurnClock

from tests.fake_llm import FakeLLM
from tests.test_graph import get_state_sync, make_runtime, run_turn_sync

SETTINGS = Settings(openai_api_key="t", retell_api_key="t", langfuse_enabled=False,
                    metrics_enabled=False)


class FakeWS:
    def __init__(self):
        self.closed = []
        self.sent_bytes = []
        self.sent_text = []

    async def close(self, code=1000):
        self.closed.append(code)

    async def send_bytes(self, b):
        self.sent_bytes.append(bytes(b))

    async def send_text(self, s):
        self.sent_text.append(s)


class FakeGate:
    def __init__(self):
        self.added = []
        self.ended = 0

    def add(self, text, epoch=0):
        self.added.append(text)

    async def end_of_turn(self):
        self.ended += 1

    async def reset(self):
        pass

    async def discard(self, upto=0):
        self.discards = getattr(self, "discards", 0) + 1

    def invalidate(self, upto=0):
        self.discards = getattr(self, "discards", 0) + 1

    def cancel(self, *a, **k):
        pass

    def discard(self, upto=0):
        self.discards = getattr(self, "discards", 0) + 1

    def epoch_ready(self):
        pass

    def advance(self, epoch=0):
        pass


class FakeTracer:
    def span(self, *a, **k):
        pass


def _mk_session(**extra) -> CallSession:
    sess = object.__new__(CallSession)          # only the attrs each test uses
    sess.settings = SETTINGS
    sess._stopped = False
    sess._ended = False
    sess._cap_closed = False
    sess._call_start = 0.0
    sess._real_turns = 0
    sess._turn_task = None
    sess._clock = TurnClock()
    sess.stream_sid = "x"
    sess.call_sid = "c1"
    sess.transport = "browser"
    sess._thread_config = {"configurable": {"thread_id": "c1"}}
    sess._outbox = asyncio.Queue()
    sess.ws = FakeWS()
    sess.gate = FakeGate()
    sess.tts = None
    sess._tts_context = None
    sess.tracer = FakeTracer()
    for k, v in extra.items():
        setattr(sess, k, v)
    return sess


# --------------------------------------------------------------------------- #
def test_eot_adopts_running_eager_turn():
    """F1 (PT-33): EOT while an EAGER turn is running must ADOPT it — no
    cancel, no rerun, no second history append. The old code cancelled and
    re-ran, double-writing every user utterance (disaster call: ~40 dupes)."""

    async def go():
        sess = _mk_session()
        eager_clock = TurnClock(turn_index=5, extra={"eager": True})
        sess._clock = eager_clock
        runs = []

        async def fake_run(transcript, epoch=0):
            runs.append(transcript)

        sess._run_turn = fake_run
        task = asyncio.ensure_future(asyncio.sleep(3600))
        sess._turn_task = task
        sess._eager_transcript = "hello"        # iter93: adopt = transcript-equality
        await asyncio.sleep(0.05)               # let the eager "turn" start
        await sess._on_eot("hello")
        await asyncio.sleep(0.05)
        assert sess._clock is eager_clock           # clock NOT swapped
        assert runs == []                           # NO rerun
        assert not task.done() or task.cancelled() is False or True  # adopted, not cancelled
        assert task.cancelled() is False
        task.cancel()

    asyncio.run(go())


def test_turn_resumed_then_eot_reruns_once():
    """F1 race guard: eager -> TurnResumed -> EOT must RERUN (not adopt the
    dying task) and the rerun writes the utterance exactly once."""

    async def go():
        sess = _mk_session()
        sess._clock = TurnClock(turn_index=1, extra={"eager": True})
        task = asyncio.ensure_future(asyncio.sleep(3600))
        sess._turn_task = task
        await sess._on_turn_resumed()
        assert sess._clock.extra.get("eager") is False   # marker cleared FIRST
        runs = []

        async def fake_run(transcript, epoch=0):
            runs.append(transcript)

        sess._run_turn = fake_run
        await sess._on_eot("hello again")
        await asyncio.sleep(0.05)
        assert runs == ["hello again"]              # reran once
        task.cancel()

    asyncio.run(go())


def test_ingest_dedupe_consecutive_user():
    """F4: a rerun with an identical user text must NOT append a second user
    entry to history (graph-level defense-in-depth)."""
    fake = FakeLLM([
        {"tokens": ["Hi", " there", "."]},
        {"tokens": ["Still", " here", "."]},
    ])
    rt = make_runtime(fake)
    run_turn_sync(rt, "dedupe-1", "Hello there Linda", first=True)
    first_users = [m["content"] for m in get_state_sync(rt, "dedupe-1")["history"]
                   if m["role"] == "user"]
    assert first_users == ["Hello there Linda"]
    run_turn_sync(rt, "dedupe-1", "Hello there Linda")      # identical text again
    users = [m["content"] for m in get_state_sync(rt, "dedupe-1")["history"]
             if m["role"] == "user"]
    assert users == ["Hello there Linda"]                   # NOT duplicated


def test_late_report_schedules_hangup_when_ended():
    """F2 regression: when the graph state says ended, _late_turn_report must
    schedule the hangup (the iter37 port broke this with a NameError on `config`
    swallowed by `except: pass` — goodbye played, call never hung up)."""

    async def go():
        sess = _mk_session()
        sess._turn_mark_seq = 0
        graph = types.SimpleNamespace(values={"ended": True, "state_name": "Closing"})

        async def aget_state(config):
            assert config == {"configurable": {"thread_id": "c1"}}
            return graph

        sess.runtime = types.SimpleNamespace(graph=types.SimpleNamespace(aget_state=aget_state))
        scheduled = []

        async def fake_hangup():
            scheduled.append(True)

        sess._hangup_after_drain = fake_hangup
        clock = TurnClock(turn_index=9)
        clock.t_audio_out = 1.0                     # skip the drain wait
        await sess._late_turn_report(clock)
        await asyncio.sleep(0.05)
        assert scheduled == [True]                  # hangup scheduled
        assert sess._ended is True                  # _ended set
        assert sess._turn_state == "Closing"        # delivery profile refreshed

    asyncio.run(go())


def test_turn_cap_graceful_close():
    """F3 (PT-34): at the cap, the NEXT EOT speaks the graceful close line and
    stops the session — no LLM call, ws closed, reason=turn_cap."""

    async def go():
        sess = _mk_session(settings=Settings(
            openai_api_key="t", retell_api_key="t", langfuse_enabled=False,
            metrics_enabled=False, max_call_turns=2))
        sess._real_turns = 2
        stopped = []

        async def fake_stop(reason="stopped"):
            stopped.append(reason)

        sess.stop = fake_stop
        runs = []

        async def fake_run(t, epoch=0):
            runs.append(t)

        sess._run_turn = fake_run
        await sess._on_eot("one more thing")
        await asyncio.sleep(0.05)
        assert runs == []                            # no LLM turn ran
        assert sess.gate.added and "temporary technical issues" in sess.gate.added[0]
        assert sess.gate.ended == 1
        assert sess.ws.closed == [1000]
        assert stopped == ["turn_cap"]
        assert sess._cap_closed is True              # fires exactly once
        await sess._on_eot("again")                  # idempotent
        assert stopped == ["turn_cap"]

    asyncio.run(go())


def test_call_time_cap_graceful_close():
    """F3 (Julio tune): a call older than max_call_seconds gets the graceful
    close even with turns to spare."""

    async def go():
        sess = _mk_session(settings=Settings(
            openai_api_key="t", retell_api_key="t", langfuse_enabled=False,
            metrics_enabled=False, max_call_seconds=600))
        sess._real_turns = 3                       # turns to spare
        sess._call_start = time.perf_counter() - 601
        stopped = []

        async def fake_stop(reason="stopped"):
            stopped.append(reason)

        sess.stop = fake_stop
        runs = []

        async def fake_run(t, epoch=0):
            runs.append(t)

        sess._run_turn = fake_run
        await sess._on_eot("still here")
        await asyncio.sleep(0.05)
        assert runs == []
        assert stopped == ["turn_cap"]
        assert sess._cap_closed is True

    asyncio.run(go())


def test_barge_in_carried_to_report():
    """F5: barge_in marked on the OLD clock must survive the EOT clock swap
    (disaster call: 6 real barge-ins, all reported barge_in:false)."""

    async def go():
        sess = _mk_session()
        prev = TurnClock(turn_index=3)
        prev.barge_in = True
        prev.t_barge_in = 123.4
        sess._clock = prev
        runs = []

        async def fake_run(t, epoch=0):
            runs.append(t)

        sess._run_turn = fake_run
        await sess._on_eot("interrupt")
        await asyncio.sleep(0.05)
        assert runs == ["interrupt"]
        assert sess._clock.barge_in is True
        assert sess._clock.t_barge_in == 123.4

    asyncio.run(go())


def test_audio_out_stamped_in_writer():
    """F6: t_audio_out is stamped when the writer actually WRITES bytes to the
    socket — not at TTS arrival (which made tts_first_to_audio_out_ms ≈ 0)."""

    async def go():
        sess = _mk_session()
        clock = TurnClock(turn_index=1)
        clock.t_tts_first = 1.0
        sess._speak_clock = clock
        sess._clock = clock
        sess._outbox.put_nowait(b"\x01\x02")
        sess._outbox.put_nowait(None)               # sentinel: writer exits
        await sess._writer()
        assert sess.ws.sent_bytes == [b"\x01\x02"]
        assert clock.t_audio_out is not None

    asyncio.run(go())
