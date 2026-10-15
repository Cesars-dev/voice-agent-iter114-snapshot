"""RAG layer tests — hermetic (no Postgres, no OpenAI). Production build.

Covers: chunking, marker parsing, the builder injection contract, the
inline fallback, and the system-prompt surgery (markers stripped, excerpts
appended, prompt bytes otherwise identical).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.rag import (
    chunk_markdown,
    kb_slugs_in,
    render_knowledge_section,
    strip_kb_markers,
)
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client

SETTINGS = Settings(
    openai_api_key="test", retell_api_key="test", langfuse_enabled=False,
    first_turn_lite=False
)
LLM_JSON = json.loads((Path(__file__).resolve().parents[1] / "agent" / "llm.json").read_text())


# --------------------------------------------------------------------------- #
# chunker
# --------------------------------------------------------------------------- #
def test_chunk_markdown_splits_on_headings_and_packs():
    md = "# Title\n\n" + ("para one. " * 30) + "\n\n## Section A\n\n" + ("alpha. " * 40) \
         + "\n\n## Section B\n\nbeta short."
    chunks = chunk_markdown(md, target=200, overlap=30)
    assert len(chunks) >= 4
    assert any("Section A" in c for c in chunks)
    assert all(len(c) <= 220 for c in chunks), [len(c) for c in chunks]
    assert "beta short." in chunks[-1]


def test_chunk_markdown_real_kbs():
    kb_dir = Path(__file__).resolve().parents[1] / "agent" / "knowledge_bases"
    total = 0
    for f in sorted(kb_dir.glob("*.md")):
        chunks = chunk_markdown(f.read_text(encoding="utf-8"))
        assert chunks, f
        total += len(chunks)
    # 63k chars / heading-dense KBs: low hundreds of ~300-char chunks
    assert 20 <= total <= 300, total


def test_kb_marker_parsing():
    text = "Use ##sales-psychology-kb## and ##industry-kb## then ##sales-psychology-kb##."
    assert kb_slugs_in(text) == ["sales-psychology", "industry"]
    stripped = strip_kb_markers(text)
    assert "-kb##" not in stripped and "sales-psychology" not in stripped


def test_render_knowledge_section():
    out = render_knowledge_section([{"kb": "industry", "content": "Dental pain: after-hours calls.", "score": 0.9}])
    assert "from industry-kb" in out and "Dental pain" in out
    empty = render_knowledge_section([])
    assert "No knowledge-base excerpt" in empty


# --------------------------------------------------------------------------- #
# builder integration (injected fake store)
# --------------------------------------------------------------------------- #
class FakeKBStore:
    def __init__(self, canned: dict[str, str]):
        self.canned = canned
        self.queries: list[tuple[str, list[str]]] = []

    async def retrieve(self, query_text, kb_slugs):
        self.queries.append((query_text, list(kb_slugs)))
        return [{"kb": s, "content": self.canned[s], "score": 0.9}
                for s in kb_slugs if s in self.canned]


def _run(coro):
    return asyncio.run(coro)


def test_builder_rag_mode_strips_markers_and_appends_excerpts():
    fake = FakeLLM([
        {"tokens": [], "tool_calls": []},
        {"tokens": ["Hi", "."], "match_system": "This is the start of the call"},
    ])
    # empty tool_calls round 1 -> finalize quickly; give round 2 as the speech
    fake.rounds = [{"tokens": ["Hi", "."], "match_system": "This is the start of the call"}]
    kb = FakeKBStore({"are-you-ai": "If asked if you are an AI: confirm, pivot to value."})
    # iter49c T1: pins the iter48 frozen-tail shape (excerpt IN the first
    # system message; FakeKBStore has no retrieve_lanes) — live mode OFF.
    # The live-mode delta placement is pinned in test_iter49_rag_parity.py.
    rt = CallRuntime(SETTINGS.model_copy(update={"rag_live_retrieve": False}),
                     LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=kb)

    async def go():
        config = {"configurable": {"thread_id": "rag-1"}}
        async for _, _d in rt.graph.astream({"user_text": "are you an AI?",
                                              **rt.initial_state("rag-1")},
                                             config=config, stream_mode=["updates"]):
            pass
    _run(go())
    system = fake.seen_systems[0]
    # markers gone, excerpt in, query scoped, prompt otherwise verbatim
    assert "-kb##" not in system
    assert "confirm, pivot to value" in system
    assert any("are-you-ai" in q[0] or "are-you-ai" in q[1] for q in kb.queries)
    # the state prompt text itself is untouched
    assert "This is the start of the call" in system


def test_builder_rag_mode_kb_scope_includes_state_kbs():
    kb = FakeKBStore({})
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=FakeLLM(),
                     http_client=mock_client(), kb_store=kb)
    scope = rt.kb_slugs_for("Discovery")
    # general_prompt's 10 KBs + Discovery-specific ones
    assert "pain-points" in scope
    assert "discovery-bridge" in scope
    assert "industry" in scope


def test_builder_inline_fallback_when_store_none():
    fake = FakeLLM([{"tokens": ["Hi", "."], "match_system": "This is the start of the call"}])
    settings = SETTINGS.model_copy(update={"fallback_inline_kb": True})
    rt = CallRuntime(settings, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)

    async def go():
        config = {"configurable": {"thread_id": "rag-2"}}
        async for _, _d in rt.graph.astream({"user_text": "hello",
                                              **rt.initial_state("rag-2")},
                                             config=config, stream_mode=["updates"]):
            pass
    _run(go())
    system = fake.seen_systems[0]
    # exact v1/lab behavior: full inline KB expansion, single pass
    # (note: KB bodies that reference ##industry-kb## internally keep that
    # marker literal — engine.py's single-pass re.sub, kept for parity)
    assert "[KB " not in system, "every top-level marker must resolve to a file"
    # iter89: general prose references "under ## KNOWLEDGE" - pin the rendered signatures
    assert "## KNOWLEDGE (most relevant excerpts" not in system
    assert "(No knowledge-base excerpt matched" not in system
    assert "Industry-Specific Sales Knowledge Base" in system
    assert "Retrieved when:" in system


def test_builder_metrics_include_rag_ms():
    fake = FakeLLM([{"tokens": ["Hi", "."], "match_system": "This is the start of the call"}])
    kb = FakeKBStore({"are-you-ai": "x"})
    # iter49c T1: pins the iter48 metrics wiring — live mode OFF.
    rt = CallRuntime(SETTINGS.model_copy(update={"rag_live_retrieve": False}),
                     LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=kb)

    async def go():
        config = {"configurable": {"thread_id": "rag-3"}}
        async for _, _d in rt.graph.astream({"user_text": "are you an AI?",
                                              **rt.initial_state("rag-3")},
                                             config=config, stream_mode=["updates"]):
            pass
        snap = await rt.graph.aget_state(config)
        return snap.values
    values = _run(go())
    assert "rag_ms" in values.get("metrics", {})
    assert values["metrics"]["rag_kbs"] == ["are-you-ai"]


# --------------------------------------------------------------------------- #
# store resilience
# --------------------------------------------------------------------------- #
def test_kbstore_connect_failure_is_soft():
    from diallux.rag import KBStore
    store = KBStore(database_url="postgresql://nobody:x@127.0.0.1:1/none",
                    kb_dir="agent/knowledge_bases", connect_timeout_s=0.2)

    async def go():
        ok = await store.connect()
        chunks = await store.retrieve("anything", ["industry"])
        return ok, chunks
    ok, chunks = _run(go())
    assert ok is False
    assert chunks == []          # per-turn fallback, never raises


def test_get_kb_store_respects_modes():
    import diallux.rag as ragmod
    ragmod.reset_singleton_for_tests()

    async def go():
        s = Settings(openai_api_key="t", retell_api_key="t", langfuse_enabled=False,
                     rag_mode="inline", database_url="postgresql://x@127.0.0.1:1/none")
        none_store = await ragmod.get_kb_store(s)
        return none_store
    assert _run(go()) is None
    ragmod.reset_singleton_for_tests()
