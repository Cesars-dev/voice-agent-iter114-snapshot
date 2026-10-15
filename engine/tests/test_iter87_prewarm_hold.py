"""iter87 VERSION B tests — THE DETERMINISTIC HOLD (owner design, 2026-09-30).

Laws under test (04_master_plan.md S8):
  1. warm_await_dest_ms=0 (default) = byte-exact iter85 — no dest-cap await
  2. hold: entry_lite ack bounded-awaits the in-flight destination warm TASK
  3. fence: never-completing warm times out, ack round STILL proceeds
  4. no_task: _await_warm returns instantly when no warm was ever fired
  5. one-ahead: entering X warms X's FSM successors (edges-derived)
  6. chain: all transition_to_* destinations warm at chain start + Closing re-fire
  7. successor map derived from edges (no hand table)
  8. PROMPT_PREWARM master gate still kills every warm
  9. ttft anchors on the first delta of ANY type (source-level pin)
 10. 10 ms settle after the warm lands, inside the hold branch (owner 2026-09-30)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())

BASE = dict(openai_api_key="t", retell_api_key="t",
            langfuse_enabled=False, metrics_enabled=False)


def _settings(**kw) -> Settings:
    return Settings(**BASE, **kw)


class SpyWarmLLM(FakeLLM):
    """FakeLLM + warm() recorder with a real in-flight pause (hold test)."""

    def __init__(self, rounds, warm_pause: float = 0.0):
        super().__init__(rounds)
        self.warms: list[tuple[list[dict], list[dict]]] = []
        self._pause = warm_pause

    async def warm(self, messages: list[dict], tools: list[dict]) -> None:
        self.warms.append((list(messages), list(tools)))
        if self._pause:
            await asyncio.sleep(self._pause)


# proven iter46 pattern (first_turn_lite=False): each turn = ONE round;
# a spoken ok-transition round one-trips, so the DESTINATION ack is turn 3
TRANSITION_ROUNDS = [
    {"tokens": ["Got", " it", "."],
     "tool_calls": [tool_call("extract_intake_details", {
         "inbound_channel": "voicemail", "interest_topic": "missed calls",
         "pain_frame": "losing leads"})]},
    {"tokens": ["Let", "'s", " dig", " in", "."],
     "tool_calls": [
         tool_call("intake_completed", {"intake_completed": True}),
         tool_call("transition_to_Discovery", {
             "intake_completed": True, "inbound_channel": "voicemail",
             "interest_topic": "missed calls", "pain_frame": "losing leads"})]},
    {"tokens": ["So", ",", " who", " answers", "?"]},     # Discovery ACK (entry_lite)
    {"tokens": ["..."]},                                   # safety tail
    {"tokens": ["..."]},
]
TEXTS = ["saw your ad about missed calls", "go on", "yeah go ahead"]


async def _walk(rt: CallRuntime, call_id: str, texts: list[str],
                gather_between: bool = True) -> None:
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": texts[0], **rt.initial_state(call_id)}
    for i, text in enumerate(texts):
        if i > 0:
            payload = {"user_text": text}
            if gather_between:
                await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
    await asyncio.gather(*rt._warm_tasks, return_exceptions=True)


def _spy_await_warm(rt: CallRuntime) -> list[tuple[str, int | None]]:
    calls: list[tuple[str, int | None]] = []
    orig = rt._await_warm

    async def spy(state_name, wait_ms=None):
        calls.append((state_name, wait_ms))
        return await orig(state_name, wait_ms=wait_ms)

    rt._await_warm = spy
    return calls


# --------------------------------------------------------------------------- #
def test_successor_map_from_edges():
    rt = CallRuntime(_settings(), LLM_JSON, tracer=None,
                     llm=FakeLLM([]), http_client=mock_client())
    assert rt._state_successors["Intake"] == ["Discovery"]
    assert rt._state_successors["Booking"] == ["Closing"]
    assert rt._state_successors["Closing"] == []          # terminal
    # the FSM is a single chain — at most one successor per state
    assert all(len(v) <= 1 for v in rt._state_successors.values())


def test_default_promoted_pack():
    async def go():
        rt = CallRuntime(_settings(), LLM_JSON, tracer=None,
                         llm=SpyWarmLLM(TRANSITION_ROUNDS),
                         http_client=mock_client())
        calls = _spy_await_warm(rt)
        await _walk(rt, "c87-off", TEXTS)
        # PROMOTED iter91: the DEST knob cap 2500 is armed by default
        assert all(w in (None, 100, 500, 2500) for _, w in calls), calls
        return rt
    rt = asyncio.run(go())
    assert "Discovery" in rt._prewarmed                    # F4 force warm
    # S4 one-ahead: PROMOTED iter91 arms it (warm_await_dest_ms=2500 is the
    # ONE knob for chain warms + entry-ack await) — successor warm EXPECTED.
    assert "Closer" in rt._prewarmed


def test_hold_awaits_inflight_task():
    async def go():
        rt = CallRuntime(_settings(warm_await_dest_ms=2500), LLM_JSON,
                         tracer=None, llm=SpyWarmLLM(TRANSITION_ROUNDS,
                                                     warm_pause=0.05),
                         http_client=mock_client())
        calls = _spy_await_warm(rt)
        # NO gather between turns: turn 2's force warm (Discovery) is still
        # IN FLIGHT when turn 3's ack round assembles — the hold must await
        # THAT task (not a timer), then the ack proceeds.
        await _walk(rt, "c87-hold", TEXTS, gather_between=False)
        assert ("Discovery", 2500) in calls, calls         # the hold fired
        assert len(rt.llm.seen_messages) >= 3              # ack round PROCEEDED
        return rt
    asyncio.run(go())


def test_hold_fence_timeout():
    async def go():
        rt = CallRuntime(_settings(warm_await_dest_ms=30), LLM_JSON,
                         tracer=None, llm=SpyWarmLLM(TRANSITION_ROUNDS,
                                                     warm_pause=1.0),
                         http_client=mock_client())
        calls = _spy_await_warm(rt)
        await _walk(rt, "c87-fence", TEXTS, gather_between=False)
        assert ("Discovery", 30) in calls, calls           # fence bounded the wait
        assert len(rt.llm.seen_messages) >= 3              # ack STILL proceeded
        # fence = shield: the slow warm kept running for the NEXT round
        assert rt._latest_warm.get("Discovery") is not None
        t = rt._latest_warm["Discovery"]
        if not t.done():
            t.cancel()
        return rt
    asyncio.run(go())


def test_hold_no_task_returns_instantly():
    async def go():
        rt = CallRuntime(_settings(warm_await_dest_ms=2500), LLM_JSON,
                         tracer=None, llm=FakeLLM([]),
                         http_client=mock_client())
        rt._latest_warm.pop("Discovery", None)             # warm never fired
        t0 = asyncio.get_running_loop().time()
        await rt._await_warm("Discovery", wait_ms=2500)    # must NOT stall
        waited = asyncio.get_running_loop().time() - t0
        assert waited < 0.05, f"no_task stalled the round: {waited:.3f}s"
        return rt
    asyncio.run(go())


def test_settle_sleep_present():
    src = (ROOT / "diallux" / "graph" / "builder.py").read_text()
    assert "await asyncio.sleep(0.01)" in src              # owner 2026-09-30
    i_warm = src.index("wait_ms=runtime.settings.warm_await_dest_ms)")
    i_sleep = src.index("await asyncio.sleep(0.01)")
    assert 0 < i_sleep - i_warm < 300                      # right AFTER the hold
    # the settle only exists inside the knob-gated hold branch
    i_gate = src.index("if runtime.settings.warm_await_dest_ms > 0:")
    assert i_gate < i_warm < i_sleep


def test_oneahead_warms_successors():
    async def go():
        rt = CallRuntime(_settings(warm_await_dest_ms=2500), LLM_JSON,
                         tracer=None,
                         llm=SpyWarmLLM(TRANSITION_ROUNDS),
                         http_client=mock_client())
        await _walk(rt, "c87-ahead", TEXTS)
        assert "Discovery" in rt._prewarmed                # F4 force warm
        assert "Closer" in rt._prewarmed                   # S4 one-ahead (successor)
        # the one-ahead warm registered under the bare state key
        assert rt._latest_warm.get("Closer") is not None
        return rt
    asyncio.run(go())


def test_chain_fires_all_destinations():
    async def go():
        rt = CallRuntime(_settings(warm_await_dest_ms=2500), LLM_JSON,
                         tracer=None,
                         llm=SpyWarmLLM([
                             # turn 1 Intake (first_turn_lite=False)
                             {"tokens": ["Got", " it", "."],
                              "tool_calls": [tool_call("extract_intake_details", {
                                  "inbound_channel": "voicemail",
                                  "interest_topic": "missed calls",
                                  "pain_frame": "losing leads"})]},
                             # turn 2 ConfirmSlots: tool round (harmless tool —
                             # validate_lead would CLOBBER slot_verified) ->
                             # deterministic -> chain gate met -> chain fires
                             {"tokens": ["Booking", " that", "."],
                              "tool_calls": [tool_call("check_current_date", {})]},
                             # Closing ACK (entry_lite after the chain)
                             {"tokens": ["You", "'re", " booked", "."]},
                             {"tokens": ["..."]},           # safety tail
                         ]),
                         http_client=mock_client())
        config = {"configurable": {"thread_id": "c87-chain"}}
        payload = {"user_text": "saw your ad", **rt.initial_state("c87-chain")}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
        # precondition the chain gate, then let the ConfirmSlots tool round run it
        await rt.graph.aupdate_state(config, {
            "state_name": "ConfirmSlots",
            "dvs": {"slot_verified": True, "selected_time": "Tuesday 10:00",
                    "prospect_timezone": "America/New_York",
                    "callback_number": "+15551234567", "first_name": "Ana"},
        })
        payload = {"user_text": "yes, that time works"}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
        # S5 fired at chain START — all transition destinations, regardless
        # of how far the chain got
        assert {"VerifyLead", "Booking", "Closing"} <= rt._prewarmed
        # S6 re-fire rode the SUCCESS path (mock webhooks return ok)
        state = (await rt.graph.aget_state(config)).values
        if not state.get("dvs", {}).get("chain_aborted_step"):
            assert rt._latest_warm.get("Closing") is not None
        return rt
    asyncio.run(go())


def test_prewarm_master_gate():
    async def go():
        rt = CallRuntime(_settings(prompt_prewarm=False), LLM_JSON,
                         tracer=None, llm=SpyWarmLLM(TRANSITION_ROUNDS),
                         http_client=mock_client())
        await _walk(rt, "c87-gate", TEXTS)
        assert rt._prewarmed == set()                       # NOTHING warmed
        assert not rt._latest_warm
        return rt
    asyncio.run(go())


def test_ttft_any_delta():
    src = (ROOT / "diallux" / "graph" / "builder.py").read_text()
    # S7: the ttft anchor sits at the TOP of the event loop — before the
    # "token" branch — so tool-first rounds anchor ttft too (T1.4).
    # iter112: the loop now iterates `ev_source` (single stream flag-off;
    # the BARE SPEAK stream when speak-first is armed) — anchor position
    # unchanged (same repin class as iter110's).
    i_loop = src.index('async for ev in ev_source:')
    i_ttft = src.index("if ttft_s is None:", i_loop)
    i_tok = src.index('if ev["type"] == "token":', i_loop)
    assert i_loop < i_ttft < i_tok
    # behavior: a tool-first round still drives fire-at-detection (the
    # tool_call_start event was consumed by the hoisted loop)
    async def go():
        rt = CallRuntime(_settings(), LLM_JSON, tracer=None,
                         llm=SpyWarmLLM(TRANSITION_ROUNDS),
                         http_client=mock_client())
        await _walk(rt, "c87-ttft", TEXTS)
        assert "Discovery" in rt._prewarmed
        return rt
    asyncio.run(go())
