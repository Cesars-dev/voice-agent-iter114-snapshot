#!/usr/bin/env python3
"""LLM-to-LLM "fake call" harness — test the agent's brain with an LLM caller.

Your SOP (docs/Testing_guidelines/LLM-TO-LLM-TESTING.md), injected as a
feature of the self-hosted LangGraph runtime. In Retell you had to create a
chat agent because a voice agent cannot be driven by create-chat-completion.
Here the graph IS text-in/text-out natively (STT/TTS/telephony wrap OUTSIDE
the brain), so faking a call is just: one thread (thread_id = call_id) +
one caller LLM looping against graph.astream(). No clone, no API in between.

Transports (same personas, same scorer — so they are comparable):
  graph  (default)  the in-process LangGraph brain (CallRuntime)
  retell             your live Retell chat agent (migration A/B: same persona
                     battery against both brains, diff before cutover)

The caller is GPT-4o (or --caller-model) with the full conversation history
every turn (SOP "Key Technique — Full Context"), temperature 0.7, <=20 words.

Usage:
  python tests/llm2llm/harness.py --list
  python tests/llm2llm/harness.py --offline              # hermetic plumbing smoke (no keys)
  python tests/llm2llm/harness.py --personas happy        # SMOKE TEST FIRST (needs OPENAI_API_KEY)
  python tests/llm2llm/harness.py --personas stress
  python tests/llm2llm/harness.py --personas all
  python tests/llm2llm/harness.py --personas Maria        # substring on name
  python tests/llm2llm/harness.py --transport retell --personas happy   # A/B vs live Retell
  python tests/llm2llm/harness.py --personas happy --rag --langfuse     # RAG + traces

Slots: --slots mock (default) runs the webhooks against the in-process mock
(tests/mock_webhooks.py — the LIVE production contract, no real bookings).
--slots live hits slots.diallux-ai.site for real: /today, slot-lock,
book-livecall WILL create real bookings. Only use it against a test server.

Exit code 0 = all runs pass, 1 = failures, 2 = config error (CI-able).
Reports: eval/llm2llm_report.json + per-run logs in tests/llm2llm/json_logs/.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from diallux.config import Settings                        # noqa: E402
from diallux.graph.builder import CallRuntime              # noqa: E402
from tests.fake_llm import FakeLLM, tool_call              # noqa: E402
from tests.mock_webhooks import mock_client                # noqa: E402
from tests.llm2llm.personas import PERSONAS, select        # noqa: E402

# The production build (typed dvs + hard-gated transitions) has diallux/schema;
# the verbatim build does not. One harness file serves both.
IS_PRODUCTION = importlib.util.find_spec("diallux.schema") is not None
BUILD = "production" if IS_PRODUCTION else "verbatim"

BOOKING_TOOL = "create_livecall_booking"    # the tool that books the live call
GOODBYE_MARKERS = ("goodbye", "bye for now", "take care", "have a great")
MAX_TURNS_DEFAULT = 48                      # your SOP bound (raised from 30 iter15: a gate
                                            # stall must not silently drop a call — the LLM
                                            # needs room to recover; root fix is the plan)
LOG_DIR = Path(__file__).resolve().parent / "json_logs"
REPORT_PATH = ROOT / "eval" / "llm2llm_report.json"


# --------------------------------------------------------------------------- #
# Caller side (the "customer" LLM)
# --------------------------------------------------------------------------- #
class CallerDone(Exception):
    """Scripted caller exhausted — ends the loop cleanly."""


class CallerLLM:
    """GPT-4o plays the caller. Full context every turn (SOP key technique)."""

    def __init__(self, model: str = "gpt-4o-mini", temperature: float = 0.3, max_tokens: int = 80):
        from openai import AsyncOpenAI   # already a dependency (langchain-openai)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    async def next_line(self, system: str, history: list[dict], agent_text: str) -> str:
        msgs = [{"role": "system", "content": system}] + list(history)
        msgs.append({"role": "user", "content":
                     f'Agent said: "{agent_text}"\nReply in <=20 words as the customer.'})
        kw = {'temperature': self.temperature}
        if 'gpt-5' in self.model:
            kw.pop('temperature')   # gpt-5-family chat models: only default(1) supported
        if 'gpt-5' in self.model:
            kw['max_completion_tokens'] = self.max_tokens
        else:
            kw['max_tokens'] = self.max_tokens
        resp = await self.client.chat.completions.create(
            model=self.model, messages=msgs, **kw)
        return (resp.choices[0].message.content or "").strip()


class ScriptedCaller:
    """Deterministic caller for --offline (plumbing validation, no keys)."""

    def __init__(self, lines: list[str]):
        self.lines = list(lines)
        self.i = 0

    async def next_line(self, system: str, history: list[dict], agent_text: str) -> str:
        if self.i >= len(self.lines):
            raise CallerDone
        line = self.lines[self.i]
        self.i += 1
        return line


# --------------------------------------------------------------------------- #
# Agent side — one AgentTurn per caller utterance (transport-agnostic shape)
# --------------------------------------------------------------------------- #
@dataclass
class AgentTurn:
    speech: str                 # what the agent said this turn ("" = tool work only)
    tool_calls: list[str]       # tools fired within the turn, in order
    ended: bool                 # end_call fired
    state: str | None           # current state_name after the turn (graph only)
    dvs: dict                   # dvs after the turn (graph only)
    agent_ms: float | None      # brain latency of the turn (graph only)


class GraphAgent:
    """The self-hosted brain, in-process: text in -> spoken tokens out.

    One graph invocation == one caller utterance (exactly like the media
    pipeline drives it). Tool rounds run INSIDE the invocation, so a turn
    always completes with speech, end_call, or exhausted rounds — the
    Retell "mid-tool empty agent content" bug cannot abort the loop here.
    """

    def __init__(self, settings: Settings, http_client, tracer=None, llm=None,
                 kb_store=None, warm_greeting_ms: int = 0,
                 rag_fire_sim: bool = False, fire_sim_tail_ms: int = 500):
        llm_json = json.loads(Path(settings.agent_llm_json).read_text())
        # kwargs work on both builds (production has an extra optional
        # `checkpointer` we deliberately leave default for tests).
        self.rt = CallRuntime(settings, llm_json, tracer=tracer, llm=llm,
                              http_client=http_client, kb_store=kb_store)
        self.call_id = ""
        self.config: dict = {}
        # iter48b experiment (FIND-3/PT-45): session-parity greeting warms.
        # 0 = off (happy-c behavior — turn-1 structurally cold in harness).
        self.warm_greeting_ms = warm_greeting_ms
        # iter63: --rag-fire-sim mirrors CallSession._fire_live_retrieve
        # (diallux/media/session.py) — laneA on the flux Update (empty
        # utterance), laneB at EagerEOT with the turn transcript, then the
        # speech-tail sleep stands in for the caller's remaining speech.
        # Flag-off path is byte-identical; engine code untouched.
        self.rag_fire_sim = rag_fire_sim
        self.fire_sim_tail_ms = fire_sim_tail_ms

    async def start(self, persona: dict) -> str:
        slug = "".join(ch for ch in persona["name"].split("—")[0] if ch.isalnum()).lower()
        self.call_id = f"l2l-{slug}-{uuid.uuid4().hex[:8]}"
        self.config = {"configurable": {"thread_id": self.call_id}}
        if self.warm_greeting_ms > 0:
            await self._greeting_warms(persona)
        return self.call_id

    async def _greeting_warms(self, persona: dict) -> None:
        """MIRROR of diallux/media/session.py:179-205 (the telephony start):
        resolve the KB store, fire warm_rag + full-Intake + lite prewarms,
        then hold the simulated greeting-playback window (the EOT head start
        production gets for free). Harness-only — engine code untouched."""
        rt = self.rt
        try:
            await asyncio.wait_for(rt._resolve_kb_store(), 2.0)
        except Exception:
            pass
        init = rt.initial_state(self.call_id, persona.get("dynvars"))
        state = rt.llm_json.get("starting_state", "Intake")
        history = list(init.get("history") or [])
        dvs = dict(init.get("dvs") or {})
        asyncio.get_event_loop().create_task(rt.warm_rag(state, dict(dvs)))
        rt.warm_prompt_cache(state, history, dvs=dvs)
        rt.warm_prompt_cache(state, history, None, lite=True)
        await asyncio.sleep(self.warm_greeting_ms / 1000.0)

    async def send(self, text: str, *, first: bool, persona: dict) -> AgentTurn:
        payload: dict[str, Any] = {"user_text": text}
        if first:
            # SOP dynvars seeding: initial_state merges persona_dvs over
            # default_dynamic_variables (production filters to typed fields).
            payload.update(self.rt.initial_state(self.call_id, persona.get("dynvars")))
        tokens: list[str] = []
        turn_tools: list[str] = []
        if self.rag_fire_sim:
            # iter63 fire-sim: SAME public contract as CallSession._fire_live_retrieve
            # — runtime.spawn_live_retrieve(state, dvs, transcript, lane=...),
            # keyed "<state>|laneA"/"<state>|laneB", every fire bumping
            # _live_task_seq. State/dvs snapshot = what the session holds when
            # EagerEOT fires (pre-turn state; "Intake" default like the session).
            snap0 = await self.rt.graph.aget_state(self.config)
            v0 = snap0.values or {}
            state_name = v0.get("state_name") or "Intake"
            dvs = dict(v0.get("dvs") or {})
            if first:
                init0 = self.rt.initial_state(self.call_id, persona.get("dynvars"))
                dvs = dict(init0.get("dvs") or {})
            self.rt.spawn_live_retrieve(state_name, dvs, "", lane="laneA")
            self.rt.spawn_live_retrieve(state_name, dvs, text, lane="laneB")
            # the one honest fake: the caller's remaining speech window — the
            # arctic embed (~90-190ms) lands inside it. agent_ms excludes it,
            # like mic latency excludes caller speech time.
            await asyncio.sleep(self.fire_sim_tail_ms / 1000.0)
        t0 = time.perf_counter()
        async for mode, data in self.rt.graph.astream(
                payload, config=self.config, stream_mode=["custom", "updates"]):
            if mode == "custom" and isinstance(data, dict) and "tts_token" in data:
                tokens.append(data["tts_token"])
            elif mode == "updates" and isinstance(data, dict):
                for upd in data.values():
                    if isinstance(upd, dict) and isinstance(upd.get("metrics"), dict):
                        turn_tools += [c for c in upd["metrics"].get("calls", [])
                                       if c and c != "<speak>"]
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        snap = await self.rt.graph.aget_state(self.config)
        v = snap.values or {}
        return AgentTurn(
            speech="".join(tokens).strip(),
            tool_calls=turn_tools,
            ended=bool(v.get("ended")),
            state=v.get("state_name"),
            dvs=dict(v.get("dvs") or {}),
            agent_ms=elapsed_ms,
        )

    async def final(self) -> dict:
        """Full transcript + tool trace + dvs — the get-chat equivalent."""
        snap = await self.rt.graph.aget_state(self.config)
        v = snap.values or {}
        return {
            "dvs": dict(v.get("dvs") or {}),
            "state": v.get("state_name"),
            "ended": bool(v.get("ended")),
            "history": list(v.get("history") or []),
            "trace": [dict(e) for e in self.rt.executor.trace],
            "gate_rejections": len(getattr(self.rt.executor, "gate_rejections", []) or []),
            "rag_stats": dict(getattr(self.rt, "_kb_stats", {}) or {}),
            "cost": None,
        }

    async def aclose(self):
        await self.rt.aclose()


class RetellAgent:
    """Your live Retell chat agent — same loop, same personas (migration A/B).

    Direct port of retell/tools/test_llm_to_llm.py's transport:
    create-chat (+ retell_llm_dynamic_variables), create-chat-completion,
    tool_call_invocation detection, GET get-chat for transcript + cost.
    """

    BASE = "https://api.retellai.com"

    def __init__(self, api_key: str, chat_agent_id: str):
        import httpx
        self.http = httpx.AsyncClient(
            timeout=180.0,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"})
        self.chat_agent_id = chat_agent_id
        self.chat_id: str | None = None
        self.tools_fired: list[str] = []
        self.booking_args: list[dict] = []

    async def start(self, persona: dict) -> str:
        dvs = {"today_date": datetime.now(ZoneInfo("America/Mexico_City")).strftime("%Y-%m-%d")}
        dvs.update(persona.get("dynvars") or {})
        r = await self.http.post(f"{self.BASE}/create-chat", json={
            "agent_id": self.chat_agent_id,
            "retell_llm_dynamic_variables": dvs,
        })
        r.raise_for_status()
        self.chat_id = r.json()["chat_id"]
        return self.chat_id

    async def send(self, text: str, *, first: bool, persona: dict) -> AgentTurn:
        r = await self.http.post(f"{self.BASE}/create-chat-completion", json={
            "agent_id": self.chat_agent_id, "chat_id": self.chat_id, "content": text})
        r.raise_for_status()
        resp = r.json()
        agent_text = ""
        for msg in resp.get("messages", []):
            if msg.get("role") == "agent":
                agent_text = msg.get("content", "")
                break
        fired: list[str] = []
        for m in resp.get("messages", []):
            if m.get("role") != "tool_call_invocation":
                continue
            fired.append(m.get("name") or "")
            if m.get("name") == BOOKING_TOOL:
                # SOP Bug 2: Retell arguments are a JSON STRING — parse guarded.
                args = m.get("arguments")
                try:
                    parsed = json.loads(args) if isinstance(args, str) else (args or {})
                except (json.JSONDecodeError, TypeError):
                    parsed = {}
                self.booking_args.append(parsed)
        for f in fired:
            if f not in self.tools_fired:
                self.tools_fired.append(f)
        return AgentTurn(speech=agent_text, tool_calls=fired,
                         ended="end_call" in fired, state=None, dvs={},
                         agent_ms=None)

    async def final(self) -> dict:
        r = await self.http.get(f"{self.BASE}/get-chat/{self.chat_id}")
        r.raise_for_status()
        chat = r.json()
        return {
            "dvs": chat.get("retell_llm_dynamic_variables")
                  or chat.get("dynamic_variables") or {},
            "state": None,
            "ended": "end_call" in self.tools_fired,
            "history": [],
            "trace": [],
            "gate_rejections": 0,
            "transcript_text": chat.get("transcript", ""),
            "tools_fired": list(self.tools_fired),
            "booking_args": list(self.booking_args),
            "cost": (chat.get("chat_cost") or {}).get("combined_cost"),
        }

    async def aclose(self):
        await self.http.aclose()


# --------------------------------------------------------------------------- #
# Scoring (pure — unit-tested in test_harness_units.py)
# --------------------------------------------------------------------------- #
def booking_evidence(trace: list[dict], dvs: dict) -> dict:
    """BOOKED = the booking tool fired with a CONFIRMED non-empty booking uid.

    SOP rule: assert a booking by non-empty parsed time/uid, NEVER a fixed
    timestamp (live availability varies by date). Reschedule/cancel intents
    do NOT count as booked.
    """
    attempts = [e for e in trace if e.get("tool") == BOOKING_TOOL]
    booked, uid, status = False, None, None
    for e in attempts:
        resp = e.get("resp") or {}
        s = resp.get("status")
        if resp.get("ok") and s in (None, "booked"):
            u = resp.get("booking_uid") or (dvs or {}).get("booking_uid")
            if u:                                  # the non-empty rule
                booked, uid, status = True, u, (s or "booked")
    if not booked:                                 # server-confirmed fallback
        du = (dvs or {}).get("booking_uid")
        if du and (dvs or {}).get("booking_verified"):
            booked, uid, status = True, du, "dvs_confirmed"
    return {"fired": bool(attempts), "attempts": len(attempts),
            "booked": booked, "uid": uid, "status": status}


def retell_booking_evidence(tools_fired: list[str], booking_args: list[dict]) -> dict:
    """Retell variant: booking tool fired + a non-empty parsed time/slot arg."""
    fired = BOOKING_TOOL in tools_fired
    slot = None
    for a in booking_args:
        for k in ("time", "slot", "requested_slot", "booking_time"):
            if a.get(k):
                slot = a[k]
                break
    return {"fired": fired, "attempts": len(booking_args),
            "booked": fired and bool(slot), "uid": slot, "status":
            "booked" if (fired and slot) else (None if not fired else "no_slot")}


def score_outcome(evidence: dict, ended: bool, expect: str) -> dict:
    outcome = "book" if evidence.get("booked") else "no-book"
    return {"outcome": outcome, "expect": expect, "pass": outcome == expect,
            "ended": ended, "booking": evidence}


# --------------------------------------------------------------------------- #
# The loop — one fake call
# --------------------------------------------------------------------------- #
async def run_persona(persona: dict, agent, caller, max_turns: int = MAX_TURNS_DEFAULT,
                      verbose: bool = True) -> dict:
    name = persona["name"]
    if verbose:
        print(f"\n{'=' * 70}\n  {name}   [{persona['type']} | expect={persona['expect']}]\n{'=' * 70}")
    await agent.start(persona)
    if verbose:
        print(f"  call/thread: {agent.call_id}  ({BUILD} build)")

    caller_line = persona["opener"]
    hist: list[dict] = []
    rows: list[dict] = []
    tools_order: list[str] = []
    turn_ms: list[float] = []
    ended = False
    t_wall = time.perf_counter()

    for turn in range(max_turns):
        t = await agent.send(caller_line, first=(turn == 0), persona=persona)
        rows.append({"turn": turn + 1, "caller": caller_line, "agent": t.speech,
                     "tools": t.tool_calls, "state": t.state, "agent_ms": t.agent_ms})
        for tool in t.tool_calls:
            if tool not in tools_order:
                tools_order.append(tool)
        if t.agent_ms is not None:
            turn_ms.append(t.agent_ms)
        if verbose:
            print(f"  [{turn + 1:>2}] C: {caller_line[:150]}")
            print(f"      A: {t.speech[:220]}" if t.speech else "      A: (tool work — no speech)")
            if t.tool_calls:
                print(f"      T: {t.tool_calls}")
        if not t.speech:
            if t.ended:
                break
            # SOP Bug 1: NEVER break on an empty turn — the agent may be
            # mid-tool. Neutral backchannel (NOT a confirmation), bounded by
            # max_turns. (In the graph design tool rounds complete inside one
            # invocation, so this is rare — kept for parity + safety.)
            caller_line = "Mm-hmm."
            continue
        ended = t.ended
        if ended:
            if verbose:
                print("  [end_call fired — call ended]")
            break
        if any(w in t.speech.lower() for w in GOODBYE_MARKERS):
            if verbose:
                print("  [agent closed politely]")
            break
        hist.append({"role": "assistant", "content": caller_line})
        try:
            caller_line = await caller.next_line(persona["system"], hist, t.speech)
        except CallerDone:
            break

    wall_s = round(time.perf_counter() - t_wall, 2)
    final = await agent.final()
    if isinstance(agent, GraphAgent):
        evidence = booking_evidence(final.get("trace", []), final.get("dvs", {}))
    else:
        evidence = retell_booking_evidence(final.get("tools_fired", []),
                                           final.get("booking_args", []))
    score = score_outcome(evidence, final.get("ended", ended), persona.get("expect", "book"))

    # production gate metrics: refusals by trace status cover BOTH the hard
    # gate (gate_failed: edge exists, required params missing -> counted in
    # executor.gate_rejections) and the state machine itself (invalid_transition:
    # no such edge from the current state).
    blocked = sum(1 for e in final.get("trace", [])
                  if (e.get("resp") or {}).get("status") in ("invalid_transition", "gate_failed"))

    result = {
        "persona": name, "type": persona["type"], "expect": persona["expect"],
        "transport": "graph" if isinstance(agent, GraphAgent) else "retell",
        "build": BUILD, "outcome": score["outcome"], "pass": score["pass"],
        "booked": evidence.get("booked"), "booking": evidence,
        "ended": score["ended"], "turns": len(rows),
        "tools": tools_order, "final_state": final.get("state"),
        "final_dvs": {k: v for k, v in (final.get("dvs") or {}).items() if v not in ("", None)},
        "gate_rejections": final.get("gate_rejections", 0),
        "blocked_transitions": blocked,
        "turn_ms": turn_ms, "p50_ms": sorted(turn_ms)[len(turn_ms) // 2] if turn_ms else None,
        "wall_s": wall_s, "cost": final.get("cost"),
        "rag_stats": final.get("rag_stats"),
        "call_id": getattr(agent, "call_id", None),
        "transcript": rows,
    }
    LOG_DIR.mkdir(exist_ok=True)
    safe = "".join(ch for ch in name.split("—")[0] if ch.isalnum()) or "run"
    log_path = LOG_DIR / f"{safe}_{getattr(agent, 'call_id', 'x')}.json"
    log_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    result["log"] = str(log_path.relative_to(ROOT))
    if verbose:
        verdict = "PASS" if score["pass"] else "FAIL"
        print(f"  --- {verdict}: expected {score['expect']}, got {score['outcome']} "
              f"(booked={evidence.get('booked')}, ended={score['ended']}, "
              f"turns={len(rows)}, tools={tools_order})")
        print(f"      log: {result['log']}")
    return result


# --------------------------------------------------------------------------- #
# Offline smoke (hermetic: FakeLLM agent + scripted caller, no keys)
# --------------------------------------------------------------------------- #
def _offline_rounds() -> list[dict]:
    """Two scripted turns.

    Turn 1 (both builds): Intake captures + transition_to_Discovery passes
    (production gate satisfied: intake_completed + inbound_channel +
    interest_topic + pain_frame), then a Discovery speech round.
    Turn 2 diverges by build:
      - verbatim:   ungated jump Discovery->Booking, booking chain + end_call
                    inside the same turn -> booked=True, ended.
      - production: the same illegal jump is REFUSED by the hard gate
                    (gate_rejections +1) and the turn ends with speech —
                    the s3 "premature transition blocked" semantic.
    """
    t1 = [
        {"tokens": [], "tool_calls": [
            tool_call("extract_intake_details",
                      {"inbound_channel": "voicemail", "interest_topic": "missed calls",
                       "pain_frame": "losing jobs"}, "c1"),
            tool_call("intake_completed", {"intake_completed": True}, "c2"),
            tool_call("transition_to_Discovery",
                      {"intake_completed": True, "inbound_channel": "voicemail",
                       "interest_topic": "missed calls", "pain_frame": "losing jobs"}, "c3"),
        ]},
        {"tokens": ["Thanks", " —", " and", " roughly", " how", " many", " calls", " a",
                    " week", " go", " to", " voicemail", "?"],
         "match_system": "Discovery"},
    ]
    t2_head = {"tokens": [], "tool_calls": [
        tool_call("extract_discovery_details", {"industry": "dental"}, "c4"),
        tool_call("discovery_completed", {"discovery_completed": True}, "c5"),
        tool_call("transition_to_Booking", {"discovery_completed": True}, "c6"),
    ]}
    if IS_PRODUCTION:
        t2 = [t2_head,
              {"tokens": ["Great", " —", " a", " couple", " more", " quick", " ones",
                          " first", "."]}]
    else:
        t2 = [t2_head,
              {"tokens": [], "tool_calls": [
                  tool_call("create_livecall_booking", {}, "c7"),
                  tool_call("record_booking_uid", {}, "c8"),
                  tool_call("end_call", {}, "c9"),
              ]}]
    return t1 + t2


OFFLINE_PERSONA = {
    "name": "(SMOKE) Offline plumbing check",
    "type": "happy-path", "expect": "book",
    "dynvars": {"callback_number": "+13125551234"},
    "opener": "Hi — I got a voicemail about missed calls after hours.",
    "system": "(offline scripted caller — not used)",
}
OFFLINE_CALLER_LINES = ["We miss about twenty calls a week; maybe half would book."]


async def run_offline() -> int:
    settings = Settings(openai_api_key="fake", retell_api_key="fake",
                        langfuse_enabled=False, rag_mode="inline")
    fake = FakeLLM(_offline_rounds())
    agent = GraphAgent(settings, http_client=mock_client(), llm=fake, kb_store=None)
    caller = ScriptedCaller(OFFLINE_CALLER_LINES)
    try:
        r = await run_persona(OFFLINE_PERSONA, agent, caller, max_turns=6)
        if IS_PRODUCTION:
            ok = (not r["booked"]) and r["blocked_transitions"] >= 1 and r["turns"] >= 2
            why = "gate held the illegal Discovery->Booking jump (expected in production)"
        else:
            ok = r["booked"] and r["ended"] and BOOKING_TOOL in r["tools"]
            why = "ungated booking chain fired end-to-end (expected in verbatim)"
        print(f"\n[{'PASS' if ok else 'FAIL'}] offline smoke ({BUILD}): {why}")
        REPORT_PATH.parent.mkdir(exist_ok=True)
        REPORT_PATH.write_text(json.dumps({"results": [r], "offline": True}, indent=2,
                                          default=str))
        return 0 if ok else 1
    finally:
        await agent.aclose()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_tracer(persona: dict, transport: str):
    try:
        from diallux.observability.tracer import Tracer
        # trace name carries the persona so `lf.py traces --name marcus` works
        slug = "".join(ch for ch in persona["name"].split("—")[0] if ch.isalnum()).lower()
        return Tracer(session_name=f"llm2llm-{transport}-{slug}",
                      metadata={"persona": persona["name"], "type": persona["type"],
                                "expect": persona["expect"], "transport": transport},
                      enabled=True)
    except Exception as exc:
        print(f"[langfuse] unavailable, continuing without traces ({exc})")
        return None


async def main() -> int:
    ap = argparse.ArgumentParser(description="LLM-to-LLM fake-call harness")
    ap.add_argument("--list", action="store_true", help="list personas and exit")
    ap.add_argument("--offline", action="store_true",
                    help="hermetic smoke: FakeLLM agent + scripted caller (no keys)")
    ap.add_argument("--transport", choices=["graph", "retell"], default="graph")
    ap.add_argument("--personas", default="happy",
                    help="happy | happy3 | stress | curve | all | name substring")
    ap.add_argument("--slots", choices=["mock", "live"], default="mock",
                    help="webhook backend: in-process mock (default) or real slots server")
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS_DEFAULT)
    ap.add_argument("--caller-model", default="gpt-4o-mini")
    ap.add_argument("--agent-model", default="",
                    help="override the agent model (Settings.openai_model)")
    ap.add_argument("--rag", action="store_true",
                    help="let the KBStore resolve (pgvector RAG if configured) instead of inline KBs")
    ap.add_argument("--langfuse", action="store_true",
                    help="one Langfuse trace per persona run (self-hosted)")
    ap.add_argument("--warm-greeting-ms", type=int, default=0,
                    help="iter48b experiment (FIND-3/PT-45): fire the session-"
                         "parity greeting prewarms (full Intake + lite) at call "
                         "start and hold this simulated greeting window (ms) "
                         "before turn 1 — mirrors production media layer. "
                         "0 = off.")
    ap.add_argument("--no-happy-gate", action="store_true",
                    help="run stress/curve even if happy-path personas failed")
    ap.add_argument("--rag-fire-sim", action=argparse.BooleanOptionalAction,
                    default=False,
                    help="iter63: chat-surface lane-fire simulation — fire laneA "
                         "(empty utterance) + laneB (turn transcript) via "
                         "runtime.spawn_live_retrieve before each graph turn, "
                         "exactly like CallSession._fire_live_retrieve (harness-"
                         "only; requires --rag). iter76: AUTO-ON under --rag "
                         "when fire mode is hybrid; opt out with "
                         "--no-rag-fire-sim.")
    ap.add_argument("--fire-sim-tail-ms", type=int, default=500,
                    help="iter63: simulated speech tail (ms) between the lane "
                         "fires and the graph turn — the caller's remaining "
                         "speech window the arctic embed lands inside. 0 = "
                         "short window (fail-clean consume).")
    args = ap.parse_args()

    if args.list:
        for p in PERSONAS:
            print(f"  {p['type']:14s} {p['name']:55s} expect={p['expect']}")
        print(f"\n  ({len(PERSONAS)} personas, {BUILD} build — groups: happy happy3 stress curve all)")
        return 0

    if args.offline:
        return await run_offline()

    personas = select(args.personas)
    print(f"LLM-to-LLM fake calls — {len(personas)} persona(s), transport={args.transport}, "
          f"slots={args.slots}, build={BUILD}, caller={args.caller_model}")

    if not os.environ.get("OPENAI_API_KEY"):
        print("config error: OPENAI_API_KEY is required (caller LLM + agent model)")
        return 2
    if args.transport == "retell":
        if not os.environ.get("RETELL_API_KEY") or not os.environ.get("CHAT_AGENT_ID"):
            print("config error: --transport retell needs RETELL_API_KEY + CHAT_AGENT_ID")
            return 2

    kwargs: dict[str, Any] = {"langfuse_enabled": False}
    if args.agent_model:
        kwargs["openai_model"] = args.agent_model
    settings = Settings(**kwargs)          # .env supplies keys/model defaults

    # iter76 guardrail: under hybrid fire mode the lanes fire in the SESSION
    # layer (STT Update/EOT). The text lane has no session — without the
    # fire-sim the consume is a structural clean-fail every round
    # (owner-iter62: no late-restart injection) and rag_pull reads 0 served.
    # The battery launches that dropped --rag-fire-sim after iter63 are
    # exactly the runs that look "broken" (chat-iter68lite 0/71, iter70 0/66,
    # hil1-maria/susan 0/18 0/26). Auto-enable it for --rag unless the
    # operator explicitly opted out.
    if args.rag and not args.rag_fire_sim \
            and getattr(settings, "rag_fire_mode", "eot") == "hybrid":
        args.rag_fire_sim = True
        print("[iter76] --rag under hybrid fire mode: auto-enabling "
              "--rag-fire-sim (session-contract lane fires for the text lane). "
              "Opt out with --no-rag-fire-sim.")

    # iter65 T1: boot LLM prewarm — one throwaway completion through the
    # SHARED httpx pool at harness start (gated by the knob ONLY; battery
    # .env has no CALL_PREWARM). Rides the battery's single event loop.
    if getattr(settings, "llm_boot_prewarm", False):
        from diallux.graph.llm import llm_boot_prewarm
        _ms = await llm_boot_prewarm(settings)
        print(f"[iter65] llm boot prewarm {_ms} ms")

    http_client = None if args.slots == "live" else mock_client()
    if args.slots == "live":
        print("WARNING: --slots live hits the REAL slot server — book-livecall WILL "
              "create real bookings. Point RETELL_API_KEY/env at a test server.")

    results: list[dict] = []
    happy_failed = False
    for persona in personas:
        tracer = _build_tracer(persona, args.transport) if args.langfuse else None
        if args.transport == "graph":
            agent = GraphAgent(settings, http_client=http_client, tracer=tracer,
                               kb_store=... if args.rag else None,
                               warm_greeting_ms=args.warm_greeting_ms,
                               rag_fire_sim=args.rag_fire_sim,
                               fire_sim_tail_ms=args.fire_sim_tail_ms)
        else:
            agent = RetellAgent(os.environ["RETELL_API_KEY"], os.environ["CHAT_AGENT_ID"])
        caller = CallerLLM(model=args.caller_model)
        try:
            r = await run_persona(persona, agent, caller, max_turns=args.max_turns)
            if tracer:
                tracer.score("harness_result", 1.0 if r["pass"] else 0.0,
                             comment=(f"outcome={r['outcome']} turns={r['turns']} "
                                      f"gate_rejections={r.get('gate_rejections', 0)} "
                                      f"agent_model={args.agent_model or 'default'}"))
                tracer.finish(output={"persona": persona["name"], "outcome": r["outcome"],
                                      "pass": r["pass"], "tools": r["tools"],
                                      "final_dvs": r["final_dvs"]})
                # iter33 OTEL tail-loss fix: block until the tail export lands
                # server-side before the process moves on/exits. The census
                # check (server count vs locally-started observations) tells
                # "complete" apart from merely "quiescent"; the verdict lands
                # in the per-persona json_log so meters can disclose gaps.
                try:
                    wait = getattr(tracer, "wait_for_tail", None)
                    if getattr(tracer, "tail_ok", None) is not None:
                        tail_server = getattr(tracer, "tail_seen", -1)  # finish() already waited
                    elif callable(wait):
                        tail_server = wait()
                    else:
                        time.sleep(10)
                        tail_server = -1
                except Exception as exc:
                    print(f"[langfuse] tail-wait skipped ({type(exc).__name__}: {exc})")
                    tail_server = -1
                r["tail_expected"] = getattr(tracer, "_started", -1)
                r["tail_server"] = tail_server
                r["tail_complete"] = getattr(tracer, "tail_ok", None)
                try:
                    Path(r["log"]).write_text(json.dumps(r, indent=2, ensure_ascii=False, default=str))
                except Exception as exc:
                    print(f"[langfuse] tail log rewrite skipped ({type(exc).__name__}: {exc})")
            results.append(r)
        finally:
            await agent.aclose()
        if persona["type"] == "happy-path" and not r["pass"]:
            happy_failed = True

    if happy_failed and not args.no_happy_gate:
        stress_left = [p["name"] for p in personas if p["type"] != "happy-path"]
        if stress_left:
            print("\nHAPPY PATH FAILED — aborting before stress personas (SOP run order: "
                  "fix the agent first). Override with --no-happy-gate.")
            print(f"  skipped: {stress_left}")

    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(
        {"results": results, "transport": args.transport, "build": BUILD,
         "slots": args.slots, "caller_model": args.caller_model,
         "ts": datetime.now().isoformat(timespec="seconds")}, indent=2, default=str))

    print(f"\n{'=' * 70}\n  RESULTS ({BUILD} / {args.transport})\n{'=' * 70}")
    for r in results:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"  [{mark}] {r['persona'][:52]:52s} {r['outcome']:8s} turns={r['turns']:<3d} "
              f"ended={str(r['ended']):5s} tools={r['tools']}")
    ok = all(r["pass"] for r in results)
    print(f"\n{'ALL PASS' if ok else 'FAILURES PRESENT'} — report: {REPORT_PATH.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
