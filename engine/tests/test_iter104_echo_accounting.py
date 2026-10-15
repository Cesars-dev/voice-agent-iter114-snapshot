"""iter104 PT-1 — echo inserts: one user entry per utterance (FIND-37, P0).

The Oct-8 shape (CAcf047 22:48:43 / 22:48:57 / 22:50:53): the eager turn
writes the fragment into history (ingest commits before the round), the
final EOT grew → the in-flight eager round is CANCELLED (no assistant
entry lands), a fresh run appends the final too → the model sees the
caller TWICE (and parrots it back). Fix: the growth rerun flags the
payload (`user_text_supersedes`); ingest emits a `supersedes` history
patch; the `merge_history` reducer pops the trailing user entry before
appending the final — one user entry per utterance.

Pins:
  1. test_growth_rerun_single_entry      — f -> final(supersede) = ONE entry
  2. test_adopt_unchanged                — identical final: guard skips, no flag
  3. test_reducer_replaces_only_trailing_user — rails: never eats assistant
  4. test_plain_append_untouched         — ordinary patches == operator.add
  5. test_three_variant_shape            — two growth reruns = still ONE entry
  6. test_history_window_renders_final   — the model's view shows the final once
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.graph.builder import CallRuntime
from diallux.media.sentence_gate import SentenceGate
from diallux.media.session import CallSession
from diallux.observability.latency import TurnClock
from diallux.state import merge_history
from tests.fake_llm import FakeLLM
from tests.test_iter49_rag_parity import _LLM_JSON, _t4_settings, mock_client

FRAG = "Yeah. Exactly. That I'm missing business"
FINAL = "Yeah. Exactly. That I'm missing business. So how much does that cost"
FINAL2 = "Yeah. Exactly. That I'm missing business. So how much does that cost. And when"


class _CapGraph:
    """astream stub: records the payload, streams nothing."""

    def __init__(self):
        self.payloads = []

    async def astream(self, payload, config=None, stream_mode=None):
        self.payloads.append(dict(payload))
        return
        yield  # pragma: no cover


class _BlockLLM:
    """A round that never completes — the eager turn mid-stream shape."""

    async def astream(self, messages, tools, verbosity=""):
        await asyncio.sleep(3600)
        yield {}  # pragma: no cover


class _Tracer:
    def span(self, *a, **k):
        pass


def _rt(rounds):
    return CallRuntime(_t4_settings(), _LLM_JSON, tracer=None,
                       llm=FakeLLM(rounds), http_client=mock_client())


async def _drain(rt, payload, config):
    async for _m, _d in rt.graph.astream(payload, config=config,
                                         stream_mode=["updates"]):
        pass


async def _seed_cancelled_turn(rt, call_id, text, supersede=False, config=None):
    """The eager shape: ingest commits the text, the round is CANCELLED
    mid-stream (the session's growth-path cancel) — no assistant entry lands.
    Pass an existing config to model a repeated growth on the same thread."""
    fresh = config is None
    config = config or {"configurable": {"thread_id": call_id}}
    payload: dict = {"user_text": text}
    if supersede:
        payload["user_text_supersedes"] = True
    if fresh:
        payload.update(rt.initial_state(call_id))
    orig_llm = rt.llm
    rt.llm = _BlockLLM()
    try:
        task = asyncio.create_task(_drain(rt, payload, config))
        for _ in range(100):
            await asyncio.sleep(0.02)
            vals = (await rt.graph.aget_state(config)).values
            users = [m.get("content") for m in vals.get("history", [])
                     if m.get("role") == "user"]
            if users and users[-1] == text:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        rt.llm = orig_llm
    return config


async def _invoke(rt, config, text, supersede=False):
    payload: dict = {"user_text": text}
    if supersede:
        payload["user_text_supersedes"] = True
    await _drain(rt, payload, config)
    return (await rt.graph.aget_state(config)).values


def _users(vals):
    return [m for m in vals.get("history", []) if m.get("role") == "user"]


# ---- 1. the Oct-8 22:48:43 shape ------------------------------------------- #
def test_growth_rerun_single_entry():
    async def go():
        rt = _rt([{"tokens": ["Got", " it", "."]}])
        config = await _seed_cancelled_turn(rt, "echo-1", FRAG)
        vals = await _invoke(rt, config, FINAL, supersede=True)
        users = _users(vals)
        assert len(users) == 1, f"one entry per utterance, got {users}"
        assert users[0]["content"] == FINAL
        assert "supersedes" not in users[0], "marker stripped, never stored"

    asyncio.run(go())


# ---- 2. adopt / identical: no rerun, no second entry, flag untouched -------- #
def test_adopt_unchanged():
    async def go():
        # graph half: fragment committed + cancelled; identical final -> skip
        rt = _rt([{"tokens": ["Got", " it", "."]}])
        config = await _seed_cancelled_turn(rt, "echo-2", FINAL)
        vals = await _invoke(rt, config, FINAL, supersede=False)
        assert len(_users(vals)) == 1
        assert _users(vals)[0]["content"] == FINAL

        # session half (a): adopt path never reruns; flag untouched
        sess = _mk_eot_sess(FRAG)
        sess._turn_task = asyncio.create_task(asyncio.sleep(5))
        await sess._on_eot(FRAG)
        assert sess.spawned == [], "adopt never reruns"
        assert sess._supersede_fragment is None
        assert sess._eager_transcript is None
        sess._turn_task.cancel()

        # session half (b): _run_turn payload carries + consumes the flag
        cap = _CapGraph()
        rs = _mk_run_sess(SimpleNamespace(graph=cap))
        rs._supersede_fragment = "the eager fragment"
        await rs._run_turn(FINAL)
        assert cap.payloads[-1]["user_text"] == FINAL
        assert cap.payloads[-1].get("user_text_supersedes") is True
        assert rs._supersede_fragment is None, "flag consumed (never leaks)"
        await rs._run_turn(FINAL2)
        assert "user_text_supersedes" not in cap.payloads[-1]
        await rs.gate.close()

    asyncio.run(go())


def _mk_eot_sess(eager_snapshot: str | None):
    sess = object.__new__(CallSession)
    sess._stopped = False
    sess._ended = False
    sess._cap_hit = lambda: False
    sess.settings = SimpleNamespace(rag_fire_mode="eot")
    sess._clock = TurnClock(turn_index=1, extra={"eager": True})
    sess._turn_task_epoch = 1
    sess._epoch = 1
    sess._eager_transcript = eager_snapshot
    sess._held = None
    sess._update_fired = False
    sess._supersede_fragment = None
    sess.gate = None
    sess.tracer = None
    sess.call_sid = "c-echo"
    sess._fire_live_retrieve = lambda *a, **k: None
    sess._turn_task = None
    sess.spawned = []

    async def fake_run(transcript, epoch=0):
        sess.spawned.append((transcript, sess._supersede_fragment))

    sess._run_turn = fake_run
    return sess


def _mk_run_sess(runtime):
    sess = object.__new__(CallSession)
    sess.settings = SimpleNamespace(metrics_enabled=False, hangup_mode="timer")
    sess._clock = TurnClock(turn_index=1)
    sess._speak_clock = None
    sess._tts_context = None
    sess._tts_ctx_epoch = 0
    sess._real_turns = 0
    sess._thread_config = {"configurable": {"thread_id": "t"}}
    sess.tts = None
    sess._first_turn = False
    sess.turn_reports = []
    sess.tracer = _Tracer()
    sess.gate = SentenceGate(lambda t, c: asyncio.sleep(0))
    sess.runtime = runtime
    sess._supersede_fragment = None
    sess.call_sid = "c"

    async def _late(_clock):
        pass

    sess._late_turn_report = _late
    return sess


# ---- 3. reducer rails ------------------------------------------------------- #
def test_reducer_replaces_only_trailing_user():
    u1 = {"role": "user", "content": "u1"}
    a1 = {"role": "assistant", "content": "a1"}
    u2 = {"role": "user", "content": "u2"}
    out = merge_history([u1, a1, u2],
                        [{"role": "user", "content": "u3",
                          "supersedes": True}])
    assert out == [u1, a1, {"role": "user", "content": "u3"}]
    # trailing assistant: marker degrades to a plain append (nothing eaten)
    out2 = merge_history([u1, a1],
                         [{"role": "user", "content": "u3",
                           "supersedes": True}])
    assert out2 == [u1, a1, {"role": "user", "content": "u3"}]
    # marker never lands in stored history
    assert all("supersedes" not in m for m in out + out2)
    # PURITY LAW: re-application-safe — LangGraph replays a cancelled run's
    # pending writes through the reducers; the SAME patch applied twice with
    # the same `a` must not accumulate (the mutating-pop disease).
    a3 = [u1, {"role": "user", "content": "u2"}]
    b3 = [{"role": "user", "content": "u3", "supersedes": True}]
    once = merge_history(a3, b3)
    twice = merge_history(a3, b3)
    assert once == twice == [u1, {"role": "user", "content": "u3"}]
    assert "supersedes" not in b3[0] or True, "b is never mutated by us"
    assert b3[0].get("supersedes") is True, "input patch left intact"


# ---- 4. parity: ordinary patches byte-identical to operator.add -------------- #
def test_plain_append_untouched():
    import operator

    a = [{"role": "user", "content": "u1"},
         {"role": "assistant", "content": "a1"}]
    b = [{"role": "assistant", "content": "a2"},
         {"role": "user", "content": "u2"}]
    assert merge_history(a, b) == operator.add(a, b)
    assert merge_history([], b) == operator.add([], b)
    assert merge_history(a, None) == a


# ---- 5. the 22:50:53 3-variant shape ---------------------------------------- #
def test_three_variant_shape():
    async def go():
        rt = _rt([{"tokens": ["Right", "."]}])
        config = await _seed_cancelled_turn(rt, "echo-5", FRAG)      # v1
        config = await _seed_cancelled_turn(rt, "echo-5", FINAL,
                                            supersede=True,
                                            config=config)           # v2 (cancelled)
        vals = (await rt.graph.aget_state(config)).values
        assert _users(vals)[-1]["content"] == FINAL, "v2 replaced v1"
        vals = await _invoke(rt, config, FINAL2, supersede=True)     # v3 (completes)
        users = _users(vals)
        assert len(users) == 1, f"still ONE after two supersedes: {users}"
        assert users[0]["content"] == FINAL2

        # session half: two consecutive growth cancels both set the flag
        sess = _mk_eot_sess(FRAG)
        sess._turn_task = asyncio.create_task(asyncio.sleep(5))
        await sess._on_eot(FINAL)
        await asyncio.sleep(0)          # let the spawned task run
        assert sess.spawned == [(FINAL, FRAG)]
        sess._turn_task = asyncio.create_task(asyncio.sleep(5))
        await sess._on_eot(FINAL2)
        await asyncio.sleep(0)
        assert len(sess.spawned) == 2
        assert all(flag is not None for _, flag in sess.spawned)
        sess._turn_task.cancel()

    asyncio.run(go())


# ---- 6. the model's view ---------------------------------------------------- #
def test_history_window_renders_final():
    async def go():
        rt = _rt([{"tokens": ["Got", " it", "."]}])
        config = await _seed_cancelled_turn(rt, "echo-6", FRAG)
        vals = await _invoke(rt, config, FINAL, supersede=True)
        msgs = rt._build_messages("Intake", vals["history"], "", "SYS")
        user_msgs = [m["content"] for m in msgs if m["role"] == "user"]
        assert user_msgs == [FINAL], \
            f"final exactly once, fragment never: {user_msgs}"

    asyncio.run(go())
