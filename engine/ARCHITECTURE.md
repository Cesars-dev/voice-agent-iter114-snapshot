# ARCHITECTURE.md — V2 production graph

Same audio/data plane as V1 (see that package's ARCHITECTURE.md); this
documents the V2 graph changes.

## The graph

```
                          START
                            │
                            ▼
                        ┌───────┐
                        │ingest │  appends user msg, resets round budget (12)
                        └───┬───┘
                  route_state│
        ┌────────────────────┼────────────────────────┐
        ▼ (9 state nodes — one per Retell state)      │
   ┌──────────────────────────────────────────┐       │
   │  system = general_prompt + state prompt  │       │
   │  (from diallux/prompts/*.md, {{dv}}+KBs) │       │
   │  V3: RAG top-3 KB excerpts per turn      │       │
   │      (pgvector; inline fallback)         │       │
   │  V3: += VOICE OUTPUT RULES               │       │
   │  stream LLM ── tts_token ──► SentenceGate│       │
   │  execute tools (HARD-GATED transitions)  │       │
   └──────────────────┬───────────────────────┘       │
        after_state   │ pure speech → finalize       │
                      │ ended / rounds=0 → finalize  │
                      │ else → deterministic         │
                      ▼                               │
              ┌──────────────────┐                    │
              │  deterministic   │  leak math (if inputs present & empty)
              │  (no LLM node)   │  /today prefetch (ConfirmSlots, empty date)
              └────────┬─────────┘                    │
                route_state ─────────────────────────┘
                      │
                      ▼
                  ┌───────┐
                  │finalize│
                  └───┬───┘
                      ▼
                     END
```

## Hard gates (the executor, not the prompt)

`transition_to_X` executes in Python:

```
edge exists?         no  → {"status":"invalid_transition"}   (topology enforced)
required params all truthy in dvs?
                     no  → {"status":"gate_failed","missing":[...]}  (stay & finish)
                     yes → state_name = X  (next round swaps prompt+tools)
```

The gate booleans (`slot_verified`, `data_verified`, `phone_confirmed`,
`booking_verified`) are `SERVER_OWNED_DVS` — extract tools physically cannot
write them; only webhook `response_variables` (or the deterministic node) can.
A gate opens because the production endpoint said so, or not at all.

## State (typed)

`GraphState` (same shape as V1) but `dvs` seeds from
`schema.DynamicVariables`:
41 typed fields, digit-string→int coercion, unknown keys dropped, `None`→
typed default. The Retell null-dv class cannot occur; no verification tool
needed.

## Session additions

| Feature | Mechanism |
|---|---|
| Playback-confirmed hangup | `mark` frame after the turn's audio; Twilio echoes it when PLAYBACK finishes; hangup waits for the echo (timer fallback) |
| STT auto-reconnect | Deepgram recv-loop death → new STT socket, call continues; `stt:reconnect` span |
| Eager EOT (optional) | `EagerEndOfTurn` starts the turn speculatively; `TurnResumed` cancels (gate reset + TTS cancel + Twilio clear) |
| Buffer watchdog | Twilio `warning` events (31931 etc.) logged + traced |
| Metrics | every turn report → Prometheus histograms; calls/bookings/barge-ins/gate-rejections counters |

## Observability additions (Langfuse)

Everything from V1, plus:
- `deterministic:leak_math` / `deterministic:today_prefetch` spans
- `gate_rejections` array on the root trace (every refused transition, with
  the exact missing fields)
- `stt:reconnect` and `twilio:warning` spans

## Failure modes (V2 deltas)

| Failure | Behavior |
|---|---|
| Model tries a fake gate | write rejected, transition refused with actionable message; counted in metrics |
| Model tries a jump transition | `invalid_transition`, stays on the path |
| Deepgram drops mid-call | transparent reconnect |
| Slow goodbye at hangup | mark-based — full playback before close |
| slots VPS 5xx/timeout | retry w/ backoff, then honest failure (no dvs write) |

---

# V3 additions

## Media pipeline (provider-agnostic speak path, V5: + delivery profile)

```
tts_token stream ──► SentenceGate (sentence-chunked, ordered)
                        │ flushed sentence
                        ▼
              normalize_for_tts()            # digits/commas, markdown/emoji strip
                        │
                        ▼
              resolve_delivery(settings, turn_state)   # V5: per-state profile
                        │  (dict lookup — emotion/speed from the graph state,
                        │   clamped to API ranges; None = .env globals only)
                        ▼
        TTS: create_tts(settings)            # env switch
             ├─ CartesiaTTS   (default): contexts + continue/cancel/flush,
             │   generation_config {speed, volume, emotion} per request
             │   (profile merged over .env globals)
             └─ ElevenLabsTTS (optional): stream-input WS, ulaw_8000,
                 voice_settings (Chloe knobs) on the first message of each
                 turn's generation (profile merged over .env base), flush via
                 {"text": ""}, barge-in = dropped-context + flush
                        │ base64 mulaw/ulaw @ 8000 — Twilio passthrough
                        ▼
                 Twilio media frames
```

The turn's profile is the state whose PROMPT generated the speech:
`CallSession` snapshots `state_name` at turn end (the same read the hangup
check performs), so turn N voices with turn N's starting state and
transitions take effect from the next turn — mirroring Retell's per-state
prompt semantics. The begin_message uses the `begin` profile.

## RAG retrieval (per state-node round)

```
state_node:
  system = general_prompt + state_prompt (from diallux/prompts/, substituted)
  if KBStore ready (RAG_MODE=auto + pgvector indexed):
      query  = "[state: <name>] <last user utterance>"
      chunks = embed(query) → cosine top-3 over kb scope (general ∪ state)
      system = strip_kb_markers(system) + "## KNOWLEDGE" section
  system += VOICE_OUTPUT_RULES
  → LLM round
```
Fallback ladder: pgvector down / key missing / index empty → full inline KB
expansion (V1 semantics). `rag` Langfuse span + `rag_ms`/`rag_kbs` metrics.
