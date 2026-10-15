"""iter42 — TTS dedupe sliding-window regression tests.

Pins the iter42 upgrade of the iter41 T1 dedupe: 1-slot memory → 6-slot
sliding window (carried across tool-rounds of the same turn, reset at
ingest) + the turn-silence guard:
  1. A,A → second A dropped (old behavior preserved)
  2. A,B,A,B → spoken A,B (THE fix — the Sofia/Carlos/Jorge shape)
  3. A,B,C,A,B,C → spoken A,B,C
  4. A,B,C,D,E,F,G → all 7 spoken (no over-drop; window trim works)
  5. same sentence in two DIFFERENT turns → both spoken (per-turn reset)
  6. cross-round carry drops a repeated A; a round whose only sentence is a
     dup with nothing spoken yet this turn is spoken anyway (silence guard)
The dedupe is spoken-only: final content/history keep the RAW model output.
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

SETTINGS = Settings(openai_api_key="test", retell_api_key="test",
                    langfuse_enabled=False, rag_min_query_chars=0)


def make_rt(rounds: list[dict], settings: Settings | None = None):
    fake = FakeLLM(rounds)
    rt = CallRuntime(settings or SETTINGS, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    return rt, fake


def run_round(rt: CallRuntime, call_id: str, user_text: str,
              state: str = "Intake", persona_dvs: dict | None = None):
    """One speak-only turn; returns (final_state, tts_tokens)."""
    tokens: list[str] = []

    async def go():
        config = {"configurable": {"thread_id": call_id}}
        payload = rt.initial_state(call_id, persona_dvs)
        payload["state_name"] = state
        payload["user_text"] = user_text
        async for mode, d in rt.graph.astream(payload, config=config,
                                              stream_mode=["updates", "custom"]):
            if mode == "custom" and isinstance(d, dict) and "tts_token" in d:
                tokens.append(d["tts_token"])
        return (await rt.graph.aget_state(config)).values

    return asyncio.run(go()), tokens


# scripted token streams (one sentence per 4-token group, as observed)
A = ["First", " sentence", " here", "."]
B = ["Second", " sentence", " follows", "."]
C = ["Third", " sentence", " closes", "."]
D = ["Fourth", " sentence", " stands", "."]
E = ["Fifth", " sentence", " holds", "."]
F = ["Sixth", " sentence", " remains", "."]
G = ["Seventh", " sentence", " ends", "."]


def test_window_a_a_dropped():
    """A,A → second A dropped (iter41 behavior preserved under the window)."""
    rt, _ = make_rt([{"tokens": A + A}])
    _, tokens = run_round(rt, "w42-1", "yeah sure")
    spoken = "".join(tokens)
    assert spoken.count("First sentence here.") == 1


def test_window_ab_ab_block_dropped():
    """A,B,A,B block double → spoken A,B (THE iter42 fix)."""
    rt, _ = make_rt([{"tokens": A + B + A + B}])
    _, tokens = run_round(rt, "w42-2", "ok")
    spoken = "".join(tokens)
    assert spoken.count("First sentence here.") == 1
    assert spoken.count("Second sentence follows.") == 1
    assert spoken.index("First sentence here.") < spoken.index("Second sentence follows.")


def test_window_abc_abc_block_dropped():
    """A,B,C,A,B,C block double → spoken A,B,C."""
    rt, _ = make_rt([{"tokens": A + B + C + A + B + C}])
    _, tokens = run_round(rt, "w42-3", "ok")
    spoken = "".join(tokens)
    assert spoken.count("First sentence here.") == 1
    assert spoken.count("Second sentence follows.") == 1
    assert spoken.count("Third sentence closes.") == 1


def test_window_seven_distinct_all_spoken():
    """7 distinct sentences → all spoken (window trim never eats fresh text)."""
    rt, _ = make_rt([{"tokens": A + B + C + D + E + F + G}])
    _, tokens = run_round(rt, "w42-4", "ok")
    spoken = "".join(tokens)
    for sent in ("First sentence here.", "Second sentence follows.",
                 "Third sentence closes.", "Fourth sentence stands.",
                 "Fifth sentence holds.", "Sixth sentence remains.",
                 "Seventh sentence ends."):
        assert spoken.count(sent) == 1


def test_window_resets_per_turn():
    """Same sentence in two DIFFERENT turns → spoken in both (no cross-turn dedupe)."""
    rt, _ = make_rt([{"tokens": A}, {"tokens": A}])
    _, t1 = run_round(rt, "w42-5", "please")
    _, t2 = run_round(rt, "w42-5", "and again")
    assert "".join(t1).count("First sentence here.") == 1
    assert "".join(t2).count("First sentence here.") == 1


# --------------------------------------------------------------------------- #
# cross-round carry + silence guard
# --------------------------------------------------------------------------- #
SLOTS = {"account_id": "diallux_live", "timezone": "America/Chicago"}
PERSONA = {"prospect_timezone": "America/Chicago"}
SLOTS_ROUND = {"tokens": A,
               "tool_calls": [{"id": "c1", "name": "query_livecall_slots",
                               "arguments": json.dumps(SLOTS)}]}


def _run_confirm_slots(rt: CallRuntime, call_id: str, tokens: list[str]):
    async def go():
        config = {"configurable": {"thread_id": call_id}}
        payload = rt.initial_state(call_id, persona_dvs=PERSONA)
        payload["state_name"] = "ConfirmSlots"
        payload["user_text"] = "as soon as possible"
        async for mode, d in rt.graph.astream(payload, config=config,
                                              stream_mode=["updates", "custom"]):
            if mode == "custom" and isinstance(d, dict) and "tts_token" in d:
                tokens.append(d["tts_token"])
        return (await rt.graph.aget_state(config)).values
    return asyncio.run(go())


def test_window_cross_round_carry_drops_repeat():
    """Round 1 speaks A; round 2 (after tool calls) repeats A → dropped;
    the window carries via tts_sentence_window."""
    rt, _ = make_rt([SLOTS_ROUND, {"tokens": A}])
    tokens: list[str] = []
    state = _run_confirm_slots(rt, "w42-6a", tokens)
    spoken = "".join(tokens)
    assert spoken.count("First sentence here.") == 1
    assert state["tts_sentence_window"] == ["first sentence here."]


def test_window_silence_guard_speaks_when_turn_silent():
    """Belt-and-braces guard: a round whose ONLY sentence is a window hit is
    spoken anyway when nothing has been spoken yet this turn. Constructed via
    a scripted-content LLM (round 1 emits tokens with EMPTY content → window
    populated, turn_spoke stays False; round 2 repeats A → guard speaks it)."""
    NO_FILLER = Settings(openai_api_key="test", retell_api_key="test",
                         langfuse_enabled=False, rag_min_query_chars=0,
                         one_trip=False)

    class ScriptedContentLLM(FakeLLM):
        async def astream(self, messages, tools, verbosity: str = ""):
            systems = [m["content"] for m in messages if m["role"] == "system"]
            self.seen_systems.append("\n".join(systems))
            self.seen_tools.append([t["function"]["name"] for t in tools])
            script = self.rounds.pop(0)
            for tok in script.get("tokens", []):
                yield {"type": "token", "text": tok}
            yield {"type": "final",
                   "content": script.get("content", "".join(script.get("tokens", []))),
                   "tool_calls": script.get("tool_calls", []),
                   "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}

    fake = ScriptedContentLLM([
        {"tokens": A, "content": "", "tool_calls": [
            {"id": "c1", "name": "query_livecall_slots",
             "arguments": json.dumps(SLOTS)}]},
        {"tokens": A},
    ])
    rt = CallRuntime(NO_FILLER, LLM_JSON, tracer=None, llm=fake,
                     http_client=mock_client(), kb_store=None)
    tokens: list[str] = []
    state = _run_confirm_slots(rt, "w42-6b", tokens)
    spoken = "".join(tokens)
    # both copies spoken: the empty-content round's A, then the guard keeps
    # round 2's A (a dup in the window, but the turn had not spoken yet)
    assert spoken.count("First sentence here.") == 2
    assert state["turn_spoke"] is True
