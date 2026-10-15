"""iter95 pins — savage piece 1: warm cancel-not-wait (the abort cap).

REPINNED 2026-10-13 to iter113 semantics (plan_v5_iter113_warm_nowait.md):
the await NEVER waits — a done warm is adopted, a pending warm is cancelled
INSTANTLY (waited_ms=0 always). The iter95 400 ms cap is obsolete (`landed`/
`timeout` retire; `cap_ms` reads 0 in every span). 5 of 6 pins repinned; the
no-garbage invariant survives unchanged.

Original disease (savage README Proof 2, iter92b audit): 169 await_warm
events — 163 already_done (96%), 6 lost races waited 1,218–2,324 ms of the
2,500 cap in the S→L window. iter95 bounded it to 400; iter113 deletes the
wait outright (default-tier warms ~1-2.3 s never land inside any window).

Fake tracer records `await_warm` spans for outcome assertions; fake warm
tasks are plain asyncio tasks so cancellation is real.
"""
from __future__ import annotations

import asyncio
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
    """Latest await_warm span for key (re-awaits supersede earlier spans)."""
    found = [meta for name, meta in tr.spans
             if name == "await_warm" and meta.get("key") == key]
    if not found:
        raise AssertionError(f"no await_warm span for {key}: {tr.spans}")
    return found[-1]


def _warm_body(delay: float, writes: list):
    async def body():
        await asyncio.sleep(delay)
        writes.append("history")          # adoption-only-on-completion
    return body()


def test_no_cap_consulted():
    """SWITCH LAW continuity + iter113: the Settings field keeps its code
    default (env/boot unchanged) but _await_warm never consults it — a
    pending warm aborts with cap_ms=0 in the span even at the default 400."""
    assert Settings(openai_api_key="t", retell_api_key="t",
                    langfuse_enabled=False).prewarm_abort_cap_ms == 400
    rt, tr = _rt()                          # cap = default 400

    async def scenario():
        rt._latest_warm["Discovery"] = _create(_warm_body(0.9, []))
        await rt._await_warm("Discovery")
        meta = _outcome(tr, "Discovery")
        assert meta["outcome"] == "aborted"
        assert meta["cap_ms"] == 0          # the cap was never consulted
        assert meta["waited_ms"] == 0
        await asyncio.sleep(0)
        assert rt._latest_warm["Discovery"].cancelled()

    asyncio.run(scenario())


def test_completed_warm_adopts_unchanged():
    """A done warm is adopted (already_done, 0 ms). A warm that WOULD land
    soon (the rare iter95 `landed` race) is now cancelled instantly instead
    — accepted loss (owner 2026-10-13: 'it either landed or didn't')."""
    rt, tr = _rt()

    async def scenario():
        rt._latest_warm["Discovery"] = _create(_warm_body(0.0, []))
        await asyncio.sleep(0.01)         # let it finish
        await rt._await_warm("Discovery")
        meta = _outcome(tr, "Discovery")
        assert meta["outcome"] == "already_done" and meta["waited_ms"] == 0
        assert not rt._latest_warm["Discovery"].cancelled()

        writes: list = []
        rt._latest_warm["Discovery2"] = _create(_warm_body(0.05, writes))
        await rt._await_warm("Discovery2")  # would have landed under iter95
        meta = _outcome(tr, "Discovery2")
        assert meta["outcome"] == "aborted" and meta["waited_ms"] == 0
        await asyncio.sleep(0.01)
        assert rt._latest_warm["Discovery2"].cancelled()
        assert writes == []

    asyncio.run(scenario())


def test_midcompletion_warm_aborts_instantly_round_proceeds():
    """A warm mid-completion is cancelled in the same tick — waited_ms=0,
    cap_ms=0, no exception, the real round proceeds."""
    writes: list = []
    rt, tr = _rt()

    async def scenario():
        rt._latest_warm["Discovery"] = _create(_warm_body(0.9, writes))
        await rt._await_warm("Discovery")           # entry context
        meta = _outcome(tr, "Discovery")
        assert meta["outcome"] == "aborted"
        assert meta["cap_ms"] == 0 and meta["waited_ms"] == 0
        rt._latest_warm["Closing"] = _create(_warm_body(0.9, writes))
        await rt._await_warm("Closing", wait_ms=2500)   # dest tier context
        meta = _outcome(tr, "Closing")
        assert meta["outcome"] == "aborted"
        assert meta["cap_ms"] == 0 and meta["waited_ms"] == 0
        await asyncio.sleep(0)                      # let the cancels land
        assert rt._latest_warm["Discovery"].cancelled()
        assert rt._latest_warm["Closing"].cancelled()
        assert writes == []                         # no-garbage

    asyncio.run(scenario())


def test_preinflight_abort_is_baseline_round():
    """A warm far from done (prefill not even started) is cancelled
    instantly; the call returns cleanly. A re-await of the dead task
    reports `aborted`, never a false `already_done` — and never waits."""
    rt, tr = _rt()

    async def scenario():
        rt._latest_warm["Intake"] = _create(_warm_body(30.0, []))
        await rt._await_warm("Intake", wait_ms=2500)    # long-tier context
        meta = _outcome(tr, "Intake")
        assert meta["outcome"] == "aborted" and meta["waited_ms"] == 0
        for _ in range(3):                          # let the cancel fully land
            await asyncio.sleep(0)
        await rt._await_warm("Intake")              # second entry, dead task
        meta2 = _outcome(tr, "Intake")
        # two spans: aborted-instantly, aborted-again (dead task stays dead)
        assert [m["outcome"] for m in
                [m for n, m in tr.spans if n == "await_warm"
                 and m.get("key") == "Intake"]] == ["aborted", "aborted"]
        assert meta2["waited_ms"] == 0

    asyncio.run(scenario())


def test_cap_zero_no_longer_shields():
    """cap=0 no longer selects the legacy wait-shield — there is no shield:
    a pending warm is cancelled instantly regardless of any knob value."""
    rt, tr = _rt(prewarm_abort_cap_ms=0, prewarm_entry_wait_ms=80)

    async def scenario():
        rt._latest_warm["Discovery"] = _create(_warm_body(30.0, []))
        t = rt._latest_warm["Discovery"]
        await rt._await_warm("Discovery")
        meta = _outcome(tr, "Discovery")
        assert meta["outcome"] == "aborted"
        assert meta["cap_ms"] == 0 and meta["waited_ms"] == 0
        await asyncio.sleep(0)
        assert t.cancelled()

    asyncio.run(scenario())


def test_aborted_warm_writes_nothing_to_history():
    """No-garbage invariant (savage README): an aborted warm never reaches
    its completion — adoption only accepts completed warm runs, so an
    aborted warm leaves zero writes behind."""
    writes: list = []
    rt, tr = _rt()

    async def scenario():
        rt._latest_warm["ConfirmSlots"] = _create(_warm_body(30.0, writes))
        await rt._await_warm("ConfirmSlots")
        assert _outcome(tr, "ConfirmSlots")["outcome"] == "aborted"
        await asyncio.sleep(0.01)
        assert writes == []                         # the completion write never ran

    asyncio.run(scenario())


def _create(coro) -> asyncio.Task:
    return asyncio.get_running_loop().create_task(coro)
