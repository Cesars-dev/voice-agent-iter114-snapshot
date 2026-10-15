# ITERATIONS.md — V2 changes vs the verbatim build, with rationale

V1 (`diallux-verbatim`) is your Retell agent, byte-for-byte, on LangGraph.
V2 (this build) keeps the same conversation design — same 9 states, same tools,
same webhooks, same KBs, same prompts except one reworded line — and hardens
everything your own docs flagged as fragile. Each change below cites the
problem it kills.

## 1. Typed dynamic variables — the null-dv bug class is dead

**Problem (your repo):** Retell dumped unknown values as JSON `null` into
`collected_dynamic_variables` ("first_name":"null" clobbering — V6.3), and you
maintained a whole tool just to verify no null dvs. The lab engine re-created
the same hazard with a flat stringly dict.

**Change:** all 41 dvs are fields on `schema.DynamicVariables` (Pydantic):
booleans are booleans, digit-strings coerce to ints, unknown keys are dropped,
`None` never survives (typed defaults `""` / `False`). `to_flat()` still feeds
the verbatim `{{dv}}` substitution, so prompts are unchanged.

**Consequence:** the null-verification tool is not ported — there is nothing
to verify. Your instinct from the kickoff call, implemented.

## 2. Server-owned variables — the LLM cannot fabricate progress

**Problem:** gate booleans (`slot_verified`, `data_verified`, `phone_confirmed`,
`booking_verified`) were only protected by prompt discipline. A hallucinating
model could extract `slot_verified: true` itself and skip the calendar gates.

**Change:** `SERVER_OWNED_DVS` — those keys can ONLY be written by webhook
`response_variables` (or deterministic nodes). Extract-tool writes to them are
rejected with an explicit tool response (`rejected_server_owned`), so the model
learns immediately.

## 3. Hard gates on transitions + topology enforcement

**Problem:** Retell's edge schemas *describe* required params, but in the lab
engine (and effectively in Retell) the transition executes whenever called.
"Never transition while {{slot_verified}} is not true" was prompt-enforced.

**Change:** `transition_to_X` is executed by Python:
- only edges that EXIST in the deployed artifact can be taken (no
  Intake→Booking jumps — `invalid_transition` response)
- every `required` param of the edge must be truthy in dvs, else the
  transition is refused with `gate_failed` + the exact missing list, and the
  model stays in the current state to finish its work

