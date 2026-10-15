# iter52 T7 — pinned industry vertical: parser, metadata fetch, resolver
# ladder, lane integration, delta composition. Hermetic (fake pool / fake
# store); only the alias-integrity pin touches the live DB (skipif absent).
from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import diallux.rag as ragmod                        # noqa: E402
from diallux.config import Settings                 # noqa: E402
from diallux.graph import builder as buildermod     # noqa: E402
from diallux.graph.builder import CallRuntime       # noqa: E402
from tests.fake_llm import FakeLLM                  # noqa: E402
from tests.mock_webhooks import mock_client         # noqa: E402

_LLM_JSON = json.loads((_ROOT / "agent" / "llm.json").read_text())


def _run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _never_embed(texts):
    raise AssertionError("embed_fn must NEVER be called on the pin path")


# --------------------------------------------------------------------------- #
# shared fakes
# --------------------------------------------------------------------------- #
class _FakeConn:
    def __init__(self, rows=None, fail=False, vertical=None):
        self.sqls = []
        self.rows = rows or []
        self.fail = fail
        self.vertical = vertical

    async def execute(self, sql, *args):
        self.sqls.append(sql)

    async def fetchval(self, sql, *args):
        self.sqls.append(sql)
        if self.fail:
            raise RuntimeError("column \"vertical\" does not exist")
        return self.vertical

    async def fetch(self, sql, *args):
        self.sqls.append(sql)
        if self.fail:
            raise RuntimeError("column \"vertical\" does not exist")
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


def _store_with_fake_pool(monkeypatch, rows=None, fail=False, vertical=None):
    conn = _FakeConn(rows, fail=fail, vertical=vertical)

    async def _fake_create_pool(*a, **kw):
        return _FakePool(conn)
    monkeypatch.setattr("asyncpg.create_pool", _fake_create_pool)
    s = ragmod.KBStore(database_url="postgresql://x/x", kb_dir=".",
                       embedding_model="text-embedding-3-small",
                       embed_fn=_never_embed, table_name="kb_chunks_v2")
    return s, conn


class _FakeKBStore:
    """Resolver/pin fake: scripted retrieve + vertical_of + pinned_by_tag."""

    def __init__(self, retrieve_rows=None, verticals=None, pinned=None):
        self.retrieve_rows = list(retrieve_rows or [])
        self.verticals = dict(verticals or {})
        self.pinned_rows = list(pinned or [])
        self.retrieve_calls: list[tuple] = []
        self.vertical_calls: list = []
        self.pin_calls: list[str] = []
        self.lanes_served: list = []

    async def retrieve(self, query, slugs, vec=None):
        self.retrieve_calls.append((query, list(slugs)))
        return list(self.retrieve_rows)

    async def vertical_of(self, chunk_id):
        self.vertical_calls.append(chunk_id)
        return self.verticals.get(chunk_id)

    async def pinned_by_tag(self, tag):
        self.pin_calls.append(tag)
        return list(self.pinned_rows) if tag else []

    async def retrieve_lanes(self, lanes):
        return [[] for _ in lanes]


def _t4_settings(**kw) -> Settings:
    # rag_pin_industry pinned True here: these tests pin PIN BEHAVIOR under
    # explicit settings — the ambient RAG_PIN_INDUSTRY env must not flip them
    # (kill-switch tests pass False explicitly below).
    kw.setdefault("rag_pin_industry", True)
    return Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, first_turn_lite=False, **kw)


def _rt(settings=None, kb_store=...):
    return CallRuntime(settings or _t4_settings(), _LLM_JSON, tracer=None,
                       llm=FakeLLM(), http_client=mock_client(),
                       kb_store=kb_store)


def _chunk(i, kb, content, score=0.5):
    return {"id": i, "kb": kb, "content": content, "score": score}


