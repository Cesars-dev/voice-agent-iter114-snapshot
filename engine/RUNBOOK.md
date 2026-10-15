# RUNBOOK.md — operating the agent

## Daily signals (60 seconds)

```bash
curl -s https://calls.your-domain.com/health
curl -s https://calls.your-domain.com/metrics | grep -E \
  'diallux_calls_total|diallux_bookings_total|diallux_turn_e2e_response_ms_count|diallux_bargeins'
docker compose logs app --since 10m | grep -E "turn .* report|barge|gate" | tail
```

In Langfuse: sessions `diallux-call-*`; check `turn:N` spans'
`e2e_response_ms` and the `diallux:*` generation latencies.

## Symptoms -> causes -> fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| Caller hears nothing at start | Cartesia key/credits or WS blocked | `docker compose logs app \| grep cartesia`; check credits; test `scripts/list_cartesia_voices.py` |
| Turns feel slow (>1500ms) | see which stage: Langfuse turn span `stt_eot_to_llm_first` vs `llm_first_to_tts_first` | high first = model choice or DEEPGRAM_EOT_THRESHOLD too high; high second = sentence gate/Cartesia TTFB — try `CARTESIA_BUFFERING=managed` |
| Agent keeps asking the same thing | repeat guard hit a genuine loop | Langfuse tool spans; check `already_captured` nudges; usually a prompt regression |
| `diallux_gate_rejections_total` climbing | model trying to skip gates | read the rejections list on the call's root trace; check the state prompt edits |
| Booking fails honestly ("calendar trouble") | slots.diallux-ai.site or Cal.com down | that's YOUR VPS pipeline — check its own services (out of scope here, per ENDPOINTS.md) |
| `twilio stream warning: 31931` | downstream buffer overflow — audio arriving faster than playback | rare (only when caller's network stalls); traced, self-clears; if frequent, check outbound frame pacing |
| Call drops mid-conversation | Deepgram drop + failed reconnect | `grep stt:reconnect`; Deepgram status page; call state is preserved (checkpointer) but the audio path needs a new call |

## Tuning knobs (all env, restart to apply)

| Knob | Default | Meaning |
|---|---|---|
| `DEEPGRAM_EOT_THRESHOLD` | 0.7 | lower (0.5) = faster replies, more false ends; higher (0.9) = fewer interrupts |
| `DEEPGRAM_EAGER` | false | speculative LLM start (sub-second path; extra LLM cost) |
| `CARTESIA_BUFFERING` | custom | `managed` lets Cartesia decide chunk boundaries |
| `OPENAI_MODEL` | gpt-5.2 | keep the measured config (no temperature/effort) unless re-measured |
| `HANGUP_MODE` | mark | `timer` for the V1 behavior |
| `DEEPGRAM_MODE` | flux | `nova3` = classic endpointing path |
| `LANGGRAPH_CHECKPOINT` | memory | `postgres` for cross-restart durability |

## Test personas (acceptance ladder parity)

Use `scripts/fake_twilio_call.py` with persona WAVs, or `chat_repl.py` for the
brain only. The LLM-simulated prospect ladder from your lab
(`run_ladder.py`) ports unchanged — point it at the graph's chat entry (same
`SDREngine.chat()` contract exposed by the REPL).

## Changing prompts safely

1. Edit `diallux/prompts/<State>.md` (source of truth)
2. `python -m pytest tests/ -q` (graph + gates still green)
3. Restart: `docker compose up -d --force-recreate app`
4. One fake call before going back live

---

# V3 runbook additions

## Eval gates (before ANY prompt or KB change ships)

```bash
.venv/bin/python -m pytest tests/ -q              # 53 unit tests
.venv/bin/python scripts/eval_accuracy.py         # 5 scenarios, offline
.venv/bin/python scripts/eval_accuracy.py --live  # real model + latency report
```
Failure = exit 1: CI-able. `eval/report.json` keeps per-check results
(state, dvs, tools, gate_blocks, KB hits, spoken-text normalization).

## KB edits

Edit `agent/knowledge_bases/*.md` → re-index → eval:
```bash
docker compose exec app python scripts/rag_ingest.py
.venv/bin/python scripts/eval_accuracy.py --live
```
(No restart needed for new calls; the store re-checks the index lazily.)
Retrieval quality check: Langfuse `rag` spans — which `kbs`/`chunks` fire per
turn; if the wrong KB wins, the chunk text needs sharpening, not the code.

## TTS tuning

| Symptom | Fix |
|---|---|
| Numbers read too fast still | `TTS_PHONE_STYLE=digits` (default) is already comma-separated; if the MODEL writes them fast, the normalizer is being bypassed — check `TTS_NORMALIZE=true` |
| Voice too slow/brisk | `CARTESIA_SPEED=1.05` (0.6–1.5) or `ELEVENLABS_SPEED=1.05` (0.7–1.2) |
| Want more warmth | `CARTESIA_EMOTION=calm` (beta, English only, best on emotive-tagged voices — see docs) or Chloe style knobs `ELEVENLABS_STYLE` |
| Chloe for client demos | `TTS_PROVIDER=elevenlabs` + voice id (DECISIONS.md D11 for costs) |
| RAG adds latency you don't want on a fast path | `RAG_MODE=inline` (back to full-KB prompts) — or accept ~100ms for the grounding |

## RAG health

- `/health` reports `rag_mode`; Langfuse `rag` span has `ms` + `kbs`.
- pgvector down → automatic inline fallback + one warning log per minute max;
  call quality (grounding) degrades, latency does not.
- `docker compose logs app | grep "rag"` shows fallback and index-empty states.
