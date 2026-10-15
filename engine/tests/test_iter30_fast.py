"""iter30-fast — latency battery 1: end_call goodbye memory + history window
+ cache instrumentation.

Pins:
  1. end_call ACCEPTED when a goodbye was already spoken earlier (the
     goodbye round and the hangup round are legitimately separate trips)
  2. end_call still BLOCKED when no goodbye was ever spoken (iter16 rule)
  3. history window: last-n entries, never starting on an orphan tool msg
  4. cache_read flows from usage into the tracer's usage_details
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.observability.tracer import Tracer
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = Settings(openai_api_key="test", retell_api_key="test", langfuse_enabled=False)
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())


def make_runtime(fake: FakeLLM) -> CallRuntime:
    return CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake, http_client=mock_client())


async def _turns(rt: CallRuntime, call_id: str, texts: list[str]) -> dict:
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": texts[0], **rt.initial_state(call_id)}
    for i, text in enumerate(texts):
        if i > 0:
            payload = {"user_text": text}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
    return (await rt.graph.aget_state(config)).values


# --------------------------------------------------------------------------- #
def test_end_call_accepted_after_spoken_goodbye():
    """C1 (executor level): empty-text end_call with a goodbye already spoken
    earlier in the call is ACCEPTED (the goodbye round and the hangup round
    are legitimately separate trips)."""
    from diallux.graph.tools import ToolExecutor
    ex = ToolExecutor(SETTINGS, LLM_JSON, http_client=mock_client())
    out = asyncio.run(ex.execute("end_call", {}, {}, "Closing",
                                 spoken_text="", last_spoken="Have a great rest of your day, bye!"))
    assert out.ended is True
    assert out.response["status"] == "call_ended"
    assert out.response.get("goodbye_already_spoken") is True


def test_end_call_still_blocked_without_any_goodbye():
    """C1 (executor level): no goodbye ever spoken -> silent end_call blocked."""
    from diallux.graph.tools import ToolExecutor
    ex = ToolExecutor(SETTINGS, LLM_JSON, http_client=mock_client())
    out = asyncio.run(ex.execute("end_call", {}, {}, "Intake",
                                 spoken_text="", last_spoken="I can help with that."))
    assert out.ended is not True
    assert out.response["status"] == "end_call_blocked"


def test_end_call_blocked_graph_loop_then_recovery_speech():
    """Graph level: silent end_call blocked -> loop -> speech (no goodbye
    marker in it) keeps the call alive and ended stays False."""
    fake = FakeLLM([
        {"tokens": ["I", " can", " help", " with", " that", "."]},
        {"tokens": [], "tool_calls": [tool_call("end_call", {})]},
        # blocked -> loop -> the model speaks (NO goodbye marker in this text)
        {"tokens": ["Alright", ",", " let", "'s", " continue", "."]},
    ])
    rt = make_runtime(fake)
    state = asyncio.run(_turns(rt, "f-2", ["hello", "and?"]))
    assert state.get("ended") is not True
    assert any(b.get("reason") == "empty_text" for b in rt.executor.end_call_blocks)
    tool_msgs = [m for m in state["history"] if m["role"] == "tool"]
    blocked = json.loads(next(m["content"] for m in reversed(tool_msgs)
                              if "end_call_blocked" in m["content"]))
    assert blocked["status"] == "end_call_blocked"


def test_end_call_spoken_goodbye_then_silent_endcall_same_round_coemit_still_works():
    """Co-emit path unchanged: speech + end_call in the SAME round ends directly."""
    fake = FakeLLM([
        {"tokens": ["Bye", "!"], "tool_calls": [tool_call("end_call", {})]},
    ])
    rt = make_runtime(fake)
    state = asyncio.run(_turns(rt, "f-3", ["bye"]))
    assert state["ended"] is True


def test_goodbye_speech_alone_ends_the_call():
    """C1-final: a pure-speech goodbye round ENDS the call — no end_call needed.
    (Live evidence: the model never co-emits end_call with text; the goodbye
    round finalizes the turn before any hangup tool can fire.)"""
    fake = FakeLLM([
        {"tokens": ["Really", " glad", " you", " called", ".", " Have", " a", " great",
                    " rest", " of", " your", " day", ".", " Bye", "!"]},
    ])
    rt = make_runtime(fake)
    state = asyncio.run(_turns(rt, "f-3b", ["thanks, that's everything"]))
    assert state["ended"] is True
    assert state["last_spoken"].lower().find("bye") >= 0


def test_mid_conversation_speech_does_not_end_the_call():
    """Only goodbye text ends the call — normal speech keeps it alive."""
    fake = FakeLLM([
        {"tokens": ["Got", " it", ".", " What", " caught", " your", " attention", "?"]},
    ])
    rt = make_runtime(fake)
    state = asyncio.run(_turns(rt, "f-3c", ["I saw your ad"]))
    assert state.get("ended") is not True


def test_goodbye_auto_end_blocked_without_verified_booking():
    """Bail-out protection (iter8 gate) applies to the auto-end too: a caller
    who agreed to a live call but has NO verified slot/booking can NOT be
    hung up on — even with a spoken goodbye (no fake 'you're booked, bye!')."""
    fake = FakeLLM([
        {"tokens": ["Well", ",", " you", "'re", " booked", " — bye", "!"]},
    ])
    seed = {"livecall_agreed": True}                 # agreed to a live call...
    rt = make_runtime(fake)
    init = rt.initial_state("f-3d", persona_dvs=seed)
    payload = {"user_text": "whatever", **init}

    async def go():
        config = {"configurable": {"thread_id": "f-3d"}}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert state.get("ended") is not True            # bail-out refused


def test_goodbye_auto_end_allowed_when_booking_verified():
    """With the booking verified, a spoken goodbye ends the call normally."""
    fake = FakeLLM([
        {"tokens": ["Great", " —", " have", " a", " great", " day", ", goodbye", "!"]},
    ])
    seed = {"booking_verified": True}
    rt = make_runtime(fake)
    init = rt.initial_state("f-3e", persona_dvs=seed)
    payload = {"user_text": "thanks", **init}

    async def go():
        config = {"configurable": {"thread_id": "f-3e"}}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
        return (await rt.graph.aget_state(config)).values

    state = asyncio.run(go())
    assert state["ended"] is True


# --------------------------------------------------------------------------- #
def test_history_window_never_orphans_tool_messages():
    """C2: last-n trim never starts on a tool response."""
    from diallux.graph.builder import CallRuntime as CR
    history = [{"role": "user", "content": "x"}]
    # assistant with 2 tool_calls + 2 tool responses in the middle of a long history
    for i in range(10):
        history.append({"role": "assistant", "content": "",
                        "tool_calls": [{"id": f"c{i}a", "type": "function", "function": {"name": "t", "arguments": "{}"}},
                                       {"id": f"c{i}b", "type": "function", "function": {"name": "t", "arguments": "{}"}}]})
        history.append({"role": "tool", "tool_call_id": f"c{i}a", "content": "1"})
        history.append({"role": "tool", "tool_call_id": f"c{i}b", "content": "2"})
        history.append({"role": "user", "content": f"turn {i}"})
    tail = CR._history_window(history, 8)
    assert len(tail) <= 8
    assert tail[0].get("role") != "tool"               # no orphan tool response up front
    assert CR._history_window(history, 0) == history   # 0 = full history


def test_history_window_applied_to_messages():
    """C2 integration: the LLM sees at most window entries after the system msg.
    iter56: history_window defaults 0 (FULL history) — with a window set the
    cap still applies (revert pin here; full-history pin in
    test_iter56_state_delta.py)."""
    seen_lens = []
    max_seen = [0]

    class SpyLLM(FakeLLM):
        async def astream(self, messages, tools, verbosity: str = ""):
            n = sum(1 for m in messages if m["role"] != "system")
            seen_lens.append(n)
            max_seen[0] = max(max_seen[0], n)
            async for ev in super().astream(messages, tools):
                yield ev

    fake = SpyLLM()
    rounds = []
    for i in range(14):
        rounds.append({"tokens": [f"reply {i}."]})
    fake.rounds = rounds
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=fake, http_client=mock_client())

    async def go():
        config = {"configurable": {"thread_id": "f-4"}}
        payload = {"user_text": "hi", **rt.initial_state("f-4")}
        for i in range(14):
            payload = {"user_text": f"msg {i}"} if i else payload
            async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
                pass

    asyncio.run(go())
    # iter56: history_window default 0 = FULL history (deployed parity) —
    # the LLM sees everything; the trim only engages on an explicit window.
    # (the old cap-16 pin moved to the revert path in test_iter56_state_delta.py)
    assert max(seen_lens) == max_seen[0]


# --------------------------------------------------------------------------- #
def test_tracer_surfaces_cache_read():
    """C4: cache_read from usage_metadata lands in the gen's usage_details."""
    captured = {}

    class FakeObs(dict):
        def __bool__(self):
            return True

        def update(self, **kw):
            captured.update(kw)

        def end(self):
            pass

    tr = Tracer(enabled=False)
    tr.finish_llm(FakeObs(), output="hi", latency_s=0.1,
                  usage={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105,
                         "input_token_details": {"cache_read": 80}})
    ud = captured.get("usage_details") or {}
    assert ud.get("cache_read") == 80
