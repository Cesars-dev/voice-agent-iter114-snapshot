"""iter49 T3 — rag_table_name plumbing pins (hermetic: no Postgres, no model).

The cutover switch: KBStore reads/writes the CONFIGURED table (retrieve
SELECT, schema-ensure, count) while the default stays kb_chunks (iter48
exact); ingest — the OpenAI-path builder — refuses to own any other table
(kb_chunks_v2 belongs to scripts/kb_reembed.py). T4 appends the lane/quota/
dedupe/lite-entry pins to this file.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from diallux.rag import KBStore


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# default + identifier validation (table name reaches SQL text, env-settable)
# --------------------------------------------------------------------------- #
def test_rag_table_name_default_is_kb_chunks_v2():
    # iter49c T1 re-pin: the cutover triple flipped the default to the
    # arctic-m corpus (kb_chunks_v2). The OLD table stays reachable via
    # explicit constructor arg / env (KBStore(..., table_name="kb_chunks")).
    from diallux.config import Settings
    assert Settings().rag_table_name == "kb_chunks_v2"


def test_kbstore_rejects_non_identifier_table_name():
    with pytest.raises(ValueError):
        KBStore(database_url="postgresql://x/x", kb_dir=".",
                table_name="kb_chunks_v2; DROP TABLE kb_chunks")


# --------------------------------------------------------------------------- #
# retrieve/schema honor the configured table (fake pool captures the SQL)
# --------------------------------------------------------------------------- #
class _FakeConn:
    def __init__(self, rows=None):
        self.sqls = []
        self.rows = rows or []

    async def execute(self, sql, *args):
        self.sqls.append(sql)

    async def fetchval(self, sql, *args):
        self.sqls.append(sql)
        return 1

    async def fetch(self, sql, *args):
        self.sqls.append(sql)
        return self.rows


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        @contextlib.asynccontextmanager
        async def _cm():
            yield self._conn
        return _cm()

    async def close(self):
        pass


async def _fake_embed(texts):
    return [[0.0] * 1536 for _ in texts]


def _store_with_fake_pool(monkeypatch, table_name, rows):
    conn = _FakeConn(rows)

    async def _fake_create_pool(*a, **kw):
        return _FakePool(conn)
    monkeypatch.setattr("asyncpg.create_pool", _fake_create_pool)
    s = KBStore(database_url="postgresql://x/x", kb_dir=".",
                embedding_model="text-embedding-3-small",
                embed_fn=_fake_embed, table_name=table_name)
    return s, conn


def test_retrieve_selects_from_configured_table(monkeypatch):
    rows = [{"kb": "pain-points", "content": "mirror math", "score": 0.5}]
    s, conn = _store_with_fake_pool(monkeypatch, "kb_chunks_v2", rows)
    out = _run(s.retrieve("the empathic mirror", ["pain-points"]))
    assert out and out[0]["kb"] == "pain-points"
    assert any("FROM kb_chunks_v2" in sql for sql in conn.sqls)
    # schema-ensure targets the configured table + its index names too
    assert any("CREATE TABLE IF NOT EXISTS kb_chunks_v2" in sql
               for sql in conn.sqls)
    assert any("kb_chunks_v2_emb_idx" in sql for sql in conn.sqls)


def test_retrieve_default_table_is_unchanged_iter48(monkeypatch):
    rows = [{"kb": "industry", "content": "dental pain", "score": 0.5}]
    s, conn = _store_with_fake_pool(monkeypatch, "kb_chunks", rows)
    out = _run(s.retrieve("q", ["industry"]))
    assert out
    # exact default-table SELECT (not the v2 table, not a bare substring hit)
    assert any("FROM kb_chunks\n" in sql for sql in conn.sqls)
    assert not any("FROM kb_chunks_v2" in sql for sql in conn.sqls)
    assert any("CREATE TABLE IF NOT EXISTS kb_chunks (" in sql
               for sql in conn.sqls)


def test_ingest_refuses_non_default_table_before_db_connect():
    # raises on the ownership guard BEFORE any DB attempt (bogus DSN would
    # raise a different error if the guard were ordered wrong)
    s = KBStore(database_url="postgresql://nobody:x@127.0.0.1:1/none",
                kb_dir=".", embedding_model="text-embedding-3-small",
                table_name="kb_chunks_v2")
    with pytest.raises(RuntimeError, match="kb_reembed"):
        _run(s.ingest())


def test_get_kb_store_passes_table_name(monkeypatch):
    import diallux.rag as ragmod
    ragmod.reset_singleton_for_tests()
    conn = _FakeConn()

    async def _fake_create_pool(*a, **kw):
        return _FakePool(conn)
    monkeypatch.setattr("asyncpg.create_pool", _fake_create_pool)
    settings = types.SimpleNamespace(
        rag_mode="auto", database_url="postgresql://x/x",
        knowledge_base_dir=".", rag_embedding_model="text-embedding-3-small",
        openai_api_key=None, local_embed_cache_dir="/cache",
        local_embed_threads=4, rag_top_k=3, rag_filter_score=0.4,
        rag_char_budget=1600, rag_table_name="kb_chunks_v2")
    store = _run(ragmod.get_kb_store(settings))
    try:
        assert store is not None and store.table_name == "kb_chunks_v2"
        assert any("FROM kb_chunks_v2" in sql for sql in conn.sqls)
    finally:
        ragmod.reset_singleton_for_tests()


# =========================================================================== #
# iter49c T4 pins — lanes / quota / placement / revert / async (hermetic)
# =========================================================================== #
import json                                                        # noqa: E402
import time                                                        # noqa: E402

from diallux.config import Settings                                # noqa: E402
from diallux.graph.builder import CallRuntime                      # noqa: E402
from tests.fake_llm import FakeLLM, tool_call                            # noqa: E402
from tests.mock_webhooks import mock_client                        # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_LLM_JSON = json.loads((_ROOT / "agent" / "llm.json").read_text())

_STATE_KBS_EXPECTED = {
    "Intake": ["pain-points", "call-context", "sales-language",
               "voice-ai-capabilities", "are-you-ai", "sales-psychology",
               "industry", "hipaa", "call-closing"],
    "Discovery": ["pain-points", "call-context", "sales-language",
                  "voice-ai-capabilities", "are-you-ai", "sales-psychology",
                  "industry", "hipaa", "call-closing", "discovery-bridge"],
    "Closer": ["sales-psychology", "industry", "sales-language",
               "call-context", "pain-points"],
    "Offer": ["sales-psychology", "sales-language", "industry",
              "voice-ai-capabilities"],
    "Closing": ["call-closing", "sales-language"],
}
_GENERAL_KBS = ["call-context", "sales-language", "pain-points",
                "sales-psychology", "industry", "voice-ai-capabilities",
                "are-you-ai", "hipaa", "call-closing"]   # general_prompt.md doc order (iter89 phase-3 FINAL file)


def _t4_settings(**kw) -> Settings:
    # iter65: pin the "eot" fire mode these iter49-62 semantics were written
    # under (the iter65 code default is now "hybrid").
    base = dict(openai_api_key="test", retell_api_key="test",
                langfuse_enabled=False, first_turn_lite=False,
                rag_fire_mode="eot")
    base.update(kw)
    return Settings(**base)


def _t4_rt(settings: Settings | None = None, kb_store=..., fake=None):
    return CallRuntime(settings or _t4_settings(), _LLM_JSON, tracer=None,
                       llm=fake or FakeLLM(), http_client=mock_client(),
                       kb_store=kb_store)


def _chunk(i: int, kb: str, content: str, score: float = 0.5) -> dict:
    return {"id": i, "kb": kb, "content": content, "score": score}


class LanesStore:
    """Fake store for the live path: scripted per-call lane results.

    sets: list (one entry per retrieve_lanes CALL) of per-lane chunk lists.
    retrieve() serves the iter48 freeze path (revert pin)."""

    def __init__(self, sets: list | None = None, delay: float = 0.0):
        self.sets = sets or []
        self.delay = delay
        self.i = 0
        self.lane_calls: list[list[dict]] = []
        self.retrieves: list[tuple[str, list[str]]] = []

    async def retrieve_lanes(self, lanes):
        self.lane_calls.append(lanes)
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.sets:
            return [[] for _ in lanes]
        out = self.sets[min(self.i, len(self.sets) - 1)]
        self.i += 1
        return out

    async def retrieve(self, query, slugs, vec=None):
        self.retrieves.append((query, list(slugs)))
        return []


async def _t4_turns(rt: CallRuntime, call_id: str, texts: list[str]) -> dict:
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": texts[0], **rt.initial_state(call_id)}
    for i, text in enumerate(texts):
        if i > 0:
            payload = {"user_text": text}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
    return (await rt.graph.aget_state(config)).values


# ---- 1. _STATE_KBS map ----------------------------------------------------- #
def test_state_kbs_map_exact_off_unchanged_union_fallback():
    rt = _t4_rt()
    for state, kbs in _STATE_KBS_EXPECTED.items():
        assert rt.kb_slugs_for(state) == kbs, state
    # iter59 T4 (KB-everywhere): the mechanical states are FREED — they fall
    # through to the union fallback == the general 9 (Retell tier).
    for s in ("Booking", "VerifyLead", "ConfirmSlots", "contact_details"):
        assert rt.kb_slugs_for(s) == _GENERAL_KBS, s
    # unmapped state -> iter48 union fallback (general ∪ state markers);
    # no state prompt -> the general 9 (discovery-bridge is state-marked)
    assert rt.kb_slugs_for("MadeUpState") == _GENERAL_KBS


# ---- 2. build_lanes -------------------------------------------------------- #
def test_build_lanes_lane_a_query_format_and_scope():
    rt = _t4_rt()
    lanes, owned = rt.build_lanes(
        "Intake", {"pain_frame": "missed calls"},
        "my receptionist just quit on me")
    # iter89 Edit A: Intake carries THREE refer lanes (I-4/I-5). Empty dvs
    # → bare anchors; the {{pain_frame}} lane appends its dv value.
    assert [l["query"] for l in lanes[:3]] == [
        "surface pain recognition and opening discovery questions",
        "probing questions below the surface symptom",
        "emotional anchoring and empathic response: missed calls",
    ], lanes
    for l in lanes[:3]:
        assert l["scope"] == ["pain-points"]          # tag KB ONLY
    assert owned[0] == {"pain-points"}
    # lane B: caller text; industry anchor only when the dv exists
    assert lanes[-1]["query"] == "my receptionist just quit on me"
    # iter64: pre-pin (no industry dv, no pin) — industry gated out of scope
    assert "industry" not in lanes[-1]["scope"]
    assert lanes[-1]["scope"] == \
        [s for s in rt.kb_slugs_for("Intake") if s != "industry"]
    lanes2, _ = rt.build_lanes(
        "Intake", {"pain_frame": "x", "industry": "dental practice"},
        "my receptionist just quit on me")
    assert lanes2[-1]["query"].endswith("(their industry: dental practice)")


def test_build_lanes_leak_dollar_formatting():
    rt = _t4_rt()
    lanes, _ = rt.build_lanes(
        "Closer", {"weekly_leak": "4,000", "monthly_leak": "17,300"},
        "sure, go ahead please")
    assert lanes[0]["query"] == "loss playback: $4,000 a week, $17,300 a month"
    # already-$ passthrough (no double format)
    lanes2, _ = rt.build_lanes("Closer", {"weekly_leak": "$4,000"},
                               "sure, go ahead please")
    assert lanes2[0]["query"] == "loss playback: $4,000"
    # empty dvs -> bare anchor
    lanes3, _ = rt.build_lanes("Closer", {}, "sure, go ahead please")
    assert lanes3[0]["query"] == "loss playback"


def test_build_lanes_closing_anchor_and_e3_booked_swap():
    rt = _t4_rt()
    lanes, owned = rt.build_lanes("Closing", {}, "ok perfect, thanks so much, bye")
    assert lanes[0]["query"] == "warm goodbye wrap-up next steps"
    assert lanes[0]["scope"] == ["call-closing", "sales-language"]
    assert owned[0] == {"call-closing", "sales-language"}
    booked, _ = rt.build_lanes("Closing", {"booking_verified": True},
                               "that's great, thank you!")
    assert booked[0]["query"] == "recap booking SMS confirmation next steps"
    booked2, _ = rt.build_lanes("Closing", {"booking_confirmed": True}, "ty")
    assert booked2[0]["query"] == "recap booking SMS confirmation next steps"


def test_build_lanes_caps_off_states_and_multiquery_off():
    rt = _t4_rt()
    # Discovery: 2 refer-tags (<= 3) + lane B
    lanes, _ = rt.build_lanes("Discovery", {"pain_points": "losing patients"},
                              "how does this thing work exactly")
    assert len(lanes) == 3
    # iter64: pre-pin — industry gated out of the lane-B scope
    assert "industry" not in lanes[-1]["scope"]
    assert lanes[-1]["scope"] == \
        [s for s in rt.kb_slugs_for("Discovery") if s != "industry"]
    # iter59 T4 (KB-everywhere): freed states retrieve via lane B over the
    # 9-KB union (single lane, no owned KBs — quota applies)
    blanes, bowned = rt.build_lanes("Booking", {"booking_verified": True},
                                    "confirming the booking please")
    assert len(blanes) == 1
    assert blanes[0]["query"] == "confirming the booking please"
    # iter64: pre-pin gate applies to the freed-state general union too
    assert "industry" not in blanes[0]["scope"]
    assert blanes[0]["scope"] == \
        [s for s in rt.kb_slugs_for("Booking") if s != "industry"]
    assert bowned == [set()]
    # rag_multiquery=False -> ONE combined lane A (iter48 refer_query shape)
    rt2 = _t4_rt(_t4_settings(rag_multiquery=False))
    dvs, msg = {"pain_frame": "missed calls"}, "my receptionist just quit on me"
    lanes2, _ = rt2.build_lanes("Intake", dvs, msg)
    q, _vals = rt2.refer_query("Intake", dvs, msg)
    assert len(lanes2) == 2                       # combined lane A + lane B
    assert lanes2[0]["query"] == q
    # iter64: pre-pin gate — middle-mode combined lane, same rule
    assert "industry" not in lanes2[0]["scope"]
    assert lanes2[0]["scope"] == \
        [s for s in rt2.kb_slugs_for("Intake") if s != "industry"]


# ---- 3. merge_lane_chunks -------------------------------------------------- #
def test_merge_lane_chunks_id_dedupe_and_quota():
    from diallux.rag import merge_lane_chunks
    # id-dedupe across lanes: the same chunk PK renders once
    out = merge_lane_chunks(
        [[_chunk(1, "pain-points", "mirror", .5)],
         [_chunk(1, "pain-points", "mirror", .4)]], [set(), set()])
    assert len(out) == 1
    # quota: a non-owned KB contributes <= 1 chunk
    out = merge_lane_chunks(
        [[_chunk(1, "industry", "a", .5), _chunk(2, "industry", "b", .45),
          _chunk(3, "sales-language", "c", .4)]], [set()])
    assert [c["id"] for c in out] == [1, 3]
    # lane-owned exemption: the owning lane's KB may fill the working set
    out = merge_lane_chunks(
        [[_chunk(1, "industry", "a", .5), _chunk(2, "industry", "b", .45),
          _chunk(3, "sales-language", "c", .4)]], [{"industry"}])
    assert [c["id"] for c in out] == [1, 2, 3]


def test_merge_lane_chunks_topk_budget_and_none_id_fallback():
    from diallux.rag import merge_lane_chunks
    out = merge_lane_chunks(
        [[_chunk(i, f"kb{i}", f"c{i}", .5 - i * .01) for i in range(6)]],
        [set()])
    assert len(out) == 3                                   # top_k 3
    # char budget: an oversized second chunk stops the fill
    out = merge_lane_chunks(
        [[_chunk(1, "a", "small", .5),
          _chunk(2, "b", "x" * 1500, .49)]], [set()], char_budget=1000)
    assert len(out) == 1 and out[0]["id"] == 1
    # None-id fallback key: (kb, content[:80]) dedupes PK-less chunks
    def nc(kb, score, content):
        return {"id": None, "kb": kb, "content": content, "score": score}
    out = merge_lane_chunks(
        [[nc("pain-points", .5, "same content bytes")],
         [nc("pain-points", .4, "same content bytes")]], [set(), set()])
    assert len(out) == 1


# ---- 4. retrieve_lanes (fake pool; ONE batched embed + per-lane scopes) ---- #
class _ArgConn:
    def __init__(self, rows):
        self.rows = rows
        self.fetches: list[tuple[str, tuple]] = []

    async def execute(self, sql, *a):
        pass

    async def fetchval(self, sql, *a):
        return 1

    async def fetch(self, sql, *a):
        self.fetches.append((sql, a))
        return self.rows


def _lanes_store_with_fake_pool(monkeypatch, rows, embed):
    conn = _ArgConn(rows)

    async def _fake_create_pool(*a, **kw):
        return _FakePool(conn)
    monkeypatch.setattr("asyncpg.create_pool", _fake_create_pool)
    return KBStore(database_url="postgresql://x/x", kb_dir=".",
                   embedding_model="text-embedding-3-small",
                   embed_fn=embed, table_name="kb_chunks_v2"), conn


def test_retrieve_lanes_one_batched_embed_and_per_lane_scopes(monkeypatch):
    embed_calls: list[list[str]] = []

    async def embed(texts):
        embed_calls.append(list(texts))
        return [[0.1, 0.2] for _ in texts]

    rows = [{"id": 1, "kb": "pain-points", "content": "mirror",
             "score": 0.5}]
    s, conn = _lanes_store_with_fake_pool(monkeypatch, rows, embed)
    lanes = [{"query": "the empathic mirror", "scope": ["pain-points"]},
             {"query": "what does it cost", "scope": ["industry",
                                                     "sales-language"]}]
    out = _run(s.retrieve_lanes(lanes))
    assert len(embed_calls) == 1                       # ONE batched call
    assert embed_calls[0] == ["the empathic mirror", "what does it cost"]
    assert len(out) == 2 and out[0][0]["kb"] == "pain-points"
    scopes = [a[1] for _sql, a in conn.fetches]        # $2 = kb scope
    assert scopes == [["pain-points"], ["industry", "sales-language"]]
    # empty lanes -> empty result, no embed, no fetch
    embed_calls.clear()
    conn.fetches.clear()
    assert _run(s.retrieve_lanes([])) == []
    assert not embed_calls and not conn.fetches


def test_retrieve_lanes_embed_failure_never_raises(monkeypatch):
    async def boom(texts):
        raise RuntimeError("embed down")

    s, conn = _lanes_store_with_fake_pool(
        monkeypatch, [{"id": 1, "kb": "x", "content": "y", "score": .5}],
        boom)
    lanes = [{"query": "q1", "scope": ["a"]}, {"query": "q2", "scope": ["b"]},
             {"query": "q3", "scope": ["c"]}]
    out = _run(s.retrieve_lanes(lanes))
    assert out == [[], [], []]                         # per-lane empties


# ---- 5. live branch placement ---------------------------------------------- #
def test_live_delta_replaces_each_turn_prefix_bytes_stable():
    store = LanesStore(sets=[
        [[_chunk(1, "pain-points", "TURN ONE chunk about mirroring")]],
        [[_chunk(2, "pain-points", "TURN TWO chunk about anchoring")]],
    ])
    fake = FakeLLM([
        {"tokens": ["Got", " it", "."]},
        {"tokens": ["And", " more", "."]},
    ])
    rt = _t4_rt(kb_store=store, fake=fake)
    _run(_t4_turns(rt, "c49-live-1",
                   ["saw your ad about missed calls",
                    "yeah tell me more about that"]))
    assert len(store.lane_calls) == 2                  # fresh EVERY turn
    heads = [m[0] for m in fake.seen_system_msgs]
    assert heads[0] == heads[1]        # pre-history prefix byte-IDENTICAL
    deltas = [m[-1] for m in fake.seen_system_msgs]
    assert "TURN ONE chunk" in deltas[0]
    assert "TURN TWO chunk" in deltas[1]
    assert "TURN ONE chunk" not in deltas[1]           # REPLACED, not appended


def test_rag_keep_markers_strips_by_default_keeps_on_flag():
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "mirror chunk")]]])
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = _t4_rt(kb_store=store, fake=fake)
    _run(_t4_turns(rt, "c49-live-2", ["saw your ad about missed calls"]))
    assert "-kb##" not in fake.seen_system_msgs[0][0]  # default: stripped

    store2 = LanesStore(sets=[[[_chunk(1, "pain-points", "mirror chunk")]]])
    fake2 = FakeLLM([{"tokens": ["Hi", "."]}])
    rt2 = _t4_rt(_t4_settings(rag_keep_markers=True),
                 kb_store=store2, fake=fake2)
    _run(_t4_turns(rt2, "c49-live-3", ["saw your ad about missed calls"]))
    assert "##pain-points-kb##" in fake2.seen_system_msgs[0][0]


# ---- 6. revert: rag_live_retrieve=False exercises the freeze path ---------- #
def test_revert_flag_uses_freeze_path_even_when_store_has_lanes():
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "never used")]]])
    fake = FakeLLM([
        {"tokens": ["Got", " it", "."]},
        {"tokens": ["And", " more", "."]},
    ])
    rt = _t4_rt(_t4_settings(rag_live_retrieve=False),
                kb_store=store, fake=fake)
    _run(_t4_turns(rt, "c49-rev-1",
                   ["saw your ad about missed calls",
                    "yeah tell me more about that"]))
    assert store.lane_calls == []                      # lanes NEVER fire
    assert store.retrieves                              # iter48 retrieve ran
    assert rt._frozen_state == "Intake"                 # freeze latch set


# ---- 7. async: bounded await / degrade / dedupe / never raises -------------- #
def test_async_bounded_await_degrades_then_lands_then_prev():
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "async chunk")]]],
                       delay=0.25)
    rt = _t4_rt(kb_store=store)

    async def go():
        rt.spawn_live_retrieve("Intake", {}, "saw your ad about missed calls")
        t0 = time.perf_counter()
        d, k, ms, ch, lq, deg, aw = await rt._consume_live_retrieve(
            "Intake", {}, "saw your ad about missed calls", store)
        assert deg is True, "slow task must degrade within the cap"
        assert aw <= 60 + 15, f"bounded await exceeded cap+slack: {aw}ms"
        # the shielded task lands for the next round
        await asyncio.shield(rt._live_tasks["Intake|full"])
        assert "Intake" in rt._live_prev
        # same round's query, task landed -> FRESH, zero await
        d2, k2, ms2, ch2, lq2, deg2, aw2 = await rt._consume_live_retrieve(
            "Intake", {}, "saw your ad about missed calls", store)
        assert deg2 is False
        assert "pain-points" in k2
        # a NEW round's query with cap=0 -> never await, previous set
        # (await_ms is MILLISECONDS — 5ms bounds the sync path; the old
        # 0.005 threshold was seconds-units and demanded <5µs, flaking
        # to 0.1ms under load)
        rt.settings.rag_live_await_ms = 0
        d3, k3, ms3, ch3, lq3, deg3, aw3 = await rt._consume_live_retrieve(
            "Intake", {}, "a brand new message entirely", store)
        assert deg3 is True and aw3 < 5
        assert k3 == k2                                # prev-set degrade
        rt.settings.rag_live_await_ms = 60
        await rt.aclose()

    _run(go())


def test_async_spawn_dedupe_cancels_inflight_and_never_raises():
    rt = _t4_rt()

    async def go():
        rt.spawn_live_retrieve("Intake", {}, "first message in flight here")
        t1 = rt._live_tasks["Intake|full"]
        rt.spawn_live_retrieve("Intake", {}, "second message supersedes")
        t2 = rt._live_tasks["Intake|full"]
        assert t1 is not t2
        await asyncio.sleep(0.02)
        assert t1.cancelled() or t1.done(), "older task must be cancelled"
        t2.cancel()
        await rt.aclose()

    _run(go())
    # outside a running loop: silent no-op, never raises
    rt.spawn_live_retrieve("Intake", {}, "no event loop here")
    assert "Intake|full" not in rt._live_tasks


def test_async_broken_store_degrades_never_raises():
    rt = _t4_rt()

    async def boom_resolve():
        raise RuntimeError("store down")

    rt._resolve_kb_store = boom_resolve

    async def go():
        rt.spawn_live_retrieve("Intake", {}, "hello there, testing broken")
        await asyncio.shield(rt._live_tasks["Intake|full"])
        d, k, ms, ch, lq, deg, aw = await rt._consume_live_retrieve(
            "Intake", {}, "hello there, testing broken", None)
        assert deg is True and k == [] and d == ""
        await rt.aclose()

    _run(go())


# --------------------------------------------------------------------------- #
# 8. iter49 T6 (T4b): state-entry lite — ack rounds are light and instant
# --------------------------------------------------------------------------- #
class _WarmSpyLLM(FakeLLM):
    """FakeLLM + warm() recorder (the _warm path calls llm.warm directly)."""

    def __init__(self, rounds=None):
        super().__init__(rounds)
        self.warm_calls: list[tuple[list[dict], list[dict]]] = []

    async def warm(self, messages, tools):
        self.warm_calls.append((list(messages), list(tools)))


class _AwaitRecorder:
    """Swaps rt._await_warm for a recorder — pins WHO awaits, not how long."""

    def __init__(self, rt: CallRuntime):
        self.calls: list[str] = []
        rt._await_warm = self._rec

    async def _rec(self, state_name, wait_ms=None):
        self.calls.append(state_name)


def _payload_bytes(msgs: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) for m in msgs)


def test_state_entry_lite_turn2_initial_state_shape():
    """first_turn_lite=True: turn 1 = the EXISTING lite path; turn 2 = the
    state-entry lite into Intake ([state head kb=False][state-block]
    [history] + the FULL tool array + a post-history SPEECH-ONLY directive
    — the FIND-5 port: tools=[] is structurally uncacheable on gpt-5.4, so
    the ack keeps the heavy pre-history prefix byte-identical and rides the
    warm at the heavy cache floor); turn 3 = HEAVY (tools + fresh lanes).
    The turn-1 lite round renders no state head, so the turn-2 entry into
    the initial state still counts as a state entry."""
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "heavy turn chunk")]]])
    fake = FakeLLM([
        {"tokens": ["Hi", " there", "."]},          # turn 1: first_turn_lite
        {"tokens": ["Got", " it", "."]},            # turn 2: state-entry lite
        {"tokens": ["And", " more", "."]},          # turn 3: HEAVY
    ])
    rt = _t4_rt(Settings(openai_api_key="test", retell_api_key="test",
                         langfuse_enabled=False, first_turn_lite=True,
                         rag_fire_mode="eot"),
                kb_store=store, fake=fake)
    rec = _AwaitRecorder(rt)

    async def go():
        await _t4_turns(rt, "c49-t6-1",
                        ["hello?", "my receptionist quit on me",
                         "yeah tell me more about that"])

    _run(go())
    # turn 1: the EXISTING lite path — untouched
    assert fake.seen_tools[0] == ["memory_note"]
    assert "Your Mission" not in fake.seen_systems[0]
    # turn 2: state-entry lite — state head rendered, FULL tools
    # (byte-identical to the heavy round's = the FIND-5 port), NO lanes
    assert "Your Mission" in fake.seen_system_msgs[1][0]      # Intake head
    assert fake.seen_tools[1] and fake.seen_tools[1] == fake.seen_tools[2]
    assert len(store.lane_calls) == 1                         # ONLY turn 3's
    assert "tell me more" in store.lane_calls[0][-1]["query"]  # turn-3 query
    # the ack's ONLY post-history system block is the speech directive —
    # never a knowledge section
    last = fake.seen_messages[1][-1]
    assert last["role"] == "system" and "SPEECH ONLY" in last["content"]
    assert "heavy turn chunk" not in last["content"]
    assert rt._prev_round_state == "Intake"
    # awaits: turn 1 awaited its own lite warm; turn 3's heavy EOT await.
    # PROMOTED iter91: warm_await_dest_ms=2500 arms the state-entry ack's
    # bounded dest-await — the turn-2 ack now awaits its destination warm
    # (the extra "Intake" entry). Designed pack behavior.
    assert rec.calls == ["lite:Intake", "Intake", "Intake"]
    # turn 3: HEAVY — tool channel open, fresh lanes
    assert fake.seen_tools[2]                                 # tools present
    assert len(store.lane_calls) == 1                         # lanes fired
    # cache-floor parity (the POINT of the FIND-5 port): the ack's
    # [head][state-block] prefix is byte-identical to the heavy round's —
    # with the tools match above, the whole pre-history prefix matches, so
    # the ack rides the landed warm at the same cache floor (2688/3712).
    assert fake.seen_messages[1][0] == fake.seen_messages[2][0]
    assert fake.seen_messages[1][1] == fake.seen_messages[2][1]
    # byte budget: the ack payload stays a strict subset of the heavy
    # payload plus the ~115-char directive (measured: Intake ack ~10.5k
    # chars = head 9810 + state-block 547 + short history; the heavy round
    # adds the fresh knowledge delta — the tools array rides the wire,
    # cached). NOTE: the plan's ~1200-tok estimate was optimistic — the
    # ack is head-bound at ~2.6k tok; the SPEED answer is the cache floor,
    # not the byte diet.
    ack = _payload_bytes(fake.seen_messages[1])
    heavy = _payload_bytes(fake.seen_messages[2])
    assert ack < heavy
    assert ack <= 13000, f"state-entry ack payload too heavy: {ack} chars"


def test_state_entry_lite_ack_head_keep_markers_mirrors_heavy():
    """iter49h T7 residue: the ack's marker decision MIRRORS the heavy
    round's strip decision. With rag_keep_markers=True + a store the ACK
    head keeps the ##slug-kb## references and stays byte-identical to the
    heavy round's head — the FIND-5 cache-floor parity holds on the KEPT
    side of the A/B too, not just the stripped default (_entry_lite_head
    and _head_for both render static_head(state, kb=False) under the same
    strip decision). Default False strips on the ack exactly like the
    heavy round (the heavy strip is pinned in section 5 above — this
    extends it to the ack)."""
    # default False: the ack strips (extends the heavy strip pin)
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "heavy chunk")]]])
    fake = FakeLLM([
        {"tokens": ["Hi", " there", "."]},          # turn 1: first_turn_lite
        {"tokens": ["Got", " it", "."]},            # turn 2: state-entry lite
        {"tokens": ["And", " more", "."]},          # turn 3: HEAVY
    ])
    rt = _t4_rt(Settings(openai_api_key="test", retell_api_key="test",
                         langfuse_enabled=False, first_turn_lite=True,
                         rag_fire_mode="eot"),
                kb_store=store, fake=fake)
    _run(_t4_turns(rt, "c49-t7-1",
                   ["hello?", "my receptionist quit on me",
                    "yeah tell me more about that"]))
    assert "-kb##" not in fake.seen_system_msgs[1][0]   # ack strips by default
    assert fake.seen_system_msgs[1][0] == fake.seen_system_msgs[2][0]

    # keep_markers=True + store: the ack KEEPS the markers — bytes still
    # equal the heavy round's head, so the ack rides the landed warm at the
    # heavy cache floor on BOTH sides of the marker A/B.
    store2 = LanesStore(sets=[[[_chunk(1, "pain-points", "heavy chunk")]]])
    fake2 = FakeLLM([
        {"tokens": ["Hi", " there", "."]},
        {"tokens": ["Got", " it", "."]},
        {"tokens": ["And", " more", "."]},
    ])
    rt2 = _t4_rt(Settings(openai_api_key="test", retell_api_key="test",
                          langfuse_enabled=False, first_turn_lite=True,
                          rag_fire_mode="eot",
                          rag_keep_markers=True),
                 kb_store=store2, fake=fake2)
    _run(_t4_turns(rt2, "c49-t7-2",
                   ["hello?", "my receptionist quit on me",
                    "yeah tell me more about that"]))
    assert "##pain-points-kb##" in fake2.seen_system_msgs[1][0]   # ack keeps
    assert "##pain-points-kb##" in fake2.seen_system_msgs[2][0]   # heavy too
    assert fake2.seen_system_msgs[1][0] == fake2.seen_system_msgs[2][0]


def test_state_entry_lite_transition_ack_then_heavy():
    """first_turn_lite=False: turn 2's heavy Intake round fires transition_to_
    Discovery; the ack round (same turn, first round in Discovery) is the
    state-entry lite — FULL tools (FIND-5 port) + speech directive, no
    lanes; the NEXT turn's round in Discovery is heavy (tools + fresh
    lanes). Turn 1 stays exempt from the entry path (the existing turn-1
    semantics are untouched)."""
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "ack then heavy")]]])
    fake = FakeLLM([
        {"tokens": ["Hi", " there", "."]},               # turn 1: heavy Intake
        {"tokens": [], "tool_calls": [
            tool_call("extract_intake_details",
                      {"inbound_channel": "voicemail", "interest_topic":
                       "missed calls", "pain_frame": "losing jobs"}),
            tool_call("intake_completed", {"intake_completed": True}, "f1"),
            tool_call("transition_to_Discovery",
                      {"intake_completed": True, "inbound_channel": "voicemail",
                       "interest_topic": "missed calls",
                       "pain_frame": "losing jobs"}, "t1"),
        ]},                                            # turn 2 r1: heavy Intake
        {"tokens": ["Thanks", " for", " sharing", "."]},   # turn 2 r2: ACK
        {"tokens": ["Sure", " thing", "."]},               # turn 3 r1: heavy
    ])
    rt = _t4_rt(kb_store=store, fake=fake)
    rec = _AwaitRecorder(rt)

    async def go():
        await _t4_turns(rt, "c49-t6-2",
                        ["got a voicemail", "dental office manager here",
                         "yeah tell me more"])

    _run(go())
    assert fake.seen_tools[0]                                 # heavy Intake
    assert rt._prev_round_state == "Discovery"
    # the ack (round 2): Discovery head rendered, FULL tools byte-identical
    # to the next heavy Discovery round's (FIND-5 port — the ack rides the
    # transition warm's cached prefix), NO await — and the lane count below
    # (3 total: t1 + t2 Intake, t3 Discovery) proves the ack fired NONE
    assert "discovery" in fake.seen_system_msgs[2][0].lower()
    assert fake.seen_tools[2] and fake.seen_tools[2] == fake.seen_tools[3]
    assert fake.seen_messages[2][0] == fake.seen_messages[3][0]
    last = fake.seen_messages[2][-1]
    assert last["role"] == "system" and "SPEECH ONLY" in last["content"]
    assert "ack then heavy" not in last["content"]
    # next turn: heavy Discovery — tools + fresh lanes + EOT await.
    # PROMOTED iter91: the ack's bounded dest-await is ARMED
    # (warm_await_dest_ms=2500) — extra "Discovery" entry in the list.
    assert fake.seen_tools[3]
    assert len(store.lane_calls) == 3
    assert rec.calls == ["Intake", "Intake", "Discovery", "Discovery"]


def test_state_entry_lite_opt_out_and_kill_switch():
    """state_entry_lite_off='Discovery' -> the Discovery ack is HEAVY (a state
    that must tool-call on entry opts out); state_entry_lite=False -> every
    round is the old shape (kill switch, iter48 exact)."""
    # opt-out: the ack round in Discovery carries tools
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "opt out chunk")]]])
    fake = FakeLLM([
        {"tokens": ["Hi", "."]},                        # turn 1: heavy Intake
        {"tokens": [], "tool_calls": [
            tool_call("extract_intake_details",
                      {"inbound_channel": "voicemail", "interest_topic":
                       "missed calls", "pain_frame": "losing jobs"}),
            tool_call("intake_completed", {"intake_completed": True}, "f1"),
            tool_call("transition_to_Discovery",
                      {"intake_completed": True, "inbound_channel": "voicemail",
                       "interest_topic": "missed calls",
                       "pain_frame": "losing jobs"}, "t1"),
        ]},                                            # turn 2 r1: heavy Intake
        {"tokens": ["Thanks", "."]},                    # turn 2 r2: ACK (opted out)
        {"tokens": ["Sure", "."]},                      # turn 3 r1: heavy
    ])
    rt = _t4_rt(_t4_settings(state_entry_lite_off="Discovery"),
                kb_store=store, fake=fake)

    async def go():
        await _t4_turns(rt, "c49-t6-3", ["got a voicemail", "dental office",
                                         "yeah tell me more"])

    _run(go())
    assert fake.seen_tools[2], "opted-out state must keep its tools on entry"
    assert len(store.lane_calls) == 4      # t1, t2 Intake + ack + t3 Discovery

    # kill switch: state_entry_lite=False -> turn 2 after a lite turn 1 is
    # the FULL payload (the pre-T6 behavior)
    store2 = LanesStore(sets=[[[_chunk(1, "pain-points", "kill switch")]]])
    fake2 = FakeLLM([
        {"tokens": ["Hi", "."]},
        {"tokens": ["Got", " it", "."]},
    ])
    rt2 = _t4_rt(Settings(openai_api_key="test", retell_api_key="test",
                          langfuse_enabled=False, first_turn_lite=True,
                          rag_fire_mode="eot",
                          state_entry_lite=False),
                 kb_store=store2, fake=fake2)

    async def go2():
        await _t4_turns(rt2, "c49-t6-4", ["hello?", "my name is Kim"])

    _run(go2())
    assert fake2.seen_tools[1], "kill switch: turn 2 must be the FULL payload"
    assert len(store2.lane_calls) == 1


def test_warm_prefills_rag_free_prefix_equal_heavy_prehistory():
    """T7 residual pin: under rag_live_retrieve the warm prefills EXACTLY
    [tools][head][state-block][history] — byte-equal to the heavy round's
    PRE-HISTORY prefix; the fresh RAG delta is the only post-history suffix
    (and renders ONLY on the hot round, never in the warm)."""
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "warm prefix chunk")]]])
    fake = _WarmSpyLLM([{"tokens": ["Hi", "."]}])
    rt = _t4_rt(kb_store=store, fake=fake)

    async def go():
        await _t4_turns(rt, "c49-t6-5", ["saw your ad about missed calls"])
        # fire the warm with the SAME history/dvs the heavy round saw
        hist = [m for m in fake.seen_messages[0] if m["role"] != "system"]
        rt.warm_prompt_cache("Intake", list(hist), dvs=rt.initial_state(
            "c49-t6-5")["dvs"], force=True)
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)

    _run(go())
    hot = fake.seen_messages[0]
    assert len(fake.warm_calls) == 1
    warm_msgs, warm_tools = fake.warm_calls[0]
    # warm == the heavy round's pre-history prefix, byte-for-byte
    assert warm_msgs == hot[:len(warm_msgs)]
    # the ONLY hot remainder is the post-history RAG delta system block
    rest = hot[len(warm_msgs):]
    assert len(rest) == 1 and rest[0]["role"] == "system"
    assert "warm prefix chunk" in rest[0]["content"]
    # warm carries the FULL tool array (the heavy prefix starts with tools)
    assert [t["function"]["name"] for t in warm_tools] == fake.seen_tools[0]


def test_transition_chain_replay_logs_shapes_and_byte_budgets():
    """The plan's transition-focused OFFLINE replay: drive the gated chain
    Intake->Discovery->Closer->Offer->contact_details->ConfirmSlots->
    VerifyLead(engine Phase B)->Booking->Closing with the live-default flags
    (first_turn_lite + state_entry_lite ON). Asserts per round: the FIRST
    round in every new state is a lite entry (FULL tools + speech directive
    per the FIND-5 port, NO RAG lanes), the later rounds are heavy (lanes on
    sales states only), and the ack payload stays well under the heavy
    payload. Prints the per-round shape + byte budgets (run with -s)."""
    store = LanesStore(sets=[[[_chunk(1, "pain-points", "chain chunk")]]])
    rounds: list[dict] = [
        # turn 1 r1: lite turn (fake still emits the gated tool script)
        {"tokens": [], "tool_calls": [
            tool_call("extract_intake_details",
                      {"inbound_channel": "voicemail", "interest_topic":
                       "missed calls", "pain_frame": "losing jobs"}),
            tool_call("intake_completed", {"intake_completed": True}, "f1"),
            tool_call("transition_to_Discovery",
                      {"intake_completed": True, "inbound_channel": "voicemail",
                       "interest_topic": "missed calls",
                       "pain_frame": "losing jobs"}, "t1"),
        ]},
        {"tokens": ["Thanks", " for", " sharing", "."]},   # Discovery ACK
        # turn 2 r1: heavy Discovery -> Closer
        {"tokens": [], "tool_calls": [
            tool_call("extract_discovery_details",
                      {"industry": "dental", "pain_points": "after-hours",
                       "interest_signal": True, "pattern_matched": True}),
            tool_call("discovery_completed", {"discovery_completed": True}, "f2"),
            tool_call("transition_to_Closer",
                      {"industry": "dental", "interest_level": "high",
                       "closer_completed": True,
                       "pain_points": "after-hours"}, "t2"),
        ]},
        {"tokens": ["Ouch", "."]},                          # Closer ACK
        # turn 3 r1: heavy Closer -> Offer
        {"tokens": [], "tool_calls": [
            tool_call("extract_leak_inputs",
                      {"missed_calls_weekly": 20, "close_rate_pct": 50,
                       "avg_job_value": 650}),
            tool_call("extract_closer_details",
                      {"objection_type": "", "interest_level": "high",
                       "last_name": "Gonzales"}),
            tool_call("closer_completed", {"closer_completed": True}, "f3"),
            tool_call("transition_to_Offer",
                      {"industry": "dental", "interest_level": "high",
                       "closer_completed": True,
                       "pain_points": "after-hours"}, "t3"),
        ]},
        {"tokens": ["That", "'s", " $6,500", " a", " week", "."]},  # Offer ACK
        # turn 4 r1: heavy Offer -> contact_details
        {"tokens": [], "tool_calls": [
            tool_call("extract_offer_details", {"livecall_agreed": True}),
            tool_call("offer_completed", {"offer_completed": True}, "f4"),
            tool_call("transition_to_contact_details",
                      {"offer_completed": True, "livecall_agreed": True}, "t4"),
        ]},
        {"tokens": ["Perfect", "."]},                       # contact_details ACK
        # turn 5 r1: heavy contact_details -> ConfirmSlots
        {"tokens": [], "tool_calls": [
            tool_call("extract_person_details",
                      {"first_name": "Maria", "last_name": "Gonzales",
                       "company_name": "Bright Smile Dental"}),
            tool_call("extract_contact_timezone",
                      {"prospect_timezone": "America/Chicago"}),
            tool_call("record_reach_details",
                      {"is_calling_best_number": True}),
            tool_call("contact_details_completed",
                      {"contact_details_completed": True}, "f5"),
            tool_call("transition_to_ConfirmSlots",
                      {"contact_details_completed": True,
                       "first_name": "Maria", "last_name": "Gonzales",
                       "company_name": "Bright Smile Dental",
                       "prospect_timezone": "America/Chicago",
                       "callback_number": "+13124001234",
                       "is_calling_best_number": True,
                       "phone_confirmed": True}, "t5"),
        ]},
        {"tokens": ["Great", "."]},                         # ConfirmSlots ACK
        # turn 6 r1: heavy ConfirmSlots -> VerifyLead (engine Phase B chain
        # then books + moves to Closing) -> the "Locked in." round is the
        # Closing ACK
        {"tokens": [], "tool_calls": [
            tool_call("extract_confirm_details",
                      {"selected_time": "2026-09-04T14:30:00"}),
            tool_call("validate_lead",
                      {"first_name": "Maria", "last_name": "Gonzales",
                       "company_name": "Bright Smile Dental",
                       "callback_number": "+13124001234",
                       "prospect_timezone": "America/Chicago",
                       "selected_time": "2026-09-04T14:30:00",
                       "missed_calls_per_week": 20, "close_rate_percent": 50,
                       "avg_job_value": 650}),
            tool_call("transition_to_VerifyLead",
                      {"slot_verified": True,
                       "selected_time": "2026-09-04T14:30:00",
                       "prospect_timezone": "America/Chicago"}, "t6"),
        ]},
        {"tokens": ["Locked", " in", "."]},                 # Closing ACK (Phase B)
        # turn 7 r1: heavy Closing speech
        {"tokens": ["You", "'re", " booked", "."]},
        # turn 8 r1: heavy Closing -> end_call
        {"tokens": ["Bye", "!"],
         "tool_calls": [tool_call("end_call", {})]},
    ]
    fake = _WarmSpyLLM(rounds)
    rt = _t4_rt(Settings(openai_api_key="test", retell_api_key="test",
                         langfuse_enabled=False, first_turn_lite=True,
                         rag_fire_mode="eot"),
                kb_store=store, fake=fake)
    texts = ["got a voicemail", "dental office", "20 calls a week",
             "sounds good", "Maria Gonzales", "tomorrow works", "thanks", "bye"]

    async def go():
        config = {"configurable": {"thread_id": "c49-t6-chain"}}
        payload = {"user_text": texts[0],
                   **rt.initial_state("c49-t6-chain",
                                      persona_dvs={"callback_number":
                                                   "+13124001234"})}
        for i, text in enumerate(texts):
            if i > 0:
                payload = {"user_text": text}
            async for _m, _d in rt.graph.astream(
                    payload, config=config, stream_mode=["updates"]):
                pass
        return (await rt.graph.aget_state(config)).values

    state = _run(go())
    assert state["ended"] is True
    assert state["dvs"]["booking_verified"] is True
    assert rt.executor.gate_rejections == []
    # per-round shape audit: round 0 = turn-1 lite; an ACK (first round in
    # a new state) is identified by its post-history SPEECH-ONLY directive
    # (it carries FULL tools now — the FIND-5 port — so tools can no longer
    # discriminate; the directive is the ack's only trailing system block).
    def _is_ack(i: int) -> bool:
        msgs = fake.seen_messages[i]
        return (i > 0 and bool(msgs)
                and msgs[-1].get("role") == "system"
                and "SPEECH ONLY" in str(msgs[-1].get("content")))

    shapes = ["lite-turn1" if i == 0 else
              "entry-ack" if _is_ack(i) else "heavy"
              for i in range(len(fake.seen_tools))]
    heads = [m[0] for m in fake.seen_system_msgs]
    assert fake.seen_tools[0] == ["memory_note"]          # turn-1 lite untouched
    ack_idx = [i for i in range(1, len(fake.seen_tools)) if _is_ack(i)]
    assert ack_idx, "the chain must produce state-entry acks"
    for i in ack_idx:
        # an ack carries a STATE head (not the general-only lite head) and
        # the FULL tool array — byte-identical to the heavy round that
        # follows in the same state (same head bytes => same tools)
        assert "Your Mission" in fake.seen_system_msgs[i][0] \
            or any(k in fake.seen_system_msgs[i][0].lower()
                   for k in ("discovery", "closer", "offer", "contact",
                             "slot", "verify", "booking", "closing")), \
            f"round {i}: ack must carry a state head"
        assert fake.seen_tools[i], f"round {i}: ack must carry FULL tools"
        for j in range(i + 1, len(fake.seen_tools)):
            if fake.seen_messages[j][0] == fake.seen_messages[i][0]:
                assert fake.seen_tools[j] == fake.seen_tools[i], \
                    f"round {i}: ack tools must equal the same-state heavy's"
                break
    # lanes fire ONLY on heavy sales-state rounds — never on acks, never on
    # the mechanical states, never on the turn-1 lite rounds. NOTE: EVERY
    # round of turn 1 is a first_turn_lite round (turn_index==1 guard), so
    # the turn-1 "ack" in Discovery renders the turn-1 lite shape and never
        # counts as a state visit — turn 2 r1 in Discovery is the ENTRY ack.
        # Heavy sales rounds: Closer(t3), Offer(t4), Closing(t7), Closing(t8)
        # + iter59 KB-everywhere: contact_details(t5?) / ConfirmSlots / VerifyLead
        # heavy rounds now retrieve too (lane B over the union) — count
        # exactly and pin the states.
        # iter103 PT-5: the query memo absorbs byte-identical re-fires — the
        # Closing(t8) "bye" round (3 chars < rag_min_query_chars → no
        # utterance lane) repeats Closing(t7) "thanks"'s refer-tag-only lane
        # set, so the store sees 5 calls for 6 heavy rounds (1 memo hit).
        assert len(store.lane_calls) == 5, \
            f"expected lanes on 6 heavy rounds with 1 PT-5 memo hit (4 sales + 2 freed mechanical), got {len(store.lane_calls)}"
        assert len(rt._rag_query_memo) == 5, \
            "5 distinct query sets memoized for the 6 fires"
    # byte budgets: every ack payload is bounded (head+state-block are the
    # controllable bytes — the plan's ~1200-tok estimate was optimistic;
    # measured acks 10.3k-13k chars incl. history + the ~115-char
    # directive) and structurally lighter than heavy: no tools cost on the
    # prompt bytes (the array rides the wire, cached), no knowledge delta.
    budgets = [_payload_bytes(m) for m in fake.seen_messages]
    print("\n--- T6 transition-chain per-round shapes ---")
    for i, (shape, head, budget) in enumerate(zip(shapes, heads, budgets)):
        print(f"round {i}: {shape:10s} tools={len(fake.seen_tools[i])} "
              f"system_blocks={len(fake.seen_system_msgs[i])} "
              f"payload={budget}b head[:40]={head[:40]!r}")
    for i in ack_idx:                                       # an ack round
        assert budgets[i] <= 15000, f"ack round {i} too heavy: {budgets[i]}"
        last = fake.seen_messages[i][-1]
        assert last["role"] == "system" and "SPEECH ONLY" in last["content"], \
            f"ack round {i} must end with the speech directive"
        assert "chain chunk" not in last["content"], \
            f"ack round {i} must not carry a knowledge section"
