"""iter64 pins: the pre-pin `industry` scope gate.

The shared `industry` KB must leave ALL live lane scopes until the vertical
is KNOWN — the pin resolves OR the industry dv is extracted. Pre-pin generic
turns cross-matched every vertical's pain/ROI prose (iter63 battery: Susan
t3/t4 plumbing chunks to a remodeler, Marcus t5 dental chunk to a law firm,
Maria t4 derm/cleaning/security to a dentist, Danny t6 plumbing/MSP).

Pins:
1. test_industry_absent_pre_pin  — gate ON pre-pin (scope + served set),
   kill switch OFF = byte-exact iter49.
2. test_industry_returns_on_pin  — pinned rounds keep the existing drop +
   pin-block re-entry; dv-present-but-unresolved rounds keep `industry`
   (iter63 behavior preserved).
"""
import asyncio

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client
from tests.test_iter49_rag_parity import _LLM_JSON, _chunk

_ROOT = __file__.rsplit("/", 3)[0] if "/" in __file__ else "."


def _s64(**kw) -> Settings:
    # pin rag_pin_industry explicitly: the ambient RAG_PIN_INDUSTRY env must
    # not flip these pins (kill-switch case passes False explicitly).
    kw.setdefault("rag_pin_industry", True)
    return Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, first_turn_lite=False, **kw)


def _rt(settings=None, kb_store=None):
    return CallRuntime(settings or _s64(), _LLM_JSON, tracer=None,
                       llm=FakeLLM(), http_client=mock_client(),
                       kb_store=kb_store)


def _run(coro):
    return asyncio.run(coro)


_UTT = "how much does this cost exactly"          # >= rag_min_query_chars
_PIN_UTT = "we miss calls constantly"             # >= rag_min_query_chars


class _GateStore:
    """Serves scripted chunks FILTERED by each lane's scope — proves the
    served set, not just the scope (LanesStore is scope-agnostic)."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.lane_calls: list = []

    async def retrieve_lanes(self, lanes):
        self.lane_calls.append(lanes)
        out = []
        for lane in lanes:
            scope = set(lane.get("scope") or [])
            out.append([c for c in self.chunks if c["kb"] in scope])
        return out

    async def retrieve(self, q, slugs, vec=None):
        return []


class _PinStore:
    """_FakeKBStore shape (test_iter52): resolver + pin + lane recording."""

    def __init__(self, pinned=None, retrieve_rows=None):
        self.pinned_rows = list(pinned or [])
        self.retrieve_rows = list(retrieve_rows or [])
        self.retrieve_calls = []
        self.pin_calls: list[str] = []
        self.lane_calls: list = []

    async def retrieve(self, query, slugs, vec=None):
        self.retrieve_calls.append((query, list(slugs)))
        return list(self.retrieve_rows)

    async def vertical_of(self, chunk_id):
        return None

    async def pinned_by_tag(self, tag):
        self.pin_calls.append(tag)
        return list(self.pinned_rows) if tag else []

    async def retrieve_lanes(self, lanes):
        self.lane_calls.append(lanes)
        return [[] for _ in lanes]


# --------------------------------------------------------------------------- #
# 1. gate ON pre-pin; kill switch OFF = byte-exact iter49
# --------------------------------------------------------------------------- #
def test_industry_absent_pre_pin():
    # (a) build_lanes: laneB scope excludes industry pre-pin
    rt = _rt()
    lanes, _owned = rt.build_lanes("Discovery", {}, _UTT)
    assert lanes, "lane B must build for a >=12-char utterance"
    lb = lanes[-1]
    assert lb["query"] == _UTT                       # no industry anchor
    assert "industry" not in lb["scope"]
    assert lb["scope"] == \
        [s for s in rt.kb_slugs_for("Discovery") if s != "industry"]

    # (b) _retrieve_raw laneB: recorded scope excludes industry AND the
    # served set carries zero industry chunks (scope-filtering store)
    store = _GateStore([_chunk(1, "industry", "dental pain prose", 0.5),
                        _chunk(2, "sales-language", "generic ammo", 0.4)])
    rt2 = _rt(kb_store=store)

    async def go():
        return await rt2._retrieve_raw("Discovery", {}, _UTT, store,
                                       lane="laneB")

    lane_results, owned, ms, lane_qs, pinned_tag, pin = _run(go())
    assert pinned_tag == "" and not pin
    rec = store.lane_calls[0][0]
    assert "industry" not in rec["scope"]
    assert rec["scope"] == \
        [s for s in rt2.kb_slugs_for("Discovery") if s != "industry"]
    served = lane_results[0]
    assert [c["kb"] for c in served] == ["sales-language"]   # industry dropped
    assert owned == [set()]

    # (c) kill switch OFF -> byte-exact iter49 (industry STAYS in scope)
    rt_ks = _rt(_s64(rag_pin_industry=False))
    lanes_ks, _o = rt_ks.build_lanes("Discovery", {}, _UTT)
    assert "industry" in lanes_ks[-1]["scope"]


# --------------------------------------------------------------------------- #
# 2. pinned / dv-known rounds: iter63 behavior preserved
# --------------------------------------------------------------------------- #
_PIN_CHUNKS = [{"id": 91, "kb": "industry", "content": "dental vertical block",
                "score": 0.8},
               {"id": 92, "kb": "industry", "content": "dental ROI block",
                "score": 0.7}]


def test_industry_returns_on_pin():
    # (a) pinned: alias "dental" resolves, pin block owns the vertical,
    # laneB scope drops industry (existing pinned drop), pin fetched
    store = _PinStore(pinned=_PIN_CHUNKS)
    rt = _rt(kb_store=store)

    async def go():
        return await rt._retrieve_raw("Discovery",
                                      {"industry": "dental practice"},
                                      _PIN_UTT, store, lane="laneB")

    lane_results, owned, ms, lane_qs, pinned_tag, pin = _run(go())
    assert pinned_tag == "Dental Practices"
    assert rt._pinned_industry["chunks"] == _PIN_CHUNKS
    assert store.pin_calls == ["Dental Practices"]
    rec = store.lane_calls[0][0]
    assert "industry" not in rec["scope"]            # pinned drop (existing)
    assert owned == [set()]

    # (c) render re-entry: the vertical never disappears from the surface
    from diallux.rag import merge_lane_chunks
    merged = merge_lane_chunks(lane_results, owned, top_k=3, char_budget=1600)
    delta, kbs = rt._render_from_raw(lane_results, owned, merged, ms,
                                     lane_qs, pinned_tag, pin)
    assert "industry" in kbs and "dental vertical block" in delta

    # (b) dv present, pin UNRESOLVED: industry STAYS in scope (iter63)
    store_b = _PinStore(retrieve_rows=[])            # resolver -> None
    rt_b = _rt(kb_store=store_b)

    async def go_b():
        return await rt_b._retrieve_raw("Discovery",
                                        {"industry": "car dealership"},
                                        _PIN_UTT, store_b, lane="laneB")

    _lr, _o, _ms, _lq, pinned_tag_b, pin_b = _run(go_b())
    assert pinned_tag_b == "" and not pin_b.get("chunks")
    rec_b = store_b.lane_calls[0][0]
    assert "industry" in rec_b["scope"]              # gate inactive: dv known
