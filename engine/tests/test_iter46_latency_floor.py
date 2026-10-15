"""iter46 — latency floor (plan_iter46_latency_floor).

Pins (every flag independently revertible):
  T1  a. openai_verbosity default is "medium" (T1 REVERTED: ladder stalls at low — H1 fix-2)
  T3  b. lite warm payload is a byte-PREFIX of the turn-1 hot payload —
         composed through the SAME _build_messages; includes the greeting
         history message; NO tools (the user message lands AFTER the warm
         fires — OpenAI prefix-cache semantics make the warm cover turn 1)
  T4  c. the post-execute transition warm's tail contains the POST-PATCH dvs
         line rendered in schema field order (tail-byte parity with the
         entry round's frozen state-block)
      d. fire-at-detection: a tool_call_start event for transition_to_<State>
         warms the DESTINATION state immediately (before executor.execute
         returns); the post-execute warm supersedes it in _latest_warm
  T2  e. rag_drift_async=True: a same-state round with a fresh user message
         performs ZERO awaited embeds on the round path (a blocking embed
         store must not stall the round); the drift lane lands as a bg task
      f. rag_drift_async=False: the old sync requery blocks the round path
  T5  g. fast first flush: with tts_gate_fast_first_flush=True the first
         clause flushes at the ","/char threshold BEFORE the stream ends;
         flag False → the first flush waits for a sentence boundary
      h. _fast_flush_cut boundaries (pure function)
  T7  i. grace window: TurnResumed then EndOfTurn inside the grace with an
         EQUAL transcript → round adopted, no rerun
      j. grace + LONGER final transcript → cancel+rerun (stale guard)
      k. grace=0 → cancel+rerun immediately (today)
      l. a second TurnResumed inside the grace cancels immediately
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime, _fast_flush_cut
from diallux.observability.latency import TurnClock
from diallux.schema import DynamicVariables
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())


class SpyLLM(FakeLLM):
    """FakeLLM + warm() recorder + token-yield timestamps."""

    def __init__(self, rounds=None, token_pause: float = 0.0):
        super().__init__(rounds)
        self.warms: list[tuple[list[dict], list[dict]]] = []
        self.t_token: list[float] = []      # per-yielded-token loop time
        self._pause = token_pause

    async def warm(self, messages: list[dict], tools: list[dict]) -> None:
        self.warms.append((list(messages), list(tools)))

    async def astream(self, messages, tools, verbosity: str = ""):
        async for ev in super().astream(messages, tools):
            if ev.get("type") == "token" and self._pause:
                await asyncio.sleep(self._pause)
                self.t_token.append(time.perf_counter())
            yield ev


def _settings(**kw) -> Settings:
    return Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, **kw)


async def _turns(rt: CallRuntime, call_id: str, texts: list[str]) -> dict:
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": texts[0], **rt.initial_state(call_id)}
    for i, text in enumerate(texts):
        if i > 0:
            payload = {"user_text": text}
            await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
    await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
    return (await rt.graph.aget_state(config)).values


# --------------------------------------------------------------------------- #
# T1 a: verbosity default low
# --------------------------------------------------------------------------- #
def test_verbosity_default_low():
    assert _settings().openai_verbosity == "medium"


# --------------------------------------------------------------------------- #
# T3 b: lite warm == byte-prefix of the turn-1 hot payload
# --------------------------------------------------------------------------- #
def test_lite_warm_is_byte_prefix_of_turn1_hot_payload():
    fake = SpyLLM([{"tokens": ["Hi", " there", "."]}])

    async def go():
        rt = CallRuntime(_settings(), LLM_JSON, tracer=None, llm=fake,
                         http_client=mock_client())  # first_turn_lite default ON
        init = rt.initial_state("c46-lite")
        greeting_hist = init.get("history") or []
        # the greeting fires the lite warm with the INITIAL history (no user msg yet)
        rt.warm_prompt_cache("Intake", greeting_hist, None, lite=True)
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
        assert fake.warms, "lite warm never fired"
        warm_msgs, warm_tools = fake.warms[0]
        payload = {"user_text": "hello?", **init}
        config = {"configurable": {"thread_id": "c46-lite"}}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
        return warm_msgs, warm_tools
    warm_msgs, warm_tools = asyncio.run(go())
    # iter48b: the lite warm binds LITE_NOOP_TOOL (FIND-5: gpt-5.4 caches
    # only tool-bearing requests) — exactly ONE do-nothing tool, byte-equal
    # to the turn-1 hot round's tool channel.
    assert [t["function"]["name"] for t in warm_tools] == ["memory_note"]
    hot = fake.seen_messages[0]                      # turn-1 hot payload
    # the warm composes through the SAME composer: head byte-identical, the
    # greeting history message included, the user message rides AFTER the
    # warm's last byte (OpenAI byte-prefix cache covers the whole warm).
    assert len(hot) == len(warm_msgs) + 1
    assert warm_msgs == hot[:-1]
    assert warm_msgs[0]["content"] == hot[0]["content"]          # lite head bytes
    assert any(m.get("role") == "assistant" for m in warm_msgs)  # greeting msg
    assert hot[-1]["role"] == "user"                             # the delta is the user msg


# --------------------------------------------------------------------------- #
# T4 c: post-patch dvs line in the warm tail (schema field order)
# iter56: under state_in_delta (default) the warm's tail is EMPTY — it
# prefills exactly [tools][head][history] (the delta never rides cache, so
# warming the state-block would warm the WRONG shape). The tail-carries-dvs
# contract is a REVERT-path pin now (state_in_delta=False, F-05).
# --------------------------------------------------------------------------- #
def test_transition_warm_tail_contains_postpatch_dvs():
    fake = SpyLLM([{"tokens": ["Got", " it", "."]}])
    # post-patch dvs: a key written by the transitioning tool
    dvs = dict(DynamicVariables().to_flat())
    dvs.update({"intake_completed": "true", "interest_topic": "missed calls"})
    norm = DynamicVariables.from_flat(dvs).to_flat()

    async def go():
        rt = CallRuntime(_settings(first_turn_lite=False,
                                   state_in_delta=False), LLM_JSON, tracer=None,
                         llm=fake, http_client=mock_client())
        rt.warm_prompt_cache("Discovery",
                             [{"role": "assistant", "content": "hi"}],
                             dvs=dict(dvs))
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
        payload = {"user_text": "hello", **rt.initial_state("c46-t4b")}
        config = {"configurable": {"thread_id": "c46-t4b"}}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
        return rt
    rt = asyncio.run(go())
    assert fake.warms
    warm_msgs, warm_tools = fake.warms[0]
    assert warm_tools                                # full payload warm: tools present
    # iter56: the HEAD carries the bare "CURRENT CALL STATE" anchor now —
    # the state-BLOCK is identified by its full "# CURRENT CALL STATE
    # (live values)" heading.
    tails = [m["content"] for m in warm_msgs
             if m["role"] == "system"
             and "# CURRENT CALL STATE (live values)" in m["content"]]
    assert tails, "warm has no STATE-BLOCK tail"
    # POST-PATCH dvs line present, rendered in schema field order
    assert f"intake_completed: {norm['intake_completed']}" in tails[0]
    assert "interest_topic: missed calls" in tails[0]


def test_transition_warm_tail_empty_under_state_in_delta():
    """iter56 (F-05): the warm prefills EXACTLY [tools][head][history] —
    NO state-block tail (the state rides the post-history delta, which never
    rides cache; a tail here would warm the wrong shape and poison the
    latch)."""
    fake = SpyLLM([{"tokens": ["Got", " it", "."]}])
    dvs = dict(DynamicVariables().to_flat())
    dvs.update({"intake_completed": "true", "interest_topic": "missed calls"})

    async def go():
        rt = CallRuntime(_settings(first_turn_lite=False), LLM_JSON, tracer=None,
                         llm=fake, http_client=mock_client())  # state_in_delta default ON
        rt.warm_prompt_cache("Discovery",
                             [{"role": "assistant", "content": "hi"}],
                             dvs=dict(dvs))
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
        return rt
    rt = asyncio.run(go())
    assert fake.warms
    warm_msgs, warm_tools = fake.warms[0]
    assert warm_tools                                # full tool array still rides
    assert all("# CURRENT CALL STATE (live values)" not in m["content"]
               for m in warm_msgs if m["role"] == "system")


# --------------------------------------------------------------------------- #
# T4 d: fire-at-detection warms the DESTINATION state
# --------------------------------------------------------------------------- #
TRANSCRIPT_ROUNDS = [
    # turn 1 Intake: extract + speak
    {"tokens": ["Got", " it", "."],
     "tool_calls": [tool_call("extract_intake_details", {
         "inbound_channel": "voicemail", "interest_topic": "missed calls",
         "pain_frame": "losing leads"})]},
    # turn 2 Intake: completion flag + ok transition to Discovery
    {"tokens": ["Let", "'s", " dig", " in", "."],
     "tool_calls": [
         tool_call("intake_completed", {"intake_completed": True}),
         tool_call("transition_to_Discovery", {
             "intake_completed": True, "inbound_channel": "voicemail",
             "interest_topic": "missed calls", "pain_frame": "losing leads"})]},
    # turn 3: first Discovery round
    {"tokens": ["So", ",", " who", " answers", "?"]},
]


def test_tool_detection_warms_destination_state():
    fake = SpyLLM(TRANSCRIPT_ROUNDS)
    rt = CallRuntime(_settings(first_turn_lite=False), LLM_JSON, tracer=None,
                     llm=fake, http_client=mock_client())
    asyncio.run(_turns(rt, "c46-t4b", ["saw your ad about missed calls",
                                       "go on", "yeah go ahead"]))
    # the fire-at-detection warm ran BEFORE the post-execute warm
    assert "Discovery" in rt._prewarmed
    assert len(fake.warms) >= 2                      # detection warm + post-execute warm
    # the latest warm (post-patch dvs) is the bounded-await target
    assert rt._latest_warm.get("Discovery") is not None


# --------------------------------------------------------------------------- #
# T2 e/f: async drift — zero awaited embeds on the round path
# --------------------------------------------------------------------------- #
class BlockingStore:
    """embed_query records then BLOCKS forever — any awaited embed on the
    round path would hang the turn; async drift must sail through."""

    def __init__(self):
        self.embeds: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.embeds.append(text)
        await asyncio.sleep(3600)

    async def retrieve(self, query_text, kb_slugs, vec=None):
        return [{"kb": "industry", "content": "entry chunk", "score": 0.9}]


def _drift_turns(store, settings):
    fake = SpyLLM([
        {"tokens": ["Got", " it", "."]},
        {"tokens": ["Tell", " me", " more", "."]},
    ])
    return CallRuntime(settings, LLM_JSON, tracer=None, llm=fake,
                       http_client=mock_client(), kb_store=store)


def test_drift_async_round_never_awaits_embed():
    async def go():
        store = BlockingStore()
        # iter49c T1: pins the iter48 async-drift path (BlockingStore has no
        # retrieve_lanes) — live mode OFF.
        rt = _drift_turns(store, _settings(first_turn_lite=False,
                                           rag_live_retrieve=False))  # drift math needs the full lane
        state = await _turns(rt, "c46-drift", ["saw your ad about missed calls",
                                               "yeah tell me more"])
        # the round path completed with a BLOCKING embed store — zero awaited
        # embeds. The bg drift lane spawned (its embed blocked forever).
        assert state.get("history")
        assert rt._drift_tasks.get("Intake") is not None
        return rt
    rt = asyncio.run(go())
    for task in list(rt._warm_tasks) + list(rt._drift_tasks.values()):
        task.cancel()


def test_drift_sync_flag_off_blocks_round():
    store = BlockingStore()
    # iter49c T1: pins the iter48 sync-drift-blocks path — live mode OFF.
    rt = _drift_turns(store, _settings(first_turn_lite=False,
                                       rag_live_retrieve=False,
                                       rag_drift_async=False))
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(
            _turns(rt, "c46-drift2", ["saw your ad about missed calls",
                                      "yeah tell me more"]), timeout=2.0))
    # the sync path DID await embed_q on the round path (it blocked)
    assert store.embeds


# --------------------------------------------------------------------------- #
# T5 g/h: fast first flush
# --------------------------------------------------------------------------- #
FLUSH_TOKENS = ["Well", " hello there,", " how are you", " doing today", " today?"]


def test_fast_flush_cut_boundaries():
    s = _settings()
    assert _fast_flush_cut("Well hello there,", s) == 17     # comma at 16 >= 12
    assert _fast_flush_cut("Well, hi", s) == 0               # comma before 12 chars
    long_text = "Well hello there how are you doing"        # 34 chars, no comma
    assert _fast_flush_cut(long_text, s) == len(long_text)   # >= 28 char threshold
    assert _fast_flush_cut("hi", s) == 0


def test_fast_first_flush_before_stream_end():
    fake = SpyLLM([{"tokens": FLUSH_TOKENS}], token_pause=0.03)
    # PROMOTED iter91: fast-flush default False (PT-71 class). This test
    # pins the MECHANISM, so flush is explicitly ON here.
    rt = CallRuntime(_settings(tts_gate_fast_first_flush=True), LLM_JSON,
                     tracer=None, llm=fake, http_client=mock_client())
    events = _stream_tts(rt, "c46-f1", ["hello there my friend"])
    assert fake.t_token, "no tokens recorded"
    first_tts_at = events[0][1] if events else None
    assert first_tts_at is not None
    # the first clause was flushed BEFORE the stream's last token arrived
    assert first_tts_at < fake.t_token[-1]


def test_fast_flush_off_first_flush_after_stream_end():
    fake = SpyLLM([{"tokens": FLUSH_TOKENS}], token_pause=0.03)
    rt = CallRuntime(_settings(tts_gate_fast_first_flush=False), LLM_JSON,
                     tracer=None, llm=fake, http_client=mock_client())
    events = _stream_tts(rt, "c46-f2", ["hello there my friend"])
    # sentence-boundary only: the first flush happens at/after the LAST token
    assert events and events[0][1] >= fake.t_token[-1]


def _stream_tts(rt: CallRuntime, call_id: str, text: str):
    """Run ONE turn streaming custom+updates; return [(tts_token, t_recv)]."""
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": text, **rt.initial_state(call_id)}

    async def go():
        out: list[tuple[str, float]] = []
        async for mode, data in rt.graph.astream(payload, config=config,
                                                 stream_mode=["custom", "updates"]):
            if mode == "custom" and isinstance(data, dict) and "tts_token" in data:
                out.append((data["tts_token"], time.perf_counter()))
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
        return out
    return asyncio.run(go())


# --------------------------------------------------------------------------- #
# T7 i-l: eager-resume grace window
# --------------------------------------------------------------------------- #
class FakeGate:
    def __init__(self):
        self.resets = 0
        self.discards = 0

    async def reset(self):
        self.resets += 1

    async def discard(self, upto=0):
        self.discards += 1

    def invalidate(self, upto=0):
        self.discards += 1

    def cancel(self, *a, **k):
        pass

    def advance(self, epoch=0):
        pass


class FakeTTS:
    def __init__(self):
        self.cancels = 0

    async def cancel(self, ctx):
        self.cancels += 1


def _eager_session():
    """iter85 T3: was _grace_session — the eager-resume grace window is
    DELETED (FIX 1 holds the audio instead). Settings no longer carries
    eager_resume_grace_ms."""
    from diallux.media.session import CallSession
    sess = object.__new__(CallSession)
    sess.settings = _settings()
    sess._stopped = False
    sess._ended = False
    sess._real_turns = 0
    sess._cap_closed = False
    sess._call_start = time.perf_counter()
    sess.stream_sid = "s46"
    sess._outbox = asyncio.Queue()
    sess._tts_context = "ctx-1"
    sess.gate = FakeGate()
    sess.tts = FakeTTS()
    sess.tracer = None
    sess._held = None
    sess._clock = TurnClock(turn_index=1, extra={
        "eager": True, "t_eager": time.perf_counter()})
    sess._eager_transcript = "hello there"
    sess.turn_reports = []
    sess.turns_run: list[str] = []

    async def fake_run_turn(transcript: str, epoch: int = 0):
        sess.turns_run.append(transcript)
        await asyncio.sleep(3600)
    sess._run_turn = fake_run_turn

    async def linger():
        await asyncio.sleep(3600)
    sess._turn_task = asyncio.get_event_loop().create_task(linger())
    return sess


def test_adopt_equal_transcript_no_rerun():
    """iter85 T3: adopt is UNCONDITIONAL now (grace window deleted — the
    hold makes a rerun unnecessary; TurnResumed discards before any EOT)."""
    async def go():
        sess = _eager_session()
        await sess._on_eot("hello there")        # EndOfTurn, task running, eager → adopt
        assert sess.turns_run == []              # adopted — no rerun
        assert not sess._turn_task.cancelled()   # still running (adopted)
        assert sess._clock.extra.get("eager_final_match") is True
        return sess
    sess = asyncio.run(go())
    sess._turn_task.cancel()


def test_mismatch_transcript_reruns_never_prefix():
    """iter93 T2 (FIND-35 §c Q4): adopt is IFF transcript-equality — a
    LONGER final transcript (growth) discards the eager generation and
    reruns fresh. Never answer the prefix."""
    async def go():
        sess = _eager_session()
        await sess._on_eot("hello there and also more")
        await asyncio.sleep(0.01)
        assert sess.turns_run == ["hello there and also more"]   # fresh run
        assert sess.gate.discards == 1               # the eager generation died
        return sess
    sess = asyncio.run(go())
    sess._turn_task.cancel()


def test_resume_cancels_immediately_when_not_held():
    """iter85 T3: no hold set (FIX 1 knob off) → TurnResumed cancels the
    eager turn the old way (tts cancel + clear + gate reset)."""
    async def go():
        sess = _eager_session()
        task = sess._turn_task
        await sess._on_turn_resumed()
        await asyncio.sleep(0.01)
        assert task.cancelled() or task.done()   # the cancel path ran
        assert sess._turn_task is None           # iter93: slot cleared sync
        assert sess.gate.discards == 1
        assert sess.tts.cancels == 1
        return sess
    asyncio.run(go())


def test_second_resume_after_cancel_is_noop():
    """iter85 T3: the SECOND TurnResumed finds the task done/cancelled and
    the eager marker cleared — the old second-resume grace shortcut is
    gone; it must simply no-op without raising."""
    async def go():
        sess = _eager_session()
        task = sess._turn_task
        await sess._on_turn_resumed()            # cancels immediately
        await sess._on_turn_resumed()            # second → no-op
        await asyncio.sleep(0.01)
        assert task.cancelled() or task.done()
        return sess
    asyncio.run(go())


def test_stale_transcript_reruns_never_prefix():
    """iter93 T2: adopt equality is EXACT — a shorter (stale-prefix) final
    transcript is a mismatch too: discard + fresh run."""
    async def go():
        sess = _eager_session()
        sess._eager_transcript = "hello there and more"   # eager saw MORE
        await sess._on_eot("hello there")                 # final is shorter
        await asyncio.sleep(0.01)
        assert sess.turns_run == ["hello there"]
        assert sess.gate.discards == 1
        return sess
    sess = asyncio.run(go())
    sess._turn_task.cancel()
