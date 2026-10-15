"""iter48 Commit A tests — RAG query truth (master plan M6, pins 1-7).

Commit B pins (M14, appended): payload coverage —
8. EOT-boundary entry await uses prewarm_entry_wait_eot_ms (500)
9. mid-turn entry await keeps prewarm_entry_wait_ms (100)
10. lite round awaits its `lite:<state>` warm (EOT cap)
11. _await_warm(None) back-compat + shield (timeout keeps warm alive)
12. lite + full warms coexist in _latest_warm (parity untouched)

Pins (research/surgeon/iter48-rag-truth/04_master_plan.md M6):
1. prefetch query contains refer whats + dv values, NO [state: scaffold
2. no-tag query == caller text verbatim
3. empty-anchor staging -> None (never embed "")
4. empty-freeze rescued in one round (forced drift, baseline adopted)
5. filler freeze resets stale base fields
6. sync drift lane forced-drift parity
7. drift cosine moves on intent change (cleaned vectors)
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client

SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, first_turn_lite=False)
LLM_JSON = json.loads((Path(__file__).resolve().parents[1] / "agent" / "llm.json").read_text())


def _rt(fake=None, kb_store=None, **kw) -> CallRuntime:
    return CallRuntime(SETTINGS, LLM_JSON, tracer=None,
                       llm=fake or FakeLLM(), http_client=mock_client(),
                       kb_store=kb_store, **kw)


class VecStore:
    """embed_query keyed by substring; retrieve records queries, returns [].
    vec_map: substring -> vec (first match wins); default otherwise."""

    def __init__(self, vec_map: dict[str, list[float]] | None = None,
                 default: list[float] | None = None,
                 chunks: list[dict] | None = None):
        self.vec_map = vec_map or {}
        self.default = default or [1.0, 0.0, 0.0]
        self.chunks = chunks or [{"kb": "pain-points", "content": "mirroring line",
                                  "score": 0.9}]
        self.embeds: list[str] = []
        self.retrieves: list[tuple[str, list[str], list | None]] = []

    async def embed_query(self, text: str) -> list[float]:
        self.embeds.append(text)
        for k, v in self.vec_map.items():
            if k in text:
                return v
        return self.default

    async def retrieve(self, query_text, kb_slugs, vec=None):
        self.retrieves.append((query_text, list(kb_slugs), vec))
        return list(self.chunks)


# --------------------------------------------------------------------------- #
# Pin 1: prefetch (empty user_msg) query = whats + dv values, no scaffold
# --------------------------------------------------------------------------- #
def test_prefetch_query_anchors_on_whats_and_dv_values():
    rt = _rt()
    dvs = {"pain_frame": "receptionist turnover"}
    query, values = rt.refer_query("Intake", dvs, "")
    assert "emotional anchoring and empathic response" in query  # refer what KEPT (R1)
    assert "receptionist turnover" in query        # dv value anchor
    assert "[state:" not in query                  # scaffold GONE
    assert values == ["receptionist turnover"]


def test_prefetch_query_no_tag_no_msg_is_empty():
    rt = _rt()
    query, values = rt.refer_query("Booking", {}, "")
    assert query == "" and values == []


# --------------------------------------------------------------------------- #
# Pin 2: no-tag query == caller text verbatim
# --------------------------------------------------------------------------- #
def test_no_tag_query_is_caller_text_verbatim():
    rt = _rt()
    query, values = rt.refer_query("Booking", {"pain_frame": "x"},
                                   "what do you guys do")
    assert query == "what do you guys do"
    assert values == []


# --------------------------------------------------------------------------- #
# Pin 3: empty-anchor staging -> None
# --------------------------------------------------------------------------- #
def test_stage_rag_empty_anchor_stages_nothing():
    store = VecStore()
    rt = _rt(kb_store=store)
    staged = asyncio.run(rt._stage_rag("Booking", {}, user_msg=""))
    assert staged is None
    assert store.embeds == []                      # never embedded ""
    assert rt._rag_staging.get("Booking") is None


# --------------------------------------------------------------------------- #
# Pin 4: empty-freeze rescued in one round (forced drift)
# --------------------------------------------------------------------------- #
def test_drift_check_base_none_forces_retrieve_and_adopts_baseline():
    store = VecStore(vec_map={"trucking": [0.0, 1.0, 0.0]},
                     default=[1.0, 0.0, 0.0])
    rt = _rt(kb_store=store)
    # empty freeze: no baseline text, no baseline vec, stale empty chunks
    rt._frozen_state = "Intake"
    rt._frozen_chunks = []
    rt._frozen_query_text = ""
    rt._frozen_query_vec = None
    rt._rag_delta_chunks = []
    asyncio.run(rt._drift_check("Intake", {}, "i run a trucking company",
                                ["pain-points"], store))
    # forced drift: re-retrieved with the round query, baseline ADOPTED
    assert rt._frozen_query_vec == [0.0, 1.0, 0.0]     # round vec adopted
    assert "trucking" in rt._frozen_query_text
    assert store.retrieves, "forced drift must re-retrieve"
    query, _scope, vec = store.retrieves[0]
    assert query == ("surface pain recognition and opening discovery questions; "
                     "probing questions below the surface symptom; "
                     "emotional anchoring and empathic response i run a trucking company")  # iter89 3-tag Intake
    assert vec == [0.0, 1.0, 0.0]
    assert rt._rag_delta_chunks, "new chunks appended as delta"


def test_drift_check_with_baseline_below_threshold_still_retrieves():
    store = VecStore(vec_map={"trucking": [0.0, 1.0, 0.0]},
                     default=[1.0, 0.0, 0.0])
    rt = _rt(kb_store=store)
    rt._frozen_query_text = "the empathic mirror"
    rt._frozen_query_vec = [1.0, 0.0, 0.0]             # orthogonaI baseline
    rt._frozen_chunks = [{"kb": "pain-points", "content": "old", "score": 0.9}]
    rt._rag_delta_chunks = []
    asyncio.run(rt._drift_check("Intake", {}, "i run a trucking company",
                                ["pain-points"], store))
    assert rt._frozen_query_vec == [0.0, 1.0, 0.0]
    assert store.retrieves


# --------------------------------------------------------------------------- #
# Pin 5: filler freeze resets stale base fields
# --------------------------------------------------------------------------- #
def test_filler_freeze_resets_stale_baseline():
    store = VecStore()
    fake = FakeLLM([{"tokens": ["Got", " it", "."]}])
    # iter49c T1: pins the iter48 filler-freeze path (VecStore has no
    # retrieve_lanes) — live mode OFF.
    rt = CallRuntime(SETTINGS.model_copy(update={"rag_live_retrieve": False}),
                     LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=store)
    rt._frozen_state = "Intake"
    rt._frozen_query_text = "the empathic mirror"      # stale PREVIOUS visit
    rt._frozen_query_vec = [1.0, 0.0, 0.0]
    rt._frozen_chunks = [{"kb": "pain-points", "content": "old", "score": 0.9}]

    async def go():
        config = {"configurable": {"thread_id": "iter48-filler"}}
        payload = {"user_text": "ok",                 # < rag_min_query_chars
                   **rt.initial_state("iter48-filler")}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass

    asyncio.run(go())
    assert rt._frozen_state == "Intake"
    assert rt._frozen_chunks == []                     # filler freeze
    assert rt._frozen_query_text == ""                 # stale base WIPED
    assert rt._frozen_query_vec is None


# --------------------------------------------------------------------------- #
# Pin 6: sync drift lane forced-drift parity
# --------------------------------------------------------------------------- #
def test_sync_drift_base_none_retrieves_instead_of_keeping_frozen():
    store = VecStore(vec_map={"trucking": [0.0, 1.0, 0.0]},
                     default=[1.0, 0.0, 0.0])
    fake = FakeLLM([
        {"tokens": ["Got", " it", "."]},               # turn 1: filler freeze
        {"tokens": ["Let", "'s", " dig", " in", "."]},  # turn 2: rescue round
    ])
    rt = CallRuntime(
        Settings(openai_api_key="test", retell_api_key="test",
                 langfuse_enabled=False, first_turn_lite=False,
                 rag_drift_async=False,           # SYNC lane
                 rag_live_retrieve=False),        # iter49c T1: iter48 path pin
        LLM_JSON, tracer=None, llm=fake,
        http_client=mock_client(), kb_store=store)

    async def go():
        config = {"configurable": {"thread_id": "iter48-sync"}}
        payload = {"user_text": "ok",                  # filler → empty freeze
                   **rt.initial_state("iter48-sync")}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
        payload = {"user_text": "i run a trucking company"}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass

    asyncio.run(go())
    # the ONLY retrieve is the rescue (no placeholder entry retrieve)
    assert store.retrieves, "sync lane must force-drift on base-None"
    for query, _scope, vec in store.retrieves:
        assert "i run a trucking company" in query, "rescued with caller text"
    assert rt._frozen_query_vec == [0.0, 1.0, 0.0], "baseline adopted"
    assert any(vec == [0.0, 1.0, 0.0] for _, _, vec in store.retrieves)
    assert rt._rag_delta_chunks, "rescued chunks appended as delta"


# --------------------------------------------------------------------------- #
# Pin 7: drift cosine moves on intent change (cleaned vectors)
# --------------------------------------------------------------------------- #
def test_cleaned_query_cosine_moves_on_intent_change():
    rt = _rt()
    # SAME intent -> queries share the whats + dv anchor
    q1, _ = rt.refer_query("Offer", {"pain_frame": "missed calls",
                                     "monthly_leak": "$4k"}, "yes but what does it cost")
    q2, _ = rt.refer_query("Offer", {"pain_frame": "missed calls",
                                     "monthly_leak": "$4k"}, "is there a discount")
    # DIFFERENT intent -> caller text diverges but whats anchor persists
    q3, _ = rt.refer_query("Offer", {"pain_frame": "staff turnover",
                                     "monthly_leak": "$9k"}, "how long is setup")
    assert "re-anchoring" in q1 and "re-anchoring" in q2 and "re-anchoring" in q3
    assert q1 != q2 != q3
    # no scaffold anywhere — the cosine compares CONTENT, not labels
    for q in (q1, q2, q3):
        assert "[state:" not in q
    # dv values ride the query (anchored, not tokens)
    assert "$4k" in q1 and "$4k" in q2
    assert "$9k" in q3


# --------------------------------------------------------------------------- #
# Commit B pin 8: EOT-boundary entry await uses the 500 ms cap (M11)
# --------------------------------------------------------------------------- #
class WarmSpyLLM(FakeLLM):
    """FakeLLM whose warm() hangs until released — makes the bounded await
    observable via elapsed time."""

    def __init__(self, rounds=None):
        super().__init__(rounds)
        self.warm_started = asyncio.Event()
        self.warm_release = asyncio.Event()

    async def warm(self, messages, tools):
        self.warm_started.set()
        await self.warm_release.wait()


def _commit_b_rt(fake, **kw) -> CallRuntime:
    kw.setdefault("first_turn_lite", False)
    return CallRuntime(
        Settings(openai_api_key="test", retell_api_key="test",
                 langfuse_enabled=False, **kw),
        LLM_JSON, tracer=None, llm=fake, http_client=mock_client())


def test_eot_and_midturn_entry_awaits_split_caps():
    """M11/M14: the entry await SPLITS — EOT boundary (rounds_left ==
    max_tool_rounds) uses the 500 ms eot cap; mid-turn keeps the 100 ms flag."""
    fake = WarmSpyLLM([{"tokens": ["Hi", "."]}])
    rt = _commit_b_rt(fake, prewarm_entry_wait_ms=100,
                      prewarm_entry_wait_eot_ms=500)

    async def go():
        rt.warm_prompt_cache("Intake", [], force=True)
        await fake.warm_started.wait()
        state = {"rounds_left": rt.settings.max_tool_rounds}  # EOT boundary
        t0 = time.perf_counter()
        await rt._await_warm("Intake",
                             wait_ms=rt.settings.prewarm_entry_wait_eot_ms
                             if state["rounds_left"] == rt.settings.max_tool_rounds
                             else None)
        eot_elapsed = time.perf_counter() - t0
        # mid-turn: _await_warm(None) → mid-turn flag (deterministic decrement)
        t0 = time.perf_counter()
        await rt._await_warm("Intake")
        mid_elapsed = time.perf_counter() - t0
        return eot_elapsed, mid_elapsed

    eot_elapsed, mid_elapsed = asyncio.run(go())
    assert eot_elapsed >= 0.5, "EOT boundary must await the eot cap (500)"
    assert eot_elapsed < 0.9, "eot await must be bounded, not hang"
    assert 0.1 <= mid_elapsed < 0.4, "mid-turn await must use the 100 ms cap"


# --------------------------------------------------------------------------- #
# Commit B pin 9 (M12): lite round awaits its `lite:<state>` warm
# --------------------------------------------------------------------------- #
def test_lite_round_awaits_lite_warm():
    fake = WarmSpyLLM([{"tokens": ["Hello", "!"]}])
    rt = _commit_b_rt(fake, first_turn_lite=True,
                      prewarm_entry_wait_ms=100,
                      prewarm_entry_wait_eot_ms=500)

    async def go():
        greeting_hist = rt.initial_state("iter48b-lite").get("history") or []
        rt.warm_prompt_cache("Intake", greeting_hist, None, lite=True)
        await fake.warm_started.wait()
        assert rt._latest_warm.get("lite:Intake") is not None, \
            "lite warm MUST be registered in _latest_warm (M12)"
        assert rt._latest_warm["lite:Intake"].done() is False
        # turn 1 IS an EOT boundary (rounds_left == max_tool_rounds) → the
        # lite round bounded-awaits its lite warm with the EOT cap.
        state = {"rounds_left": rt.settings.max_tool_rounds}
        t0 = time.perf_counter()
        await rt._await_warm("lite:Intake",
                             wait_ms=rt.settings.prewarm_entry_wait_eot_ms)
        return time.perf_counter() - t0

    elapsed = asyncio.run(go())
    assert elapsed >= 0.5, "lite round must bounded-await its warm (EOT cap)"


# --------------------------------------------------------------------------- #
# Commit B pin 10 (M10): _await_warm(None) back-compat + shield semantics
# --------------------------------------------------------------------------- #
def test_await_warm_none_uses_midturn_flag_and_shield():
    fake = WarmSpyLLM([{"tokens": ["Hi", "."]}])
    rt = _commit_b_rt(fake, prewarm_entry_wait_ms=60,
                      prewarm_entry_wait_eot_ms=500)

    async def go():
        rt.warm_prompt_cache("Intake", [], force=True)
        await fake.warm_started.wait()
        t0 = time.perf_counter()
        await rt._await_warm("Intake", wait_ms=None)   # back-compat path
        bounded = time.perf_counter() - t0
        # shield: the timeout must NOT cancel the warm — release it now and
        # confirm the task completes normally afterwards.
        fake.warm_release.set()
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)
        return bounded, rt._latest_warm["Intake"]

    bounded, task = asyncio.run(go())
    assert 0.06 <= bounded < 0.3, "None → mid-turn flag (60 ms here)"
    assert task.done() and task.exception() is None, \
        "shield kept the warm alive through the timeout"


# --------------------------------------------------------------------------- #
# Commit B pin 11 (M12): lite + full warms coexist in _latest_warm
# --------------------------------------------------------------------------- #
def test_lite_and_full_warms_coexist_in_latest_warm():
    fake = FakeLLM([{"tokens": ["Hi", "."]}])
    rt = _commit_b_rt(fake, first_turn_lite=True)

    async def go():
        rt.warm_prompt_cache("Intake", [], lite=True)
        rt.warm_prompt_cache("Intake", [], force=True)
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)

    asyncio.run(go())
    assert "lite:Intake" in rt._latest_warm
    assert "Intake" in rt._latest_warm
    assert rt._latest_warm["lite:Intake"] is not rt._latest_warm["Intake"]


# --------------------------------------------------------------------------- #
# Commit B pin 12 (M13): full-shape warm failure → WARNING + ONE lite retry
# --------------------------------------------------------------------------- #
def test_full_warm_failure_retries_lite_shape_once():
    calls: list[tuple[list[dict], list[dict]]] = []
    fail_first = {"n": 0}

    class FailFullLLM(FakeLLM):
        async def warm(self, messages, tools):
            calls.append((list(messages), list(tools)))
            if len(calls) == 1:
                # first attempt (full shape) fails; the lite retry must run
                fail_first["n"] += 1
                raise RuntimeError("16-token cap rejected")
            return

    fake = FailFullLLM([{"tokens": ["Hi", "."]}])
    rt = _commit_b_rt(fake)

    async def go():
        rt.warm_prompt_cache("Intake", [{"role": "user", "content": "hello"}],
                             force=True)
        await asyncio.gather(*rt._warm_tasks, return_exceptions=True)

    asyncio.run(go())
    assert fail_first["n"] == 1, "full-shape warm attempted once"
    assert len(calls) == 2, "exactly ONE lite-shape retry after the failure"
    full_msgs, full_tools = calls[0]
    lite_msgs, lite_tools = calls[1]
    assert full_tools, "first attempt = full shape (tools present)"
    assert [t["function"]["name"] for t in lite_tools] == ["memory_note"], \
        "retry = lite shape (the noop tool only)"
    assert lite_msgs[0]["role"] == "system"
    assert lite_msgs[0]["content"] == rt._lite_head()
    # no exception escaped (the retry failure stays silent)
    assert all(t.exception() is None for t in rt._warm_tasks)
