"""iter49 T2 — local embedder wiring (arctic-m, fastembed in-process).

Hermetic routing/selection tests (no Postgres, no model load, no OpenAI) +
ONE real integration embed (skipped when fastembed or the arctic-m cache is
absent). The wiring contract: rag_embedding_model="snowflake/snowflake-
arctic-embed-m" selects the LOCAL query path (768-d, arctic query prefix,
to_thread-parked); anything else keeps the OpenAI path (iter48 exact).
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from diallux.rag import (
    ARCTIC_QUERY_PREFIX,
    EMBEDDING_DIMS,
    KBStore,
    LOCAL_EMBED_MODELS,
)

ARCTIC = "snowflake/snowflake-arctic-embed-m"


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# registration + dims
# --------------------------------------------------------------------------- #
def test_arctic_registered_with_768_dims():
    assert EMBEDDING_DIMS[ARCTIC] == 768
    assert ARCTIC in LOCAL_EMBED_MODELS
    assert "text-embedding-3-small" not in LOCAL_EMBED_MODELS


def test_kbstore_dims_follow_model():
    s = KBStore(database_url="postgresql://x/x", kb_dir=".", embedding_model=ARCTIC)
    assert s.dims == 768
    s2 = KBStore(database_url="postgresql://x/x", kb_dir=".")
    assert s2.dims == 1536


# --------------------------------------------------------------------------- #
# _ensure_embed_fn routing (hermetic — factories monkeypatched)
# --------------------------------------------------------------------------- #
def test_ensure_embed_fn_routes_local(monkeypatch):
    routed = {}

    async def fake_local(model, cache_dir, threads=4, prefix=ARCTIC_QUERY_PREFIX):
        routed.update(model=model, cache_dir=cache_dir, threads=threads)

        async def fn(texts):
            return [[0.0] * 768 for _ in texts]
        return fn

    monkeypatch.setattr("diallux.rag.local_embed_fn", fake_local)
    s = KBStore(database_url="postgresql://x/x", kb_dir=".",
                embedding_model=ARCTIC,
                local_embed_cache_dir="/cache", local_embed_threads=4)
    fn = _run(s._ensure_embed_fn())
    assert routed == {"model": ARCTIC, "cache_dir": "/cache", "threads": 4}
    assert len(_run(fn(["q"]))[0]) == 768
    # built ONCE and reused (iter44 T6a contract preserved)
    assert _run(s._ensure_embed_fn()) is fn


def test_ensure_embed_fn_routes_openai(monkeypatch):
    async def fake_openai(model, key):
        async def fn(texts):
            return [[0.0] * 1536 for _ in texts]
        return fn

    monkeypatch.setattr("diallux.rag.openai_embed_fn", fake_openai)
    s = KBStore(database_url="postgresql://x/x", kb_dir=".",
                embedding_model="text-embedding-3-small", api_key="k")
    fn = _run(s._ensure_embed_fn())
    assert len(_run(fn(["q"]))[0]) == 1536


def test_ingest_refuses_local_model_before_db_connect():
    # must raise on the model guard BEFORE any DB attempt (bogus DSN would
    # raise a different RuntimeError if the guard were ordered wrong)
    s = KBStore(database_url="postgresql://nobody:x@127.0.0.1:1/none",
                kb_dir=".", embedding_model=ARCTIC)
    with pytest.raises(RuntimeError, match="kb_reembed"):
        _run(s.ingest())


# --------------------------------------------------------------------------- #
# local_embed_fn semantics — prefix applied to every query (hermetic)
# --------------------------------------------------------------------------- #
def test_local_embed_fn_prefixes_and_parks(monkeypatch):
    import diallux.rag as ragmod
    captured = {}

    class FakeTE:
        def __init__(self, model_name, cache_dir, threads):
            captured.update(model=model_name, cache_dir=cache_dir, threads=threads)

        def embed(self, texts):
            captured.setdefault("calls", []).append(list(texts))
            return [[0.0] * 768 for _ in texts]

    monkeypatch.setitem(sys.modules, "fastembed",
                        types.SimpleNamespace(TextEmbedding=FakeTE))
    fn = _run(ragmod.local_embed_fn(ARCTIC, "/cache", threads=4))
    assert captured["model"] == ARCTIC and captured["threads"] == 4
    queries = ["missed calls pricing objection", "warm goodbye wrap-up next steps"]
    out = _run(fn(queries))
    assert all(len(v) == 768 for v in out)
    # every call (warm + real) prefix-stamps every text
    for call in captured["calls"]:
        assert all(t.startswith(ARCTIC_QUERY_PREFIX) for t in call)
    assert captured["calls"][-1] == [ARCTIC_QUERY_PREFIX + q for q in queries]


# --------------------------------------------------------------------------- #
# real integration (runs when fastembed + the arctic-m cache exist)
# --------------------------------------------------------------------------- #
def test_local_embed_fn_real_arctic_768d():
    pytest.importorskip("fastembed")
    if not Path("/home/julio/fastembed/models/"
                "models--Snowflake--snowflake-arctic-embed-m").exists():
        pytest.skip("arctic-m model cache not present")
    import time

    import diallux.rag as ragmod
    t0 = time.perf_counter()
    fn = _run(ragmod.local_embed_fn(ARCTIC, "/home/julio/fastembed/models", threads=4))
    vecs = _run(fn(["missed calls pricing objection",
                    "warm goodbye wrap-up next steps"]))
    ms = (time.perf_counter() - t0) * 1000
    assert len(vecs) == 2 and all(len(v) == 768 for v in vecs)
    print(f"arctic-m: 2 queries incl. session warm in {ms:.0f}ms")
