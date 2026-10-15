"""Text REPL for the brain — no telephony, no STT/TTS. Drives the LangGraph
runtime exactly like a phone call would (one graph invocation per utterance).

Usage:
    python scripts/chat_repl.py                     # interactive
    python scripts/chat_repl.py --fake              # scripted fake LLM, no keys needed
    echo "I got a voicemail about missed calls" | python scripts/chat_repl.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diallux.config import Settings
from diallux.graph.builder import CallRuntime


async def repl(fake: bool):
    settings = Settings()
    llm_json = json.loads(Path(settings.agent_llm_json).read_text())

    llm = None
    if fake:
        from tests.fake_llm import FakeLLM, tool_call
        llm = FakeLLM([
            {"tokens": [], "tool_calls": [
                tool_call("extract_intake_details", {"inbound_channel": "voicemail",
                                                      "interest_topic": "missed calls",
                                                      "pain_frame": "losing jobs"}),
                tool_call("intake_completed", {"intake_completed": True}, "call_flag"),
                tool_call("transition_to_Discovery", {"intake_completed": True,
                                                        "inbound_channel": "voicemail",
                                                        "interest_topic": "missed calls",
                                                        "pain_frame": "losing jobs"}, "call_t"),
            ]},
            {"tokens": ["Got", " it", " —", " what", " happens", " when", " a", " call", " comes", " in", "?"],
             "match_system": "Bridge what they came for"},
        ])
        settings = Settings(openai_api_key="fake", retell_api_key="fake", langfuse_enabled=False)

    runtime = CallRuntime(settings, llm_json, llm=llm)
    call_id = f"repl-{id(runtime):x}"
    initial = runtime.initial_state(call_id)
    print("Linda:", initial["history"][0]["content"])
    print("(type 'quit' to exit; '/state' shows dvs)\n")

    first = True
    config = {"configurable": {"thread_id": call_id}}
    try:
        while True:
            try:
                user = input("You> ").strip()
            except EOFError:
                break
            if not user:
                continue
            if user == "quit":
                break
            if user == "/state":
                snap = await runtime.graph.aget_state(config)
                print(json.dumps({"state": snap.values.get("state_name"),
                                  "dvs": {k: v for k, v in (snap.values.get("dvs") or {}).items() if v != ""},
                                  "ended": snap.values.get("ended")}, indent=2))
                continue
            payload = {"user_text": user}
            if first:
                payload.update(initial)
                first = False
            tokens: list[str] = []
            try:
                async for mode, data in runtime.graph.astream(payload, config=config,
                                                              stream_mode=["custom", "updates"]):
                    if mode == "custom" and isinstance(data, dict) and "tts_token" in data:
                        tokens.append(data["tts_token"])
                        print(data["tts_token"], end="", flush=True)
            except Exception as exc:
                print(f"\n[error] {type(exc).__name__}: {exc}")
                continue
            if not tokens:
                print("(no speech this round — tool work only)", end="")
            print("\n")
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    asyncio.run(repl(fake="--fake" in sys.argv))
