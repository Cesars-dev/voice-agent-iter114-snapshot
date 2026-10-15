"""iter65 T2 — TTS speech-sanitizer pins.

The sanitizer runs BEFORE the dedupe window on every flushed sentence:
  1. verbatim `function_calls` token stripped, natural remainder spoken
     byte-identical;
  2. sentences with { } [ ] => or the `":` pair dropped from the spoken
     stream (model content untouched);
  3. single ':' ("10:30") and single '"' survive — no over-drop;
  4. stream-end (boundary) flush sanitized too;
  5. knob False = byte-identical passthrough.
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


def test_function_calls_token_stripped():
    """`function_calls` protocol token stripped; natural remainder spoken."""
    rt, _ = make_rt([{"tokens": ["Okay", " function_calls", " noted", "."]}])
    _, tokens = run_round(rt, "san-1", "yeah")
    spoken = "".join(tokens)
    assert "function_calls" not in spoken
    assert "Okay" in spoken and "noted." in spoken


def test_code_brace_sentence_dropped():
    """`{"a":1}` artifact sentence dropped from speech; content untouched."""
    bad = "Here {\"a\":1} done."
    rt, _ = make_rt([{"tokens": [bad]}])
    state, tokens = run_round(rt, "san-2", "ok")
    assert "".join(tokens) == ""
    # model content untouched: the scripted bytes still reached history
    recorded = json.dumps(state.get("history") or []) + str(state.get("assistant_text"))
    assert bad in recorded


def test_bracket_and_arrow_dropped():
    """[x] and => artifact sentences dropped."""
    rt, _ = make_rt([{"tokens": ["See [tool] ok.",
                                 "Maps a => b now.",
                                 "Clean sentence."]}])
    _, tokens = run_round(rt, "san-3", "ok")
    spoken = "".join(tokens)
    assert "[" not in spoken and "=>" not in spoken
    assert "clean sentence." in spoken.lower()


def test_quote_colon_pair_dropped():
    """The JSON-ish `":` pair drops the sentence."""
    rt, _ = make_rt([{"tokens": ["The rule \"strict\": true applies."]}])
    _, tokens = run_round(rt, "san-4", "ok")
    assert "".join(tokens) == ""


def test_natural_colon_and_quotes_survive():
    """Times (single ':') and quoted words (single '"') are SPOKEN."""
    rt, _ = make_rt([{"tokens": ["We open at", " 10:30", " sharp",
                                 ". He said ", '"great"', " today."]}])
    _, tokens = run_round(rt, "san-5", "ok")
    spoken = "".join(tokens)
    assert "10:30" in spoken
    assert '"great"' in spoken


def test_stream_end_boundary_flush_sanitized():
    """A turn ending mid-sentence flushes at stream end — sanitized too."""
    rt, _ = make_rt([{"tokens": ["Here is the artifact {x} and no period"]},
                     {"tokens": ["Normal close."]}])
    _, t1 = run_round(rt, "san-6", "first")
    assert "".join(t1) == ""          # brace sentence dropped at the boundary
    _, t2 = run_round(rt, "san-6", "second")
    assert "".join(t2).count("Normal close.") == 1


def test_knob_off_passthrough():
    """tts_sanitize_tokens=False = raw stream (iter64 exact)."""
    s = Settings(openai_api_key="test", retell_api_key="test",
                 langfuse_enabled=False, rag_min_query_chars=0,
                 tts_sanitize_tokens=False)
    raw = "Bad {stuff} here."
    rt, _ = make_rt([{"tokens": [raw]}], s)
    _, tokens = run_round(rt, "san-7", "ok")
    assert "".join(tokens) == raw
