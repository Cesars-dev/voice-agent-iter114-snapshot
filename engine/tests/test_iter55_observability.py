"""iter55 T0 — warm/greeting/await observability pins (02_action_plan §1e).

Pins (observability only, ZERO behavior change):
  1. full-shape warm emits `warm:Intake` with all metadata fields
  2. lite-shape warm emits `warm:lite:Intake` (key matches `_latest_warm`)
  3. failing warm records `_warm_diag` + span degraded=True with the error
     (llm.warm is un-swallowed per F-08; `_warm`'s except owns the record —
     the task itself still completes via the lite retry, so `outcome` stays
     "ok"; `degraded`/`error` carry the failure truth)
  4-7. `_await_warm` outcomes: no_task / already_done / landed / timeout,
       waited_ms ≤ cap, span `await_warm`
  8. wait_ms=0 → no span, no await (flag-0 semantics unchanged)
  9. warm_observability=False → ZERO new spans (exact pre-T0 surface)
 10. latch + force=True supersede + aclose() cancellation unchanged
 11. session `_on_tts_audio` greeting anchors → `greeting:first_audio` span
 12. `_greeting_drain` → one `greeting` span with the 3 runway fields
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.media.session import CallSession
from diallux.observability.latency import TurnClock
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())


class SpyTracer:
    def __init__(self):
        self.spans: list[tuple[str, dict]] = []

    def span(self, name, **kw):
        self.spans.append((name, kw.get("metadata") or {}))

    def spans_named(self, name):
        return [m for n, m in self.spans if n == name]

    def names(self):
        return [n for n, _ in self.spans]


class WarmSpyLLM(FakeLLM):
    """Records warm() calls separately from the streaming hot path."""

    def __init__(self, rounds=None, fail=False):
        super().__init__(rounds)
        self.warm_calls: list[tuple[list[dict], list[dict]]] = []
        self._fail = fail

    async def warm(self, messages, tools):
        if self._fail:
            raise RuntimeError("warm boom")
        self.warm_calls.append((list(messages), [t["function"]["name"] for t in tools]))


def _settings(**extra) -> Settings:
    return Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, fallback_inline_kb=True, **extra)


def _runtime(tracer, fake, **cfg):
    return CallRuntime(_settings(**cfg), LLM_JSON, tracer=tracer, llm=fake,
                       http_client=mock_client(), kb_store=None)


async def _drain(rt, spins=6):
    """Let warm tasks finish AND their done-callbacks run."""
    await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
    for _ in range(spins):
        await asyncio.sleep(0)


def test_warm_span_full_shape():
    spy, fake = SpyTracer(), WarmSpyLLM()
    rt = _runtime(spy, fake)

    async def go():
        rt.warm_prompt_cache("Intake", [])
        await _drain(rt)

    asyncio.run(go())
    spans = spy.spans_named("warm:Intake")
    assert len(spans) == 1
    m = spans[0]
    assert m["shape"] == "full"
    assert m["state"] == "Intake"
    assert m["outcome"] == "ok"
    assert m["degraded"] is False
    assert m["error"] is None
    assert isinstance(m["ms"], int) and m["ms"] >= 0
    assert isinstance(m["fired_at"], float)


def test_warm_span_lite_key():
    spy, fake = SpyTracer(), WarmSpyLLM()
    rt = _runtime(spy, fake)

    async def go():
        rt.warm_prompt_cache("Intake", [], lite=True)
        await _drain(rt)

    asyncio.run(go())
    spans = spy.spans_named("warm:lite:Intake")
    assert len(spans) == 1
    assert spans[0]["shape"] == "lite"
    assert spans[0]["state"] == "Intake"


def test_warm_failure_degraded_and_diag():
    spy, fake = SpyTracer(), WarmSpyLLM(fail=True)
    rt = _runtime(spy, fake)

    async def go():
        rt.warm_prompt_cache("Intake", [])
        await _drain(rt)

    asyncio.run(go())            # must not raise (fire-and-forget contract)
    assert rt._warm_diag["Intake"]["retried_lite"] is True
    assert "warm boom" in rt._warm_diag["Intake"]["error"]
    spans = spy.spans_named("warm:Intake")
    assert len(spans) == 1
    assert spans[0]["degraded"] is True
    assert "warm boom" in spans[0]["error"]


def test_await_warm_no_task():
    spy, fake = SpyTracer(), WarmSpyLLM()
    rt = _runtime(spy, fake)

    async def go():
        await rt._await_warm("Intake", 100)

    asyncio.run(go())
    spans = spy.spans_named("await_warm")
    assert len(spans) == 1
    assert spans[0]["outcome"] == "no_task"
    assert spans[0]["waited_ms"] == 0
    assert spans[0]["cap_ms"] == 0     # iter113: no cap consulted, ever


def test_await_warm_already_done():
    spy, fake = SpyTracer(), WarmSpyLLM()
    rt = _runtime(spy, fake)

    async def go():
        task = asyncio.create_task(asyncio.sleep(0.01))
        await asyncio.sleep(0.05)
        rt._latest_warm["Intake"] = task
        await rt._await_warm("Intake", 200)

    asyncio.run(go())
    assert spy.spans_named("await_warm")[0]["outcome"] == "already_done"


def test_await_warm_landed():
    """iter113 repin: the `landed` race is retired — a pending warm is
    cancelled instantly (aborted, waited 0); nothing waits in-path."""
    spy, fake = SpyTracer(), WarmSpyLLM()
    rt = _runtime(spy, fake)

    async def go():
        task = asyncio.create_task(asyncio.sleep(30.0))
        rt._latest_warm["Intake"] = task
        await rt._await_warm("Intake", 500)
        await asyncio.sleep(0.01)      # let the cancel settle
        task.cancel()

    asyncio.run(go())
    m = spy.spans_named("await_warm")[0]
    assert m["outcome"] == "aborted"
    assert m["waited_ms"] == 0 and m["cap_ms"] == 0


def test_await_warm_timeout_keeps_warm_alive():
    spy, fake = SpyTracer(), WarmSpyLLM()
    rt = _runtime(spy, fake)

    async def go():
        task = asyncio.create_task(asyncio.sleep(5.0))
        rt._latest_warm["Intake"] = task
        await rt._await_warm("Intake", 30)
        assert not task.done()       # shield keeps the warm running
        task.cancel()

    asyncio.run(go())
    m = spy.spans_named("await_warm")[0]
    assert m["outcome"] == "timeout"
    assert m["waited_ms"] <= m["cap_ms"] + 20


def test_await_warm_zero_wait_no_span():
    spy, fake = SpyTracer(), WarmSpyLLM()
    rt = _runtime(spy, fake)

    async def go():
        await rt._await_warm("Intake", 0)

    asyncio.run(go())
    assert spy.spans_named("await_warm") == []


def test_observability_off_zero_new_spans():
    spy, fake = SpyTracer(), WarmSpyLLM()
    rt = _runtime(spy, fake, warm_observability=False)

    async def go():
        rt.warm_prompt_cache("Intake", [])
        rt.warm_prompt_cache("Intake", [], lite=True)
        await _drain(rt)
        await rt._await_warm("Intake", 50)

    asyncio.run(go())
    names = spy.names()
    assert not [n for n in names if n.startswith("warm:") or n == "await_warm"]


def test_latch_force_and_aclose_unchanged():
    spy, fake = SpyTracer(), WarmSpyLLM()
    rt = _runtime(spy, fake)

    async def go():
        rt.warm_prompt_cache("Intake", [])
        await asyncio.sleep(0.01)
        rt.warm_prompt_cache("Intake", [])          # latched: no new task
        await asyncio.sleep(0.01)
        assert len(fake.warm_calls) == 1
        rt.warm_prompt_cache("Intake", [], force=True)   # supersede: refires
        await asyncio.sleep(0.01)
        assert len(fake.warm_calls) == 2
        await rt.aclose()                            # cancels without raise

    asyncio.run(go())


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def _mk_session(tracer, settings) -> CallSession:
    sess = object.__new__(CallSession)
    sess.settings = settings
    sess.tracer = tracer
    sess._clock = TurnClock()
    sess._speak_clock = None
    sess._greet = None
    sess.transport = "browser"
    sess._send_raw = lambda payload: None
    return sess


def test_greeting_first_audio_span():
    import base64

    spy = SpyTracer()
    sess = _mk_session(spy, _settings())
    sess._greet = {"ctx": "g1", "t_stream": 0.0,
                   "first_audio": None, "last_audio": None}

    async def go():
        payload = base64.b64encode(b"\x00\x00").decode()
        await sess._on_tts_audio(payload, "g1")
        await sess._on_tts_audio(payload, "g1")   # second stamp: last_audio only
        await sess._on_tts_audio(payload, "other")  # non-greeting ctx ignored

    asyncio.run(go())
    spans = spy.spans_named("greeting:first_audio")
    assert len(spans) == 1
    assert "stream_to_first_ms" in spans[0]
    assert sess._greet["last_audio"] is not None
    assert sess._greet["first_audio"] is not None


def test_greeting_drain_span():
    spy = SpyTracer()
    sess = _mk_session(spy, _settings())
    sess._greet = {"ctx": "g1", "t_stream": 1.0,
                   "first_audio": 1.25, "last_audio": 4.75}

    async def go():
        real_sleep = asyncio.sleep

        async def fake_sleep(_s, *a, **k):
            await real_sleep(0)

        import diallux.media.session as session_mod
        orig = session_mod.asyncio.sleep
        session_mod.asyncio.sleep = fake_sleep
        try:
            await sess._greeting_drain()
        finally:
            session_mod.asyncio.sleep = orig

    asyncio.run(go())
    spans = spy.spans_named("greeting")
    assert len(spans) == 1
    m = spans[0]
    assert m["stream_to_first_ms"] == 250
    assert m["audio_drain_ms"] == 3500
    assert m["total_ms"] == 3750


def test_greeting_drain_no_audio_silent():
    spy = SpyTracer()
    sess = _mk_session(spy, _settings())
    sess._greet = {"ctx": "g1", "t_stream": 0.0,
                   "first_audio": None, "last_audio": None}

    async def go():
        real_sleep = asyncio.sleep

        async def fake_sleep(_s, *a, **k):
            await real_sleep(0)

        import diallux.media.session as session_mod
        orig = session_mod.asyncio.sleep
        session_mod.asyncio.sleep = fake_sleep
        try:
            await sess._greeting_drain()
        finally:
            session_mod.asyncio.sleep = orig

    asyncio.run(go())
    assert spy.spans_named("greeting") == []
