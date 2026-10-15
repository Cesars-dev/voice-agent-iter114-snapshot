"""iter113 pins — warm-nowait: adopt-if-done else cancel-instantly.

plan_v5_iter113_warm_nowait.md (owner 2026-10-13): the _await_warm dead
wait is DELETED. No wait_for, no shield, no cap anywhere on the path:
done → already_done (adopts, waited 0); pending → cancelled NOW
(aborted, waited_ms=0 always). The iter95 knobs stay in env for boot
continuity but are NO-OPs for the await decision.

The static no-wait pin (3) is the load-bearing one: nothing may ever
reintroduce an in-path wait on this route.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime


class FakeTracer:
    def __init__(self):
        self.spans = []

    def span(self, name, **k):
        self.spans.append((name, k.get("metadata", k)))


def _settings(**kw) -> Settings:
    return Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, metrics_enabled=False, **kw)


def _rt(**kw) -> tuple[CallRuntime, FakeTracer]:
    tr = FakeTracer()
    rt = object.__new__(CallRuntime)
    rt.settings = _settings(**kw)
    rt._latest_warm = {}
    rt.tracer = tr
    return rt, tr


def _outcome(tr, key: str) -> dict:
    found = [meta for name, meta in tr.spans
             if name == "await_warm" and meta.get("key") == key]
    if not found:
        raise AssertionError(f"no await_warm span for {key}: {tr.spans}")
    return found[-1]


def _warm_body(delay: float, writes: list):
    async def body():
        await asyncio.sleep(delay)
        writes.append("history")
    return body()


def _create(coro) -> asyncio.Task:
    return asyncio.get_running_loop().create_task(coro)


def test_done_adopts_waited_zero_task_untouched():
    # pin 1: done → already_done, waited 0, the task is left alone
    writes: list = []
    rt, tr = _rt()

    async def scenario():
        rt._latest_warm["Discovery"] = _create(_warm_body(0.0, writes))
        await asyncio.sleep(0.01)
        await rt._await_warm("Discovery")
        meta = _outcome(tr, "Discovery")
        assert meta["outcome"] == "already_done"
        assert meta["waited_ms"] == 0 and meta["cap_ms"] == 0
        assert rt._latest_warm["Discovery"].done()
        assert not rt._latest_warm["Discovery"].cancelled()
        assert writes == ["history"]               # adoption ran

    asyncio.run(scenario())


def test_pending_cancelled_same_tick_waited_zero():
    # pin 2: pending → aborted, waited 0, cancel requested same tick
    rt, tr = _rt()

    async def scenario():
        rt._latest_warm["Discovery"] = _create(_warm_body(30.0, []))
        t = rt._latest_warm["Discovery"]
        await rt._await_warm("Discovery")
        meta = _outcome(tr, "Discovery")
        assert meta["outcome"] == "aborted"
        assert meta["waited_ms"] == 0 and meta["cap_ms"] == 0
        assert t.cancelling() >= 1                 # cancel requested same tick
        await asyncio.sleep(0)                     # let the cancel land
        assert t.cancelled()
        assert rt._last_warm_wait == 0

    asyncio.run(scenario())


def test_static_no_wait_for_no_cap_in_await_warm():
    # pin 3 (STATIC): _await_warm contains no wait/shield/cap machinery
    doc = CallRuntime._await_warm.__doc__ or ""
    code = inspect.getsource(CallRuntime._await_warm)
    body = code.split('"""', 2)[2] if doc else code
    assert "wait_for" not in body, "in-path wait reintroduced"
    assert "shield" not in body, "shield reintroduced"
    assert "prewarm_abort_cap_ms" not in body, \
        "the cap knob must not steer the await decision"


def test_no_task_unchanged():
    # pin 4: no warm ever fired → no_task, waited 0, nothing raised
    rt, tr = _rt()

    async def scenario():
        await rt._await_warm("Ghost")
        meta = _outcome(tr, "Ghost")
        assert meta["outcome"] == "no_task" and meta["waited_ms"] == 0

    asyncio.run(scenario())


def test_call_site_signatures_unchanged():
    # pin 5: all four call sites compile against (state, wait_ms=None)
    sig = inspect.signature(CallRuntime._await_warm)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["self", "state_name", "wait_ms"]
    assert params[2].default is None


def test_log_line_carries_zeroes():
    # pin 6: log + span always carry waited_ms=0 cap_ms=0 (grep continuity)
    rt, tr = _rt()

    async def scenario():
        rt._latest_warm["Discovery"] = _create(_warm_body(0.0, []))
        await asyncio.sleep(0.01)
        await rt._await_warm("Discovery")
        rt._latest_warm["Discovery2"] = _create(_warm_body(30.0, []))
        await rt._await_warm("Discovery2")
        fetch = lambda k: [m for n, m in tr.spans
                           if n == "await_warm" and m.get("key") == k][-1]
        for meta in (fetch("Discovery"), fetch("Discovery2")):
            assert meta["waited_ms"] == 0 and meta["cap_ms"] == 0

    asyncio.run(scenario())


def test_all_four_contexts_inherit_instant_cancel():
    # pin 7: lite / entry_lite / EOT / mid-turn all share the same instant
    # cancel — the context only ever passed wait_ms, which no longer waits.
    writes: list = []
    rt, tr = _rt()

    async def scenario():
        for ctx, wait in (("lite:Intake", 100), ("Intake", 2500),
                          ("EOT:Intake", 500), ("Intake", None)):
            key = f"{ctx}#{id(wait)}"
            rt._latest_warm[key] = _create(_warm_body(30.0, writes))
        for key in list(rt._latest_warm):
            await rt._await_warm(key, wait_ms=2500)
            meta = _outcome(tr, key)
            assert meta["outcome"] == "aborted" and meta["waited_ms"] == 0
        await asyncio.sleep(0)
        assert writes == []

    asyncio.run(scenario())
