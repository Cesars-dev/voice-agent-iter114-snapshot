"""iter43 — first-turn-lite (iter44: default ON, owner-approved 2026-09-11).

Pins (Settings with first_turn_lite=True):
  a. turn 1 payload = general_prompt + VOICE_OUTPUT_RULES only — the system
     has the identity but NOT the state prompt ("Your Mission"), NO tools
     beyond the LITE_NOOP dummy, NO KNOWLEDGE (RAG) section
  b. turn 2 = state-entry lite (iter49 T6 + FIND-5 port): the state prompt
     is back with the FULL tool array (byte-identical heavy prefix — the
     ack rides the warm; tools=[] is structurally uncacheable on gpt-5.4)
     and a post-history SPEECH-ONLY directive; turn 3 = FULL payload
  c. kill switch: Settings(first_turn_lite=False) keeps the FULL payload on
     turn 1 (legacy rollback; was the pre-iter44 default)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())


def _settings(lite: bool) -> Settings:
    return Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, first_turn_lite=lite)


async def _turns(rt: CallRuntime, call_id: str, texts: list[str]) -> dict:
    config = {"configurable": {"thread_id": call_id}}
    payload = {"user_text": texts[0], **rt.initial_state(call_id)}
    for i, text in enumerate(texts):
        if i > 0:
            payload = {"user_text": text}
        async for _m, _d in rt.graph.astream(payload, config=config,
                                             stream_mode=["updates"]):
            pass
    return (await rt.graph.aget_state(config)).values


def test_turn1_lite_payload_no_tools_no_state_prompt():
    """Turn 1 with first_turn_lite=True: identity in the head, no state prompt
    block, NO tool channel beyond the LITE_NOOP dummy, no KNOWLEDGE section.
    iter48b: gpt-5.4 only caches tool-bearing requests (FIND-5) — turn 1
    carries exactly ONE do-nothing tool (memory_note)."""
    fake = FakeLLM([{"tokens": ["Hi", " there", "."]},
                    {"tokens": ["Tell", " me", " more", "."]}])
    rt = CallRuntime(_settings(True), LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client())
    state = asyncio.run(_turns(rt, "ft-1", ["hello?"]))
    # lite head: general_prompt identity present, state prompt absent
    assert "Identity" in fake.seen_systems[0]
    assert "Your Mission" not in fake.seen_systems[0]      # state prompt dropped
    assert fake.seen_tools[0] == ["memory_note"]           # ONE do-nothing tool (FIND-5)
    # iter89: general prose references "under ## KNOWLEDGE" - pin the RENDERED
    # section signatures, not the bare substring
    assert "## KNOWLEDGE (most relevant excerpts" not in fake.seen_systems[0]
    assert "(No knowledge-base excerpt matched" not in fake.seen_systems[0]
    assert "Intake" not in fake.seen_systems[0] or "Your Mission" not in fake.seen_systems[0]
    assert state.get("ended") is not True


def test_turn2_state_entry_lite_turn3_full_payload():
    """iter49 T6 + FIND-5 port: turn 2 is the STATE-ENTRY lite into the
    initial state — the state prompt is back with the FULL tool array
    (tools=[] is structurally uncacheable on gpt-5.4, so the ack keeps the
    heavy pre-history prefix byte-identical and rides the warm) and a
    post-history SPEECH-ONLY directive. Turn 3 is the FULL payload (lanes
    reopen; the prewarm covers this round's prefix)."""
    fake = FakeLLM([{"tokens": ["Hi", " there", "."]},
                    {"tokens": ["Got", " it", "."]},
                    {"tokens": ["Sure", " thing", "."]}])
    rt = CallRuntime(_settings(True), LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client())
    asyncio.run(_turns(rt, "ft-2", ["hello", "my name is Kim",
                                    "yeah tell me more"]))
    # turn 2: state-entry lite — Intake state head, FULL tools (== the
    # heavy round's) + the speech directive as the only trailing block
    assert "Your Mission" in fake.seen_systems[1]
    assert fake.seen_tools[1] and fake.seen_tools[1] == fake.seen_tools[2]
    last = fake.seen_messages[1][-1]
    assert last["role"] == "system" and "SPEECH ONLY" in last["content"]
    # turn 3: FULL payload — tool channel open, no directive
    assert "Your Mission" in fake.seen_systems[2]
    assert fake.seen_tools[2]


def test_turn2_full_payload_kill_switch():
    """state_entry_lite=False (legacy rollback): turn 2 after a lite turn 1 is
    the FULL payload — the pre-T6 behavior, exactly."""
    fake = FakeLLM([{"tokens": ["Hi", " there", "."]},
                    {"tokens": ["Got", " it", "."]}])
    rt = CallRuntime(Settings(openai_api_key="test", retell_api_key="test",
                              langfuse_enabled=False, first_turn_lite=True,
                              state_entry_lite=False),
                     LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client())
    asyncio.run(_turns(rt, "ft-2b", ["hello", "my name is Kim"]))
    assert "Your Mission" in fake.seen_systems[1]          # full Intake head
    assert fake.seen_tools[1]                              # tools present


def test_kill_switch_off_full_turn1():
    """first_turn_lite=False (legacy rollback): turn 1 is the FULL payload."""
    fake = FakeLLM([{"tokens": ["Hi", " there", "."]}])
    rt = CallRuntime(_settings(False), LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client())
    asyncio.run(_turns(rt, "ft-3", ["hello?"]))
    assert "Your Mission" in fake.seen_systems[0]          # full Intake head on turn 1
    assert fake.seen_tools[0]                              # tools present


def test_default_is_lite():
    """iter44: DEFAULT Settings → lite is ON — turn 1 lite payload out of the
    box (owner-approved; first TTFT 1913→976 ms on the battery rerun)."""
    fake = FakeLLM([{"tokens": ["Hi", " there", "."]}])
    rt = CallRuntime(Settings(openai_api_key="test", retell_api_key="test",
                              langfuse_enabled=False), LLM_JSON, tracer=None,
                     llm=fake, http_client=mock_client())
    asyncio.run(_turns(rt, "ft-4", ["hello?"]))
    assert "Your Mission" not in fake.seen_systems[0]      # state prompt dropped
    assert fake.seen_tools[0] == ["memory_note"]           # ONE do-nothing tool (FIND-5)
