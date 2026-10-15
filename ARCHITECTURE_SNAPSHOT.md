# ARCHITECTURE SNAPSHOT — Diallux voice engine @ `bb6d216` (iter 114: mouth/brain split)

This repository is a read-only snapshot of a production inbound-sales voice agent's engine tree at
revision `bb6d216` (branch `engine/114-speech-auth`, the "iter 114 mouth/brain split" line).
It is published for technical research and review. The code is the source of truth; this file is the map.

## 1. System

Real PSTN inbound calls:

```
Twilio Media Streams (µ-law 8k, 20 ms frames)
  → Python/LangGraph engine (FastAPI)                     engine/diallux/
  → Deepgram flux STT (native EOT: eager 0.35 / eot 0.65 / timeout 3000 ms)
  → per-turn "round": TWO parallel chat-completions generations (see §2)
  → sentence-gated TTS flush (sentence granularity only — clause flushing rejected)
  → ElevenLabs turbo_v2_5 streaming → audio back to caller
```

Long-lived server-owned state variables (`dvs`: captured fields, computed figures, booking facts) are
patched by tool results; certain spoken values (computed money figures, calendar facts) are supposed to
originate only from `dvs`.

## 2. The split (what this build is)

Each heavy round runs **two concurrent generations on the same conversation context**:

- **SPEAK arm** — the "mouth": authors the spoken text. Its request carries NO real tools
  (`tools=[]`; speak twin routing per state via `speak_model_states`). Payload is a curated "diet"
  view: system head + conversation (tool frames stripped) + a compact "WHAT IS TRUE RIGHT NOW" card
  (`speak_diet`, `speak_card`). → sentence gate → sanitizer → dedupe → **render guard** → TTS writer.
- **TOOLS arm** — the "brain": real tool schema for the state; its streamed TEXT is discarded;
  its `tool_calls` (from the final event) are the ONLY tool source; `tool_call_start` fires the
  destination-state cache prewarm. Tool execution happens AFTER the stream completes (existing executor);
  results patch `dvs` and feed the NEXT round.

Merge is await-on-arrival at three points (no timeout caps in this revision):
**M1** — tools land while the turn is open → executed in-round, natural follow-up round.
**M2** — caller speaks first → next turn's ingest awaits the pending tools future before building.
**M3** — call close → pending future awaited/cancelled cleanly.

The **render guard** (`_speak_guard_cut`) authorizes spoken figures/times/booking-status claims against
dvs ∪ tool results in context ∪ the caller's own spoken words (regex + normalized value sets, zero LLM
calls, computed once per round); fail-closed: an unauthorized sentence is cut from speech, one short
recovery line plays; events are telemetry-only (never prompt context).

Code anchors (paths relative to `engine/`):

| Mechanic | Location |
|---|---|
| Background TOOLS stream | `diallux/graph/builder.py` `_fire_split_tools` ~L1106; M3 ~L1221 |
| SPEAK arm call site | `builder.py` ~L3164–3180 (`astream(speak_messages, [], …)`) |
| Speak diet view / WIT card | `builder.py` `_speak_view` ~L2427, `_speak_delta` ~L2415 |
| Per-state mouth model | `builder.py` `_speak_model_for` ~L2530; config `speak_model(_states)` |
| M1 adopt | `builder.py` ~L3456 |
| M2 next-turn ingest await | `builder.py` ~L3955 |
| Render guard | `builder.py` `_speak_guard_cut` ~L446; call sites ~L3255/L3388 |
| Ask-hold defer gate | `builder.py` `_DEFER_SET` ~L574; gate ~L3586 |
| Turn lifecycle / epochs / eager adopt-discard | `diallux/media/session.py` (`_on_eot`, `_on_eager_eot`, `_on_turn_resumed`) |
| Sentence gate (epoch laws) | `diallux/media/sentence_gate.py` |
| Speaker/tool twins, warm machinery | `diallux/graph/llm.py` (`StreamingLLM`, `speak_llm`, `warm`) |
| Switches | `diallux/config.py` (`speak_first_pipeline`, `speak_model`, `speak_model_states`, `speak_diet`, `speak_card`, `speak_fact_guard`, `derive_on_write`, `leak_normalize`, …) |
| Prompts / state machine | `diallux/prompts/*.md`, `agent/llm.json` (9-state, Retell-shaped) |

## 3. Metric identity (measured on production calls)

`e2e_response = ttft + B + C` (±40 ms), where ttft = confirmed EOT → first LLM token (838–972 ms,
prefill/queue dominated), B = first token → first complete sentence flushed to TTS (250 ms best /
279–478 typical / 779 ms median + 2.4–2.6 s stalls on the worst merged-stream call), C = TTS request →
first audio byte (175–228 ms, stable). Per-turn clock: `diallux/observability/latency.py` (`TurnClock`)
+ session turn reports.

## 4. Production evidence on this build

Owner-call forensics (`context/`):
- **1029 call** (first phone call on the split, trace `102a0c973152f409c7c615dd56e236ba`): e2eR p50
  1,488 / p90 2,355 ms. The split removed the merged-stream stall class; the remaining wounds:
  (a) **M2 await-on-arrival stalls dispatch** — worst turn 2,347 ms, consumed a future that returned
  `tool_calls=[]` (pure waste; the value-carrying future that day was consumed `already_done waited_ms=1`);
  (b) corrected leak inputs never re-derived (guard cut the corrected math; dvs stale → correct figure
  never spoken); (c) ask-hold slots-defer loop → 15 s dead air, un-booked.
- **0949 call** (previous line, merged stream): the F1 stall forensics that motivated the split.

## 5. State of review (round 2)

A prior research round validated the split and proposed amendments; the owner session that followed
settled them (see `context/SESSION_DECISIONS.md` — the authoritative decision record). In brief:
M0 instrumentation stamps **GO**; eager=speak-only **GO**; join budgets **amended to a never-drop
late-delivery policy with a ~1 s M2 cap, no M1 wait**; empty-speak **parked** (silence ≥ X ms AND no
tools in flight, review later); speak role tail **parked** (cache-safe, one tail line); parity/tripwire
tests **GO**; value-turn authority **settled as one policy** (engine owns values at money/slots/booking
turns; the missing re-derive loop is the open half).

## 6. Reading order for a researcher

1. This file.
2. `context/SESSION_DECISIONS.md` (settled decisions + exact semantics + open questions).
3. `context/10_phone_1029_report.md` + `context/03_phone_1029_latency.md` + `context/04_phone_1029_leak_math.md`.
4. `context/12_latency_forensics_0949.md` (the disease the split cured).
5. Code: `engine/diallux/graph/builder.py` (round loop), `media/session.py`, `graph/llm.py`, `config.py`, `media/sentence_gate.py`.
