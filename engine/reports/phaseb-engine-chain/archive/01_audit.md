# 01 — Phase B AUDIT (engine-owned booking chain + interstitial fillers)

- Date: 2026-09-08
- Coder: surgeon session (analysis-only; no code touched)
- Repo: `/home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/dialux-langgraph-production-v5`
- Baseline: `iter32-silent-round-gate` @ `596f1bb` (branch, NOT merged; parent `48d6ad3`)
- Reference: prior stash `stash@{0}: On iter29-30-one-trip: phaseB-wip` (abandoned sketch — see §9)

## 1. What EXISTS (verified by reading code this session)

### 1.1 The chain as the LLM walks it today (battery evidence, 2026-09-08 10:19–10:27)

The 7 BOOK battery calls each ended with a turn like Maria t12: agent speech ("Thanks, Maria! You're booked…") + 9 tool calls in ONE round: `extract_confirm_details, validate_lead, transition_to_VerifyLead, verify_lead_data, transition_to_Booking, create_livecall_booking, record_booking_uid, record_booking_outcome, transition_to_Closing` — followed by silent rounds looping ConfirmSlots→VerifyLead→Booking→Closing because each state's node only knows its own tools and the walk needs 4 state hops. Langfuse: Booking 22 gens 100% silent, VerifyLead 16 gens 100% silent, ConfirmSlots 49/63 silent; the booking turn wall = 15–24s (max Maria t12 23.6s). Caller-visible dead air.

State topology (agent/llm.json, verified):
- `ConfirmSlots` tools: check_current_date, query_livecall_slots, extract_confirm_details, validate_lead. Edge → VerifyLead (gate: slot_verified required).
- `VerifyLead` tools: verify_lead_data. Edge → Booking (gate: data_verified, selected_time, prospect_timezone required).
- `Booking` tools: create_livecall_booking, extract_booking_intent, record_booking_uid, record_booking_outcome. Edge → Closing (gate: booking_verified required).
- `Closing` tools: none. end_call via general tools; goodbye-auto-end engine rule.

### 1.2 Existing engine machinery (diallux/graph/builder.py + tools.py — line-verified)

