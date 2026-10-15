"""iter31 C3 — prompt-cache architecture tests (hermetic, no Postgres/OpenAI).

Pins the byte-stable prefix contract:
  1. static head is byte-identical across rounds with changing dvs
  2. static head is byte-identical across calls (iter56: {{var}} tokens are
     STRIPPED to the "CURRENT CALL STATE" anchor under head_strip_vars)
  3. the dynamic STATE BLOCK carries the live dv values (iter56: it rides
     the POST-HISTORY DELTA, not the pre-history tail — state_in_delta)
  4. tool descriptions/params are dvs-free ({{var}} literal, static bytes)
  5. RAG chunks frozen per state-visit (one retrieval per state entry)
  6. no unresolved {{ tokens in the dynamic delta
  7. state_block=False drops the block; the head stays static either way
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.graph.llm import build_tool_schemas
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())
GENERAL = (ROOT / "diallux" / "prompts" / "general_prompt.md").read_text(encoding="utf-8")
MARKER = "# CURRENT CALL STATE (live values)"


def _settings(**kw) -> Settings:
    # rag_min_query_chars=0: iter21's filler-turn RAG skip is OFF in these
    # tests — short utterances ("hi", "ok") are the retrieval probes here.
    # fallback_inline_kb=True: these tests pin the LEGACY inline-KB expansion
    # path (kb_store=None → whole-KB head) — the iter44 guard default (False)
    # is exercised in test_iter44_cache_floor.py.
    # state_in_delta=False: the iter31 tests pin the ORIGINAL pre-history
    # tail contract (head byte-stability + tail-carries-values). The iter56
    # delta placement has its own pins in test_iter56_state_delta.py.
    return Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, rag_min_query_chars=0,
                    fallback_inline_kb=True, first_turn_lite=False,
                    state_in_delta=False, **kw)


def make_runtime(fake: FakeLLM, settings: Settings | None = None) -> CallRuntime:
    return CallRuntime(settings or _settings(), LLM_JSON, tracer=None, llm=fake,
                       http_client=mock_client(), kb_store=None)


async def _turns(rt: CallRuntime, call_id: str, texts: list[str]) -> dict:
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": texts[0], **rt.initial_state(call_id)}
    for i, text in enumerate(texts):
        if i > 0:
            payload = {"user_text": text}
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
    return (await rt.graph.aget_state(config)).values


def _head(system: str) -> str:
    return system.split(MARKER, 1)[0]


# --------------------------------------------------------------------------- #
# 1+2: static head byte-stability
# --------------------------------------------------------------------------- #
def test_head_byte_stable_across_rounds_with_changing_dvs():
    """Round 1 captures dvs via extract_intake_details; round 2's system must
    share the EXACT head bytes of round 1 (only the STATE BLOCK tail differs)."""
    fake = FakeLLM([
        {"tokens": ["Got", " it", "."],
         "tool_calls": [tool_call("extract_intake_details", {
             "inbound_channel": "voicemail", "interest_topic": "missed calls",
             "pain_frame": "losing leads"})]},
        {"tokens": ["And", " what", " else", "?"]},
    ])
    rt = make_runtime(fake)
    asyncio.run(_turns(rt, "c3-1", ["hi, jay's voicemail", "yeah"]))
    assert len(fake.seen_systems) == 2
    assert _head(fake.seen_systems[0]) == _head(fake.seen_systems[1])
    # and the captured value really landed in the TAIL of round 2
    assert "inbound_channel: voicemail" in fake.seen_systems[1]


def test_head_byte_stable_across_calls_and_dvs_literal():
    """Two runtimes (two calls, different persona dvs) produce byte-identical
    heads. iter56 head_strip_vars default ON: the head carries ZERO {{var}}
    tokens — they became the stable "CURRENT CALL STATE" anchor. The
    head_strip_vars=False revert path (literal tokens) is pinned separately
    in test_iter56_state_delta.py."""
    fake_a = FakeLLM([{"tokens": ["Hi", "."]}])
    fake_b = FakeLLM([{"tokens": ["Hi", "."]}])
    rt_a = make_runtime(fake_a)
    rt_b = make_runtime(fake_b)
    asyncio.run(_turns(rt_a, "c3-2a", ["hello"]))
    asyncio.run(_turns(rt_b, "c3-2b", ["hello"]))
    head = _head(fake_a.seen_systems[0])
    assert head == _head(fake_b.seen_systems[0])
    assert "{{callback_number}}" not in head      # iter56: stripped to the anchor
    assert "CURRENT CALL STATE" in head           # the stable anchor text
    # prompts feed the head unmutated (up to the first inline KB expansion)
    assert head.startswith(GENERAL.split("##call-context-kb##")[0])
    assert "Industry-Specific Sales Knowledge Base" in head   # top-level ## expanded


# --------------------------------------------------------------------------- #
# 3+6: STATE BLOCK carries values; tail has no unresolved tokens
# --------------------------------------------------------------------------- #
def test_state_block_carries_values_head_stays_literal():
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = make_runtime(fake)
    config = {"configurable": {"thread_id": "c3-3"}}
    payload = {"user_text": "hi",
               **rt.initial_state("c3-3", {"first_name": "Maria",
                                           "inbound_channel": "voicemail"})}
    async def go():
        async for _m, _d in rt.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
    asyncio.run(go())
    system = fake.seen_systems[0]
    assert MARKER in system
    tail = system.split(MARKER, 1)[1]
    assert "first_name: Maria" in tail
    assert "inbound_channel: voicemail" in tail
    head = _head(system)
    assert "{{callback_number}}" not in head      # iter56: anchor text, never a token
    assert "first_name: Maria" not in head        # values only ever in the tail/delta


def test_no_unresolved_tokens_in_tail():
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = make_runtime(fake)
    asyncio.run(_turns(rt, "c3-4", ["hi"]))
    system = fake.seen_systems[0]
    tail = system.split(MARKER, 1)[1]
    assert "{{" not in tail


# --------------------------------------------------------------------------- #
# 4: tool schemas are dvs-free + cached
# --------------------------------------------------------------------------- #
def test_tool_descriptions_dvs_free_and_cached():
    settings = _settings()
    rt = CallRuntime(settings, LLM_JSON, tracer=None, llm=FakeLLM(),
                     http_client=mock_client(), kb_store=None)
    # ConfirmSlots tool blobs carry the densest {{dv}} usage — dvs must not
    # leak into the bytes: output with live dvs == output with empty dvs.
    def build(dvs):
        return json.dumps(build_tool_schemas(
            rt.states["ConfirmSlots"], rt.llm_json.get("general_tools", []),
            lambda text: rt.subst.subst(text, dvs, kb=True), dvs))
    blob = build({})
    assert build({}) == blob                      # deterministic → static bytes
    assert "{{prospect_timezone}}" in blob        # token stays literal
    # runtime wiring: cached schemas equal the dvs-free build even when the
    # call carries live dv values
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt2 = make_runtime(fake)
    config = {"configurable": {"thread_id": "c3-5"}}
    payload = {"user_text": "hi",
               **rt2.initial_state("c3-5", {"prospect_timezone": "America/Chicago",
                                            "today_date": "2026-09-07"})}
    async def go():
        async for _m, _d in rt2.graph.astream(payload, config=config, stream_mode=["updates"]):
            pass
    asyncio.run(go())
    cached = rt2._tools_cache[("Intake", True)]
    assert json.dumps(cached) == json.dumps(build_tool_schemas(
        rt2.states["Intake"], rt2.llm_json.get("general_tools", []),
        lambda text: rt2.subst.subst(text, {}, kb=True), {}))
    assert rt2._tools_cache[("Intake", True)] is cached


# --------------------------------------------------------------------------- #
# 5: RAG freeze per state-visit
# --------------------------------------------------------------------------- #
class SpyKB:
    def __init__(self):
        self.calls = 0

    async def retrieve(self, query_text, kb_slugs):
        self.calls += 1
        return [{"kb": "pain-points", "content": "empathic mirror tactic", "score": 0.9}]


def test_rag_chunks_frozen_per_state_visit():
    kb = SpyKB()
    fake = FakeLLM([
        # turn 1, Intake round: extract + speak (one-trip finalize)
        {"tokens": ["Got", " it", "."],
         "tool_calls": [tool_call("extract_intake_details", {
             "inbound_channel": "voicemail", "interest_topic": "missed calls",
             "pain_frame": "losing leads"})]},
        # turn 2, Intake again: frozen visit — NO new retrieval
        {"tokens": ["What", " worries", " you", "?"]},
        # turn 3: completion flag + transition to Discovery (gate reads dvs).
        # iter40 L3: the ok transition ONE-TRIP-finalizes the turn — the
        # Discovery state visit (and its retrieval) moves to the next turn.
        {"tokens": ["Let", "'s", " dig", " in", "."],
         "tool_calls": [
             tool_call("intake_completed", {"intake_completed": True}),
             tool_call("transition_to_Discovery", {
                 "intake_completed": True, "inbound_channel": "voicemail",
                 "interest_topic": "missed calls", "pain_frame": "losing leads"})]},
        # turn 4: first round in Discovery (edge swap happened last turn)
        # -> new state visit -> retrieval
        {"tokens": ["So", ",", " who", " answers", "?"]},
    ])
    # iter49c T1: pins the iter48 freeze path (SpyKB has no retrieve_lanes)
    # — live mode OFF.
    rt = CallRuntime(_settings(rag_live_retrieve=False), LLM_JSON, tracer=None,
                     llm=fake, http_client=mock_client(), kb_store=kb)
    asyncio.run(_turns(rt, "c3-6", ["hi", "ok", "go on", "yeah go ahead"]))
    # PROMOTED iter91 (warm armed): 3 = Intake entry (1) + the tool-detect
    # PRE-DVS Discovery staging (2, scaffold query — dvs not committed yet)
    # + the with-dvs Discovery staging (3). Designed pack behavior (proven
    # live, forensics rows 13/14).
    assert kb.calls == 3, f"expected 3 (Intake + 2 Discovery staging), got {kb.calls}"
    # systems: [Intake r1, Intake r2 (frozen reuse), Intake r3, Discovery r4]
    assert len(fake.seen_systems) == 4
    assert "start of the call" in fake.seen_system_msgs[2][0]   # turn 3 round 1: still Intake
    assert "Bridge what they came for" in fake.seen_system_msgs[3][0]  # turn 4: Discovery
    # bare head byte-identical across the two pre-transition Intake rounds
    assert fake.seen_system_msgs[0][0] == fake.seen_system_msgs[1][0]
    # and the FROZEN knowledge bytes (the tail's RAG section) are identical
    a, b = fake.seen_systems[0], fake.seen_systems[1]
    assert a[a.index("## KNOWLEDGE (most relevant excerpts"):] == \
        b[b.index("## KNOWLEDGE (most relevant excerpts"):]   # iter89: prose says "under ## KNOWLEDGE" - anchor at the rendered header


# --------------------------------------------------------------------------- #
# 7: state_block flag
# --------------------------------------------------------------------------- #
def test_state_block_flag_off_keeps_head_static():
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = make_runtime(fake, _settings(state_block=False))
    asyncio.run(_turns(rt, "c3-7", ["hi"]))
    system = fake.seen_systems[0]
    assert MARKER not in system
    head = rt.static_head("Intake", True)
    assert "{{callback_number}}" not in head      # iter56: anchor (head_strip_vars)
    assert "CURRENT CALL STATE" in head
    assert rt.static_head("Intake", True) is head   # cached identity


def test_rag_frozen_round_with_tracer_no_unbound_query():
    """iter37 regression: a frozen-reuse round with a LIVE tracer used to hit
    UnboundLocalError on `query` inside the rag span (first live mic test,
    round 2, same state, utterance >= rag_min_query_chars). Tests with
    tracer=None never executed the span — this one does."""
    from diallux.observability.tracer import Tracer

    class SpyTracer(Tracer):
        """Real interface (start_llm etc. no-op via enabled=False); span() records."""

        def __init__(self):
            super().__init__(enabled=False)
            self.spans = []

        def span(self, name, *, input=None, output=None, metadata=None):
            self.spans.append((name, output))

    class SpyKB:
        def __init__(self):
            self.calls = 0

        async def retrieve(self, query_text, kb_slugs):
            self.calls += 1
            return [{"kb": "pain-points", "content": "empathic mirror tactic", "score": 0.9}]

    kb = SpyKB()
    tr = SpyTracer()
    fake = FakeLLM([
        # turn 1: extract + speak -> retrieval happens (real query)
        {"tokens": ["Got", " it", "."],
         "tool_calls": [tool_call("extract_intake_details", {
             "inbound_channel": "voicemail", "interest_topic": "missed calls",
             "pain_frame": "losing leads"})]},
        # turn 2: SAME state, real (non-filler) utterance -> frozen-reuse round
        {"tokens": ["What", " else", "?"]},
    ])
    # iter49c T1: pins the iter48 frozen-reuse + tracer-span path — live OFF.
    rt = CallRuntime(_settings(rag_live_retrieve=False), LLM_JSON, tracer=tr,
                     llm=fake, http_client=mock_client(), kb_store=kb)
    asyncio.run(_turns(rt, "c3-8", ["saw your ad about missed calls", "yeah tell me more"]))
    assert kb.calls == 1                      # frozen: no second retrieval
    rag_spans = [o for n, o in tr.spans if n == "rag"]
    assert len(rag_spans) == 2                # round 1 (fresh) + round 2 (frozen)
    assert rag_spans[1]["frozen_reuse"] is True
    assert rag_spans[1]["query"] == ""        # was UnboundLocalError before the fix
