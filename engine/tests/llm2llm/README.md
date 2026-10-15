# LLM-to-LLM Testing — "Faking a Call" on the Self-Hosted Brain

Your SOP, injected as a feature of this package. Preferred first-pass testing
method: an LLM (GPT-4o) plays the caller against the agent's brain — faster and
more adaptive than scripted tests. This is how you validate the agent BEFORE
wiring STT/TTS/telephony, and after every prompt change.

---

## The one insight that changes vs Retell

In Retell you had to **create a chat agent** to do this, because a voice agent
cannot be driven by `create-chat-completion` — the brain sat behind Retell's
audio infrastructure. Here **the brain is a plain text-in/text-out LangGraph
invocation**: Deepgram, Cartesia and Twilio wrap OUTSIDE it in the media
pipeline (`diallux/media/`). So "faking a call" is not an emulation at all —
it is the same graph, same prompts, same tools, same checkpointer, driven by
typed text instead of audio. No clone, no extra agent, no channel split.

What the media pipeline does per utterance:

```
Deepgram STT transcript ──▶ graph.astream({"user_text": transcript}) ──▶ tts_token stream ──▶ sentence gate ──▶ TTS
```

What the harness does per utterance (identical boundary, typed text in):

```
caller LLM line ──▶ graph.astream({"user_text": line}) ──▶ collect tts_token stream ──▶ agent reply
```

Same thread (`thread_id = call_id`), same state machine, same executor. If it
passes here, the brain works; what remains for voice is purely the media layers.

---

## Mapping: your Retell SOP → this runtime

| Retell SOP | This runtime |
|---|---|
| `POST /create-chat` | one `thread_id` per run; first `astream` payload includes `rt.initial_state(call_id, persona_dvs)` |
| `retell_llm_dynamic_variables` | `persona["dynvars"]` merged over `default_dynamic_variables` (production filters to typed `DynamicVariables` fields) |
| `POST /create-chat-completion {content}` | `graph.astream({"user_text": line}, config={"configurable": {"thread_id": ...}})` |
| `messages[role=agent]` | the `tts_token` custom stream (exactly what would be spoken) |
| `role=tool_call_invocation` | `executor.trace` (full tool call + args + response, per turn and cumulative) |
| `end_call` detection | graph state `ended: True` (the `end_call` tool sets it) |
| `GET /get-chat/{id}` | `graph.aget_state(config)` → history/dvs/state + `executor.trace`; **and** Langfuse traces (`--langfuse`) |
| `chat_cost` | token usage in metrics; per-turn brain latency (`agent_ms`) is measured instead — the currency you actually care about (<1500 ms budget) |
| "target must be a chat agent" | **gone** — the graph is channel-agnostic |
| create one chat agent per session, delete after | nothing to provision — a thread id is free; logs persist in `tests/llm2llm/json_logs/` |

## Quickstart

```bash
# 0) hermetic plumbing smoke — no keys, FakeLLM agent + scripted caller
python tests/llm2llm/harness.py --offline

# 1) ALWAYS the happy path first (SOP smoke test)
export OPENAI_API_KEY=sk-...
python tests/llm2llm/harness.py --personas happy

# 2) stress batteries
python tests/llm2llm/harness.py --personas stress     # mean/dumb/problematic/enquiry/ai-question
python tests/llm2llm/harness.py --personas curve      # Brenda/Gene/Frank/Ray gauntlet

# 3) single persona by name substring
python tests/llm2llm/harness.py --personas Maria

# 4) full battery, happy-gated (stress aborts if happy failed — SOP run order)
python tests/llm2llm/harness.py --personas all

# 5) migration A/B: SAME personas against your LIVE Retell chat agent
export RETELL_API_KEY=key_... CHAT_AGENT_ID=agent_...
python tests/llm2llm/harness.py --transport retell --personas happy

# 6) with RAG (pgvector) and Langfuse traces
python tests/llm2llm/harness.py --personas happy --rag --langfuse
```

Exit code 0/1/2 (pass/fail/config) — CI-able. Report: `eval/llm2llm_report.json`.
Per-run logs (full transcript, tools order, dvs, latencies): `tests/llm2llm/json_logs/`.

## Slots: mock vs live

Default is `--slots mock`: webhooks run against `tests/mock_webhooks.py`, which
implements the **live production contract** (HMAC-signed calls, real response
shapes: slot lists, `booked` + `booking_uid`, `slot_verified`, ...). Nothing
touches `slots.diallux-ai.site`, so `create_livecall_booking` cannot create a
real booking.

`--slots live` points the executor at the real endpoints (keys from `.env`).
`/today` and slot-lock are safe reads; **book-livecall creates real bookings** —
only run it against a test slot server. The mock keeps the response contract
identical, so scoring behaves the same either way.

## Scoring (the SOP rules, kept)

- **BOOKED** = `create_livecall_booking` fired with a confirmed **non-empty**
  `booking_uid` (response or dvs), or server-confirmed dvs
  (`booking_uid` + `booking_verified`). **Never a fixed timestamp** — live
  availability varies by date; assert non-emptiness, not a value.
- Reschedule/cancel intents (`status: reschedule_options | released`) do NOT
  count as booked.
- `expect` (`book | no-book`) per persona; the run passes iff outcome == expect.
- Production build additionally reports `gate_rejections` — how many illegal
  transitions the executor refused (want 0 in happy paths, ≥1 in the
  premature-transition scenarios).
