#!/usr/bin/env python3
"""Maria-v2 conversational LLM caller over the REAL mic WS path.

Standalone driver: stdlib + websockets + openai + requests only, zero
engine imports (runs against ANY engine branch/box). Streams Cartesia
synthesized caller speech into ws://<engine>/mic/ws in 20ms linear16 16k
frames, HEARS the agent through a second Deepgram flux WS fed the agent
PCM the engine sends back, and PACES like a human: waits for the agent
to finish every turn, waits 5-15s between turns (sends NO frames during
the wait).

Scenarios: happy (full arc incl. forced-repeat + question-dump),
barge-in-only, dump-only, watchdog (zero-energy frames 12s).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import time
import urllib.parse

import requests
import websockets
from openai import AsyncOpenAI

FRAME_BYTES = 640
FRAME_SECS = 0.02
SAMPLE_RATE = 16000
DEFAULT_LOG = "/tmp/opencode/voice_server_8020.log"

PERSONA_PROMPT = (
    "You are Maria, the office manager at Bright Smile Dental, calling an AI "
    "voice agent that handles calls for a lighting/electrical business. "
    "You want a walkthrough of their service and to get a demo call booked. "
    "You are a real human caller on a phone call.\n"
    "PACING RULES (hard): you speak in SHORT bursts of one or two sentences "
    "maximum, like a person on a phone. You WAIT for the other side to finish "
    "speaking before you reply, and you take a natural pause (5-15 seconds) "
    "before speaking again. You never say more than two sentences in one turn "
    "unless told otherwise. You never invent outcomes: only react to what the "
    "agent actually said. You never mention being an AI or a test."
)

DUMP_QUESTIONS = (
    "Okay so I have a few questions real quick: how fast can someone come out? "
    "Do you handle the permits? What does it usually cost? And do you work "
    "weekends?"
)
REPEAT_PHRASE = "Sorry, I didn't catch that - could you say that part again?"
GOODBYE_PHRASE = "Perfect, that's everything I needed. Thanks so much, have a good one!"

PRICING_HINTS = ("$", "price", "cost", "per month", "quote", "pricing")
QUESTION_HINTS = ("question", "ask", "anything else")
BOOKED_HINTS = ("booked", "confirmed", "scheduled", "put you down", "see you on")

# iter82 T3 root cause (sid 05c90a2fc483, 2026-09-28): the persona goodbye
# matched loose BOOKED_HINTS over ALL agent turns - "booked" matched the
# agent's stock intake question "...getting more booked jobs" and the driver
# wrapped up the call at caller turn 3 (36s, zero bookings). Strong phrases
# only + last agent turn only (same pattern the scripted branch always had).
STRONG_BOOKED_HINTS = ("you're booked", "you are booked", "booked you",
                       "all set for", "locked in", "confirmation")


PACING_PROMPT = (
    "THE RULE: WAIT until the agent has completely finished speaking, then "
    "answer the EXACT question it just asked — nothing else. If the agent "
    "did not ask a question, reply with a natural short backchannel (like "
    "(yeah, go on / mhm) and wait. Never volunteer information the "
    "agent did not ask for, and never push your goal unprompted — let the "
    "conversation draw it out. You never invent outcomes: only react to "
    "what the agent actually said. You never mention being an AI."
)
# iter81 T3: deterministic happy-path script — transport-test instrument.
# Answer-first lines in fixed order; the agent's arc consumes them. The
# BOOKED_HINTS/turn-cap goodbye logic in run_happy still fires on top.
# Scripted happy path = three phases keyed to what the AGENT is asking, not a
# fixed turn index (the agent arc runs ~2 turns behind a fixed script; a fixed
# index dies at contact_details before booking). Discovery answers cycle; the
# contact/confirm phases advance on agent-question keywords.
CONTACT_HINTS = ("first name", "last name", "company name", "best contact number",
                 "time zone", "timezone", "calling from", "best number")
SLOTS_HINTS = ("later today", "tomorrow", "time slots", "which works", "time work",
               "do you have available", "slot")
# iter112 tooling: bare "confirm" removed — the agent's contact_details asks say
# "booking confirmation and reminders" (a NOUN), which latched phase=confirm
# before any slot talk and fed the false booked-goodbye (E4b 06:50). Question-
# shaped asks only.
CONFIRM_HINTS = ("is that correct", "correct?", "confirm that", "can you confirm",
                 "please confirm", "so i have", "got you down")
BOOK_CONFIRM_HINTS = ("book it", "book it.", "booked you", "you're booked",
                      "you are booked", "locked in")
SCRIPT_DISCOVERY = [
    "Hi, I saw your ad about missed calls. I run an auto repair shop and I lose calls after hours.",
    "About fifteen calls a week hit voicemail, mostly after hours or when we're on a job.",
    "Last month we missed a forty eight hundred dollar water heater job. The caller hired the shop that answered first.",
    "Roughly half of those callers would have booked with us.",
    "Average repair job is around four hundred fifty dollars. Big jobs run four to five thousand.",
    "We're a three-truck shop. My wife Diane tries to answer but she can't catch everything.",
    "If we don't call back fast they book somewhere else. That's the killer.",
    "Yeah, that math is worth a look. Show me how it works on my numbers first.",
]
SCRIPT_DISCOVERY_FILLER = [
    "Mostly new customers calling for service. My regulars know to text Diane.",
    "Yeah, that sounds about right.",
    "Go ahead, use those numbers.",
    "Sounds worth exploring to me.",
]
SCRIPT_CONTACT = [
    "I'm Danny Rourke, and the shop is Precision Auto Care.",
    "Best number is five one two, three one two, zero zero zero one. We're in Central time, Texas.",
    "Yes, that's all correct.",
]
SCRIPT_SLOTS = [
    "Tomorrow works better for the demo.",
    "Tomorrow at one pm works. Yes, book it.",
]
SCRIPT_GOODBYE = "No, that's everything. Thanks. — talk tomorrow at one."

# iter110 tooling: the agent offers REAL calendar times ("tomorrow at four PM
# or four thirty PM"); the scripted caller must pick one of ITS offers — the
# canned "one pm" line has no acceptance path against a calendar that lacks
# 1 PM (ConfirmSlots re-ask loop, zero bookings).
_TIME_WORD = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
              r"(?:\s+(?:thirty|fifteen|forty[\s-]?five|twenty|ten|forty))?"
              r"\s*(?:a\.?m\.?|p\.?m\.?)")
_TIME_DIGIT = r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)"
_TIME_RE = re.compile(rf"\b(?:{_TIME_WORD}|{_TIME_DIGIT})\b", re.IGNORECASE)


def _first_offered_time(text: str) -> str | None:
    # LAST match: the agent's negated lead ("I can't do one PM … openings are
    # four PM or four thirty PM") puts the REJECTED time first — the final
    # time mentioned is an acceptable offer in that shape.
    matches = _TIME_RE.findall(text or "")
    return matches[-1] if matches else None


_DAY_RE = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow)\b",
    re.IGNORECASE)


def _last_offered_day(text: str) -> str | None:
    """iter112 tooling: the day the agent LAST offered — the negated lead
    ("one PM tomorrow isn't open, but I can get you Friday at one PM") puts
    the REJECTED day first; the final day token is the real offer. Without
    this the scripted caller answered "Tomorrow at one pm works" against a
    FRIDAY offer → re-ask loop, no booking (E1 run 06:00–06:02)."""
    matches = _DAY_RE.findall(text or "")
    return matches[-1].lower() if matches else None

PERSONA_DIRECTIVE = (
    "Wait for the agent to finish, then answer the question it just asked in "
    "one short natural reply. Your goal stays in your persona — only use it "
    "when the agent's question calls for it. Never invent outcomes. Never "
    "mention being an AI or a test."
)
PERSONA_GOODBYE_DIRECTIVE = (
    "Wrap up and end the call naturally, one short sentence."
)


def load_persona(name: str) -> str:
    """iter78b: load a caller persona system prompt from tests/llm2llm/personas.py."""
    import importlib.util
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    spec = importlib.util.spec_from_file_location(
        "mic_personas", os.path.join(root, "tests", "llm2llm", "personas.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    matches = [p for p in mod.PERSONAS if name.lower() in p["name"].lower()]
    if not matches:
        gks = ", ".join(p["name"].split(")")[0][1:] for p in mod.PERSONAS
                        if p["name"].startswith("(GK)"))
        raise SystemExit(f"no persona matches {name!r}; available GKs: {gks}")
    return matches[0]["system"] + "\n\n" + PACING_PROMPT


def load_env(path: str):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def chunk_frames(pcm: bytes, size: int = FRAME_BYTES) -> list[bytes]:
    return [pcm[i:i + size] for i in range(0, len(pcm), size)]


def zero_pcm(seconds: float) -> bytes:
    return b"\x00" * (int(seconds * SAMPLE_RATE) * 2)


def agent_idle(last_audio_ts: float | None, now: float, idle_s: float) -> bool:
    return last_audio_ts is not None and (now - last_audio_ts) >= idle_s


class CartesiaCallerTTS:
    URL = "https://api.cartesia.ai/tts/bytes"

    def __init__(self, api_key: str, model_id: str, voice_id: str):
        self.api_key = api_key
        self.model_id = model_id
        self.voice_id = voice_id
        self.output_format = {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": SAMPLE_RATE,
        }

    def synth(self, text: str) -> bytes:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = requests.post(
                    self.URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Cartesia-Version": "2025-04-16",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model_id": self.model_id,
                        "transcript": text,
                        "voice": self.voice_id,
                        "language": "en",
                        "output_format": self.output_format,
                    },
                    timeout=30,
                )
                if r.status_code == 200:
                    return r.content
                last_exc = RuntimeError(f"cartesia {r.status_code}: {r.text[:200]}")
            except Exception as exc:
                last_exc = exc
            time.sleep(0.5)
        raise RuntimeError(f"cartesia synth failed after retries: {last_exc}")


class AgentEar:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.ws = None
        self.turns: list[str] = []
        self.updates: list[str] = []
        self.speaking = False
        self.last_feed_ts: float | None = None

    def _flush_turn(self):
        """iter78b: flux EndOfTurn is too slow for TTS streaming - finalize the
        open turn driver-side on an audio-idle gap (same threshold as turn-end
        wait). Duplicates the flux EndOfTurn append."""
        if self.updates and (not self.turns or self.updates[-1] != self.turns[-1]):
            self.turns.append(self.updates[-1])

    def _url(self) -> str:
        params = urllib.parse.urlencode({
            "model": "flux-general-en",
            "encoding": "linear16",
            "sample_rate": str(SAMPLE_RATE),
        })
        return "wss://api.deepgram.com/v2/listen?" + params

    async def start(self):
        headers = {"Authorization": f"Token {self.api_key}"}
        self.ws = await websockets.connect(
            self._url(), additional_headers=headers, max_size=None, open_timeout=10
        )
        asyncio.create_task(self._recv_loop())
        asyncio.create_task(self._keepalive())

    async def _keepalive(self):
        """iter78b: the caller's 5-15s human gaps starve the flux WS (idle
        timeout = silent death mid-call) - drip 100ms of silence every 3s."""
        silence = zero_pcm(0.1)
        while self.ws is not None:
            try:
                await self.ws.send(silence)
            except Exception:
                self.ws = None
                return
            await asyncio.sleep(3)

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                if msg.get("type") != "TurnInfo":
                    continue
                event = msg.get("event")
                transcript = (msg.get("transcript") or "").strip()
                if event == "StartOfTurn":
                    self.speaking = True
                elif event == "EndOfTurn" and transcript:
                    self.speaking = False
                    self.turns.append(transcript)
                elif event == "Update" and transcript:
                    self.updates.append(transcript)
        except Exception as exc:
            print(f"[ear] deepgram stream error: {exc}", flush=True)
        finally:
            self.ws = None

    async def feed(self, pcm: bytes):
        if self.ws is None:
            try:
                await self.start()
                print("[ear] deepgram reconnected", flush=True)
            except Exception:
                return
        now = time.monotonic()
        if self.last_feed_ts is not None and now - self.last_feed_ts > 1.2:
            self._flush_turn()
        self.last_feed_ts = now
        if self.ws is not None:
            try:
                await self.ws.send(pcm)
            except Exception:
                self.ws = None


class MicDriver:
    def __init__(self, ws_url: str, ear: AgentEar,
                 custom_parameters: dict | None = None):
        self.ws_url = ws_url
        self.ear = ear
        self.custom_parameters = custom_parameters or {}
        self.ws = None
        self.agent_audio_bytes = 0
        self.agent_first_audio_ts: float | None = None
        self.last_agent_audio_ts: float | None = None
        self.events: list[dict] = []
        self.open = False
        self._speaking = False

    def _stamp(self, kind: str, **detail):
        self.events.append({"t": time.time(), "mono": time.monotonic(), "kind": kind, **detail})

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=None, open_timeout=10)
        self.open = True
        await self.ws.send(json.dumps({"event": "start",
                                       "customParameters": self.custom_parameters}))
        self._stamp("start_sent", callback_number=self.custom_parameters.get("callback_number", ""))
        asyncio.create_task(self._recv_loop())
        asyncio.create_task(self._room_tone())

    async def _room_tone(self):
        """Continuous silence frames while not speaking - keeps BOTH the
        engine's flux VAD fed (trailing silence -> EOT commits) and the WS
        alive. Real mics stream always; the fake caller must too."""
        frame = zero_pcm(FRAME_SECS)
        while self.open:
            if not self._speaking:
                try:
                    await self.ws.send(frame)
                except Exception:
                    return
            await asyncio.sleep(FRAME_SECS)

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                if isinstance(raw, (bytes, bytearray)):
                    now = time.monotonic()
                    b = bytes(raw)
                    self.agent_audio_bytes += len(b)
                    if self.agent_first_audio_ts is None:
                        self.agent_first_audio_ts = now
                        self._stamp("agent_first_audio")
                    self.last_agent_audio_ts = now
                    await self.ear.feed(b)
                else:
                    self._stamp("server_text", text=str(raw)[:200])
        except websockets.ConnectionClosed:
            pass
        except Exception as exc:
            self._stamp("recv_error", error=str(exc))
        finally:
            self.open = False
            self._stamp("ws_closed")

    async def stream_pcm(self, pcm: bytes):
        self._stamp("caller_stream_start", bytes=len(pcm))
        self._speaking = True
        try:
            for frame in chunk_frames(pcm):
                await self.ws.send(frame)
                await asyncio.sleep(FRAME_SECS)
            # 300ms deterministic terminator: 15 x 20ms zero frames
            tail = zero_pcm(0.3)
            for frame in chunk_frames(tail):
                await self.ws.send(frame)
                await asyncio.sleep(FRAME_SECS)
        except websockets.ConnectionClosed:
            # iter114d (F-114-3/4): the engine closed the call mid-send
            # (end_call -> 1000 OK). Stop quietly; the journal + --out
            # JSON must still land.
            self.open = False
            self._stamp("ws_closed")
            return
        finally:
            self._speaking = False
        self._stamp("caller_stream_end")

    async def close(self):
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps({"event": "stop"}))
        except Exception:
            pass
        try:
            await self.ws.close()
        except Exception:
            pass


class CallerBrain:
    def __init__(self, api_key: str, model: str, prompt: str | None = None,
                 base_url: str | None = None):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.prompt = prompt or PERSONA_PROMPT

    async def reply(self, conversation: list[dict], directive: str) -> str:
        # plain-text format = the chat-harness shape gpt-4o-mini already
        # books with (BOOK Danny 26 turns 2026-09-28); JSON blobs bury the
        # agent's question and the small model answers the directive instead.
        lines = []
        for m in conversation[-12:]:
            who = "Agent" if m["role"] == "agent" else "Customer"
            lines.append(f"{who}: {m['text']}")
        lines.append('Agent said the line above. ' + directive)
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.prompt},
                {"role": "user", "content": chr(10).join(lines)},
            ],
            max_completion_tokens=120,
        )
        return (resp.choices[0].message.content or "").strip()


class RunState:
    def __init__(self, args):
        self.args = args
        self.t0 = time.monotonic()
        self.caller_turns: list[dict] = []
        self.repeat_used = False
        self.dump_used = False
        self.goodbye_done = False
        self.stop_reason = "max_turns"
        self.script = {"phase": "discovery", "di": 0, "ci": 0, "si": 0,
                       "contact_done": False}

    def elapsed(self) -> float:
        return time.monotonic() - self.t0


def conversation_pairs(agent_turns: list[str], caller_turns: list[dict]) -> list[dict]:
    # chronological interleave: each caller turn answers the agent turn
    # before it (block-appending all-agent-then-all-caller mangled the
    # transcript and the small model answered its OWN last line instead).
    out = []
    for i, t in enumerate(agent_turns):
        out.append({"role": "agent", "text": t})
        if i < len(caller_turns):
            out.append({"role": "caller", "text": caller_turns[i]["text"]})
    return out


async def wait_agent_turn_end(driver: MicDriver, state: RunState):
    idle = state.args.agent_idle_s
    while driver.open:
        now = time.monotonic()
        if state.elapsed() > state.args.max_run_s:
            state.stop_reason = "max_run_s"
            return False
        last = driver.last_agent_audio_ts
        if agent_idle(last, now, idle) and driver.agent_first_audio_ts is not None:
            return True
        if last is None and (now - state.t0) > state.args.max_turn_wait:
            state.stop_reason = "agent_never_spoke"
            return False
        await asyncio.sleep(0.1)
    return False


async def human_gap(state: RunState, min_gap: float, max_gap: float) -> float:
    gap = random.uniform(min_gap, max_gap)
    await asyncio.sleep(gap)
    return gap


async def speak(driver: MicDriver, tts: CartesiaCallerTTS, state: RunState,
                ear: AgentEar, text: str, gap: float | None):
    entry = {"text": text, "gap_before_s": gap, "agent_turns_heard": len(ear.turns)}
    t_gen = time.monotonic()
    pcm = await asyncio.to_thread(tts.synth, text)
    entry["tts_ms"] = int((time.monotonic() - t_gen) * 1000)
    entry["audio_bytes"] = len(pcm)
    entry["dur_s"] = round(len(pcm) / (SAMPLE_RATE * 2), 2)
    await driver.stream_pcm(pcm)
    state.caller_turns.append(entry)


async def run_happy(driver, tts, brain, state, ear):
    args = state.args
    await asyncio.sleep(2.0)
    ok = await wait_agent_turn_end(driver, state)
    if not ok:
        return
    agent_turns = []
    persona_mode = bool(getattr(args, "persona", ""))
    while driver.open and state.stop_reason == "max_turns":
        if state.elapsed() > args.max_run_s:
            state.stop_reason = "max_run_s"
            break
        last_agent = ear.turns[-1] if ear.turns else ""
        agent_turns = list(ear.turns)
        if getattr(args, "scripted", False):
            agent_text = " ".join(agent_turns).lower()
            # iter110 tooling fix (iter82 T3 class, scripted lane): question-
            # keyed matching MUST use the agent's LAST turn only — matching the
            # accumulated transcript latched `CONFIRM_HINTS`/"confirmation" from
            # old turns ('best number for the text confirmation' during
            # contact_details) and looped ConfirmSlots forever (no booking).
            last_text = (agent_turns[-1] if agent_turns else "").lower()
            sc = state.script
            if state.goodbye_done:
                break
            # iter112 tooling: booking-COMPLETION phrases only. "confirmation"
            # alone is a contact_details noun ("text confirmation") and
            # "all set for" is the contact_details ACK ("You're all set for
            # the walk through") — both false-fired booked_goodbye mid-contact
            # with NO booking (E2 06:16, E4b 06:50). The real closing line
            # (T2 base run) is "Glad we got that set… confirmation text… on
            # the way".
            strong_booked = any(h in last_text for h in
                                ("you're booked", "you are booked", "booked you",
                                 "locked in", "got that set", "got you set",
                                 "confirmation text", "confirmation email",
                                 "confirmation is on", "confirmation's on",
                                 "you are confirmed", "you're confirmed",
                                 "get a confirmation", "confirmation with all"))
            if strong_booked and sc["phase"] in ("slots", "confirm"):
                text = SCRIPT_GOODBYE
                state.goodbye_done = True
                state.stop_reason = "booked_goodbye"
            else:
                if sc["phase"] == "contact" and sc["ci"] < len(SCRIPT_CONTACT):
                    text = SCRIPT_CONTACT[sc["ci"]]
                    sc["ci"] += 1
                    if sc["ci"] >= len(SCRIPT_CONTACT):
                        sc["phase"] = "slots"
                        sc["si"] = 0
                        sc["contact_done"] = True
                elif not sc.get("contact_done") and any(h in last_text for h in CONTACT_HINTS):
                    # iter112 tooling: contact data (name/number) MUST be given
                    # before confirm/slot logic locks in — the gpt-4.1 phrasing
                    # hit CONFIRM_HINTS ("just to confirm…") during Offer and
                    # SLOTS_HINTS early, latching phase=slots and never saying
                    # the name → 12-turn contact_details re-ask loop, no
                    # booking (E2 run 06:14–06:17).
                    sc["phase"] = "contact"
                    text = SCRIPT_CONTACT[0]
                    sc["ci"] = 1
                elif any(h in last_text for h in CONFIRM_HINTS):
                    # agent is re-confirming the chosen slot: answer yes, then book
                    text = "Yes, that's correct. Go ahead and book it."
                    sc["phase"] = "confirm"
                elif sc["phase"] == "slots" and sc["si"] < len(SCRIPT_SLOTS):
                    offered = _first_offered_time(last_text)
                    if offered and sc["si"] >= 1:
                        # the agent offered its own times — take ITS LAST one,
                        # and echo the day the agent actually offered (never a
                        # hardcoded "Tomorrow" — the E1 Friday-offer loop)
                        text = f"{_last_offered_day(last_text) or 'Tomorrow'} at {offered} works. Yes, book it."
                        sc["si"] = len(SCRIPT_SLOTS)
                    else:
                        text = SCRIPT_SLOTS[sc["si"]]
                        sc["si"] += 1
                elif sc["phase"] == "slots" or (sc.get("contact_done") and any(h in last_text for h in SLOTS_HINTS)):
                    sc["phase"] = "slots"
                    sc["si"] = sc.get("si", 0)
                    offered = _first_offered_time(last_text)
                    if offered and sc["si"] >= len(SCRIPT_SLOTS):
                        text = f"{_last_offered_day(last_text) or 'Tomorrow'} at {offered} works. Yes, book it."
                    else:
                        text = SCRIPT_SLOTS[sc["si"]] if sc["si"] < len(SCRIPT_SLOTS)                         else SCRIPT_SLOTS[-1]
                        sc["si"] = min(sc["si"] + 1, len(SCRIPT_SLOTS))
                elif any(h in last_text for h in CONTACT_HINTS):
                    # contact data already provided — this is a verification
                    # ask ("is that the best number?") → confirm it
                    sc["phase"] = "contact"
                    text = SCRIPT_CONTACT[-1]        # "Yes, that's all correct."
                    sc["ci"] = len(SCRIPT_CONTACT)
                else:
                    if sc["di"] < len(SCRIPT_DISCOVERY):
                        text = SCRIPT_DISCOVERY[sc["di"]]
                    else:
                        text = SCRIPT_DISCOVERY_FILLER[sc["di"] % len(SCRIPT_DISCOVERY_FILLER)]
                    sc["di"] += 1
            gap = await human_gap(state, args.min_gap, args.max_gap)
            await speak(driver, tts, state, ear, text, gap)
            if state.goodbye_done:
                await wait_agent_turn_end(driver, state)
                break
            ok = await wait_agent_turn_end(driver, state)
            if not ok:
                break
            continue
        text = None
        directive = PERSONA_DIRECTIVE if persona_mode \
            else "reply naturally as Maria in one short burst"
        if not persona_mode:
            text = REPEAT_PHRASE if (state.repeat_used is False and any(
                h in last_agent for h in PRICING_HINTS) and len(ear.turns) >= 2
            ) else None
            if text:
                state.repeat_used = True
                directive = "say exactly the given line"
            elif state.dump_used is False and any(h in last_agent for h in QUESTION_HINTS):
                text = DUMP_QUESTIONS
                state.dump_used = True
                directive = "ask exactly these 4 questions in one breath"
        elif any(h in last_agent.lower() for h in STRONG_BOOKED_HINTS) \
                and not state.goodbye_done and len(state.caller_turns) >= 2:
            state.goodbye_done = True
            state.stop_reason = "booked_goodbye"
            matched = next(h for h in STRONG_BOOKED_HINTS if h in last_agent.lower())
            state.goodbye_trigger = {"phrase": matched,
                                     "agent_turn_idx": len(agent_turns) - 1}
            text = None
            directive = PERSONA_GOODBYE_DIRECTIVE
        if text is None and not persona_mode:
            if any(h in " ".join(agent_turns).lower() for h in BOOKED_HINTS) \
                    and not state.goodbye_done and len(state.caller_turns) >= 2:
                text = GOODBYE_PHRASE
                state.goodbye_done = True
                state.stop_reason = "booked_goodbye"
                directive = "say goodbye exactly like this"
            elif len(state.caller_turns) >= args.max_caller_turns:
                text = GOODBYE_PHRASE
                state.goodbye_done = True
                state.stop_reason = "turn_cap_goodbye"
                directive = "say goodbye exactly like this"
        if persona_mode and len(state.caller_turns) >= args.max_caller_turns \
                and not state.goodbye_done:
            state.goodbye_done = True
            state.stop_reason = "turn_cap_goodbye"
            text = None
            directive = PERSONA_GOODBYE_DIRECTIVE
        gap = await human_gap(state, args.min_gap, args.max_gap)
        if text is None:
            try:
                text = await asyncio.wait_for(
                    brain.reply(conversation_pairs(agent_turns, state.caller_turns),
                                directive), timeout=30)
            except Exception as exc:
                state.stop_reason = f"brain_error: {exc}"
                break
        await speak(driver, tts, state, ear, text, gap)
        if state.goodbye_done:
            await wait_agent_turn_end(driver, state)
            break
        ok = await wait_agent_turn_end(driver, state)
        if not ok:
            break


async def run_barge_in_only(driver, tts, state, ear):
    while driver.open:
        now = time.monotonic()
        if driver.last_agent_audio_ts and not agent_idle(
                driver.last_agent_audio_ts, now, 0.3) and driver.agent_audio_bytes > 0:
            break
        if state.elapsed() > state.args.max_turn_wait:
            state.stop_reason = "no_agent_speech"
            return
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)
    if driver.open:
        await speak(driver, tts, state, ear, "Oh hold on, one second- sorry go ahead.", None)
        state.stop_reason = "barged_in"


async def run_dump_only(driver, tts, brain, state, ear):
    ok = await wait_agent_turn_end(driver, state)
    if not ok:
        return
    gap = await human_gap(state, state.args.min_gap, state.args.max_gap)
    await speak(driver, tts, state, ear, DUMP_QUESTIONS, gap)
    state.dump_used = True
    state.stop_reason = "dumped"
    await wait_agent_turn_end(driver, state)


async def run_watchdog(driver, tts, state, ear):
    ok = await wait_agent_turn_end(driver, state)
    if not ok:
        return
    await asyncio.sleep(2.0)
    await driver.stream_pcm(zero_pcm(12.0))
    state.stop_reason = "watchdog_silence_sent"
    await wait_agent_turn_end(driver, state)


async def main_async(args):
    load_env(args.env)
    api_key = os.environ["CARTESIA_API_KEY"]
    model_id = os.environ["CARTESIA_MODEL_ID"]
    voice_id = args.voice_id or os.environ["CARTESIA_VOICE_ID"]
    dg_key = os.environ["DEEPGRAM_API_KEY"]
    oa_key = os.environ["OPENAI_API_KEY"]
    model = args.caller_model or os.environ.get("OPENAI_MODEL", "gpt-5.4")
    args.persona_prompt = load_persona(args.persona) if args.persona else None

    journal_start = 0
    if os.path.exists(args.journal_log):
        journal_start = os.path.getsize(args.journal_log)

    tts = CartesiaCallerTTS(api_key, model_id, voice_id)
    ear = AgentEar(dg_key)
    brain = CallerBrain(oa_key, model, prompt=args.persona_prompt)
    driver = MicDriver(args.ws_url, ear,
                       custom_parameters={"callback_number": args.callback_number})
    state = RunState(args)

    await ear.start()
    await driver.connect()
    try:
        try:
            if args.scenario == "happy":
                await run_happy(driver, tts, brain, state, ear)
            elif args.scenario == "barge-in-only":
                await run_barge_in_only(driver, tts, state, ear)
            elif args.scenario == "dump-only":
                await run_dump_only(driver, tts, brain, state, ear)
            elif args.scenario == "watchdog":
                await run_watchdog(driver, tts, state, ear)
        except websockets.ConnectionClosed:
            # iter114d (F-114-3/4): engine closed the call (end_call) —
            # not a driver failure. Fall through so evidence + --out land.
            state.stop_reason = "ws_closed"
    finally:
        await asyncio.sleep(1.0)
        await driver.close()
        try:
            if ear.ws is not None:
                await ear.ws.close()
        except Exception:
            pass

    journal = []
    if os.path.exists(args.journal_log):
        with open(args.journal_log, "rb") as f:
            f.seek(journal_start)
            journal = f.read().decode(errors="replace").splitlines()[-400:]

    evidence = {
        "scenario": args.scenario,
        "persona": args.persona or None,
        "ws_url": args.ws_url.split("k=")[0] + "k=<redacted>",
        "caller_model": model,
        "started_wall": time.time(),
        "events": driver.events,
        "agent_turns": ear.turns,
        "agent_updates_tail": ear.updates[-5:],
        "caller_turns": state.caller_turns,
        "agent_audio_bytes": driver.agent_audio_bytes,
        "stop_reason": state.stop_reason,
        "goodbye_trigger": getattr(state, "goodbye_trigger", None),
        "journal_lines": journal,
        "elapsed_s": round(state.elapsed(), 2),
    }
    with open(args.out, "w") as f:
        json.dump(evidence, f, indent=2, default=str)
    print(json.dumps({
        "out": args.out, "scenario": args.scenario,
        "agent_turns": len(ear.turns), "caller_turns": len(state.caller_turns),
        "stop_reason": state.stop_reason,
    }))
    return evidence


def build_parser():
    p = argparse.ArgumentParser(description="iter76 LLM mic caller")
    p.add_argument("--ws-url", required=True)
    p.add_argument("--scenario", choices=["happy", "barge-in-only", "dump-only", "watchdog"],
                   default="happy")
    p.add_argument("--min-gap", type=float, default=5.0)
    p.add_argument("--max-gap", type=float, default=15.0)
    p.add_argument("--agent-idle-s", type=float, default=1.5)
    p.add_argument("--max-turn-wait", type=float, default=30.0)
    p.add_argument("--scripted", action="store_true",
                   help="deterministic SCRIPT_HAPPY lines instead of the LLM caller brain")
    p.add_argument("--caller-model", default="gpt-4o-mini")
    p.add_argument("--persona", default="",
                   help="registry persona name (e.g. Sam, Priya) - persona mode")
    p.add_argument("--voice-id", default="",
                   help="override CARTESIA_VOICE_ID for the CALLER voice only"),
    p.add_argument("--max-run-s", type=float, default=600.0)
    p.add_argument("--max-caller-turns", type=int, default=12)
    p.add_argument("--journal-log", default=DEFAULT_LOG)
    p.add_argument("--callback-number", default="+15123120001",
                   help="sent as customParameters.callback_number at start "
                        "( Danny persona number; mic page + chat lane parity)")
    p.add_argument("--env", default=os.path.join(os.path.dirname(__file__), "..", ".env"))
    p.add_argument("--out", required=True)
    return p


def main():
    args = build_parser().parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
