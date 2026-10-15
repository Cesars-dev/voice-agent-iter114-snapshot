"""iter105 T1 (ask-hold chop) — one question per round, spoken-only history.

The chop: the first SPOKEN chunk of a round that ends "?" kills the rest of
that round's TTS tail (builder-side) and the SentenceGate holds the same rule
as a session-level belt (`chop_on_question`). History + assistant_text carry
only the spoken prefix; `round_asked`/`unspoken_tail` record the chop; the
next round's delta prepends the ratified NOTE telling the model the caller
did NOT hear the rest. ASK_HOLD=false = byte-exact pre-branch behavior.

Pins:
  1. test_question_stops_round_stream   — spoken tokens END at the flushed "?"
  2. test_chopped_tail_never_in_history — history == spoken prefix; round_asked; unspoken_tail
  3. test_next_round_note_prepended     — next round's delta carries the NOTE (byte-exact, then consumed)
  4. test_flag_off_byte_parity          — ask_hold=False → full stream/history (rollback proof)
  5. test_no_question_no_chop           — statement rounds stream whole
  6. test_chop_reset_per_round          — gate: chop drops adds; round_reset/end_of_turn/reset lift
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime
from diallux.media.sentence_gate import SentenceGate
from tests.fake_llm import FakeLLM
from tests.mock_webhooks import mock_client

ROOT = Path(__file__).resolve().parents[1]
LLM_JSON = json.loads((ROOT / "agent" / "llm.json").read_text())

# ratified verbatim 2026-10-10 (T0.2) — pinned here, not imported, so the
# wording can never drift silently.
CHOP_NOTE = ("NOTE: your previous reply was cut off after your question — "
             "the caller did NOT hear the rest. Do not repeat what you already "
             "asked; wait for their answer.")

SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, rag_min_query_chars=0)

QUESTION_ROUND = ["Is", " everything", " there", " correct", "?",
                  " Perfect", ".", " So", " we", " can", " book", " that", "."]
QUESTION_TEXT = "Is everything there correct?"
TAIL_TEXT = " Perfect. So we can book that."


def make_rt(rounds: list[dict], settings: Settings | None = None):
    fake = FakeLLM(rounds)
    rt = CallRuntime(settings or SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    return rt, fake


def run_round(rt: CallRuntime, call_id: str, user_text: str,
              state: str = "Intake", first: bool = True):
    """One turn; returns (final_state, tts_tokens)."""
    tokens: list[str] = []

    async def go():
        config = {"configurable": {"thread_id": call_id}}
        payload: dict = rt.initial_state(call_id) if first else {}
        payload["state_name"] = state
        payload["user_text"] = user_text
        async for mode, d in rt.graph.astream(payload, config=config,
                                              stream_mode=["updates", "custom"]):
            if mode == "custom" and isinstance(d, dict) and "tts_token" in d:
                tokens.append(d["tts_token"])
        return (await rt.graph.aget_state(config)).values

    return asyncio.run(go()), tokens


def last_assistant(state: dict) -> str:
    for m in reversed(state.get("history", [])):
        if m.get("role") == "assistant" and not m.get("tool_calls"):
            return m.get("content", "")
    return ""


# ---- 1. the stream stops at the question ---------------------------------- #
def test_question_stops_round_stream():
    rt, _ = make_rt([{"tokens": QUESTION_ROUND}])
    state, tokens = run_round(rt, "chop-1", "go ahead")
    spoken = "".join(tokens)
    assert spoken == QUESTION_TEXT, f"spoken must end at the question, got: {spoken!r}"
    assert "Perfect" not in spoken and "book that" not in spoken


# ---- 2. history == heard; the chop is recorded ---------------------------- #
def test_chopped_tail_never_in_history():
    rt, _ = make_rt([{"tokens": QUESTION_ROUND}])
    state, tokens = run_round(rt, "chop-2", "go ahead")
    assert last_assistant(state) == QUESTION_TEXT
    assert state.get("round_asked") is True
    assert state.get("unspoken_tail") == TAIL_TEXT
    assert state.get("assistant_text") == QUESTION_TEXT


# ---- 3. the next round is told (NOTE) and the tail is consumed ------------ #
def test_next_round_note_prepended():
    rt, fake = make_rt([{"tokens": QUESTION_ROUND},
                        {"tokens": ["Sure", ",", " let", "'s", " book", " it", "."]}])
    _, tokens = run_round(rt, "chop-3", "go ahead")
    assert "".join(tokens) == QUESTION_TEXT
    state, tokens2 = run_round(rt, "chop-3", "yes please", first=False)
    assert "".join(tokens2) == "Sure, let's book it."
    assert CHOP_NOTE in fake.seen_systems[-1], \
        "the post-chop round's delta must carry the ratified NOTE"
    assert state.get("unspoken_tail") == ""
    assert state.get("round_asked") is False


# ---- 4. flag off = byte-exact pre-branch ---------------------------------- #
def test_flag_off_byte_parity():
    settings = Settings(openai_api_key="test", retell_api_key="test",
                        langfuse_enabled=False, rag_min_query_chars=0,
                        ask_hold=False)
    rt, _ = make_rt([{"tokens": QUESTION_ROUND}], settings=settings)
    state, tokens = run_round(rt, "chop-4", "go ahead")
    # pre-branch bytes: spoken stream = per-sentence sanitized (strip); history
    # = raw model output (unchanged by ASK_HOLD=false)
    assert "".join(tokens) == QUESTION_TEXT + "Perfect.So we can book that."
    assert last_assistant(state) == "".join(QUESTION_ROUND)
    assert state.get("round_asked") is False
    assert state.get("unspoken_tail") == ""


# ---- 5. statements never chop --------------------------------------------- #
def test_no_question_no_chop():
    rt, _ = make_rt([{"tokens": ["Got", " it", ".", " We", " can", " book", " that", "."]}])
    state, tokens = run_round(rt, "chop-5", "ok")
    assert "".join(tokens) == "Got it.We can book that."       # spoken (sanitized)
    assert last_assistant(state) == "Got it. We can book that."  # history (raw)
    assert state.get("round_asked") is False
    assert state.get("unspoken_tail") == ""


# ---- 6. the gate belt: chop → round_reset/lifecycle lift ------------------ #
def test_chop_reset_per_round():
    async def go():
        spoken: list[str] = []

        async def on_sentence(text: str, _continue: bool):
            if text:
                spoken.append(text)

        g = SentenceGate(on_sentence, chop_on_question=True)
        # round 1: the question flushes; the tail dies unbuffered
        g.add("Is everything there correct? ")
        g.add(" This tail never speaks.")
        assert g._chop_round is True
        await g.end_of_turn()
        assert spoken == ["Is everything there correct?"], spoken
        assert g._chop_round is False, "turn over — next turn unchopped"
        # a new round flows again
        g.add("A brand new round speaks.")
        await g.end_of_turn()
        assert spoken[-1] == "A brand new round speaks."
        # explicit mid-turn round boundary lifts a chop too
        g.add("Is there anything else at all? ")
        assert g._chop_round is True
        g.round_reset()
        assert g._chop_round is False
        g.add("And the answer to that question.")
        await g.end_of_turn()
        assert "And the answer to that question." in spoken
        # teardown clears
        g.add("Second question here for you? ")
        assert g._chop_round is True
        await g.reset()
        assert g._chop_round is False

    asyncio.run(go())