- Per-turn `agent_ms` is brain-only latency (no STT/TTS) — your budget check
  before the media layers add their ~300–500 ms.

## The two bugs from your Retell notes, translated

1. **"Never break when the agent returns no speech"** — in Retell,
   `create-chat-completion` can return empty `agent` content mid-tool. Here,
   tool rounds execute INSIDE one graph invocation (the state node loops via
   conditional edges until a pure-speech round, `end_call`, or
   `MAX_TOOL_ROUNDS`), so a turn always completes. The harness still keeps the
   guard: an empty non-ended turn gets a neutral `Mm-hmm.` backchannel, never a
   break (bounded by `--max-turns`, default 30).
2. **"Tool arguments are a JSON string, not a dict"** — Retell's
   `tool_call_invocation.arguments` is serialized JSON. Here `executor.trace`
   stores parsed args already (`{"tool", "args", "resp", "dvs_patch", ...}`).
   The **retell transport** keeps the guarded `json.loads` (Bug 2 lives there).

## Full-context technique (kept)

Every turn the caller LLM receives the ENTIRE conversation history —
everything it said plus the agent's latest reply — so the caller never
contradicts itself or forgets details it already gave. Exactly your SOP.

## Personas are per-agent — these are per-THIS-agent

The persona **types** are reusable categories; every concrete persona is
authored from scratch against the agent under test. The 13 personas in
`personas.py` are the port of your Retell battery — they were already built
from scratch for THIS agent (Linda, V7.9 slot-lock: Intake → Discovery →
Closer → Offer → contact_details → ConfirmSlots → VerifyLead → Booking →
Closing, webhooks to slots.diallux-ai.site), so they port 1:1: same narratives,
same seeded `dynvars` (`callback_number` etc. — validated against
`default_dynamic_variables` by `test_harness_units.py`), same `expect` flags.

Types in the battery (add more per agent, same rules):

| type | purpose | watch for |
|---|---|---|
| happy-path (×4, REQUIRED) | smoke test — main flow end-to-end | all fields collected in order, booking fired, clean confirm |
| mean (Carlos) | emotional resilience | calm, one question per turn, no mirroring hostility |
| dumb (Pedro) | patience + edge cases | rephrases, no stacked questions, handles "I don't know" |
| problematic (Sofia) | adaptability | rolls with contradictions and mid-call changes |
| enquiry-only (Jorge) | graceful no-book | handles "let me think" without pushiness |
| ai-question (Daniel) | AI honesty | honest, reassuring, no dodging |
| curve ×4 (Brenda/Gene/Frank/Ray) | objection gauntlet, curmudgeon, hostile trust, luddite | honesty under pressure, callback path, no fake guarantees |

**Rules for new personas** (from your SOP, still binding): read the agent
first (its prompts and toolset here: `agent/llm.json` or
`diallux/prompts/*.md`); define expected behavior + pass condition; seed
`dynvars` honestly with exactly the values the agent expects; set `expect`;
review rules so the caller is an **adaptive LLM, not a script** — persona
rules exercise the agent, they do not choreograph it.

Happy-path discipline (kept verbatim): full address/number ready, no
curveballs, **only confirms once the agent reads back a specific day + time
and asks clearly** — a bare "okay"/"mm-hmm" is never a confirmation. Real
numbers only (`+13125551234`), never 555 — Cal.com rejects them.

## Run order (SOP, enforced)

```
1. Happy Path (always first)   → smoke test: main functions work
2. Mean (1–2)                  → emotional resilience
3. Dumb (1–2)                  → patience + edge cases
4. Problematic (1–2)           → adaptability
5. Extra types                 → enquiry-only / ai-question / curve
```

`--personas all` runs them in battery order and **aborts before stress
personas if any happy-path run failed** ("no point stressing a flow that does
not work"). Override with `--no-happy-gate`. If a stress persona fails, fix
the prompt before running the rest.

## When to use vs the scripted harness

- **This harness** (LLM-to-LLM): after any prompt change, edge-case
  exploration, regression spot-checks (happy + 3–5 stress personas). Costs
  caller tokens (~80 per turn ≈ pennies per call) + agent tokens.
- **`scripts/eval_accuracy.py`** (already shipped): deterministic scripted
  scenarios with a FakeLLM — the "When to Move to Python Scripts" tier of
  your SOP: reproducible, free, hermetic, CI-able, no caller-LLM variance.
  LLM-to-LLM first, scripted for regression.

## Langfuse

`--langfuse` creates one trace per persona run (`llm2llm-<transport>-<hex>`
session) with a generation per LLM round, a span per tool call, and the final
transcript/dvs on the root trace — review failed personas in your self-hosted
Langfuse exactly like production calls. Keys come from `LANGFUSE_*` env vars.
Tracing is fail-safe: if Langfuse is unreachable the run continues.

## Cost sketch (per full battery, 13 personas × ~10 turns)

- Caller GPT-4o: ~80 output tokens/turn → ~10k tokens ≈ **$0.01–0.03**.
- Agent: your model at your provider (gpt-5.4-class ≈ 15–20k input
  tokens/call with inline KBs, ~3.5k with RAG) — the report records per-turn
  latency; token usage lands in Langfuse when `--langfuse` is on.
- Mock slots: free. A/B against Retell: Retell chat costs apply (their
  `chat_cost` is captured in the report).