The gate booleans it checks are server-owned (see #2), so a gate can only open
because the real endpoint said so. `diallux_gate_rejections_total` in
Prometheus counts every refusal.

## 4. Deterministic layer (the graph node Retell didn't have)

**Problem (your AGENTS.md open item #1):** "Closer sometimes skips the
leak-monetization pitch (`calculate_monthly_leak` never fires) — stochastic;
fix queued in the deterministic layer." Your own repo queued this fix; Retell
had no place to put it.

**Change:** a `deterministic` node runs after every state round:
- if the three leak inputs exist and `weekly_leak` is empty → compute it
  server-side (same 1:1 ported math). The pitch numbers are ALWAYS in the
  state before the model needs them; it can still re-verify via the tool.
- entering ConfirmSlots with an empty `today_date` → fetch `/today` directly
  (open item #3: time is never stale, never LLM-dependent).

## 5. The SMS false promise — reworded (the one prompt change)

**Problem (open item #2):** "Booking.md scripts 'SMS text confirmation' —
nothing sends SMS; reword or wire SMS."

**Change:** Closing.md now says "you'll get a confirmation with all the
details" instead of promising an SMS. Every other prompt is byte-identical.

## 6. Playback-confirmed hangup (mark events)

**Problem:** V1 hangs up on a 1.2s timer after `end_call` — on a slow goodbye,
Twilio could cut audio mid-sentence.

**Change:** after the turn's audio is queued we send a `mark` frame; Twilio
echoes it only when the buffer has FINISHED playing. Hangup waits for that
echo (timer fallback retained). Callers always hear the full goodbye.

## 7. Barge-in hardening + eager end-of-turn (optional)

- barge-in now also increments `diallux_bargeins_total` (Prometheus) and logs
  the interruption point (TurnClock `barge_in_at_ms`)
- `DEEPGRAM_EAGER=true` enables Flux's `EagerEndOfTurn`: the LLM starts
  speculatively at medium confidence; `TurnResumed` cancels it. Deepgram
  guarantees the eager transcript equals the final one when the user actually
  stops — this is the documented sub-second latency path. OFF by default
  (cost: extra LLM calls on false starts).

## 8. STT auto-reconnect + Twilio warning watchdog

**Problem:** a Deepgram socket death mid-call meant a silent, dead call; Twilio
buffer-overflow warnings (error 31931, media discarded) were invisible.

**Change:** STT reconnects transparently (`stt:reconnect` span in Langfuse);
Twilio `warning` events are logged + traced.

## 9. Webhook retries with backoff

Connection-level retries (2) + one retry on 5xx for every webhook call. A VPS
hiccup no longer fails a booking turn. Fail-closed semantics unchanged.

## 10. Prometheus metrics + optional Postgres checkpointing

- `/metrics`: the latency budget as histograms (turn E2E, eot→TTFT, TTFT→TTS,
  TTS→audio-out, turn total), plus counters (calls, barge-ins, bookings, gate
  rejections, tool errors). Grafana-ready.
- `LANGGRAPH_CHECKPOINT=postgres` + `DATABASE_URL` → LangGraph state persists
  across process restarts and works with multiple uvicorn workers (per-call
  MemorySaver is the default: simpler, zero infra).

## 11. Prompts as source-of-truth files

The state prompts now live in `diallux/prompts/*.md` (verified identical to
the deployed `llm.json` embeddings at build time) — edit the prompt, restart,
no JSON surgery, no `build_llm_json.py` parity dance. `agent/llm.json` remains
the machine config for tools/edges.

## What was deliberately NOT changed

- The 9-state order, tool set per state, webhook contract, KB inlining,
  repeat-call guard, MAX_TOOL_ROUNDS=12, gpt-5.2 invocation config (no
  temperature, no reasoning effort).
- All webhooks still point at the LIVE production services — the booking
  pipeline (Cal.com holds, idempotent booking, validator gates) is untouched.
- No "clever" prompt rewrites: your sales flow is validated; V2 hardens the
  rails, not the script.

---

# V3 — the RAG / TTS / voice-testing iteration

Same 9 states, same sales angles, same tools, same webhooks. V3 adds the
layers you asked for on the call. Nothing below touches conversation design.

## 8. RAG knowledge bases (your design, now with receipts)

**What you asked:** "LLM must have little tokens per prompt and just retrieve
chunks when needed, not a bloated prompt that will trigger hallucination
roulette. Let's re-design using our own KB query. I have Postgres installed so
we can do pgvector."

**What the artifact revealed:** your deployed agent ALREADY ran this way —
`llm.json` ships `kb_config: {filter_score: 0.6, top_k: 3}` over 9 attached
KBs. Retell retrieved ~3 chunks per turn. AND the lab engine's file-based
replica was broken: `##industry-kb##` looked up `industry.md` while the files
are `industry-kb.md` → every marker resolved to `[KB x MISSING]`; the lab
never had KB content at all. Both fixed/replaced.

**Change:** `diallux/rag.py` — Postgres+pgvector (the `db` compose service is
now `pgvector/pgvector:pg16`), OpenAI `text-embedding-3-small`, heading-aware
chunking, cosine top-k (default 3 = your deployed `top_k`) within the KB
scope of the active state (general ∪ state, same reach the markers had),
char-budgeted 1,600. Build once with `scripts/rag_ingest.py` (~$0.02).
`RAG_MODE=auto`: pgvector when indexed, automatic inline fallback otherwise —
a dead DB can never take a call down. Every retrieval is a `rag` Langfuse
span (which KBs, which chunks, how many ms) + `rag_ms`/`rag_kbs` in state
metrics.

**Latency honesty:** adds ~65–135ms/turn (one embedding call + local
pgvector). That is what your deployed Retell agent was already paying. The
gain vs V1-inline is cost and grounding, not milliseconds.

## 9. Speech normalization — the Retell-native layer, self-hosted

**What you asked:** "the only issue I see is speech normalization (numbers,
addresses etc) — agent will ramble at super speed 665657 instead of
hyphenated 6-5-1. This is native in Retell, not in LangGraph."

**Change:** `diallux/media/normalize.py` on every flushed sentence before TTS:
phone-shaped strings and 5+ digit runs → `6, 6, 5, 6, 5, 7` (Sonic 3.5's
documented comma-delimited style; ElevenLabs reads it the same), conventional
`$6,500`/dates/times untouched (both engines read them correctly), markdown
and emoji stripped (engines read them literally), booking-UID tokens
char-by-char, terminal punctuation guaranteed. `TTS_PHONE_STYLE=spell` wraps
runs in Cartesia `<spell>` tags instead; `natural` leaves phone forms as
written. Deterministic regex (µs), provider-agnostic, never touches
history/dvs/prompts.

## 10. VOICE OUTPUT RULES in the system prompt (belt to the normalizer's braces)

Cartesia's own voice-agent starter rules, appended to every state prompt
(`TTS_VOICE_RULES=true`, default ON here): plain prose, terminal punctuation,
digit runs comma-separated, normal money/date forms, tool arguments are never
spoken. The normalizer catches violations deterministically; the rules make
the model write speakable text in the first place — together they cover both
"model forgot" and "regex didn't match".

## 11. ElevenLabs as a switchable provider (your Chloe, no Retell)

**What you asked:** "check ElevenLabs pay-per-go price... how much would it
cost me to have a few demos? ElevenLabs is my go-to choice, we bypass Retell
entirely, I even have settings."

**Change:** `diallux/media/elevenlabs_tts.py` — stream-input WebSocket,
`ulaw_8000` (still zero transcoding to Twilio), voice_settings on the first
message (stability/similarity/style/speed — your Chloe knobs, env-able),
`{"text": ""}` flush, per-turn context emulation, barge-in drops
cancelled-context audio. `TTS_PROVIDER=elevenlabs` and nothing else changes:
same gate, same clock, same spans, same barge-in. **Demo math (PAYG, top up
from $5):** Flash $0.05/1k chars → ~$0.14 per 5-min call; Multilingual v2/v3
$0.10/1k → ~$0.28. A 20-call demo batch ≈ **$3–6 including Twilio+STT+LLM**.
Cartesia stays default (lower TTFB, contexts, your credits) — switching is a
business decision, which is why it's one env var.

## 12. Cartesia generation_config — the payload fix

Speed (0.6–1.5), volume (0.5–2.0), emotion (documented list, English-only
beta, best on emotive-tagged voices) ride EVERY request — there is no
persistent account-level setting. This is the Cartesia-native equivalent of
your Chloe tuning; it lives in `.env`, not in the prompt (the prompt never
reaches the TTS engine — only spoken text does).

## 13. Accuracy eval before wiring voice

**What you asked:** "how do we test it? I need to check accuracy etc before
wiring the TTS-STT."

**Change:** `scripts/eval_accuracy.py` + `eval/scenarios/*.json`:
- **offline** (default): scripted FakeLLM, hermetic, free — verifies the
  MACHINE (transitions, gate holds, tool calls, dvs writes, KB retrieval
  wiring, TTS normalization of the spoken stream).
- **--live**: your real model — verifies the AGENT (right transition at the
  right moment, stays in state when data is missing) and reports per-turn
  latency. Exit code 1 on any failure → CI-able; JSON report in `eval/report.json`.
Five scenarios ship: happy path, "are you an AI" KB hit, premature-transition
gate hold, leak-math chain, TTS normalization.

## 14. Temperature 0.03

Default `OPENAI_TEMPERATURE=0.03` with the gpt-5* guard (reasoning models are
API-fixed at default temperature; the code omits the param for them and logs
it — set `OPENAI_MODEL=gpt-4.1` to make it apply). This replaces
V2's "no temperature" default per your ask. *(V5: corrected to **0.3** — the
value you actually asked for; see §16.)*

## 15. V4 — LLM-to-LLM fake calls (your SOP, injected as a feature)

**What you asked:** "how do I fake a call?" — your LLM-to-LLM testing SOP
(docs/Testing_guidelines/LLM-TO-LLM-TESTING.md), built for Retell's
create-chat API, wired into this runtime as `tests/llm2llm/`.

**Why it is BETTER here than it was on Retell:** Retell forced you to create
a chat agent (voice agents can't be driven by create-chat-completion). The
LangGraph brain is text-in/text-out natively — STT/TTS/telephony wrap outside
it — so the fake call drives the SAME graph, prompts, tools, webhooks and
checkpointer a real phone call does. No clone, no channel split, no session
lifecycle to manage.

**What ships:**
- `tests/llm2llm/harness.py` — the loop. GPT-4o caller (full context every
  turn, temp 0.7, <=20 words) vs the graph; one thread per run
  (`thread_id = call_id`); persona `dynvars` seeded via
  `initial_state(call_id, persona_dvs)`; tool fires from `executor.trace`;
  `end_call` from state `ended`; per-turn brain latency; `--langfuse` writes
  one trace per run (session `llm2llm-<transport>`); exit codes are CI-able.
- **Two transports, same personas, same scorer:** `--transport graph`
  (in-process brain, default) and `--transport retell` (your live Retell chat
  agent — run both, diff the reports: the migration A/B you could never do
  cheaply before). Retell keeps the guarded-`json.loads` arguments handling
  (your SOP Bug 2) — the graph side already has parsed dicts.
- `tests/llm2llm/personas.py` — the 13-persona battery ported 1:1 from
  `retell/tools/test_llm_to_llm.py` (they were authored from scratch for THIS
  agent, so they carry over unchanged): 4 happy paths, mean/dumb/problematic/
  enquiry/AI-question, and the curve gauntlet (Brenda/Gene/Frank/Ray).
- **Slots:** `--slots mock` (default) — the in-process mock implements the
  live production contract, so `create_livecall_booking` cannot create real
  bookings. `--slots live` hits the real server (bookings are REAL — test
  server only).
- **Scoring, your rules:** booked = booking tool fired + non-empty
  `booking_uid` (never a fixed timestamp — live availability varies);
  reschedule/cancel don't count; production additionally reports
  `blocked_transitions` (gate refusals) — happy paths must hold 0.
- **Run order enforced:** `--personas all` aborts before stress personas if
  any happy path failed (SOP: "no point stressing a flow that does not work";
  `--no-happy-gate` overrides).
- `tests/llm2llm/harness.py --offline` — hermetic plumbing smoke (FakeLLM
  agent + scripted caller, no keys): in production it proves the gate HOLDS
  an illegal Discovery→Booking jump; in verbatim it proves the ungated
  booking chain fires end-to-end.
- `tests/llm2llm/test_harness_units.py` — 13 hermetic pytest units: persona
  schema, dynvars-are-real-dvs validation, happy-path-first ordering, and the
  scorer's non-empty/reschedule/cancel rules.

**Relationship to the scripted eval (#13):** this is your SOP's tier order —
LLM-to-LLM for adaptive first-pass exploration (after every prompt change),
`scripts/eval_accuracy.py` for deterministic regression (free, reproducible,
CI). Reports: `eval/llm2llm_report.json` + per-run logs in
`tests/llm2llm/json_logs/` (full transcript, tool order, final dvs,
latencies).

## 16. V5 — per-state delivery profiles (emotion/speed is the graph's job)

**What you asked:** how do we manage a good emotional tone (Cartesia), plus
the emotion/speed parameter mapping from your iteration list. This is the
feature: `diallux/media/delivery.py`.

**The principle:** the LLM writes WHAT to say; WHERE the call is decides HOW
it sounds. Delivery is a deterministic function of the graph state — one dict
lookup per turn, no extra LLM call, no prompt tokens, zero added latency
(two JSON fields on requests the adapters already send). The alternative —
asking the LLM to emit emotion tags — costs tokens, adds latency, and drifts
mid-call; the state machine cannot drift.

**The table** (editable source of truth, same philosophy as
`diallux/prompts/*.md` — tune HERE, not in .env, for per-state feel):

| State | Cartesia | ElevenLabs | Why |
|---|---|---|---|
| begin / Intake | speed 1.05, happy | stab 0.50, style 0.30, speed 1.02 | opener: warm, energetic, smile |
| Discovery | speed 1.0 | stab 0.60, style 0.15, 1.0 | interested, unhurried questioning |
| VerifyLead | speed 1.0 | stab 0.65, style 0.10, 1.0 | precise, matter-of-fact |
| contact_details | speed 0.92, calm | stab 0.70, style 0.05, 0.95 | digits read-back: slow + clear |
| ConfirmSlots | speed 1.0 | stab 0.60, style 0.10, 1.0 | helpful clarity |
| Booking | speed 1.05, happy | stab 0.50, style 0.25, 1.02 | confident, upbeat |
| Offer | speed 1.08 | stab 0.45, style 0.35, 1.05 | the one expressive state: pitch |
| Closing | speed 0.95, calm | stab 0.65, style 0.10, 0.97 | objections: patient, never pushy |
| Closer | speed 0.95 | stab 0.60, style 0.10, 0.95 | goodbye: warm, unhurried |

**Wiring:**
- `CallSession` snapshots `state_name` at the END of each turn (the snapshot
  the hangup check already does — zero extra graph reads), so turn N speaks
  with the profile of the state whose PROMPT generated it (turn N's starting
  state; transitions take effect from the next turn — matching Retell's
  per-state prompt semantics). The begin_message uses the `begin` profile.
- Cartesia: profile fields merge OVER the `.env` globals into
  `generation_config`, per request, clamped to the documented ranges
  (speed 0.6–1.5, volume 0.5–2.0) so the table can never 400 a call.
- ElevenLabs: profile fields merge into `voice_settings` on the FIRST text
  message of the turn's generation (the documented slot; same semantic as
  Cartesia's per-request config since EL contexts are emulated per turn).
  Clamped to stability/style 0–1, speed 0.7–1.2.
- `TTS_DELIVERY_PROFILES=false` → exact V4 behavior (.env globals only).
  A `None`/blank field → falls back to the .env global for that field, so
  .env stays the base tuning and the table is per-state direction on top.

**Discipline baked in:** emotion is used like salt (5 states, never stacked —
Cartesia docs: guidance, not a strict adjustment); expressiveness only on
Offer, never on data-collection states where clarity beats charisma; speed
deltas stay small (±10%) so the voice never "jumps". Values are clamped to
the documented API ranges at resolve time AND in the adapters.

**Tests:** `tests/test_delivery.py` (15 units) — table completeness (every
deployed state in `agent/llm.json` + `begin` must have a profile; a new state
fails here until profiled), documented ranges, lookup/fallback semantics,
both providers' merge precedence, and the `CallSession._speak_chunk` seam.

**Temperature correction (your §14 ask):** default is now `0.3` (V3/V4
shipped `0.03` — a misread of your request). Same env knob, same gpt-5*
guard (reasoning models are API-fixed; the param is omitted for them and
logged). `OPENAI_TEMPERATURE` if you want it different.

## 17. iter18 — gpt-5.2 on the SAME machine — 3-way model verdict (2026-09-05)

Zero code/prompt changes; the only variable was `--agent-model gpt-5.2` (temperature dropped by
the guard, `reasoning_effort="none"`). T0: 86 pytest green + Maria smoke (book, zero 400s).
Battery: 24 personas staggered-async 30 s, wall 14.5 min, zero 429s. Full audit:
`tasks/surgeon/v5-pgvector-gpt52-argfix/16_iter18_gpt52_3way_audit.md`; artifacts:
`baseline_gpt52_full24/` (28 files); charts: `16_iter18_gpt52_3way_chart.png` +
`16_iter18_gpt52_3way_lines.png` in the same folder.

**3-way verdict (same day/machine/code as both prior columns):**

| SOP | gpt-4.1 | gpt-5.1 | gpt-5.2 |
|---|---|---|---|
| CALL clean runs | 15/24 | 9/24 | 16/24 |
| SALES expect-match | 21/24 | 21/24 | **22/24** |
| SALES hire band | 11 | 11 | **12** |
| HUMANIZED PASS/FAIL | 17 / 0 | 14 / 2 | **18 / 0** |
| Latency p50 / max | **0.91s / 2.65s** | 1.26s / 4.52s | 1.25s / 4.41s |
| Input tokens | **~3.2M** | 14.2M | ~5.1M |

**VERDICT: move-to-gpt-5.2** (gpt-5.1 eliminated — dominated on every row; gpt-4.1 keeps speed+cost
as cheap-iteration fallback). Walkthrough-promise class is DEAD on 5.2 (Brenda + Boris + Gene all book
with full ladder + leak math; Gene was 5.1's worst failure). D1 zombie tails INHERITED but 3× milder
than 5.1 (26 silent turns vs 85; 3 maxed-48 vs 5; farewell 24/24, zero silent hangups).

**gpt-5.2 open failures (the fix queue):**
1. **D1 zombie tails** — ended=False 24/24, 26 "(no agent text)" turns, 3 runs maxed at 48
   (Nick, Suzy, Wendy). Same class as 5.1, milder. THE next fix (speak-or-terminate).
2. **Own-pricing fabrication** — Priya ×5 ("$1,200–$2,800/mo", byte-identical to the BASELINE's
   fabricated numbers → grep pgvector for an echo chunk) + Boris (range + "setup 1–3 days").
3. **Over-books (2 expect-misses)** — Pedro (5th battery in a row, all models; persona variance,
   closed case) + Wendy (NEW: agent picked times/dates/name FOR the waffler → booking with garbage
   data; needs an over-book guard: never decide FOR the caller).
4. **Ray callback promise** — t29 "Jay will call you after hours" with no mechanism (softer than
   baseline, more explicit than 5.1's "noted").
5. **Sofia soft early-slot promise** — t14 "2 PM works on our side" before query_livecall_slots
   (self-corrected to a real slot; milder than baseline).
6. **Sam security-infra claims** — "encrypt in transit and at rest / least-privilege / log every
   access / we don't sell or share" — no KB backing located; extend the knowledge-boundary rule.
7. Minor: Dave slot-wobble + fabricated digit read-back; Suzy verbatim re-ask (R5.5×1).
Zero HUMANIZED FAILs, zero run-ons, injection defense held, farewell in all 24.

Langfuse ingest dropped 6/24 battery traces (same ~25% loss as both prior batteries); latency
aggregate = 18/24 traces, calls=580 p50=1.25s p90=1.79s max=4.41s. Prices in `lf.py costs` for
gpt-5.x remain placeholders. No snapshot (nothing changed on disk). No port action this session.

**CORRECTIONS (Julio, post-review 2026-09-05) — supersedes the severity/ordering above:**
- **Pricing NOT fabrication — RECLASSIFIED.** `$1,200–2,800` is IN the corpus:
  `agent/knowledge_bases/industry-kb.md:26` ("$4,500/mo human vs. $1,200–2,800 AI"). gpt-5.2 read
  the KB. Real defect = POLICY CONFLICT: Closer.md:102 "No pricing — defer to ##sales-language-kb##"
  vs a Dialux price range living in industry-kb. All 3 models quoted it BECAUSE it's in the corpus.
  Fix = decide: sanction the KB range (update Closer.md:102/Offer.md:42) or scrub it from the KB.
  Drop the "grep for echo chunk" action.
- **Wendy persona is DESIGNED vague** (personas.py:429: "enthusiastically AGREE with everything but
  COMMIT to nothing… no real company name"). "Marsh" is her real name — data wasn't garbage. Defect
  is narrower: the agent answered its OWN questions (tz/time/date) instead of failing the booking.
  Over-book guard stays: agent proposes, caller decides; no commit → no booking.
- **D1 downgraded: NOT production-blocking.** Milder on 5.2 (26 silent turns, farewell 24/24); fix
  (speak-or-terminate: after a spoken farewell, a second empty end_call attempt terminates the graph
  deterministically) is small and mechanical. Fold into the same pass as the pricing decision.
- **Latency framing:** p50 1.25s is FULL-LLM-call time; with streaming TTS the caller's
  time-to-first-audio lands well under it — 5.2 latency is a non-issue for voice. Max calls (4.4s)
  are tool-heavy turns (booking), not speech turns.

**NEXT SESSION QUEUE (on gpt-5.2, in order):** (1) pricing policy decision: sanction industry-kb
range or scrub it + align Closer.md:102/Offer.md:42; (2) D1 speak-or-terminate gate; (3) over-book
guard (never decide FOR the caller — Wendy); (4) callback honesty on human-demanding personas (Ray:
`record_reach_details` never fired, promise unbacked — route to walkthrough or close honestly);
(5) Sofia soft early-slot promise; (6) Sam security-infra claims boundary. Then re-battery +
end_call nudge re-validation ×3.

## 17. iter21 — browser-mic bridge (IN PROGRESS, 2026-09-06)

Same pipeline, two transports: `AUDIO_TRANSPORT=twilio|browser` at the codec layer only
(`deepgram_stt.py` linear16@16k vs mulaw@8k; `cartesia_tts.py` pcm_s16le@16k vs pcm_mulaw@8k;
`session.py` binary writer + browser hangup). `GET /mic` + `WS /mic/ws` in `app.py`, page in
`diallux/static/mic/index.html`. Bridge calls trace `micbridge-<session>`.

Session also fixed (live):
- RAG: keep-alive embed client (was fresh TLS per turn, 190-900ms), greeting-time warmup,
  `rag_min_query_chars=12` filler skip. First RAG 277ms (was 1677ms cold).
- Eager EOT ON (`DEEPGRAM_EAGER_EOT=true` @0.6) — dormant `settings.deepgram_eager` AttributeError
  fixed by adding the missing config field.
- Turn-report race: TTS marks route to the speaking turn's clock; late `turn:N:final` span.
- SSML per-sentence jitter (`<volume ratio>/<speed ratio>`, ±6%/±10%, occasional dip) — tags at
  request boundary are never spoken (duration-verified A/B).
- `diallux/observability/lf_sdk.py` + `scripts/lfpull.py` — one-command pull-all + latency
  budget + defect flags.

Measured (live 116-turn call): e2e p50 1354ms / p90 6263ms; llm_first ≈ 90% of budget; gpt-5.2
TTFT cold 1638ms vs 843-952ms cached (9.7k prefix) — live calls always uncached because dvs+RAG
mutate the prompt prefix. Calendar outage (WAL sidecar ownership, service user `2nd_workspace`)
caused the ConfirmSlots zombie loop — fixed via chown (ops), not v5 code.

## 18. iter26 — iter25-b first-13 ladder deep-dive (2026-09-06)

Branch `iter25-b` @ c1cc944 (ledger e04d644). **NO MERGE.** Ran the first-13 acceptance-ladder
personas (PERSONAS[0:13], Maria→Ray) with the parallel launcher pattern (one harness process per
persona, 30s stagger — documented in `PARALLEL_BATTERY.md` at repo root). Config: gpt-5.2,
`OPENAI_VERBOSITY=medium`, `--rag --langfuse --max-turns 48`, mock slots. 13/13 json logs, zero
400/429, zero trace loss.

**Results: 12/13 PASS · expect-match 8/9 · curve 4/4 sane · turns avg 23.3 · wall avg 98.4s ·
LLM p50 avg ≈1.30s (1.12–1.52) · RAG 715 pulls / ≈156ms avg / 0 errors · books-only SALES avg
13.1/14 (best battery to date).**

- PASS books: Maria 13/14, Danny 13/14, Susan 12/14 (best name-refusal handling yet),
  Marcus 14/14, Sofia 13/14 (no early-slot promise). NO-BOOK: Carlos 9/14, Jorge 12/14,
  Daniel 7/14 (AI-disclosure perfect ×3, 7 turns).
- Curve: Brenda 14/14 (best call of battery — pricing deferral + market anchor, no leak),
  Gene 13/14, Frank 6/14 (refused to fabricate a callback number under hostility — highlight),
  Ray 5/14 POLISH (watch-list HELD: no false callback promise, honest self-termination; but
  t35–41 slot loop, recycled "1:00 PM or 2:30 PM Pacific?" ×4).
- **FAIL: Pedro over-book (×6th battery)** — on b the agent fabricated the qualification numbers
  ("let's use fifty", "$450 — let's split it" → $11,250/wk leak) and closed a waffler who never
  committed. Fork-scope: deterministic guard at the leak-math boundary.
- **New defect: ISO timestamps spoken in slot readback** ("2026-09-04T13:00:00") — Maria t16,
  Brenda t26; render/subst layer. Analyzer R4 regex needs an ISO-datetime rule.

Report: `tasks/surgeon/v5-pgvector-gpt52-argfix/18_iter26_b_first13_deepdive.md`.
Carry-forward to fork decision (Julio's call): Pedro guard, ISO-slot render fix, Ray loop cap,
c's cache/speed line as the performance half.

## 19. iter27 — iter27-human-slots LIVE-slots first-13 battery (2026-09-07)

Branch `iter27-human-slots` @ e69f512 (fork of `iter25-b` @ 11f4d93; commit = human-slot contract
re-point in `agent/llm.json` + `ConfirmSlots.md`, mock mirror, per-endpoint webhook signing in
`tools.py`/`config.py`). **NO MERGE.** First-13 ladder (Maria→Ray), parallel launcher, **`--slots live`**
— real bookings on Cal.com event 3801235 / `dialux_live`. v1 :8001 serves the human-slot contract
(`slots_human`, human-time booking resolver); validator :8003 on the v5 key; two-wave run (6 finished
in pass-1, 7 relaunched async after a Julio-ordered kill), 13/13 completed.

**Results (amended post-OTEL sweep): 12/13 true PASS · expect-match 8/9 (Pedro over-book ×7,
masked by cal_400) · curve 4/4 sane · turns avg 20.9 · wall avg 88.9s · p50 ≈3.0s on booked calls
(live webhook round-trips included; no-book calls ≈1.5–1.9s ≈ iter26 LLM p50) · spoken ISO-slot-leak
0 (iter26: 2 — TARGET HIT) · 7 real bookings created and ALL cancelled (find_existing_booking None
×7 seeded phones) · zero signature/auth/HTTP errors.**

- Human-slot contract verified live on the SPEECH side (offers/readbacks all human form), and
  `book-livecall` accepted human time strings on all 7 bookings. **BUT the contract stops at the
  validator**: :8003 `/verify-lead-data` `_fix_time` is ISO-only → `unfixable_format` on any call
  that stores the human string verbatim as `selected_time`.
- **BLOCKER BUG (OTEL trace 4d956d46068c): Maria loop ×15** — agent obeyed ConfirmSlots verbatim
  clause ("tomorrow, 9/7 at 12:30 PM" stored) → validator bounced ×15 + booking gate ×4 → 33 turns
  / 187.6s; escaped only when the model self-converted to ISO. Deterministic, not flaky: fired on
  the one call that obeyed; latent on the other six bookings (models stored ISO, disobeying the
  clause 12/13). Mock suite blind spot: mock validator accepts human strings. Fix (a) teach
  validator the human format or (b) split speech vs payload + battery assertion verify ≤2 calls.
- **Pedro over-book ×7 (report-18 streak NOT ended)**: agent drove the waffler to
  `create_livecall_booking` → Cal.com 400 (555 fake number) → polite abort looked like discipline
  in the transcript alone; OTEL exposed the attempt. Retrain: leak-math guard + waffle gate.
- Minor: Brenda "$697/mo" self-quote (regression vs 18's sanctioned deferral); Sofia afternoon
  pref ignored by slot offer; `unknown_account='dialux_live'` transient ×1 on Maria's first
  create (retried ok); Ray watch-list cleared.
- **Regression: Brenda product-price self-quote** ("starts at $697 a month" t7) vs iter26's
  sanctioned deferral — same pricing-leak guard family, fork-scope.
- Minor: Sofia's afternoon preference not honored by the slot offer (booked 10:30 AM, accepted);
  Ray watch-list cleared (no false callback promise, no loop).

Report: `tasks/surgeon/v5-pgvector-gpt52-argfix/19_iter27_live_slots_battery.md`.
Carry-forward: **validator human-time fix (BLOCKER)**, Pedro leak-math + waffle gates, Brenda
pricing-leak guard, v1 env-pin (`dialux` vs `diallux`) — Julio owner action.

## §20 — iter28 `server-time-payload` (2026-09-07)

Branch `iter28-server-time-payload` @ `62c9a81` (fork of `iter27-human-slots` @ `8c0a0bb`) — **NO MERGE, NO PUSH**.
v1 humanizer + validator containment gate (`slot_options` payload, human-verbatim `selected_time`) + `booked_human`
close-out; suite 102 passed. Maria smoke PASS (report 20). **First-13 LIVE battery DONE 2026-09-07: 13/13 PASS,
expect-match 13/13**, spoken ISO 0, `unfixable_format` 0, verify ≤2 everywhere, containment gate live-verified
(Sofia invented-time bounce → re-book; Pedro 555 bounces ×3, zero real bookings), Brenda 697 leak absent,
7 battery bookings created + all cancelled, calendar clean. BOOK SALES avg 13.4/14; HUMANIZED 10 PASS / 3 POLISH.
Watch: end_call empty-text race in 13/13 (pre-existing, turns inflated, `ended=False`; iter29 candidate).
Report: `tasks/surgeon/iter28-server-time-payload/20_iter28_smoke.md`.

## §22 — iter32 `silent-round-gate` (2026-09-08)

Branch `iter32-silent-round-gate` @ `5ac026b` (fork of `iter31-c3b-tail-after-history` @ `48d6ad3`
— C3+C3b caching + STRICT end_call gate verbatim, NO dampener; tools.py byte-identical to
`6c2d1cf`). **NO MERGE, NO PUSH.**

The fix: payload if/else on the tool channel — a SILENT round whose tools were ALL mechanical
(`_MECHANICAL_RE` = FIRE_AND_FORGET prefixes + `end_call`) closes the tool channel for the NEXT
round (`tools=[]`): the model must speak. Result-dependent chains (query_livecall_slots,
transitions, verify/booking) keep their channel (chain-legal). Plus recency co-emit directive
("SPEAK FIRST…") prepended to the STATE BLOCK tail (engine-generated text — prompts/*.md
untouched). Dampener stays REJECTED; forced-speech round → goodbye lands → goodbye-auto-end
ends the call bounded.

**The machine-gun end_call bug (root cause):** silent end_call → strict gate blocks → end_call
NOT in FIRE_AND_FORGET → loop-back to state node → model re-fires silently, bounded only by
rounds_left×turns (C-battery live: Ray 163 / Frank 179 / Carlos 181 gens at 48 turns). iter32
kills it by construction: one blocked silent end_call → tools=[] → speech/goodbye → auto-end.

**First-13 battery (2026-09-08 10:19–10:27):** 12/13 PASS (Pedro over-book = known), ended
13/13, **ZERO 48-turn runs** (Frank 13t, Ray 6t — the hammer personas), **agent-silent tool
turns = 0/230 turns battery-wide** (Maria-D was silent-heavy), gens 480, Langfuse silent% 66% →
**54.2%** (hostiles collapsed: Frank 13.3% / Ray 14.3% / Carlos 25%; residual concentrated in
BOOK chains — transition_to_* + query/verify chains, chain-legal by design, plus extract_*
sawtooth at 1 tool/round), cache-hit 58.9%, turn p50 ~2.4–3.3s, BOOK turns/call 19.1 (caller
sim asked more; craft intact — every turn speaks). Suite **133** (127 + 6 gate tests).
New meter: `scripts/silent_rounds.py` (Langfuse GENERATION output.text ground truth; D-battery
repro 412/140/272/66.0% verified).

**Latency gates NOT fully met:** silent% 54% vs ≤30% target, gens 480 vs ≤300. The gate capped
mechanical+end_call silence; the remaining ~half of rounds are chain-legal silent tool rounds
(1–2/turn) inside the verify/booking chains — exactly the Phase B target (engine-owned state
machine, stash `phaseB-wip`): 2.4→~1.2 gens/turn structural kill. Craft gates all green; no
drift (Ray 6t clean exit with spoken goodbye).

Carry-forward: Phase B (structural), Pedro over-book retrain, cache-hit re-tune (forced-speech
rounds rebind tools → one cache miss per forced round; acceptable), history window re-tune.

## §23 — iter33 `phaseb-engine-chain` + OTEL census (2026-09-08)

Branch `engine/iter33-phaseb-engine-chain` off `596f1bb` (plan
`plans/plan_iter33_otel-maria-battery_2026-09-08.md`, 2-iter cap + 2 same-branch fixes).

Engine: booking chain moved engine-side (`_deterministic_node`, resumable 8-step plan,
fillers via tts_token, abort→model repair, `chain_done` latch, `PHASEB_CHAIN` flag) +
`fillers_said` channel (plan gap, would-have-crashed). Suite **147** (141 + 6 OTEL tests).

OTEL: smoke trace ended at turn 18 (exit-race) → blocking confirm in `finish()` +
harness wait (T1); battery then showed SILENT mid-run batch drops (4/13) → census fix
(server count vs locally-started obs, `tail_ok` + LOUD SHORTFALL + verdict in every
json_log) + `span()` errors un-silenced. Tails 13/13 complete after same-branch re-runs.
Ingest drops cluster transiently (payloads ≤3.2KB — not size); meters now disclose.

**gpt-5.2 battery (16:00–16:19Z):** 12/13 PASS (Pedro over-book = known deferred),
ended 13/13, 212 turns, gens ~366 (480 iter32 → −24%), silent% 54.9% (flat),
LLM p50 1.52s, booking turns ≤6s. **3-SOP:** CALL 11/1/1 · SALES 4 hire/7 solid/
2 retrain · HUMANIZED 10/3/0. Reports `reports/phaseb-engine-chain/05_battery.md`,
`06_sops_full_call.md`. **NO MERGE (Julio call).**

Carry-forward: Phase B v2 (per-state tool-protocol redesign for ≤300/≤30%), Pedro
retrain, Daniel t10 identity-consistency, prompt-track repetition governor, costs
endpoint 400 (no cost lines either model).

## §25 — iter36 `sop-autopilot` gpt-5.2 audit re-run (2026-09-08)

Autopilot plan `plans/plan_iter36_sop-autopilot_2026-09-08.md`: fresh agent, READ-ONLY
from wt-iter35 SDKs (`call.py digest/sop`, `lf.py lat`, `state_latency.py`,
`silent_rounds.py`), pulled its own digest + lat + dossiers + meters for the iter33
gpt-5.2 battery (13 staged json_logs), graded table→summary→relevant-info from pulled
files only. Fresh numbers CONFIRM §23: 12/13 PASS (Pedro over-book), 13/13 ended,
harness turn p50 3.02s/p90 6.08s, OTEL ALL 354 calls p50 1.50s p90 2.23s (2
empty-named traces counted by ID), silent 54.9%. 3-SOP: CALL 11/1/1 · SALES 4 hire/
7 solid/2 retrain · HUMANIZED 10/3/0 — identical bands to 06. One correction vs 06:
Brenda R5.5 (t31/t36) reclassified as designed closing-script re-fire → POLISH stands.
Report `reports/phaseb-engine-chain/08_sops_autopilot_gpt52.md`. Only Pedro FAIL =
known deferred craft item — no new engine work. Gate OPEN (merge = Julio call).

## §26 — iter40 `spike-readback` (2026-09-09)

Branch `engine/iter40-spike-readback` @ `91aa4ac` (fork of `iter38b-gpt54` @ `491a7a3`) — **NO MERGE (LAW 0); T8 live A/B open, running under iter41 plan.**

- `8600e00` + `6bd7972` **L2 slots prefetch** — warm TODAY+TOMORROW in `CallRuntime._slots_warm` (freeze-mode parity args, keyed by date); served ONLY on exact arg-match (same date, empty uids, no preferred_time, <120s TTL, ok+non-empty, same tz). Flag `slots_prefetch`. Live endpoint REQUIRES `slot_target_date` (probe: no-date → `no_start_time`) — plan's no-date pin impossible.
- `5565ef2` **L3 one-trip** — spoken round + ok `transition_to_*` finalizes in ONE trip; `gate_failed` loops; empty-text slots round speaks "One moment while I check availability." Flag `one_trip`; iter31 RAG-freeze fixture updated to one-trip turn shape.
- `188d3de` **C3 frozen state block per turn** — ingest renders `state.frozen_state_block` once; engine-fired rounds re-render. Flag `frozen_state_block`.
- `ecd58ae` **T6 commit5** — `slot_target_date` auto-fill: omitted date → last SUCCESSFUL slots date, else requested_slot day word via today_date; response `source: prefill`; nothing known → unchanged live `no_start_time`.
- `c00a815` **T6 commit6 (JULIO-APPROVED verbatim)** — ConfirmSlots.md availability re-query logic + Critical Rules ("no 'Yes' on a no", "keep the thread on tool errors"); `agent/llm.json` state_prompt lockstepped (iter28 no-drift gate).
- `91aa4ac` **T6b commit7** — Closer-scoped end_call skip gate: spoken end_call from Closer with `closer_completed` unset refused ONCE with repair message, second accepted (iter31 dampener escape). v1 all-states variant broke iter16/30/31 → REVERTED pre-commit; scoped variant suite-green. Lesson: scope gates to the state's OWN completion flag.
- **LIVE endpoint (ORIGINAL repo, owner-gated, both verified):** `2a64523` `_slots_human` drops `m/dd` joins ("tomorrow at 6 pm or 6:30 pm"; noon/midnight kept); `7094bd8` humanized `preferred_time` resolver wired into `/check_availability` (root cause of live 2pm loop t48: resolver existed only on `/book-livecall`). Snapshot `snapshot/pre-iter40-deploy` @ `93a744c` + stash `77c7340`. Fork mirrors `87fae0f`/`9acac66`.
- **Battery:** 13-persona first-13 IN PARALLEL → **11/13 PASS** (Marcus/Gene doubles, Brenda mock-date fail). 2 live happy paths REAL: Susan booked+cancelled clean; Maria t9 Closer flake (model role-played funnel as speech, zero tools) → root cause of commit7. Latency vs iter38b same-harness: p90 −30–50%, max −35–70%. OTEL ALL p50 1.58s (load-inflated; gen p50 ≤1.2s gate still open → T8).
- Suite **187** (25 new in `tests/test_iter40_spike_readback.py`).
- Carry-forward into iter41: in-turn double-speak (model repetition artifact, pinned via Langfuse gen text), Phase B filler stacking (3 fillers/turn on booked Closing), mock `check_availability` not date-aware (Brenda lost conversion), Pedro over-book (pre-existing, retrain candidate), gen p50 ≤1.2s.
- Report: `research/surgeon/iter40-spike-readback/{01_branch_archaeology,02_latency_fixes,03_endpoint_910_noon,04_call_analysis_13}.md`.
- **iter43 (`engine/iter43-eot-cache-askgate` @ `374318b`) — ABANDONED** (tag `iter43-failed`, unpushed). Live call 532d proved: +latency was cache-miss driven (turn2 cold 1,870ms; transition 3,052ms; cache_read stuck at 2,688 floor — tail-after-history) + embed TLS-handshake waste (425-675ms vs 60-120 designed) + frozen-per-state retrieval missed industry-kb entirely (77 chunks, zero used). DeepSeek's 16.5k root cause REFUTED (blob never fired); its fixes untested live. One-line kb=False fix (1,648-token turn 1) ADOPTED into iter44 via cherry-pick (`68541f7`). Evidence: `research/surgeon/iter43-eot-firstturn/01_telemetry_and_live_calls.md` + Langfuse traces `de9d47732ad2…` / `44866edbea…`. Superseded by **iter44-cache-floor** (plan `plans/plan_iter44_cache_floor.md`, branch `engine/iter44-cache-floor` = 17b5c21 + fix, suite 224/224 at T0).

- **iter48 (`engine/iter48-rag-truth` @ `033f742`, UNMERGED — LAW 0) — Commit B payload coverage + the gpt-5.4 cache wall.** Commit A (`d9a3396`) = RAG query truth (refer-whats + dv values + caller text, `[state:]` scaffold gone), clean drift vectors, forced-drift rescue + filler-freeze stale-base reset. Commit B (`32d6ea9`) = EOT/mid-turn await split (`prewarm_entry_wait_eot_ms` 500 vs `prewarm_entry_wait_ms` 100), lite warm registered+awaited (`lite:<state>`, audit A5), warm WARNING + one lite retry. `d1dd5a9` = harness `--warm-greeting-ms` (session-parity greeting warms; the harness never fired them — FIND-3). `7188db7` = live_sql G3 threshold fix (formula was n+1 = impossible — FIND-6). **`033f742` THE FIX**: raw cURL+OTEL proved gpt-5.4 prompt-caches ONLY requests carrying `tools` (9/9 no-tools attempts cached=0, back-to-back; 3/3 with-tools hit — FIND-5) → lite turn-1 carries `LITE_NOOP_TOOL` (`memory_note`, never called, executor no-ops) + `prewarm_max_completion_tokens` 16→64 (BLOCKED item; 400 rejection observed in happy-e). **Answers @ `033f742`:** battery-iter48 13/13 PASS, turn-1 cache 12/13 (1664-tok lite prefix), turn-1 TTFT 716-1562ms, LLM TTFT p50 821-1023ms, bugs ALL COVERED. Suite **270**.
- **RAG retrieval diagnosis (FIND-9, `research/surgeon/iter48-rag-truth/10_latency_findings.md` + ledger):** live replay of 4 calls showed the prompt's tag-KB is systematically missing (Intake mirror → industry, pain-points never; Discovery deferral → industry, sales-language never; Offer re-anchor prefetch hit 1/4; **Closing `call-closing` → 0 chunks 4/4**); mid-visit freeze + async drift burns ~180ms/round landing ZERO deltas (dedupe); scope = general ∪ state = fake per-state attachment. Retell parity target extracted: 9 KBs global, `kb_config {filter_score 0.6, top_k 3}`, fresh every turn, `##slug-kb##` = platform-resolved references (not tools).
- **Latency forensics (`10_latency_findings.md`):** the VPS is **Helsinki**, not UK; OpenAI origin SF (~150-180ms RTT); **Deepgram connect 120-214ms / TTFB 457-586ms**; Cartesia 90-114ms; Cal.com US VPS 75-90ms. Projected e2e 1,900-2,400ms → **1,000-1,400ms** after US region + local embedder.
- **Next (two plans, PLAN ONLY):** `plans/plan_v5_iter49_rag_retrieval_parity.md` (own the RAG: mimic Retell then beat it — per-tag multi-query, per-KB quota, local embedder, fresh-per-turn) and `plans/plan_v5_iter50_us_region_migration.md` (US-East migration, runs after iter49). PT-43..PT-49 in `plans/PENDING_TASKS.md`; findings FIND-1..9 in the ledger.
- **MVP-Finally (`engine/mvp-finally`, cut from `engine/iter49c-engine-finish` @ `1f15e8b`) — the stabilization/battery branch (NOT iterNN-numbered; iterations = commits). State at cut: suite 305, smoke 7/7, gold replay GATE PASS (0.788 / 1/47 / 2.17); registry rebuilt 93/1217; json_logs consolidated 428. Battery = NEXT session (owner-gated); report → research/surgeon/mvp-finally/.**
- **MVP-Finally MERGED to main — owner say-so 2026-09-16: merge `4559a33` (unrelated-histories `-Xsubtree=engine`, tag `mvp-finally`), suite 305 re-verified on the main checkout, git-tree.sh refreshed. Happy gate T4 PASSED pre-merge (Maria book 19 turns, G1/G2 PASS, steady TTFT p50 805ms, cache floors 2688/3712 on heavy turn-2+; turn-1 cold 2258 + 5 cache-0 acks = harness lacks greeting warm + caller speech time — PT-45/FIND-5 classes, no new failure). Battery deferred; next = `engine/chat-tests` branch: `--warm-greeting-ms 3000` across all states, gate = every transition TTFT <1000ms. Registry double-count documented: FIND-15/PT-50.**

- **iter52→iter64c LADDER MERGED to main — owner say-so 2026-09-22: merge `40b3b40` (`--no-ff engine/iter64-industry-gate`, tag `engine/iter64` @ `801fcdf`). Linear 21-commit ladder, zero conflicts: iter52 industry pin (+1800 cap, 20th vertical) · iter55 warm spans/hitl_ping (suite 346) · iter56 delta payload + {{var}} head strip · iter57 voice preflight/serve_voice.sh · iter58 mic RTT metrics + VOICE_TEST_TOKEN gate · iter59 rag_fire_mode (speech-window/round-sync/hybrid) + KB-everywhere + dedupe v2 0.90 + transport prewarm · iter60 AUD-1..10 · iter61 AUD-11..13 · iter62 lane restore E1-E11 + R1/R2 (suite 422) · iter63 rag_replay SDK + hybrid battery · iter64 pre-pin industry scope gate (suite 427) · iter64c MODEL LAW (llm.json=gpt-5.4, matches .env pin). Evidence: batteries 9/9 BOOK (chat-iter64-happy/gk @ 7145370), mic live test on :8022 (53 rounds, mic-iter64-live), 5-SOP audit research/surgeon/iter64-industry-gate/02_5sop_report.md + 03_exec_dashboard.md. Untested-on-main risk: none new — suite 427 ran on the identical tip. Branch refs hygiene: iter56 ref lags (points at iter57 T2-fix), iter64-elevenlabs-cutover shares 2cb810f (docs-only, PT-60 ElevenLabs migration = separate decision). git-tree.sh refreshed @ 40b3b40.**

- **iter66 ElevenLabs TTS cutover MERGED to main — owner say-so 2026-09-22: merge `84fd1ce` (`--no-ff engine/iter66-elevenlabs-cutover`, tag `engine/iter66` @ `e91af1a`). EL multi-stream-input adapter rewrite (text flush/cancel no longer closes the WS), factory transport lane (ulaw_8000 + browser PCM), prewarm gate protocol-match, `elevenlabs_inactivity_timeout` 180 replaces deprecated `latency_opt`, prod voice pinned by owner ear test (Sarah Casual&Modern `uG1JFy6xppqckhHCs2KG`, turbo_v2_5, speed 1.06, style 1.0, stability 0.5). Suite 439 on main post-merge (427 + 12 EL pins, `test_iter66_elevenlabs_cutover.py`). Cutover gate T7 (flip `tts_provider` in .env) stays OWNER-gated; merge ≠ flip. git-tree.sh refreshed.**

- **iter65 model-defaults (`engine/iter65-model-defaults` @ `031bb73`, rebased over iter66, merged `50a453f` (`--no-ff`, rebased over iter66 @ `fc45536`) — owner say-so 2026-09-22): T1 OpenAI boot-prewarm (ONE shared httpx pool + throwaway boot completion at app lifespan AND harness start; `/health llm_prewarm`) · T2 TTS speech sanitizer (`tts_sanitize_tokens`, strips `function_calls` tokens + `{ } [ ] =>` / `":` artifacts BEFORE the dedupe window, speech-only) · T3 dv carryover DAILY (`missed_calls_daily` schema + daily-aware `leak_inputs_present` + weekly=daily×5 in tools.py/llm.json JS/Retell validator `c790bdf`; Discovery/Closer DAILY ask + never-re-ask) · T4 mic spans (`eot_silence_wait`, `tts_first_byte`, mic_events filter) · T5 defaults (`openai_model`="gpt-5.4" MODEL LAW, `rag_fire_mode`="hybrid" PT-58, gpt-5.2 sweep). Evidence: suite 451 (427+24 new pins), battery 4/4 BOOK @ 031bb73 (chat-iter65-happy 22:47-22:52 + chat-iter65-rourke 22:53-22:55), turn-1 TTFT 842/803/836/739 ms ≤900 gate 4/4, sanitizer scan 0 hits, Susan 0 leak re-asks, 4-SOP audit `research/surgeon/iter65-model-defaults/03_full_call_analysis.md` (SALES 13-14/14 hire ×4), validator restarted + healthy :8003. Rollback: `llm_boot_prewarm=False` / `tts_sanitize_tokens=False` / `OPENAI_MODEL`+`RAG_FIRE_MODE` env overrides.**

- **iter80→87 LADDER + iter89 prompts + iter91 pack MERGED to main — owner say-so 2026-10-05 (S1+S2+S3 of the iter92c staged plan): S1 a10eec4 (--no-ff 730dfdb = the 2026-09-30 Real_MVP fold: iter70 lineage + iter80 EL-mic-flip + iter82 pause-nudge + iter83 stt-liveness + iter84 post-interrupt-backoff + iter85 eager-hold + iter86 warm-sync lineage + iter87 deterministic hold @ 02eb2ac), S2 a546454 (cherry-pick d158b4c = call_forensics.py SDK — iter86 branch tip is a forensics-only leaf; SDK otherwise would NEVER have landed), S3 6d9f277 (--no-ff 2b3e900 = iter89 prompt edits A/B/C + gk-iter89 evidence + iter91 round-B pack to config defaults + lane tooling; PENDING_TASKS.md doc-union conflict resolved, PT-74 widened row kept). Tree proof: diff of main vs 2b3e900 on engine/ = ONLY the two intended files (.env.example truth-fix 58fe765/f20bcc1 + call_forensics.py) — engine code BYTE-EQUAL to iter91 tip. Suite on merged main: 490 passed, 3 skipped, 0 failed in 88s (= the 493-state @ 2b3e900; the S1-only iter44 dead-store 401 hermeticity flake was cured by S3, as forecast). Tag engine/iter91 @ 6d9f277; GIT_TREE regenerated; numbering: iter92/92b/92c = zero-code rides per ZERO-CODE RULE (86cfb03). Findings to carry: FIND-35 (adopt-after-false-start race — reply never spoken; live owner test trace a6f7c585a68f; NEVER previously fixed — only commit touching the region ever = 542d3e8) + FIND-36 (unconsumed flux word confidences; no repair reply). Next: engine/92-fix-held-adopt off landed tip (owner-ordered fix-before-Twilio).**

- **iter95/sdk — TEST-LANE SDK LANDED straight to main per the owner-approved whitelist lane (AGENTS.md LAW0 v2.3, Mode A):** T1 instrumented run (`research/iter95-sdk/durations.txt`): **496 passed, 3 skipped, 0 failed in 90.75s** — the plan's 20-25-min premise is VOID (surgeon audit F4 confirmed; the ledger's 88s was right; the real cycle-killer was `-qq` hiding the verdict line — fixed: fast_suite.sh adds no `-q`, verdict always visible). T3 virtual-clock conversions CANCELLED by the binding decision gate (top cumulative offender ≈6s/file — no ≥5s offenders worth converting); T4 xdist SKIPPED by owner. `scripts/fast_suite.sh` verified live: usage exit 2 · targeted run "4 passed in 1.21s" · `--touched` selects exactly changed test files · empty selection exits 0 · runs from any worktree via the canonical venv path. Docs + dossier landed inside 784a424 (concurrent iter94/95 session's commit); dossier: `tasks/surgeon/iter95-sdk-fast-test-lane/` (audit found 14 flaws incl. wrong ITERATIONS path, impossible Mode B lane name, moved iter95 base).
- **iter93+94+95 MERGED to main — owner say-so 2026-10-06 (plan plan_v5_iter94_95_validation_close):** 2f7644f (--no-ff e57d2b2 iter93 held-adopt race; epoch races + observability pins, suite 496/3/0 on lane) · 5681513 (--no-ff 3ac544a iter94 silence ladder stretch: one rung per silence stretch, agent turn NEVER re-arms a running ladder) · eb54217 (--no-ff 63e013c iter95 warm cancel-not-wait: prewarm_abort_cap_ms=400 CEILING min(wait,cap), cap=0=legacy shield; 4 fix-pins fail pre-fix). Zero conflicts, tags engine/iter93|94|95, GIT_TREE regenerated. LIVE VALIDATION (test/iter93-94-95 lane 8049/8050 @ 9eb341c==merged content): scripted Maria happy-path BOOKED live (validator uid r3F7uGWKd4M7baLatzPczi 08:39:42, cancelled+verified post-test) · owner REAL Twilio call +1888 595 8927 via voicetest.diallux-ai.site (trace 152d98c0c8d73f27680d950f2914e1bb, 9min 63turns 37 rounds): TTFT p50=962 p90=1224, e2e_turn p50=1882 p90=4031, iter95 outcome=aborted x12 waited_ms=0, ladder idle, 0 held_swallow, 0 degraded RAG, 0 errors, reached contact_details (turn_cap end, no booking). NEXT GOAL (owner 2026-10-06, hard): e2e_turn p50 <= 1200ms on telephony — session state + latency levers: plans/SESSION_2026-10-06_iter93_94_95_working-but-not-great.md. Findings carried: F-92b-5 sanitizer gap (spoken protocol text), caller-script journal crash on server-side ws close (unhandled ConnectionClosedOK, no journal written), iter77-twilio parked merge @ 8c5f72f still owner-decision.**
- **iter93+94+95 MERGED to main — owner say-so 2026-10-06 (plan plan_v5_iter94_95_validation_close):** 2f7644f (--no-ff e57d2b2 iter93 held-adopt race; epoch-race + observability pins, suite 496/3/0 on lane) · 5681513 (--no-ff 3ac544a iter94 silence ladder stretch: one rung per silence stretch, agent turn NEVER re-arms a running ladder) · eb54217 (--no-ff 63e013c iter95 warm cancel-not-wait: prewarm_abort_cap_ms=400 CEILING min(wait,cap), cap=0=legacy shield; 4 fix-pins fail pre-fix). Zero conflicts, tags engine/iter93|94|95, GIT_TREE regenerated. LIVE VALIDATION (test/iter93-94-95 lane 8049/8050 @ 9eb341c == merged content): scripted Maria happy-path BOOKED live (validator uid r3F7uGWKd4M7baLatzPczi 08:39:42, cancelled + verified post-test) · owner REAL Twilio call +1 888 595 8927 via voicetest.diallux-ai.site (trace 152d98c0c8d73f27680d950f2914e1bb, 9min 63turns 37 rounds): TTFT p50=962 p90=1224, e2e_turn p50=1882 p90=4031, iter95 outcome=aborted x12 waited_ms=0, ladder idle, 0 held_swallow, 0 degraded RAG, 0 errors, reached contact_details (turn_cap end, no booking). NEXT GOAL (owner 2026-10-06, hard): e2e_turn p50 <= 1200ms on telephony — session state + latency levers: plans/SESSION_2026-10-06_iter93_94_95_working-but-not-great.md. Findings carried: F-92b-5 sanitizer gap (spoken protocol text), caller-script journal crash on server-side ws close (unhandled ConnectionClosedOK, no journal written), iter77-twilio parked merge @ 8c5f72f still owner-decision.**
- **iter102 roadmap session (2026-10-10, ZERO CODE):** wave shape evaluated + branch structure owner-designed — STACK branch = one branch carrying PT-8→PT-5→PT-6→PT-4→PT-11 (owner run order, per-PT clean commits, cut from main 3daf11c) → chain: PT-1 branch off stack tip → PT-2 off PT-1's tip; PT-10 own branch after PT-11's wire (owner dictation pending); PT-9 = LAST, `engine/100-split-round` from `engine/98-service-tier` tip; PT-7 folded; PT-3 parked (decided after PT-1). Serial merge-back at the END, LAW 0 per merge, NO squash. Eval evidence: `research/surgeon/iter102-fix-wave/01_wave_eval.md` (per-PT table, suite baseline 5F red set, dependency check). Next session = iter102b master-plan authoring: `plans/plan_v5_iter102b_wave_master_plan_authoring.md` (per-ticket code analysis → 02_edit_specs → THE wave master plan, iter81 master-ladder style). Docs: main 06e673b.**
- **iter102b master-plan session (2026-10-10, ZERO WAVE CODE):** THE wave master plan + 11 per-iteration plans landed (`plans/iter102_plus_wave_edits/` = one MD per PT: PT-1..PT-11 incl. PT-3 PARKED / PT-7 FOLDED) — per-ticket code analysis vs origin/main (all eval line-refs re-verified; PT-5 memo normalize-rule + PT-8 field-names edit-specs closed inside their plans; PT-11 suspected root cause: `/twiml` reads `From` from query params while Twilio voice webhooks POST it form-encoded → `<Parameter>` silently empty — trace confirms at the stack session). `lf.py pair` SDK cherry-picked to main (owner order 2026-10-10, zero-conflict vs `c877e51` — only `engine/scripts/lf.py`, +64). Wave branch law unchanged (06e673b): stack branch (PT-8→PT-5→PT-6→PT-4→PT-11, per-PT commits, cut from main) → PT-1 → PT-2 → PT-10 (post-dictation) → PT-9 LAST from 98 tip. Next = owner GO → W1 stack cut. Docs: main (this commit).**