1. `_deterministic_node` (builder.py:444–475) ALREADY runs after every state round, no LLM: leak-math auto-compute (`calculate_monthly_leak` when inputs present, `leak_inputs_present`), today-prefetch (`check_current_date` when entering ConfirmSlots without today_date). Sets `engine_fired=True` → `route_after_deterministic` routes back to the state node so the model speaks the news. **This is the exact pattern Phase B extends — proven 2 iterations (iter29/30).**
2. `ToolExecutor.execute` (tools.py:130–147) — the single door for tools: repeat-guard (already_captured), extract / custom(webhook+HMAC+retry) / code dispatch, transitions with `_gate_check_edge` hard gates (required/presence params), end_call truth gates. `outcome.dvs_patch`, `outcome.new_state`, `outcome.ended`.
3. Hard gates already bounce premature transitions with coaching messages (GATE_FAIL_MESSAGES tools.py:64–98; live battery: Susan bounce missing first_name self-corrected in 1 round; Jorge missing livecall_agreed).
4. `get_stream_writer` TTS channel (builder.py:43, 242, 345–347): `writer({"tts_token": …})` streams spoken text to the voice layer. **A filler can be emitted here WITHOUT any LLM call.**
5. `GraphState` (diallux/state.py:22–47): `state_name, dvs, history, round_spoke, engine_fired, last_spoken, rounds_left, last_round_tool_calls, ended` — all merge semantics in place (`dvs` merge_dict, `history` operator.add).
6. Routing (builder.py:495–528): `after_state` → pure speech round finalizes turn; tool rounds → deterministic; `route_after_deterministic`: ended→finalize; engine_fired→state node (model speaks); one-trip (round_spoke + all FIRE_AND_FORGET) → finalize; else loop.
7. iter32 gate (builder.py:310–320): silent all-mechanical round → next round `tools=[]` (forced speech). `_MECHANICAL_RE` includes extract_*, markers, record_booking_outcome, end_call. Chain tools NOT in it (by design — result-dependent).
8. Suite 133 green on `596f1bb`; prompts drift test compares prompts/*.md ↔ llm.json (both must stay in sync — Booking/VerifyLead/ConfirmSlots prompts all carry the LLM-walk instructions).
9. Mock webhooks (tests/mock_webhooks.py): validate_lead echoes slot_verified, verify_lead_data echoes data_verified (with selected_time format checks), book-livecall returns booking_uid + booking_verified (reschedule/cancel branches keyed on booking_intent), record-booking-uid echoes uid. Fixed UID `qeTqHuZ1EDzH8bxEdhPQ6H`.

### 1.3 Latency ground truth (this session's T3/T4 data)

- Booking-turn walk = 8–10 gens at ~1.6–2s each → 15–24s caller wait.
- Battery: 480 gens / 230 turns = 2.09 gens/turn; chain-state gens (ConfirmSlots 63 + VerifyLead 16 + Booking 22) = 101 = 21% of all gens.
- Silent attribution: chain-involved 158 silent gens (60.8% of all silence).
- TTFT: 55% of turns open with ≥1 silent gen before first speech (pattern Ss 40%/SSs 14%) → first word at 3.4–5.0s instead of 1.7s.

## 2. What's MISSING (for Phase B)

1. **No engine chain-runner.** The deterministic node only computes leak math + today. Nothing walks validate→verify→book when all booleans line up.
2. **No interstitial fillers.** The only mid-walk speech is the LLM's state-prompt filler (VerifyLead/Booking prompts prescribe "One moment…"). Works only on rounds where the model speaks; the BETWEEN rounds are silent. No `tts_token` emission from engine-side code exists today.
3. **No state-hop batching.** Each hop (ConfirmSlots→VerifyLead→Booking→Closing) is a separate LLM round even when zero new conversation happened. The state_name update path (`outcome.new_state` → updates["state_name"]) exists but only advances one hop per round.
4. **No engine-side args builder.** Chain args are templated by the MODEL today ("pass {{first_name}} exactly as stored…"). An engine chain needs the same arg assembly deterministic (from dvs).
5. **No filler voice.** Filler lines are LLM-generated per state prompts. Engine fillers need a small canned pool + anti-repeat rule (general_prompt already bans repeating a filler verbatim — engine must respect the same rule across its own emissions, and not collide with what the model said).

## 3. Risks / unknowns found in the audit

1. **Reschedule/cancel branches** (Booking prompt: caller changes mind after confirmation) — `extract_booking_intent` MUST stay LLM-decided; engine chain must only fire on the happy path (booking_intent empty) and hand back to the model otherwise.
2. **verify_lead_data selected_time checks** (mock: ISO fast-path injection; human pick must be contained in requested_slot) — engine must pass `slot_options={{requested_slot}}` verbatim like the model does, or verify fails.
3. **validate_lead actions list** — on `slot_verified:false` the response carries Say-lines the MODEL must speak (conversation repair). Engine chain must abort → route to state node with the actions in a tail block, not retry silently.
4. **`record_booking_outcome` server-owned** — booking_verified/booking_failed are SERVER_OWNED via webhook response_variables (create_livecall_booking returns booking_verified) and via record_booking_uid; the engine must not write them manually (extract rejects server-owned — tools.py:246–258).
5. **History/tool_result parity.** The harness asserts tool ORDER in transcripts (`tools_order` in json_logs; PASS prints the full list). Phase B keeps all 9 tools firing (engine-side) so tools_order stays identical-ish — but the ORDER/round-grouping changes (engine batches). Tests asserting per-round grouping may need updates (audit of test files needed in action plan T2).
6. **Prompts drift test** (133 suite): prompts/*.md ↔ agent/llm.json must stay byte-identical. Any ConfirmSlots/VerifyLead/Booking prompt edit = edit BOTH. Prefer NO prompt edits (chain instructions become dead text but harmless) OR minimal edit in both places.
7. **`_last_call` repeat-guard** (tools.py:154–161): engine firing validate_lead right after the model fired it with identical args → already_captured response. Chain-runner must pass through execute() (guard handles it) and treat already_captured as "already done, continue".
8. **TTFT of the FINAL spoken line**: engine chain finishing → route to state node → LLM speaks "you're booked" = 1 gen (~1.6s). Good. But the model round needs a REASON to speak — the tail (STATE BLOCK) must carry the booking outcome (booked_human) so the model reads it. `_state_block` renders dvs lines; booking_uid/booked_human already in dvs after chain — tail shows them.
9. **Stash conflict**: `stash@{0} phaseB-wip` touches the same files (builder/llm/tools/llm.json/prompts). DO NOT pop it. Start clean from `596f1bb` per git discipline (new branch off iter32).

## 4. Design decisions the audit supports (input to 02_action_plan)

1. Extend `_deterministic_node` (pattern proven) with a **chain-runner block** gated on: state==ConfirmSlots AND slot_verified AND selected_time AND prospect_timezone AND callback_number AND first_name (the exact booleans that gate the edges today) AND booking_intent empty.
2. Chain-runner executes via `executor.execute` (keeps HMAC, retries, gates, repeat-guard, response_variables → dvs_patch) in order: validate_lead (if not slot_verified) → transition_to_VerifyLead → verify_lead_data → transition_to_Booking → create_livecall_booking → record_booking_uid → (record_booking_outcome writes booking_verified/booking_failed server-side via webhook response) → transition_to_Closing. Abort on ANY non-ok with the response surfaced to the model.
3. **Fillers**: engine emits `writer({"tts_token": …})` BEFORE each slow webhook (create_livecall_booking, verify_lead_data, validate_lead) from a canned pool (≤6 lines, no verbatim repeat within call — tracked in runtime state). Caller hears "one moment… booking that for you now…" during the walk instead of dead air. Zero LLM cost.
4. **Post-chain speech**: set `engine_fired=True` (existing route sends the model back to the state node — now Closing) where it speaks the confirmation from the tail dvs (booked_human). ONE LLM round for the whole booking turn.
5. Model keeps: slot OFFERING conversation (ConfirmSlots dialogue), extract_booking_intent reschedule/cancel, all fillers it already speaks, end_call, goodbye.
6. Prompts: NO prompt edits in Phase B v1 (drift test stays green; dead chain-instructions text is harmless — the model just won't fire those tools because the engine already did; repeat-guard answers already_captured).

## 5. Verified file inventory (for the action plan's File Map)

| File | Role | Lines of interest |
|---|---|---|
| `diallux/graph/builder.py` | graph, state nodes, deterministic node, routing, TTS writer | 64–75 (_MECHANICAL_RE), 202–225 (_state_block), 242+ (writer), 291–430 (state_node + gate), 444–475 (_deterministic_node), 495–539 (routing/_build) |
| `diallux/graph/tools.py` | executor, gates, HMAC webhooks, repeat-guard, server-owned | 64–98 (GATE_FAIL_MESSAGES), 100–147 (execute), 148–236 (_execute_inner incl. transitions/end_call), 238–258 (_exec_extract), 260–271 (_gate_check_edge), 272–332 (_exec_custom HMAC), 334–339 (_sign) |
| `diallux/state.py` | GraphState | 22–47 |
| `diallux/schema.py` | DynamicVariables, SERVER_OWNED | booking_confirmed:91; server-owned list in tools.py |
| `agent/llm.json` | state machine + tool schemas + state prompts (drift-pinned to prompts/*.md) | ConfirmSlots 635+, VerifyLead 820+, Booking 902+ |
| `tests/mock_webhooks.py` | mock contract (validate/verify/book/record-uid) | 160–235 |
| `tests/llm2llm/harness.py` | caller-sim harness; tools_order capture | 163–172, 351, 424 |
| `tests/test_iter32_gate.py` | gate tests (6) | whole file |
| `scripts/silent_rounds.py`, `scripts/state_latency.py` | meters (read-only REST) | whole files |

## 6. Success gates for Phase B (measured, from this session's data)

| Gate | iter32 measured | Phase B target |
|---|---|---|
| booking-turn caller wait | 15–24s | ≤6s (fillers cover it) |
| gens/turn (BOOK calls) | 2.44 | ≤1.3 |
| battery gens | 480 | ≤300 (gate) |
| silent% | 54.2% | ≤30% (gate) |
| TTFT (first word) | 1.7–5.0s | ≤1.7s p50 |
| expect-match | 12/13 | ≥12/13 (Pedro unchanged — retrain is separate) |
| suite | 133 | all green (+ new chain tests) |
| chain tools fired per booking | 9 (LLM rounds) | 9 (engine-side, 0 LLM rounds) |

## 7. What Phase B explicitly does NOT touch

- extract_* layer, transitions the MODEL initiates in conversation states (Intake→…→contact_details), end_call gates, goodbye-auto-end, iter32 silent-round gate, prompts/*.md + agent/llm.json (v1), RAG, history window, TTS voice, webhooks themselves, harness personas.

## 8. Open items for the action plan to resolve

1. Exact abort-surfacing format when a chain step fails (tail block text shape).
2. Filler pool copy (6 lines, English, matching Linda's voice, never verbatim twice in one call).
3. Where filler-anti-repeat state lives (runtime vs dvs — must NOT be a dvs the model sees).
4. Whether record_booking_outcome stays model-fired (it writes server-owned anyway → engine can fire it; audit says engine-side, keeps tools_order identical).
5. Test updates: any per-round tool-grouping assertions; new engine-chain hermetic tests (FakeLLM: chain fires 0 LLM rounds, filler emitted, abort path).
6. Harness PASS criteria unchanged (tools_order will still show the 9 tools — engine-executed; verify harness counts engine-executed tools the same way — it reads `metrics.calls` + `last_round_tool_calls`; engine rounds must append to the same fields or tools_order loses entries — ACTION PLAN MUST SOLVE: chain-runner writes executed list into state so the harness transcript row keeps all tool names).

## 9. Why the stash sketch is abandoned

`stash@{0} phaseB-wip` predates iter31 C3/C3b (tools bytes/cache architecture) and iter32 (payload gate) — popping it onto today's builder would conflict on every hunk and re-introduce a pre-C3 request layout. Audit verdict: discard; re-derive on `596f1bb`.