# --------------------------------------------------------------------------- #
# 1. heading parser (kb_reembed.parse_vertical)
# --------------------------------------------------------------------------- #
def _load_kb_reembed():
    spec = importlib.util.spec_from_file_location(
        "kb_reembed_iter52", _ROOT / "scripts" / "kb_reembed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parser_3_sections_header_non_industry():
    kb = _load_kb_reembed()
    for sec in ("Business Owner Pain Points", "Voice AI Solutions",
                "ROI Metrics"):
        content = (f"[industry | Industry-Specific Sales Knowledge Base > "
                   f"Plumbing > {sec}]\n- bullet")
        assert kb.parse_vertical("industry", content) == "Plumbing"
    assert kb.parse_vertical(
        "industry",
        "[industry | Industry-Specific Sales Knowledge Base]\n> Retrieved when") \
        == "header"
    assert kb.parse_vertical("plumbing", "[plumbing | X > Y]") is None
    assert kb.parse_vertical("industry", "garbage") is None
    assert kb.parse_vertical("industry", "") is None


def test_parser_real_corpus_60_rows_20_verticals():
    corpus_path = Path("/home/julio/projects/diallux_kb_lab/corpi/"
                       "it8_plumbing.json")
    if not corpus_path.exists():
        pytest.skip("it8_plumbing.json not present on this box")
    kb = _load_kb_reembed()
    rows = json.loads(corpus_path.read_text())
    industry = [r for r in rows if r["kb"] == "industry"]
    tags = [kb.parse_vertical("industry", r["content"]) for r in industry]
    assert len(industry) == 60 and all(tags)
    verticals = {t for t in tags if t != "header"}
    assert len(verticals) == 20 and "Plumbing" in verticals
    assert sum(1 for t in tags if t == "header") == 1
    assert len([r for r in rows if r["kb"] == "plumbing"]) >= 10


# --------------------------------------------------------------------------- #
# 2-3. pinned_by_tag / vertical_of (metadata only, NEVER embeds)
# --------------------------------------------------------------------------- #
def test_pinned_by_tag_sql_shape_zero_embeds(monkeypatch):
    rows = [{"id": 7, "kb": "industry", "content": "c7"},
            {"id": 8, "kb": "industry", "content": "c8"}]
    s, conn = _store_with_fake_pool(monkeypatch, rows)
    out = _run(s.pinned_by_tag("Plumbing"))
    assert out == [
        {"id": 7, "kb": "industry", "content": "c7", "score": None,
         "vertical": "Plumbing"},
        {"id": 8, "kb": "industry", "content": "c8", "score": None,
         "vertical": "Plumbing"}]
    sel = [sql for sql in conn.sqls if "SELECT id, kb, content" in sql]
    assert sel and "vertical=$1" in sel[0] and "ORDER BY id" in sel[0]
    assert not any("<=>" in sql for sql in conn.sqls)
    assert not any("embedding" in sql for sql in sel)
    assert _run(s.pinned_by_tag("")) == []             # empty tag: no query
    assert _run(s.pinned_by_tag(None)) == []


def test_pinned_by_tag_failure_returns_empty(monkeypatch):
    s, _conn = _store_with_fake_pool(monkeypatch, fail=True)
    assert _run(s.pinned_by_tag("Plumbing")) == []      # missing column


def test_vertical_of(monkeypatch):
    s, conn = _store_with_fake_pool(monkeypatch, vertical="Plumbing")
    assert _run(s.vertical_of(5)) == "Plumbing"
    assert any("SELECT vertical FROM" in sql for sql in conn.sqls)
    assert not any("<=>" in sql for sql in conn.sqls)
    assert _run(s.vertical_of(None)) is None


def test_vertical_of_failure_returns_none(monkeypatch):
    s, _conn = _store_with_fake_pool(monkeypatch, fail=True)
    assert _run(s.vertical_of(5)) is None


# --------------------------------------------------------------------------- #
# 4. resolver ladder
# --------------------------------------------------------------------------- #
def _resolver_store(retrieve_rows=None, verticals=None):
    return _FakeKBStore(retrieve_rows=retrieve_rows, verticals=verticals)


def test_resolver_alias_hits_incl_noisy_and_plumbing():
    rt = _rt()
    store = _resolver_store()
    assert _run(rt._resolve_industry_tag("dental practice", store)) \
        == "Dental Practices"
    assert _run(rt._resolve_industry_tag(
        "personal injury law firm in New York, NY, Bell & Associates Legal",
        store)) == "Law Firms (Personal Injury, Family, Immigration)"
    assert _run(rt._resolve_industry_tag("Plumbing/plumbing", store)) \
        == "Plumbing"
    assert _run(rt._resolve_industry_tag("emergency plumber", store)) \
        == "Plumbing"
    assert _run(rt._resolve_industry_tag("roofing contractor", store)) \
        == "Residential Roofing & Solar Installers"
    assert _run(rt._resolve_industry_tag("home remodeling and contractors",
                                         store)) \
        == "Home Remodeling & Small General Contractors"


def test_resolver_ambiguous_goes_to_embed_fallback_never_guess():
    rt = _rt()
    store = _resolver_store(
        retrieve_rows=[_chunk(9, "industry", "hvac chunk", 0.31)],
        verticals={9: "HVAC & Home Services"})
    assert _run(rt._resolve_industry_tag("dental practice and law firm",
                                         store)) == "HVAC & Home Services"
    assert store.retrieve_calls == [("dental practice and law firm",
                                     ["industry"])]


def test_resolver_stoplist_never_resolves():
    rt = _rt()
    store = _resolver_store(retrieve_rows=[_chunk(1, "industry", "x", 0.9)],
                            verticals={1: "Dental Practices"})
    for v in ("", "unknown", "n/a", "none", "not sure", "unsure", "tbd",
              "various", "multiple", "header"):
        assert _run(rt._resolve_industry_tag(v, store)) is None, v
        assert _run(rt._resolve_industry_tag(f"  {v}  ", store)) is None
    assert not store.retrieve_calls                     # no embed wasted


def test_resolver_embed_below_filter_is_none():
    rt = _rt()
    store = _resolver_store(
        retrieve_rows=[_chunk(1, "industry", "x", 0.20)],   # < 0.24
        verticals={1: "Dental Practices"})
    assert _run(rt._resolve_industry_tag("hardware store", store)) is None


def test_resolver_embed_header_top1_skipped():
    rt = _rt()
    store = _resolver_store(
        retrieve_rows=[_chunk(46, "industry", "header chunk", 0.6),
                       _chunk(2, "industry", "real vertical", 0.55)],
        verticals={46: "header", 2: "Dental Practices"})
    assert _run(rt._resolve_industry_tag("medispa downtown", store)) \
        == "Dental Practices"
    # only-header results -> None
    store2 = _resolver_store(
        retrieve_rows=[_chunk(46, "industry", "header chunk", 0.6)],
        verticals={46: "header"})
    assert _run(rt._resolve_industry_tag("unknown shop", store2)) is None


def test_resolver_embed_only_passes_industry_scope():
    rt = _rt()
    store = _resolver_store()
    _run(rt._resolve_industry_tag("hardware store", store))
    assert store.retrieve_calls == [("hardware store", ["industry"])]


# --------------------------------------------------------------------------- #
# 5. alias integrity vs the LIVE DB (skipif no DB)
# --------------------------------------------------------------------------- #
def test_alias_targets_exist_in_live_verticals():
    dsn = _t4_settings().database_url
    if not dsn:
        pytest.skip("no DATABASE_URL")
    import asyncpg

    async def _tags():
        try:
            c = await asyncpg.connect(dsn)
        except Exception:
            return None
        try:
            rows = await c.fetch(
                "SELECT DISTINCT vertical FROM kb_chunks_v2 WHERE "
                "kb='industry' AND vertical IS NOT NULL")
            return {r["vertical"] for r in rows}
        finally:
            await c.close()
    tags = _run(_tags())
    if tags is None:
        pytest.skip("diallux-db unreachable")
    assert "Plumbing" in tags
    for _alias, tag in buildermod._PIN_ALIASES:
        assert tag in tags, f"alias target {tag!r} missing from live verticals"


# --------------------------------------------------------------------------- #
# 6. build_lanes: pinned scope + None-parity + plumb trigger
# --------------------------------------------------------------------------- #
def test_build_lanes_pinned_drops_industry_query_text_unchanged():
    rt = _rt()
    dvs = {"industry": "plumbing company"}
    lanes0, owned0 = rt.build_lanes("Intake", dvs, "my phone never stops")
    assert "industry" in lanes0[-1]["scope"]
    q0 = [l["query"] for l in lanes0]
    lanes1, _o1 = rt.build_lanes("Intake", dvs, "my phone never stops",
                                 pinned_tag="Plumbing")
    assert "industry" not in lanes1[-1]["scope"]
    assert [l["query"] for l in lanes1] == q0           # query text UNCHANGED
    lanes2, owned2 = rt.build_lanes("Intake", dvs, "my phone never stops",
                                    pinned_tag=None)
    assert lanes2 == lanes0 and owned2 == owned0        # None-parity


def test_build_lanes_plumb_trigger_adds_deep_dive_scope():
    rt = _rt()
    lanes, _o = rt.build_lanes("Intake", {"industry": "Plumbing"},
                               "we book jobs off voicemail")
    assert "plumbing" in lanes[-1]["scope"]
    assert "industry" in lanes[-1]["scope"]             # deep dive, not the pin
    lanes2, _o2 = rt.build_lanes("Intake", {"industry": "dental practice"},
                                 "we book jobs off voicemail")
    assert "plumbing" not in lanes2[-1]["scope"]


def test_build_lanes_pinned_plus_trigger_compose():
    rt = _rt()
    lanes, _o = rt.build_lanes("Intake", {"industry": "plumbing company"},
                               "the phones ring all day", pinned_tag="Plumbing")
    assert "industry" not in lanes[-1]["scope"]         # pinned KB out
    assert "plumbing" in lanes[-1]["scope"]             # deep-dive KB in


def test_build_lanes_kill_switch_disables_trigger():
    rt = _rt(_t4_settings(rag_pin_industry=False))
    lanes, _o = rt.build_lanes("Intake", {"industry": "Plumbing"},
                               "we book jobs off voicemail")
    assert "plumbing" not in lanes[-1]["scope"]
    assert "industry" in lanes[-1]["scope"]


# --------------------------------------------------------------------------- #
# 7. pin-once / dv-change / per-call reset
# --------------------------------------------------------------------------- #
_PIN_ROW = {"id": 1, "kb": "industry", "content": "c", "score": None,
            "vertical": "Plumbing"}


def test_pin_fetched_once_per_dv_reresolves_on_change_resets_per_call():
    rt = _rt()
    store = _FakeKBStore(pinned=[dict(_PIN_ROW)])
    _run(rt._ensure_pinned_industry({"industry": "plumbing"}, store))
    assert rt._pinned_industry["tag"] == "Plumbing"
    assert rt._pinned_industry["chunks"] == [dict(_PIN_ROW)]
    assert store.pin_calls == ["Plumbing"]
    _run(rt._ensure_pinned_industry({"industry": "plumbing"}, store))
    assert store.pin_calls == ["Plumbing"]              # ONCE per dv value
    # dv change -> re-resolve
    store2 = _FakeKBStore(pinned=[{"id": 2, "kb": "industry", "content": "d",
                                   "score": None,
                                   "vertical": "Dental Practices"}])
    rt2 = _rt()
    _run(rt2._ensure_pinned_industry({"industry": "dental office"}, store2))
    _run(rt2._ensure_pinned_industry({"industry": "dental clinic"}, store2))
    assert store2.pin_calls == ["Dental Practices", "Dental Practices"]
    # per-call reset
    rt2.initial_state("c52-reset")
    assert rt2._pinned_industry == {}


def test_pin_resolved_miss_is_cached_no_reembed_storm():
    rt = _rt()
    store = _FakeKBStore()                              # no rows -> miss
    _run(rt._ensure_pinned_industry({"industry": "hardware store"}, store))
    assert rt._pinned_industry["tag"] == ""
    n = len(store.retrieve_calls)
    _run(rt._ensure_pinned_industry({"industry": "hardware store"}, store))
    assert len(store.retrieve_calls) == n               # cached: no re-embed
    assert rt._pinned_industry["chunks"] == []


def test_pin_resolve_hit_fetch_empty_degrades():
    rt = _rt()
    store = _FakeKBStore(pinned=[])                     # tag resolves, fetch []
    _run(rt._ensure_pinned_industry({"industry": "plumbing"}, store))
    pin = rt._pinned_industry
    assert pin["tag"] == "Plumbing" and pin["chunks"] == []
    delta, _kbs, _ms, _merged, _qs = _run(
        rt._live_retrieve("Intake", {"industry": "plumbing"}, "the phones",
                          store))
    assert "## INDUSTRY CONTEXT" not in delta           # no pin block


def test_pin_kill_switch_noop():
    rt = _rt(_t4_settings(rag_pin_industry=False))
    store = _FakeKBStore(pinned=[dict(_PIN_ROW)])
    _run(rt._ensure_pinned_industry({"industry": "plumbing"}, store))
    assert rt._pinned_industry == {}
    assert store.pin_calls == []


# --------------------------------------------------------------------------- #
# 8. delta composition (pin FIRST) + budget + eviction resistance
# --------------------------------------------------------------------------- #
_PIN_CHUNKS = [
    {"id": 1, "kb": "industry", "content": "PAIN chunk", "score": None,
     "vertical": "Plumbing"},
    {"id": 2, "kb": "industry", "content": "VOICE chunk", "score": None,
     "vertical": "Plumbing"},
]


def _live_rt_with_pin(store, settings=None):
    rt = _rt(settings or _t4_settings(), kb_store=store)
    rt._pinned_industry = {"dv": "plumbing", "tag": "Plumbing",
                           "chunks": [dict(c) for c in _PIN_CHUNKS]}
    return rt


def test_delta_composition_pin_first_and_kbs_gain_industry():
    store = _FakeKBStore()
    rt = _live_rt_with_pin(store)
    delta, kbs, _ms, _merged, _qs = _run(
        rt._live_retrieve("Discovery", {"industry": "plumbing"}, "q", store))
    assert "## INDUSTRY CONTEXT (Plumbing — pinned for this call)" in delta
    assert delta.index("## INDUSTRY CONTEXT") < delta.index("## KNOWLEDGE")
    assert "PAIN chunk" in delta and "VOICE chunk" in delta
    assert "industry" in kbs


def test_delta_pin_present_when_merged_empty():
    store = _FakeKBStore()                              # lanes -> all empty
    rt = _live_rt_with_pin(store)
    delta, _kbs, _ms, merged, _qs = _run(
        rt._live_retrieve("Discovery", {"industry": "plumbing"}, "q", store))
    assert merged == []
    assert "## INDUSTRY CONTEXT" in delta               # pin NEVER evicted
    assert "(No knowledge-base excerpt matched this turn" in delta


def test_delta_pin_present_when_dynamic_budget_consumed():
    big = _chunk(3, "pain-points", "b" * 1600, 0.9)

    class BudgetStore(_FakeKBStore):
        async def retrieve_lanes(self, lanes):
            return [[dict(big)] for _ in lanes]
    store = BudgetStore()
    rt = _live_rt_with_pin(store)
    delta, _kbs, _ms, merged, _qs = _run(
        rt._live_retrieve("Discovery", {"industry": "plumbing"}, "q", store))
    assert merged == [big]
    assert "## INDUSTRY CONTEXT" in delta


def test_pin_whole_chunk_budget_never_truncates():
    chunks = [{"id": 1, "kb": "industry", "content": "a" * 1000,
               "score": None, "vertical": "X"},
              {"id": 2, "kb": "industry", "content": "b" * 1000,
               "score": None, "vertical": "X"},
              {"id": 3, "kb": "industry", "content": "c" * 200,
               "score": None, "vertical": "X"}]
    out = ragmod.render_pinned_section(chunks, "X", cap=1800)
    assert "INDUSTRY CONTEXT (X" in out
    assert "b" * 1000 not in out                        # whole-chunk DROP
    assert "a" * 1000 in out and "c" * 200 in out       # later small one fits
    assert len(ragmod.render_pinned_section(chunks, "X", cap=9999)) > 0
    assert ragmod.render_pinned_section([], "X") == ""
    assert ragmod.render_pinned_section(chunks, "X", cap=50) == ""  # all dropped


def test_build_messages_appends_delta_with_long_history():
    rt = _rt()
    history = [{"role": "user" if i % 2 else "assistant",
                "content": f"turn {i}"} for i in range(40)]
    delta = ("\n\n## INDUSTRY CONTEXT (Plumbing — pinned for this call)\n\n"
             "### from industry-kb\nchunk")
    msgs = rt._build_messages("Discovery", history, "tail", "system",
                              delta=delta)
    assert msgs[-1]["content"] == delta                 # appended, unsliced
    assert sum(1 for m in msgs if m["role"] in ("user", "assistant")) >= 8


# --------------------------------------------------------------------------- #
# 9. prefix byte-stability with pin on/off
# --------------------------------------------------------------------------- #
def test_prefix_bytes_stable_pin_on_off():
    rt_on = _rt()
    rt_off = _rt(_t4_settings(rag_pin_industry=False))
    history = [{"role": "user", "content": "my phones ring all day"}]
    delta_pin = ("\n\n## INDUSTRY CONTEXT (Plumbing — pinned for this call)\n"
                 "\n### from industry-kb\nPAIN chunk\n\n## KNOWLEDGE\nx")
    delta_plain = "\n\n## KNOWLEDGE\nx"
    sys_on = rt_on._build_messages("Discovery", history, "tail", "system",
                                   delta=delta_pin)
    sys_off = rt_off._build_messages("Discovery", history, "tail", "system",
                                     delta=delta_plain)
    pre_on = [m["content"] for m in sys_on if m["content"] != delta_pin]
    pre_off = [m["content"] for m in sys_off if m["content"] != delta_plain]
    assert pre_on == pre_off                            # prefix byte-IDENTICAL
    assert sys_on[-1]["content"] == delta_pin
    assert sys_off[-1]["content"] == delta_plain


def test_settings_defaults_and_overrides():
    # env-independent: field defaults (the kill-switch suite run flips the
    # ambient env on purpose — this pin must NOT follow it)
    assert Settings.model_fields["rag_pin_industry"].default is True
    assert Settings.model_fields["rag_pin_char_budget"].default == 1800
    s2 = Settings(openai_api_key="test", retell_api_key="test",
                  langfuse_enabled=False, rag_pin_industry=False,
                  rag_pin_char_budget=999)
    assert s2.rag_pin_industry is False and s2.rag_pin_char_budget == 999
