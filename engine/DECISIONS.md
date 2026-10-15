# DECISIONS.md — V2 additions

The stack research (LangGraph vs LangChain, Deepgram vs Cartesia STT, Cartesia
TTS vs alternatives, Twilio vs Telnyx, Deepgram Voice Agent API as Option B,
the LLM invocation saga, the latency budget, the cost model) lives in the V1
package's `DECISIONS.md` — identical conclusions apply here. This file covers
the V2-only design decisions.

## D1. Gates in the executor, not the graph edges

We could have encoded each gate as a LangGraph conditional edge that routes
back on failure. We put it in the tool executor instead, because:

1. The refusal is a TOOL RESPONSE the model reads — it sees *which* fields are
   missing and self-corrects in the next round. An edge-level rejection is
   invisible to the model.
2. The executor is the single door for state swaps — there is no second
   enforcement path to keep in sync.
3. It stays data-driven from `llm.json` edge schemas: add an edge/required
   param in the artifact and the gate updates with zero code.

## D2. Deterministic node, not a rewritten Closer prompt

Your repo queued the leak-math fix "in the deterministic layer, not the
prompt." A graph node is exactly that layer: same math, model-independent,
fires whenever inputs exist. The prompt (and the tool) stay available — the
deterministic write simply gets there first, so the numbers are guaranteed
present. Server values from `validate_lead` still win (documented precedence).

## D3. MemorySaver default, Postgres optional

Per-call `MemorySaver` gives crash isolation and zero infra. Postgres
checkpointer is one env flag for cross-restart durability + multi-worker
scaling. Default = simple; the upgrade is a config change, not a rewrite.

## D4. Eager EOT off by default

Flux's EagerEndOfTurn is the documented sub-second path, but it costs extra
LLM calls on false starts and adds cancellation logic to every turn. Default
off; one env flag to enable once the basic budget is verified in production.

## D5. One prompt change only (SMS line)

Every other prompt byte-identical to deployed V7.9. Your sales flow is
validated by an acceptance ladder; hardening the rails (state, gates,
determinism) is where the production risk actually lived. If you want deeper
prompt iterations later, `diallux/prompts/` is now editable source-of-truth
and every change is one `pytest` away from safety.

## D6. Prometheus in addition to Langfuse

Langfuse answers "what happened on THIS call" (traces, transcripts, dvs);
Prometheus answers "how is the SYSTEM behaving" (p95 latency, barge-in rate,
gate rejection rate, booking rate). Different questions, both needed for an
SLA you can defend. `/metrics` costs nothing if you don't scrape it.

---

# V3 additions

## D7. RAG on pgvector, not a vector SaaS, not prompt surgery

You already run Postgres; pgvector + HNSW keeps retrieval local (5–15ms),
data in your VPS, and zero new vendors. Alternatives rejected: Pinecone/Qdrant
cloud (another vendor + network hop on the hot path), full inline KBs (the
"hallucination roulette" you called out — 16k tokens of diluted attention),
and prompt-embedded "consult the KB" instructions with no retriever (nothing
to consult). Defaults mirror the deployed `kb_config` (`top_k=3`), so
retrieval behavior is calibrated to what your acceptance ladder validated on
Retell — the migration risk is the retriever, not the conversation.

## D8. Automatic per-turn retrieval, not a KB tool call

The model COULD call a `retrieve_kb(query)` tool — but that costs an extra
LLM round (one full round-trip of latency, the thing we're optimizing away)
and lets a lazy model skip retrieval. We retrieve automatically before every
LLM round, scoped to the state's KBs, embedded against the user utterance.
Deterministic, one embedding call, no extra round. (The production
`deterministic` node philosophy applied to knowledge access.)

## D9. Normalizer as code, voice rules as prompt — both, by design

A prompt-only fix ("write numbers slowly") fails silently ~5–15% of the time
and you can't test it. A regex-only fix can't teach the model to avoid
markdown in the first place. Production ships both: rules in the system
prompt (V3 `VOICE_OUTPUT_RULES`) + `normalize_for_tts()` as the deterministic
backstop. Each one's failure mode is covered by the other.

## D10. ElevenLabs integration shape: adapter, not fork

Both TTS engines behind one four-method contract (`connect/speak/cancel/
close`) so the sentence gate, barge-in, latency clock, and Langfuse spans are
written once. ElevenLabs has no server-side cancel on stream-input; we drop
cancelled-context audio locally + flush the socket clean — semantics
equivalent to Cartesia's `cancel` frame from the caller's perspective (the
`clear` frame to Twilio already stops playback of anything buffered).

## D11. Why Cartesia remains the default TTS

At your <1500ms (target 1200ms) budget: Sonic's TTFB + contexts give the
best p50 with the credits you already hold; ElevenLabs Flash is the
quality-known option at ~2–3× the per-character cost for v2/v3 tiers. The
ADAPTER is the decision that matters — flipping providers is now an env var,
so "Chloe for client demos, Cartesia for volume" is a runtime choice.

## D12. Delivery (emotion/speed) is a graph concern, not an LLM output

The tempting design is asking the LLM to emit emotion tags per turn ("say
this sadly"). Rejected: it burns tokens on every turn, adds output latency,
produces unstable tone (a temperature knob controlling a mood knob), and
cannot be regression-tested. The graph already knows the state — so V5 ships
`diallux/media/delivery.py`: a per-state profile table resolved at the turn
boundary (the snapshot the hangup check already performs — zero extra graph
reads) and merged into the provider payloads both adapters already send:

  Cartesia   -> generation_config (speed/volume/emotion), per request
  ElevenLabs -> voice_settings (stability/style/speed), first message of
                each turn's generation

Precedence is explicit: profile field > .env global > provider default, with
`TTS_DELIVERY_PROFILES=false` restoring exact V4 behavior, and values
clamped to the documented API ranges at both resolve time and send time (a
miscalibrated table can never 400 a live call). The alternative framing, for
honesty's sake: this is exactly what Retell's platform hid from you —
there, emotion was one global agent setting; here it is a per-state
directable, testable dial. The `tests/test_delivery.py` completeness check
(every deployed state must have a profile) keeps the table from rotting as
states evolve.
