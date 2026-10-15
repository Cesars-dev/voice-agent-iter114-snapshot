"""iter25 tag-framework tests — hermetic (no Postgres, no OpenAI).

Covers: the #[refer ...] tag regex against the 5 real tag placements, the
per-state retrieval gate whitelist, and the dv-value-anchored query build.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.rag import parse_refer_tags
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client

SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, first_turn_lite=False)
LLM_JSON = json.loads((Path(__file__).resolve().parents[1] / "agent" / "llm.json").read_text())
PROMPTS = Path(__file__).resolve().parents[1] / "diallux" / "prompts"


def _prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# tag regex — hits all 5 tag shapes
# --------------------------------------------------------------------------- #
def test_refer_tag_regex_hits_all_five_placements():
    closer = parse_refer_tags(_prompt("Closer"))
    offer = parse_refer_tags(_prompt("Offer"))
    discovery = parse_refer_tags(_prompt("Discovery"))
    intake = parse_refer_tags(_prompt("Intake"))

    assert len(closer) == 1
    assert closer[0]["kb"] == "sales-psychology"
    assert closer[0]["what"] == "loss playback"
    assert closer[0]["dvs"] == ["weekly_leak", "monthly_leak"]

    assert len(offer) == 1
    assert offer[0]["kb"] == "sales-psychology"
    assert offer[0]["what"] == "re-anchoring"
    assert offer[0]["dvs"] == ["pain_frame", "monthly_leak"]

    assert len(discovery) == 2
    assert discovery[0]["kb"] == "pain-points"
    assert discovery[0]["dvs"] == ["pain_points"]
    assert discovery[1]["kb"] == "sales-language"
    assert discovery[1]["what"] == "deferral escalation"
    assert discovery[1]["dvs"] == ["pain_frame"]

    # iter89 Edit A: Intake carries THREE refer tags now (I-4/I-5)
    assert len(intake) == 3
    assert intake[0]["kb"] == "pain-points"
    assert intake[0]["what"] == "surface pain recognition and opening discovery questions"
    assert intake[0]["dvs"] == ["inbound_channel"]
    assert intake[1]["what"] == "probing questions below the surface symptom"
    assert intake[1]["dvs"] == ["interest_topic"]
    assert intake[2]["what"] == "emotional anchoring and empathic response"
    assert intake[2]["dvs"] == ["pain_frame"]


def test_refer_tag_regex_ignores_prose_without_tags():
    assert parse_refer_tags("no tags here, just ##industry-kb## markers") == []
    assert parse_refer_tags(_prompt("Booking")) == []
    assert parse_refer_tags(_prompt("Closing")) == []


# --------------------------------------------------------------------------- #
# retrieval gate whitelist
# --------------------------------------------------------------------------- #
def test_gate_returns_empty_for_mechanical_states():
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=FakeLLM(),
                     http_client=mock_client(), kb_store=None)
    # iter59 T4 (KB-everywhere): the four mechanical states are FREED — they
    # retrieve via lane B over the 9-KB general union (Retell tier: KB on
    # every response). The gate attribute stays as an EMPTY set (the revert
    # is a one-line restore of the old names).
    assert rt._RETRIEVAL_OFF == set()
    from diallux.rag import kb_slugs_in
    from pathlib import Path as _P
    general = _P(rt.settings.prompts_dir) / "general_prompt.md"
    expected = kb_slugs_in(general.read_text(encoding="utf-8"))
    for state in ("Booking", "VerifyLead", "ConfirmSlots", "contact_details"):
        got = rt.kb_slugs_for(state)
        assert got, state
        assert got == expected, (state, got)


def test_gate_closing_allowlists_call_closing_only():
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=FakeLLM(),
                     http_client=mock_client(), kb_store=None)
    # iter49c T1 re-pin: _STATE_KBS["Closing"] = [call-closing,
    # sales-language] — the hand-written Closing anchor owns BOTH KBs
    # (resolved decision: "warm goodbye wrap-up next steps"
    # [call-closing, sales-language]).
    assert rt.kb_slugs_for("Closing") == ["call-closing", "sales-language"]


def test_gate_sales_states_keep_general_plus_state_scope():
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=FakeLLM(),
                     http_client=mock_client(), kb_store=None)
    scope = rt.kb_slugs_for("Discovery")
    assert "discovery-bridge" in scope          # state marker
    assert "pain-points" in scope               # general + tag
    assert "industry" in scope                  # state marker


# --------------------------------------------------------------------------- #
# dv-anchored query build
# --------------------------------------------------------------------------- #
def test_query_contains_dv_value_not_token():
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=FakeLLM(),
                     http_client=mock_client(), kb_store=None)
    dvs = {"weekly_leak": "$1,400", "monthly_leak": "$5,600", "pain_frame": "missing calls"}
    query, values = rt.refer_query("Closer", dvs, "yeah that sounds about right")
    # iter48 R1: no [state: X] scaffold — whats + dv values + caller text only
    assert "[state:" not in query
    assert "loss playback" in query
    assert "$1,400" in query and "$5,600" in query
    assert "{{weekly_leak}}" not in query and "{{monthly_leak}}" not in query
    assert values == ["$1,400", "$5,600"]
    assert query.endswith("yeah that sounds about right")


def test_query_falls_back_to_baseline_without_tags_or_values():
    rt = CallRuntime(SETTINGS, LLM_JSON, tracer=None, llm=FakeLLM(),
                     http_client=mock_client(), kb_store=None)
    # Booking has no tags -> verbatim caller text (iter48: no state tag)
    q, values = rt.refer_query("Booking", {"pain_frame": "x"}, "hello")
    assert q == "hello" and values == []
    # tags present but dvs empty -> whats still anchor the query, no token leaks
    q, values = rt.refer_query("Intake", {"pain_frame": ""}, "my receptionist quit")
    assert "emotional anchoring and empathic response" in q
    assert "{{pain_frame}}" not in q and values == []


def test_query_integrated_into_rag_turn():
    class SpyKB:
        def __init__(self):
            self.queries: list[tuple[str, list[str]]] = []

        async def retrieve(self, query_text, kb_slugs):
            self.queries.append((query_text, list(kb_slugs)))
            return []

    # iter49c T1: this pins the iter48 single-query path — run it with the
    # live multi-lane mode OFF (the revert switch; SpyKB has no
    # retrieve_lanes).
    settings = SETTINGS.model_copy(update={"rag_live_retrieve": False})
    kb = SpyKB()
    rt = CallRuntime(settings, LLM_JSON, tracer=None,
                     llm=FakeLLM([{"tokens": ["Hi", "."], "match_system": "worries them"}]),
                     http_client=mock_client(), kb_store=kb)

    import asyncio

    async def go():
        config = {"configurable": {"thread_id": "iter25-b-1"}}
        async for _, _d in rt.graph.astream(
                {"user_text": "my receptionist just quit",
                 **rt.initial_state("iter25-b-1",
                                    {"pain_frame": "receptionist turnover"})},
                config=config, stream_mode=["updates"]):
            pass

    asyncio.run(go())
    assert kb.queries, "expected a rag turn"
    query, scope = kb.queries[0]
    assert "[state:" not in query                    # iter48 R1: no scaffold
    assert "emotional anchoring and empathic response" in query
    assert "receptionist turnover" in query          # dv VALUE anchored
    assert "pain-points" in scope                    # tag slug in scope
