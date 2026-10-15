"""Fake streaming LLM for deterministic graph tests.

Same interface as graph.llm.StreamingLLM.astream(messages, tools). Each round
pops the next scripted response. Scripts can depend on the system prompt so we
can verify state swaps (mid-turn transition semantics).
"""
from __future__ import annotations

import json
from typing import AsyncIterator


class FakeLLM:
    def __init__(self, rounds: list[dict] | None = None):
        # round: {"tokens": [...], "tool_calls": [{"id","name","arguments"}], "match_system": "Discovery"}
        self.rounds: list[dict] = list(rounds or [])
        self.seen_systems: list[str] = []
        self.seen_system_msgs: list[list[str]] = []
        # iter44 T3: full message lists (head/tail position + history growth)
        self.seen_messages: list[list[dict]] = []
        self.seen_tools: list[list[str]] = []

    def add_round(self, round_: dict):
        self.rounds.append(round_)

    async def astream(self, messages: list[dict], tools: list[dict],
                      verbosity: str = "") -> AsyncIterator[dict]:
        systems = [m["content"] for m in messages if m["role"] == "system"]
        system = systems[0] if systems else ""
        # iter31 C3b: head is messages[0]. iter44 T3 (tail_before_history):
        # the dynamic tail is messages[1] and the history follows it —
        # seen_systems joins all system blocks (substring checks keep
        # working); seen_system_msgs keeps the surgical view; seen_messages
        # records the full layout for position asserts.
        self.seen_systems.append("\n".join(systems))
        self.seen_system_msgs.append(systems)
        self.seen_messages.append(list(messages))
        self.seen_tools.append([t["function"]["name"] for t in tools])
        script = self.rounds.pop(0)
        if script.get("match_system"):
            assert script["match_system"] in system, (
                f"expected state prompt {script['match_system']!r} in system, got: {system[:200]}"
            )
        for tok in script.get("tokens", []):
            yield {"type": "token", "text": tok}
        # iter46 T4a: tool-call name detection precedes the final event (the
        # real StreamingLLM emits `tool_call_start` as soon as the name is
        # complete; the builder's fire-at-detection hook consumes it).
        for tc in script.get("tool_calls", []):
            yield {"type": "tool_call_start", "name": tc["name"]}
        usage = script.get("usage") or {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        yield {
            "type": "final",
            "content": "".join(script.get("tokens", [])),
            "tool_calls": script.get("tool_calls", []),
            "usage": usage,
        }


def tool_call(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {"id": call_id, "name": name, "arguments": json.dumps(args)}
