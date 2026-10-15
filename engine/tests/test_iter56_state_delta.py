"""iter56 — state→delta payload relocation (cache fix + {{dv}} rendering +
full history).

Pins the NEW payload contract (default flags: state_in_delta=True,
head_strip_vars=True, history_window=0):
  1. HEAVY round layout = [head][FULL history][delta(state + RAG)] — the
     state-block rides the POST-HISTORY delta, never the pre-history prefix
  2. delta composition order pinned: state-block → RAG → (ack directive)
  3. ACK (entry-lite) delta = live state block + the SPEECH-ONLY directive
     (directive stays FINAL); pre-history prefix byte-identical to the heavy
  4. head strip: static_head carries ZERO {{var}} tokens under
     head_strip_vars; the anchor text is byte-stable across calls;
     head_strip_vars=False → literal tokens (revert)
  5. history_window=0 → FULL history rides every round (deployed parity);
     HISTORY_WINDOW=16 restores the trim (revert)
  6. warm parity: the warm payload == the hot round's PRE-HISTORY prefix
     ([tools][head][history], NO tail) under the new layout
  7. ingest freeze write is inert under state_in_delta; fires on the revert
  8. revert matrix: state_in_delta=False → the iter55 layout byte-exactly
     ([head][state-block][history], delta = RAG/directive only)
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
BLOCK_HEADING = "# CURRENT CALL STATE (live values)"
SPEECH_DIRECTIVE = "THIS ROUND: SPEECH ONLY"
CARD_HEADING = "# WHAT IS TRUE RIGHT NOW"
ANCHOR = "CURRENT CALL STATE"


def _settings(**kw) -> Settings:
    base = dict(openai_api_key="test", retell_api_key="test",
                langfuse_enabled=False, first_turn_lite=False,
                rag_fire_mode="eot")   # iter65: pre-hybrid semantics pinned
    base.update(kw)
    return Settings(**base)


def make_runtime(fake, settings=None, kb_store=None) -> CallRuntime:
    return CallRuntime(settings or _settings(), LLM_JSON, tracer=None, llm=fake,
                       http_client=mock_client(), kb_store=kb_store)


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
    return (await rt.graph.aget_state(config)).values


class EmptyStore:
    async def retrieve_lanes(self, lanes):
        return [[] for _ in lanes]

    async def retrieve(self, query, slugs, vec=None):
        return []


# --------------------------------------------------------------------------- #
# 1+2: heavy round layout + delta composition order
# --------------------------------------------------------------------------- #
EXTRACT_ROUND = {
    "tokens": ["Got", " it", "."],
    "tool_calls": [tool_call("extract_intake_details", {
        "inbound_channel": "voicemail", "interest_topic": "missed calls",
        "pain_frame": "losing leads"})],
}


def test_heavy_round_state_rides_post_history_delta():
    """iter56 (F-01): the heavy round = [head][history...][delta] — the LAST
    system message carries the LIVE state block; the pre-history prefix is
    [head] + history only (no state system message before the history)."""
    store = EmptyStore()
    fake = FakeLLM([EXTRACT_ROUND, {"tokens": ["And", " what", " else", "?"]}])
    rt = make_runtime(fake, _settings(), kb_store=store)
    asyncio.run(_turns(rt, "c56-1", ["hi, saw your ad", "yeah go on"]))
    r1, r2 = fake.seen_messages[0], fake.seen_messages[1]
    for msgs in (r1, r2):
        system_positions = [i for i, m in enumerate(msgs) if m["role"] == "system"]
        assert system_positions == [0, len(msgs) - 1], \
            "layout must be [head][history...][delta]"
    # the delta (last message) carries the LIVE state block heading
    assert r1[-1]["role"] == "system" and BLOCK_HEADING in r1[-1]["content"]
    assert r2[-1]["role"] == "system" and BLOCK_HEADING in r2[-1]["content"]
    # the captured values are visible in the round-2 delta (fresh EVERY round)
    assert "inbound_channel: voicemail" in r2[-1]["content"]
    assert "interest_topic: missed calls" in r2[-1]["content"]
    # the head (messages[0]) carries NO live values — only the anchor
    assert "inbound_channel: voicemail" not in r2[0]["content"]


def test_heavy_round_prefix_is_strict_prefix_extension_with_full_history():
    """iter56 (F-03 + cache fix): with history_window=0 the round-2 payload is
    a strict PREFIX EXTENSION of round-1's ([head][history] append-only) —
    only the delta and the appended history entries differ. This IS the
    cache-climb contract."""
    store = EmptyStore()
    fake = FakeLLM([EXTRACT_ROUND, {"tokens": ["And", " more", "?"]}])
    rt = make_runtime(fake, _settings(), kb_store=store)
    asyncio.run(_turns(rt, "c56-2", ["hi there", "tell me more"]))
    r1, r2 = fake.seen_messages[0], fake.seen_messages[1]
    # everything r1 sent (head + history-so-far) reappears byte-identical in
    # r2 (positions shift only by appended history) — check the head + the
    # shared history slice byte-equality via the shared prefix of histories
    assert r2[0] == r1[0]                            # head byte-identical
    hist1 = [m for m in r1[1:] if m["role"] != "system"]
    hist2 = [m for m in r2[1:-1] if m["role"] != "system"]
    assert hist2[:len(hist1)] == hist1               # append-only history
    assert len(hist2) > len(hist1)                   # history GREW (full, no trim)
    # both deltas are last-message system blocks carrying live state
    assert r1[-1]["role"] == "system" and BLOCK_HEADING in r1[-1]["content"]
    assert r2[-1]["role"] == "system" and BLOCK_HEADING in r2[-1]["content"]


def test_delta_state_before_rag_pin():
    """iter56 (CR-2/P2): when the delta carries BOTH, the state block comes
    FIRST and the knowledge section tails it (directive order pinned at the
    ack test below)."""
    class OneChunkStore:
        async def retrieve_lanes(self, lanes):
            return [[{"kb": "pain-points", "content": "mirror tactic chunk",
                      "score": 0.9}] for _ in lanes]

        async def retrieve(self, query, slugs, vec=None):
            return []

    store = OneChunkStore()
    fake = FakeLLM([EXTRACT_ROUND, {"tokens": ["And", " more", "?"]}])
    rt = make_runtime(fake, _settings(), kb_store=store)
    asyncio.run(_turns(rt, "c56-3", ["hi there", "tell me more"]))
    delta = fake.seen_messages[1][-1]["content"]
    assert BLOCK_HEADING in delta and "mirror tactic chunk" in delta
    assert delta.index(BLOCK_HEADING) < delta.index("mirror tactic chunk")


# --------------------------------------------------------------------------- #
# 3: entry-lite ack delta = state + directive (directive FINAL)
# --------------------------------------------------------------------------- #
def test_ack_delta_carries_state_then_directive():
    """iter56 (F-09): the state-entry ack's delta = live state + the
    speech-only directive; the directive stays the FINAL text (imperative =
    last). The ack's pre-history prefix is byte-identical to the heavy's.
    (iter49 T6 shape: first_turn_lite ON — turn 1 lite, turn 2 ack, turn 3
    heavy.)
    iter114 T3: with speak_card ON the ack delta's live-state section is the
    WIT card (facts + flags) — the directive is STILL final; flag OFF keeps
    the raw state block (covered by the second half below)."""
    store = EmptyStore()
    fake = FakeLLM([
        {"tokens": ["Hi", " there", "."]},          # turn 1: lite
        {"tokens": ["Got", " it", "."]},            # turn 2: entry-lite ack
        {"tokens": ["And", " more", "?"]},          # turn 3: heavy
    ])
    rt = make_runtime(fake, _settings(first_turn_lite=True), kb_store=store)
    asyncio.run(_turns(rt, "c56-4", ["hello?", "my receptionist quit on me",
                                     "yeah tell me more"]))
    ack_msgs = fake.seen_messages[1]
    delta = ack_msgs[-1]
    assert delta["role"] == "system"
    assert CARD_HEADING in delta["content"]         # card FIRST
    assert BLOCK_HEADING not in delta["content"]    # card REPLACED the block
    assert SPEECH_DIRECTIVE in delta["content"]     # directive present
    assert delta["content"].index(CARD_HEADING) \
        < delta["content"].index(SPEECH_DIRECTIVE)  # directive FINAL
    # pre-history prefix parity: ack head == heavy head (FIND-5 port intact)
    assert ack_msgs[0] == fake.seen_messages[2][0]

    # flag OFF: the raw state block rides as before (rollback path)
    fake2 = FakeLLM([
        {"tokens": ["Hi", " there", "."]},
        {"tokens": ["Got", " it", "."]},
        {"tokens": ["And", " more", "?"]},
    ])
    rt2 = make_runtime(fake2, _settings(first_turn_lite=True,
                                        speak_card=False), kb_store=store)
    asyncio.run(_turns(rt2, "c56-4b", ["hello?", "my receptionist quit on me",
                                       "yeah tell me more"]))
    delta2 = fake2.seen_messages[1][-1]
    assert BLOCK_HEADING in delta2["content"]
    assert delta2["content"].index(BLOCK_HEADING) \
        < delta2["content"].index(SPEECH_DIRECTIVE)


# --------------------------------------------------------------------------- #
# 4: head strip
# --------------------------------------------------------------------------- #
def test_head_strip_zero_var_tokens_and_byte_stable():
    """iter56 (F-02): under head_strip_vars (default ON) the static head
    contains ZERO {{ tokens — every one became the stable anchor. Byte-identical
    across runtimes (cross-call cache parity preserved)."""
    fake_a = FakeLLM([{"tokens": ["Hi", "."]}])
    fake_b = FakeLLM([{"tokens": ["Hi", "."]}])
    rt_a = make_runtime(fake_a, _settings())
    rt_b = make_runtime(fake_b, _settings())
    asyncio.run(_turns(rt_a, "c56-5a", ["hello"]))
    asyncio.run(_turns(rt_b, "c56-5b", ["hello"]))
    head = fake_a.seen_systems[0]
    assert "{{" not in head
    assert ANCHOR in head
    assert head == fake_b.seen_systems[0]


def test_head_strip_off_keeps_literals():
    """Revert: head_strip_vars=False → the head keeps the literal {{var}}
    tokens (the iter55 head, byte-for-byte)."""
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = make_runtime(fake, _settings(head_strip_vars=False))
    asyncio.run(_turns(rt, "c56-6", ["hello"]))
    head = fake.seen_systems[0]
    assert "{{callback_number}}" in head


def test_head_strip_applies_to_lite_head_and_cached_identity():
    """The strip is a head-BUILD pass: _lite_head and every static_head key
    strip; the cached identity holds (same object returned)."""
    rt = make_runtime(FakeLLM([{"tokens": ["Hi", "."]}]), _settings())
    lite = rt._lite_head()
    assert "{{" not in lite
    h = rt.static_head("Intake", True)
    assert rt.static_head("Intake", True) is h
    assert "{{" not in h


# --------------------------------------------------------------------------- #
# 5: full history
# --------------------------------------------------------------------------- #
def test_history_window_zero_sends_full_history():
    """iter56 (F-03): history_window=0 (default) → every round carries the
    FULL append-only history (deployed parity); no orphan-tool trimming
    needed because nothing is ever dropped."""
    fake = FakeLLM([{"tokens": [f"reply {i}."]} for i in range(18)])
    rt = make_runtime(fake, _settings())
    texts = [f"msg {i}" for i in range(18)]

    async def go():
        config = {"configurable": {"thread_id": "c56-7"}}
        payload = {"user_text": texts[0], **rt.initial_state("c56-7")}
        for i, text in enumerate(texts):
            if i > 0:
                payload = {"user_text": text}
            async for _m, _d in rt.graph.astream(payload, config=config,
                                                 stream_mode=["updates"]):
                pass
    asyncio.run(go())
    last = fake.seen_messages[-1]
    non_system = [m for m in last if m["role"] != "system"]
    # 18 user turns + 18 assistant replies (the greeting assistant message is
    # REPLACED by the first user turn in this flow, not appended to)
    assert len(non_system) == 36
    # turn 1 still riding (nothing trimmed)
    assert any(m.get("role") == "user" and m.get("content") == "msg 0"
               for m in non_system)


def test_history_window_revert_restores_trim():
    """Revert: HISTORY_WINDOW=16 restores the hysteretic trim exactly."""
    from diallux.graph.builder import CallRuntime as CR
    history = [{"role": "user", "content": f"m{i}"} for i in range(25)]
    assert CR._history_window(history, 0) == history          # full
    assert CR._history_window(history, 16, 8)[0] is history[16]  # jump pin


# --------------------------------------------------------------------------- #
# 6: warm parity under the new layout
# --------------------------------------------------------------------------- #
def test_warm_prefills_head_history_only_no_tail():
    """iter56 (F-05/CR-4): the warm payload == the hot heavy round's
    PRE-HISTORY prefix ([tools][head][FULL history]) — NO state-block tail
    (the delta never rides cache, so warming it is pointless)."""
    store = EmptyStore()
    fake = FakeLLM([{"tokens": ["Hi", "."]}])

    class WarmSpy(FakeLLM):
        def __init__(self, rounds=None):
            super().__init__(rounds)
            self.warm_calls = []

        async def warm(self, messages, tools):
            self.warm_calls.append((list(messages), list(tools)))

    spy = WarmSpy([{"tokens": ["Hi", "."]}])
    rt = make_runtime(spy, _settings(), kb_store=store)
    dvs = {"first_name": "Maria", "inbound_channel": "voicemail"}

    async def go():
        st = rt.initial_state("c56-8", dvs)
        rt.warm_prompt_cache("Intake", list(st["history"]), dvs=dict(st["dvs"]))
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
        config = {"configurable": {"thread_id": "c56-8"}}
        async for _m, _d in rt.graph.astream({"user_text": "hello there",
                                              **st}, config=config,
                                             stream_mode=["updates"]):
            pass
    asyncio.run(go())
    assert spy.warm_calls
    warm_msgs, warm_tools = spy.warm_calls[0]
    hot = spy.seen_messages[0]
    assert warm_msgs == hot[:len(warm_msgs)]         # warm == hot pre-history
    # NO state-block tail in the warm
    assert all(BLOCK_HEADING not in m["content"]
               for m in warm_msgs if m["role"] == "system")
    # the hot round's remainder = the user message + the post-history delta
    rest = hot[len(warm_msgs):]
    assert rest[0]["role"] == "user"
    assert rest[-1]["role"] == "system" and BLOCK_HEADING in rest[-1]["content"]
    assert [t["function"]["name"] for t in warm_tools] == spy.seen_tools[0]


# --------------------------------------------------------------------------- #
# 7: ingest freeze inert under state_in_delta
# --------------------------------------------------------------------------- #
def test_ingest_freeze_inert_under_state_in_delta():
    """iter56 (F-04/CR-3): the ingest frozen_state_block write is INERT under
    state_in_delta (nothing reads it; the state rides the live delta)."""
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = make_runtime(fake, _settings())             # state_in_delta default ON
    state = asyncio.run(_turns(rt, "c56-9", ["hello"]))
    assert "frozen_state_block" not in state
    assert BLOCK_HEADING in fake.seen_messages[0][-1]["content"]


def test_ingest_freeze_fires_on_revert():
    """Revert: state_in_delta=False → the freeze write fires again (the
    iter40 machinery is intact behind the flag)."""
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = make_runtime(fake, _settings(state_in_delta=False))
    state = asyncio.run(_turns(rt, "c56-10", ["hello"]))
    assert "frozen_state_block" in state


# --------------------------------------------------------------------------- #
# 8: full revert matrix — iter55 layout byte-exactly
# --------------------------------------------------------------------------- #
def test_revert_state_in_delta_false_restores_iter55_layout():
    """state_in_delta=False → [head][state-block tail][history] with a
    RAG/directive-only post-history delta — the iter55 payload, byte-exactly
    (rollback matrix: any regression flips ONE env var)."""
    store = EmptyStore()
    fake = FakeLLM([EXTRACT_ROUND, {"tokens": ["And", " what", " else", "?"]}])
    rt = make_runtime(fake, _settings(state_in_delta=False), kb_store=store)
    asyncio.run(_turns(rt, "c56-11", ["hi, saw your ad", "yeah go on"]))
    r2 = fake.seen_messages[1]
    roles = [m["role"] for m in r2]
    assert roles[0] == "system"                              # head
    assert roles[1] == "system" and BLOCK_HEADING in r2[1]["content"]  # tail
    assert roles[2] != "system"                              # history follows
    # the tail carries the captured values (pre-history, frozen/re-rendered)
    assert "inbound_channel: voicemail" in r2[1]["content"]
    # the DELTA position (post-history trailing systems, excluding the
    # pre-history tail at index 1) never carries a state block — RAG only
    post_history = [m for m in r2[2:] if m["role"] == "system"]
    assert all(BLOCK_HEADING not in m["content"] for m in post_history)


def test_revert_history_window_env_respected():
    """The three knobs are independent revert switches: state_in_delta=False
    + history_window=16 → the full iter55 double-revert in one runtime."""
    store = EmptyStore()
    fake = FakeLLM([EXTRACT_ROUND] + [{"tokens": [f"r{i}."]} for i in range(20)])
    rt = make_runtime(fake, _settings(state_in_delta=False, history_window=16),
                      kb_store=store)
    texts = [f"msg {i}" for i in range(20)]

    async def go():
        config = {"configurable": {"thread_id": "c56-12"}}
        payload = {"user_text": texts[0], **rt.initial_state("c56-12")}
        for i, text in enumerate(texts):
            if i > 0:
                payload = {"user_text": text}
            async for _m, _d in rt.graph.astream(payload, config=config,
                                                 stream_mode=["updates"]):
                pass
    asyncio.run(go())
    last = fake.seen_messages[-1]
    non_system = [m for m in last if m["role"] != "system"]
    assert len(non_system) <= 16                     # window engaged again
