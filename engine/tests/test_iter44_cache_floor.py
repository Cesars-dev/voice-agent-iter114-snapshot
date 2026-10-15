"""iter44 — cache floor + conversation-driven RAG.

Pins:
  T3  a. tail-before-history layout: a same-state round's messages are a
         strict PREFIX extension of the previous round (prefix cache grows)
      b. kill-switch tail_before_history=False → legacy layout
  T6c c. same-topic repeat round → NO re-retrieval (drift dedupe)
      d. topic-shift round → re-retrieve with the NEW query + vec reuse,
         tail rebuilt, rag:drift span emitted
  T4  e. store None + flag False (default) → markers stripped, NO inline KB,
         loud log
      f. flag True → legacy inline blob (rollback switch)
      g. explicit rag_mode="inline" → inline blob UNTOUCHED (legit V1 mode)
  T2  h. store resolve failing once does NOT latch None — next call retries
      i. session start with a dead/hanging store still proceeds
  T6b j. ok transition → staged chunks consumed at entry, NO second retrieve
      k. prefetch failure → entry falls back to the sync fetch (never raises)
  T6a l. embed client built ONCE for 2+ retrieves
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux import rag as ragmod
from tests.fake_llm import FakeLLM, tool_call
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())
MARKER = "# CURRENT CALL STATE (live values)"


def _settings(**kw) -> Settings:
    # first_turn_lite=False: these tests pin the FULL-payload layout/drift
    # math (T3/T6c/T6b) — the lite default (iter44, owner-approved) has its
    # own dedicated tests (test_first_turn_lite.py + test_default_is_lite).
    # state_in_delta=False: the T3 layout pins below (tail-before-history
    # prefix growth, legacy layout) describe the PRE-HISTORY tail — under
    # the iter56 default the state rides the delta (own pins in
    # test_iter56_state_delta.py).
    return Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, first_turn_lite=False,
                    state_in_delta=False, **kw)


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


# --------------------------------------------------------------------------- #
# T3 a: prefix grows round-to-round
# --------------------------------------------------------------------------- #
def test_tail_before_history_prefix_grows():
    fake = FakeLLM([
        {"tokens": ["Hi", "."]},
        {"tokens": ["What", " brings", " you", "?"]},
    ])
    rt = make_runtime(fake, _settings(fallback_inline_kb=True))
    asyncio.run(_turns(rt, "c44-1", ["hello there", "ok sure"]))
    r1, r2 = fake.seen_messages[0], fake.seen_messages[1]
    assert len(r2) > len(r1)
    assert r2[:len(r1)] == r1                      # strict prefix extension
    assert r1[1]["role"] == "system" and MARKER in r1[1]["content"]


# --------------------------------------------------------------------------- #
# T3 b: kill-switch → legacy layout
# --------------------------------------------------------------------------- #
def test_tail_before_history_off_legacy_layout():
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = make_runtime(fake, _settings(fallback_inline_kb=True,
                                      tail_before_history=False))
    asyncio.run(_turns(rt, "c44-2", ["hello"]))
    msgs = fake.seen_messages[0]
    assert msgs[0]["role"] == "system"                       # head first
    assert msgs[-1]["role"] == "system" and MARKER in msgs[-1]["content"]  # tail LAST
    assert msgs[1]["role"] != "system"                       # history follows head


# --------------------------------------------------------------------------- #
# T6c c+d: drift dedupe + re-retrieval
# --------------------------------------------------------------------------- #
BASE_VEC = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
SHIFT_VEC = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class DriftStore:
    """embed_query keyed by text; retrieve records everything."""

    def __init__(self, vec_map: dict[str, list[float]]):
        self.vec_map = vec_map                # substring -> vec
        self.embeds: list[str] = []
        self.retrieves: list[tuple[str, list[str], list[float] | None]] = []

    async def embed_query(self, text: str) -> list[float]:
        self.embeds.append(text)
        for k, v in self.vec_map.items():
            if k in text:
                return v
        return BASE_VEC

    async def retrieve(self, query_text, kb_slugs, vec=None):
        self.retrieves.append((query_text, list(kb_slugs), vec))
        return [{"kb": "industry", "content": "trucking niche opener",
                 "score": 0.9}]


class SpyTracer:
    def __init__(self):
        self.spans: list[tuple[str, dict]] = []

    def start_llm(self, *a, **k):
        return None

    def finish_llm(self, *a, **k):
        pass

    def span(self, name, **kw):
        self.spans.append((name, kw.get("output")))


def test_drift_same_topic_skips_reretrieve():
    store = DriftStore({"trucking": SHIFT_VEC})   # only "trucking" drifts
    fake = FakeLLM([
        {"tokens": ["Got", " it", "."]},
        {"tokens": ["Tell", " me", " more", "."]},
    ])
    # iter49c T1: pins the iter48 drift path (DriftStore has no
    # retrieve_lanes) — live mode OFF.
    rt = make_runtime(fake, _settings(rag_live_retrieve=False), kb_store=store)
    asyncio.run(_turns(rt, "c44-3", ["saw your ad about missed calls",
                                     "yeah tell me more"]))
    # entry retrieves once; drift check embeds round query + lazy baseline
    assert len(store.retrieves) == 1
    assert len(store.embeds) == 2


def test_drift_topic_shift_reretrieves_with_new_query():
    store = DriftStore({"trucking": SHIFT_VEC})
    tr = SpyTracer()
    fake = FakeLLM([
        {"tokens": ["Got", " it", "."]},
        {"tokens": ["And", " what", " else", "?"]},
        {"tokens": ["Great", " niche", "."]},
    ])

    async def go():
        rt = make_runtime(fake, _settings(rag_live_retrieve=False),
                          kb_store=store)
        rt.tracer = tr
        await _turns(rt, "c44-4", ["saw your ad about missed calls",
                                   "ok tell me more",
                                   "i run a trucking company"])
        return rt
    rt = asyncio.run(go())
    # iter46 T2: the drift check is a BG task — one per same-state round with
    # a user message (turn 2 same-topic no-drift, turn 3 trucking drift).
    assert len(store.retrieves) == 2              # entry + drifted re-retrieval
    q2, scope2, vec2 = store.retrieves[1]
    assert "trucking" in q2
    assert vec2 == SHIFT_VEC                      # vec reuse — no double embed
    drift_spans = [o for n, o in tr.spans if n == "rag:drift"]
    assert len(drift_spans) == 2                  # one bg task per drift-checked round
    trucking = [o for o in drift_spans if o["cosine"] < 0.85]
    assert len(trucking) == 1 and trucking[0]["async"] is True
    # append-only: the round itself stayed on the frozen entry tail (zero
    # awaits on the round path); this store returns the SAME chunk set on
    # every retrieve, so the delta dedupe correctly yields no NEW chunks.
    assert "trucking niche opener" in fake.seen_systems[0]


# --------------------------------------------------------------------------- #
# iter44 append-only: drift delta rides AFTER the history — the [head][tail]
# prefix stays byte-stable round-to-round even across topic shifts.
# --------------------------------------------------------------------------- #
class TwoChunkStore:
    """retrieve returns a DIFFERENT chunk per query (drift always changes set)."""

    def __init__(self):
        self.retrieves: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        return SHIFT_VEC if "trucking" in text else BASE_VEC

    async def retrieve(self, query_text, kb_slugs, vec=None):
        self.retrieves.append(query_text)
        if "trucking" in query_text:
            return [{"kb": "industry", "content": "trucking niche opener",
                     "score": 0.9}]
        return [{"kb": "pain-points", "content": "missed calls cost revenue",
                 "score": 0.8}]


def test_drift_delta_appends_after_history_tail_byte_stable():
    store = TwoChunkStore()
    fake = FakeLLM([
        {"tokens": ["Got", " it", "."]},          # turn 1: entry round
        {"tokens": ["Hmm", " ok", "."]},          # turn 2: frozen reuse (no drift)
        {"tokens": ["Neat", "."]},                # turn 3: DRIFT (trucking)
        {"tokens": ["Bye", "."]},                 # turn 4: drift again, SAME chunks
    ])
    texts = ["saw your ad about missed calls",
             "ok tell me more",
             "i run a trucking company",
             "still trucking by the way"]

    async def go():
        rt = make_runtime(fake, _settings(rag_live_retrieve=False),
                          kb_store=store)
        config = {"configurable": {"thread_id": "c44-10"}}
        payload = {"user_text": texts[0], **rt.initial_state("c44-10")}
        for i, text in enumerate(texts):
            if i > 0:
                payload = {"user_text": text}
                await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
                # iter46 T2: the previous round's BG drift task lands here —
                # its delta renders THIS round (correctness lag ≤ 1 round).
                await asyncio.gather(*rt._drift_tasks.values(),
                                     return_exceptions=True)
            async for _m, _d in rt.graph.astream(payload, config=config,
                                                 stream_mode=["updates"]):
                pass
        return rt
    rt = asyncio.run(go())
    r1, r2, r3, r4 = (fake.seen_messages[i] for i in range(4))
    # r2 is a strict PREFIX EXTENSION of r1 (tail + history byte-stable)
    assert r2[:len(r1)] == r1
    # the drifted chunk did NOT re-render the tail — it appended AFTER history
    assert "trucking niche opener" not in r1[1]["content"]   # tail untouched
    # async drift: turn 3's round itself still used the FROZEN entry chunks —
    # the trucking delta renders on the NEXT round (r4), lag ≤ 1 round.
    assert r3[-1]["role"] == "user"
    assert "trucking niche opener" not in fake.seen_systems[2]
    # r3's tail block is byte-equal to r2's (no mid-prompt re-render)
    assert r3[1]["content"] == r2[1]["content"]
    # r4 = r3 + the drift delta system block AFTER the history
    assert r4[:len(r3)] == r3
    assert r4[-1]["role"] == "system" \
        and "trucking niche opener" in r4[-1]["content"]
    # dedupe: turn 4 re-drifts the SAME chunk set → delta unchanged
    assert ("industry", "trucking niche opener") in \
        {(c["kb"], c["content"]) for c in rt._rag_delta_chunks}


# --------------------------------------------------------------------------- #
# T4 e/f/g: the inline guard
# --------------------------------------------------------------------------- #
def test_guard_default_no_inline_no_kb(caplog):
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = make_runtime(fake, _settings())          # fallback_inline_kb default False
    import logging
    with caplog.at_level(logging.WARNING, logger="diallux.graph"):
        asyncio.run(_turns(rt, "c44-5", ["hello"]))
    system = fake.seen_systems[0]
    assert "-kb##" not in system                  # markers stripped
    assert "Industry-Specific Sales Knowledge Base" not in system   # NO blob
    # iter89: general prose references "under ## KNOWLEDGE" - pin the rendered signatures
    assert "## KNOWLEDGE (most relevant excerpts" not in system
    assert "(No knowledge-base excerpt matched" not in system
    assert any("fallback_inline_kb=False" in r.message for r in caplog.records)


def test_guard_flag_true_legacy_blob():
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = make_runtime(fake, _settings(fallback_inline_kb=True))
    asyncio.run(_turns(rt, "c44-6", ["hello"]))
    assert "Industry-Specific Sales Knowledge Base" in fake.seen_systems[0]


def test_explicit_inline_mode_untouched():
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = make_runtime(fake, _settings(rag_mode="inline"))   # flag still False
    asyncio.run(_turns(rt, "c44-7", ["hello"]))
    assert "Industry-Specific Sales Knowledge Base" in fake.seen_systems[0]


# --------------------------------------------------------------------------- #
# T2 h/i: resolve latch fix + session-start safety
# --------------------------------------------------------------------------- #
def test_resolve_latch_retries_after_transient_failure(monkeypatch):
    # kb_store left at the SENTINEL → lazy resolve path (what production uses)
    rt = CallRuntime(_settings(), LLM_JSON, tracer=None, llm=FakeLLM(),
                     http_client=mock_client())
    calls = []

    async def flaky(settings):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("pg blip")
        return object()

    monkeypatch.setattr(ragmod, "get_kb_store", flaky)

    async def go():
        first = await rt._resolve_kb_store()
        second = await rt._resolve_kb_store()
        return first, second
    first, second = asyncio.run(go())
    assert first is None                          # transient failure → None
    assert second is not None                     # retried, NOT latched
    assert len(calls) == 2


def test_session_start_dead_and_hanging_store_proceeds(monkeypatch):
    """CallSession.start() awaits the store resolve bounded at 2.0 s,
    best-effort: a dead or hanging store never blocks the call start."""
    from diallux.media.session import CallSession
    from diallux.media import session as session_mod

    # PROMOTED iter91 hermeticity: session.start() builds its own STT/TTS
    # (eager wiring) — inject the fakes at the FACTORY level; the test
    # previously made REAL Deepgram/TTS connects and only passed where
    # ambient keys were valid.
    class FakeSTT:
        async def connect(self, ws=None):
            pass

    class FakeTTS:
        async def connect(self, ws=None):
            pass

        def new_context_id(self):
            return "ctx-1"
        async def speak(self, ctx, text, continue_=False, overrides=None):
            pass

    monkeypatch.setattr(session_mod, "DeepgramSTT", lambda *a, **k: FakeSTT())
    monkeypatch.setattr(session_mod, "create_tts", lambda *a, **k: FakeTTS())

    def mk_session(settings):
        sess = object.__new__(CallSession)
        sess.settings = settings
        sess.call_sid = "c44"
        sess.stream_sid = "s44"
        sess.transport = "browser"
        sess.tracer = None
        sess.llm_json = {"default_dynamic_variables": {},
                         "starting_state": "Intake"}
        sess.custom_parameters = {}
        sess.runtime = make_runtime(FakeLLM(), settings)
        sess.stt = FakeSTT()
        sess.tts = FakeTTS()
        sess._initial_payload = {"history": [{"role": "assistant",
                                              "content": "hi"}], "dvs": {}}
        sess._turn_state = "Intake"
        return sess

    async def dead():
        async def boom(settings):
            raise RuntimeError("pg down")
        monkeypatch.setattr(ragmod, "get_kb_store", boom)
        await mk_session(_settings()).start()

    async def hanging():
        async def hang(settings):
            await asyncio.sleep(30)
        monkeypatch.setattr(ragmod, "get_kb_store", hang)
        await asyncio.wait_for(mk_session(_settings()).start(), timeout=6.0)

    asyncio.run(dead())
    asyncio.run(hanging())                        # bounded by wait_for(2.0)


# --------------------------------------------------------------------------- #
# T6b j/k: transition prefetch
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
    # turn 3: first Discovery round — staged chunks must cover it
    {"tokens": ["So", ",", " who", " answers", "?"]},
]


def test_prefetch_staged_chunks_used_no_second_retrieve():
    store = DriftStore({})
    fake = FakeLLM(TRANSCRIPT_ROUNDS)
    # iter49c T1: pins the iter48 prefetch/staging path — live mode OFF.
    # iter49 T6: state_entry_lite OFF too — this pins the iter48 ENTRY
    # semantics (staged chunks consumed AT state entry); the T6 ack moves
    # that to the first HEAVY round (pinned in test_iter49_rag_parity.py).
    rt = make_runtime(fake, _settings(rag_live_retrieve=False,
                                      state_entry_lite=False), kb_store=store)
    asyncio.run(_turns(rt, "c44-8", ["saw your ad about missed calls",
                                     "go on", "yeah go ahead"]))
    # PROMOTED iter91 (warm armed): Intake entry (1) + tool-detect PRE-DVS
    # Discovery staging (2) + with-dvs Discovery staging (3). The ENTRY
    # still pays 0 — staging consumed (asserted below).
    assert len(store.retrieves) == 3
    assert rt._rag_staging.get("Discovery") is None     # consumed (popped)
    assert "trucking niche opener" in fake.seen_systems[-1]


def test_prefetch_failure_falls_back_sync():
    store = DriftStore({})
    real_retrieve = store.retrieve
    calls = []

    async def flaky(query, scope, vec=None):
        calls.append(query)
        if len(calls) == 2:                   # the Discovery STAGING call
            raise RuntimeError("webhook blip")
        return await real_retrieve(query, scope, vec=vec)

    store.retrieve = flaky
    fake = FakeLLM(TRANSCRIPT_ROUNDS)
    # iter49c T1: pins the iter48 staging-fallback path — live mode OFF.
    # iter49 T6: state_entry_lite OFF too (iter48 ENTRY semantics — see above).
    rt = make_runtime(fake, _settings(rag_live_retrieve=False,
                                      state_entry_lite=False), kb_store=store)
    asyncio.run(_turns(rt, "c44-9", ["saw your ad about missed calls",
                                     "go on", "yeah go ahead"]))
    # iter46 T4a (HISTORICAL, bare defaults): three Discovery staging attempts
    # each re-staged after the fast flaky failure.
    # PROMOTED iter91 (warm armed): the flaky call is the tool-detect PRE-DVS
    # attempt ('loss playback' scaffold); the post-execute + transition-
    # prefetch attempts dedupe against the in-flight restage — ONE restage
    # with dv values. The ENTRY round still pays 0 (consumed staging).
    assert len(calls) == 3
    assert sum(1 for q in calls if "losing leads" in q) == 1
    assert "trucking niche opener" in fake.seen_systems[-1]


# --------------------------------------------------------------------------- #
# T6a l: embed client built once
# --------------------------------------------------------------------------- #
def test_embed_client_built_once(monkeypatch):
    from diallux.rag import KBStore

    constructions = []

    async def spy_embed_fn(model, api_key):
        constructions.append(model)

        async def _embed(texts):
            return [[0.1] * 8 for _ in texts]
        return _embed

    monkeypatch.setattr(ragmod, "openai_embed_fn", spy_embed_fn)

    class FakePool:
        def acquire(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch(self, *a, **k):
            return []

    store = KBStore(database_url="postgresql://x", kb_dir="agent/knowledge_bases")

    async def go():
        async def ok_connect():
            store._pool = FakePool()
            return True
        store.connect = ok_connect            # skip pgvector entirely
        await store.retrieve("query one", ["industry"])
        await store.retrieve("query two", ["industry"])
    asyncio.run(go())
    assert len(constructions) == 1            # ONE client, two retrieves
    assert store.embed_fn is not None
