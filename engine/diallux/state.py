"""LangGraph state — V2 typed build.

Same shape as V1 (state_name / dvs / history / turn scratch), but dvs are
seeded through schema.DynamicVariables: typed defaults ("" / False), numeric
coercion, and no null passthrough — the Retell null-dv bug class is
structurally impossible, which is why V2 needs no "verify no null dvs" tool.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from .schema import DynamicVariables


def merge_dict(a: dict | None, b: dict | None) -> dict:
    out = dict(a or {})
    out.update(b or {})
    return out


def merge_history(a: list[dict] | None, b: list[dict] | None) -> list[dict]:
    """operator.add + the iter104 PT-1 replace marker.

    A patch entry {"role": "user", "content": X, "supersedes": True} pops the
    LAST entry from `a` before appending itself — and ONLY when that entry is
    a user entry (the previous variant of the SAME utterance being replaced
    by the confirmed final). A trailing assistant/tool entry is never eaten:
    the marker degrades to a plain append. Every other patch shape = exact
    operator.add (byte-identical semantics for every existing writer).

    PURITY LAW (paid lesson, this iteration): the reducer NEVER mutates `b`
    — LangGraph re-applies a cancelled run's pending writes through the
    reducers on resume, so a mutating pop made the replay append without
    replacing (FRAG + FINAL). The marker is stripped on a COPY here; the
    same (a, b) pair always produces the same output."""
    out = list(a or [])
    for m in (b or []):
        entry = m
        if "supersedes" in m:
            entry = {k: v for k, v in m.items() if k != "supersedes"}
        if m.get("role") == "user" and m.get("supersedes") \
                and out and out[-1].get("role") == "user":
            out.pop()
        out.append(entry)
    return out


class GraphState(TypedDict, total=False):
    # identity
    call_id: str

    # Retell runtime replica
    state_name: str
    dvs: Annotated[dict[str, Any], merge_dict]
    history: Annotated[list[dict], merge_history]

    # per-turn scratch
    turn_active: bool
    turn_spoke: bool
    round_spoke: bool          # iter29: the LAST round produced speech (one-trip routing)
    engine_fired: bool         # iter29: the deterministic engine acted this pass
    last_spoken: str           # iter30: last NON-EMPTY agent speech (end_call goodbye memory)
    rounds_left: int
    last_round_tool_calls: list[dict]
    assistant_text: str
    ended: bool
    fillers_said: list[str]    # iter33: engine filler lines already spoken this call (complete-list overwrite)
    frozen_state_block: str    # iter40 C3: state-block bytes rendered ONCE at turn start (ingest)
    tts_sentence_window: list[str]  # iter42: last N SPOKEN sentence keys this turn (sliding dedupe window; reset at ingest)
    question_stem_window: list[dict]  # iter59 T4: cross-turn spoken QUESTION stems {"text","vec"} (plain key — NO reducer, NOT reset at ingest; survives the whole call; seeded empty in initial_state)
    round_asked: bool          # iter105 T1: the LAST round's spoken text ended on a question (chop fired)
    unspoken_tail: str         # iter105 T1: text chopped after the question — never spoken; drives the next round's NOTE
    held_facts: list[str]      # iter105 T3: engine news/results parked while a question waits for the caller

    # user input channel
    user_text: str
    user_text_supersedes: bool  # iter104 PT-1: replace marker (eager-fragment rerun)

    # observability
    turn_index: int
    metrics: Annotated[dict[str, Any], merge_dict]


def begin_message(llm_json: dict) -> str:
    msg = (llm_json.get("begin_message") or "").strip()
    return msg or "Hello! You have reached Dialux, this is Linda speaking, how may I help you today?"


def initial_state(call_id: str, llm_json: dict, persona_dvs: dict | None = None) -> GraphState:
    """Typed seed: defaults come from DynamicVariables (no stale dates, no nulls)."""
    flat_defaults = DynamicVariables().to_flat()
    # only accept known keys from persona overrides (e.g. Twilio <Parameter> seeds)
    persona = {k: v for k, v in (persona_dvs or {}).items() if k in DynamicVariables.model_fields}
    dvs = DynamicVariables.from_flat({**flat_defaults, **persona}).to_flat()
    return GraphState(
        call_id=call_id,
        state_name=llm_json.get("starting_state", "Intake"),
        dvs=dvs,
        history=[{"role": "assistant", "content": begin_message(llm_json)}],
        turn_active=False,
        turn_spoke=False,
        rounds_left=0,
        ended=False,
        turn_index=0,
        question_stem_window=[],
        metrics={},
    )
