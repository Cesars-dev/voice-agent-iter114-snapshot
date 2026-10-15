"""iter103 PT-8 — pre-LLM gap stopwatch pins (observability-only commit).

The stack branch's first commit: a per-round breakdown of the window from
state_node entry to LLM dispatch, surfaced as `pre_llm_ms` on the session's
final turn report. Zero behavior change — the graph-state key set is
byte-identical to the pre-change capture; the breakdown lives on the
runtime + the final report only.

Pins:
  1. test_pre_llm_fields_present    — every key present, values non-negative
  2. test_no_existing_key_changes   — graph-state keys == the pre-change set
  3. test_other_ms_accounts         — entry == warm + rag_await + other (±1 ms)
  4. test_lite_round_stamped        — the turn-1 lite round is measured too
  5. test_final_report_carries_pre_llm — session surfaces it; None-safe
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.media.session import CallSession
from diallux.observability.latency import TurnClock
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())

# Graph-state key set captured on main @ a0d6a87 BEFORE the PT-8 commit ran
# (3-turn scripted FakeLLM) — the zero-behavior-change parity anchor.
# iter104 PT-1 adds `user_text_supersedes` to the typed state deliberately
# (the replace marker) — the golden gets exactly that one key.
# iter105 T1+T3 add `round_asked`, `unspoken_tail`, `held_facts` (ask-hold
# chop + hold-release records) — exactly those three.
GOLDEN_STATE_KEYS = sorted([
    "assistant_text", "call_id", "dvs", "ended", "engine_fired", "held_facts",
    "history", "last_round_tool_calls", "last_spoken", "metrics",
    "question_stem_window", "round_asked", "round_spoke", "rounds_left",
    "state_name", "tts_sentence_window", "turn_active", "turn_index",
    "turn_spoke", "unspoken_tail", "user_text", "user_text_supersedes",
])

PRE_LLM_KEYS = {"state", "turn", "entry_to_dispatch_ms", "warm_await_ms",
                "rag_ms", "rag_await_ms", "other_ms"}


def _settings(**kw) -> Settings:
    return Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, **kw)


async def _turns(rt, call_id, texts):
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": texts[0], **rt.initial_state(call_id)}
    for i, text in enumerate(texts):
        if i > 0:
            payload = {"user_text": text}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
    return (await rt.graph.aget_state(config)).values


def _rt(rounds):
    return CallRuntime(_settings(), LLM_JSON, tracer=None,
                       llm=FakeLLM(rounds), http_client=mock_client())


def test_pre_llm_fields_present():
    rt = _rt([{"tokens": ["Hi", " there", "."]},
              {"tokens": ["Got", " it", "."]}])
    asyncio.run(_turns(rt, "gap-1", ["hello", "my name is Kim"]))
    pre = rt._last_round_pre_llm
    assert isinstance(pre, dict), "per-round breakdown stamped"
    assert set(pre.keys()) == PRE_LLM_KEYS
    assert isinstance(pre["state"], str) and pre["state"]
    assert pre["turn"] == 2
    for k in ("entry_to_dispatch_ms", "warm_await_ms", "rag_ms",
              "rag_await_ms", "other_ms"):
        assert isinstance(pre[k], (int, float)) and pre[k] >= 0.0, f"{k} >= 0"


def test_no_existing_key_changes():
    rt = _rt([{"tokens": ["Hi", " there", "."]},
              {"tokens": ["Got", " it", "."]},
              {"tokens": ["Sure", " thing", "."]}])
    vals = asyncio.run(_turns(rt, "gap-2", ["hello", "my name is Kim",
                                            "yeah tell me more"]))
    assert sorted(vals.keys()) == GOLDEN_STATE_KEYS, \
        "graph-state key set byte-identical to the pre-change capture"
    assert "pre_llm_ms" not in vals, "the breakdown is runtime-side only"


def test_other_ms_accounts():
    rt = _rt([{"tokens": ["Hi", " there", "."]}])
    asyncio.run(_turns(rt, "gap-3", ["hello"]))
    pre = rt._last_round_pre_llm
    total = pre["warm_await_ms"] + pre["rag_await_ms"] + pre["other_ms"]
    assert abs(pre["entry_to_dispatch_ms"] - total) <= 1.0, \
        "entry == warm + rag_await + other (±1 ms slack)"


def test_lite_round_stamped():
    rt = _rt([{"tokens": ["Hi", " there", "."]}])
    asyncio.run(_turns(rt, "gap-4", ["hello"]))
    pre = rt._last_round_pre_llm
    assert pre is not None and pre["turn"] == 1, \
        "the turn-1 lite round is measured too"


def _mk_session(runtime):
    sess = object.__new__(CallSession)
    sess.settings = SimpleNamespace(metrics_enabled=False, hangup_mode="timer")
    sess._resumed_count = 0
    spans = []
    sess.tracer = SimpleNamespace(
        span=lambda name, output=None, **k: spans.append(output))
    sess.spans = spans
    sess.runtime = runtime
    sess._thread_config = {"configurable": {"thread_id": "gap-5"}}
    sess._turn_mark_seq = 0
    sess.stream_sid = "x"
    sess.transport = "twilio"
    sess.sent = []
    sess._send = lambda payload: sess.sent.append(payload)
    return sess


def test_final_report_carries_pre_llm():
    async def scenario():
        rt = _rt([{"tokens": ["Hi", " there", "."]}])
        await _turns(rt, "gap-5", ["hello"])
        assert rt._last_round_pre_llm is not None
        clock = TurnClock(turn_index=1)
        clock.t_user_end = 1.0
        clock.t_turn_done = 2.0
        clock.t_audio_out = 3.0                 # drain loop exits fast
        sess = _mk_session(rt)
        await sess._late_turn_report(clock)
        assert sess.spans[-1].get("pre_llm_ms") == rt._last_round_pre_llm

        # None-safe: an unset scratch → no key
        sess2 = _mk_session(SimpleNamespace(
            graph=rt.graph, _last_consume=None, _last_round_pre_llm=None))
        clock2 = TurnClock(turn_index=2)
        clock2.t_user_end = 1.0
        clock2.t_turn_done = 2.0
        clock2.t_audio_out = 3.0
        await sess2._late_turn_report(clock2)
        assert "pre_llm_ms" not in sess2.spans[-1]

        # attr entirely absent → getattr guard, no key, no raise
        sess3 = _mk_session(SimpleNamespace(graph=rt.graph, _last_consume=None))
        clock3 = TurnClock(turn_index=3)
        clock3.t_user_end = 1.0
        clock3.t_turn_done = 2.0
        clock3.t_audio_out = 3.0
        await sess3._late_turn_report(clock3)
        assert "pre_llm_ms" not in sess3.spans[-1]

    _run(scenario())


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)
