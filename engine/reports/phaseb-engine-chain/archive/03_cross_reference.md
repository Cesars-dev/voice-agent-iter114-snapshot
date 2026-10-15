# 03 — Phase B CROSS-REFERENCE (audit ↔ action plan)

- Date: 2026-09-08
- Method: every audit issue checked against the action plan; every action-plan unit checked against audit facts. Flaws listed here are resolved in `04_master_plan.md`.

## 1. Coverage matrix (audit issue → plan unit)

| Audit finding | Plan unit | Covered? |
|---|---|---|
| §2.1 no engine chain-runner | U3 | ✅ |
| §2.2 no interstitial fillers | U2 (pool + emitter) | ✅ |
| §2.3 no state-hop batching | U3 (state_name tracking per step) | ✅ |
| §2.4 no engine args builder | U4 `_chain_args` | ✅ |
| §2.5 filler voice/pool/anti-repeat | U2 | ✅ |
| §3.1 reschedule/cancel stays LLM | U3 guard `booking_intent` empty + U7 test 2 | ✅ |
| §3.2 slot_options verbatim | U4 verify args include `slot_options={{requested_slot}}` | ✅ |
| §3.3 validate_lead actions → model repair | U3 abort branch + U5 tail | ✅ |
| §3.4 server-owned booking_verified | U3 DECISION (fire record_booking_outcome for parity; extract rejects manual write; create_livecall_booking response_variables write booking_verified) | ✅ with nuance (see §2.4 below) |
| §3.5 tools_order parity | U3 returns `last_round_tool_calls`/`metrics` + U7 test 8 + U8 verification | ✅ |
| §3.6 prompts drift test | Plan v1 touches NO prompts/llm.json | ✅ |
| §3.7 repeat-guard already_captured | Runner goes through `execute()` — guard handles; treat `already_captured` as continue (NOT in abort list) | ✅ (explicit in master plan) |
| §3.8 post-chain speech reason | engine_fired → state node; tail renders booked_human/booking_uid dvs | ✅ |
| §3.9 stash conflict | Branch fresh off `596f1bb`; stash untouched | ✅ |
| §6 success gates | Plan §3 verification ladder | ✅ |
| §8 open items 1–6 | U5 (abort format), U2 (filler copy), U1 (fillers_said in GraphState, not dvs), U3 DECISION (record_booking_outcome engine-side), U7 (new tests), U8 (harness accounting) | ✅ |

## 2. Contradictions / flaws found (must fix in master plan)

### 2.1 `chain_ran` (GraphState) vs `chain_done` (dvs) redundancy
U1 defines `chain_ran`; U3 also writes dvs `chain_done`; U6 adds schema `chain_done`. TWO idempotency flags for one purpose = drift risk.
**Resolution:** keep ONLY dvs `chain_done` (idempotency, engine-write-only). Drop `chain_ran` from U1. `fillers_said` (GraphState) and `chain_aborted_step/msg` (dvs, tail-rendered) stay.

### 2.2 Abort-branch return shape is inconsistent
U3 sketch mixes `patch.update(...)` with a bespoke return dict referencing `patch` — `patch` is the node's dvs accumulator; the abort path must return ONE clean dict (same shape as the success path).
**Resolution:** single return; `dvs_patch` carries `chain_done` popped on abort + `chain_aborted_step/msg` set; `engine_fired: True` on both paths; `state_name` = wherever the walk stopped.

### 2.3 `_IN_STREAM` is invented
`get_stream_writer()` isn't gated by a module constant anywhere; state_node calls it directly (builder.py:242). Outside a stream context it raises.
**Resolution:** `try: writer = get_stream_writer() except Exception: writer = None` inside the chain-runner. No new constant.

### 2.4 record_booking_outcome nuance (audit §3.4, plan DECISION)
Firing it with `{"booking_verified": True}` returns `status:"ok"` + `rejected_server_owned` — harmless and keeps tools_order parity. But the ABORT condition in U3 lists `"error"` etc.; `already_captured` is NOT an abort (repeat-guard path — the model may have fired it first in the same conversation). Also `record_booking_outcome` args must be exactly `{"booking_verified": True}` — and if the ENGINE fires it, a LATER model attempt gets already_captured (desired).
**Resolution:** abort statuses = `{"gate_failed","incomplete","unfixable_format","book_failed","error","invalid_uid","uid_not_found","request_failed"}`; continue statuses include `"ok"`, `"already_captured"`.

### 2.5 The "Maria one-round case" must be covered explicitly
Battery evidence: Maria t12 fired ALL 9 chain tools in ONE model round (state ends in Closing). The runner's trigger (`state_name == "ConfirmSlots"`) correctly NEVER fires for that shape — no double-booking. But the SPLIT shape (model fires extract_confirm_details+validate_lead only → silent state-hop rounds begin) is the one Phase B rescues.
**Resolution:** documented; trigger condition unchanged; U7 adds test 9 (all-in-one model round → chain must NOT fire; state already Closing).

### 2.6 selected_time ISO fast-path response is NOT in response_variables (existing behavior)
Mock verify_lead_data returns `selected_out["selected_time"]` but the tool's response_variables map only `data_verified/callback_number/first_name/last_name` — the ISO injection never reaches dvs (today the model re-captures via extract_confirm_details if needed). NOT a Phase B blocker: engine passes `{{selected_time}}` as stored to create_livecall_booking (same bytes the model would).
**Resolution:** no change; noted as pre-existing contract.

### 2.7 Fillers are TTS-only — harness verification path
`tts_token` custom events feed the VOICE runtime; the llm2llm harness transcript will NOT show engine fillers (they're not assistant_text). Battery verification of fillers = `fillers_said` in final state + `phaseb:chain_ok` span output + hermetic stream test (U7 test 5 captures writer events via `astream(stream_mode="custom")`).
**Resolution:** verification ladder uses those three signals; no harness change.

### 2.8 `fillers_said` merge semantics
GraphState lists are plain replace-on-return (only `history` is operator.add, `dvs`/`metrics` merge_dict). The node returns `fillers_said` wholesale (cumulative) — correct, but the runner must build it as `state.fillers_said + new lines` and return the full list.
**Resolution:** master plan code returns the complete list.

### 2.9 Test-audit gap: which existing tests assert per-round tool grouping?
Audit §8.5 said "audit of test files needed" — the action plan didn't do the grep. Master plan MUST include a preflight task: `grep -rn "tools_order\|last_round_tool_calls\|transition_to_" tests/test_*.py` and read hits before U3 lands. Known today: `tests/test_iter32_gate.py` (gate behavior, FakeLLM — likely unaffected), window/drift tests (prompt bytes — unaffected). Harness PASS prints tools list (log only, no assertion on grouping). Risk assessed LOW but must be verified, not assumed.

### 2.10 Filler anti-collision with model fillers
Model already speaks "One moment while I check availability" (ConfirmSlots step) and state-prompt hold-lines. Engine fillers fire only AFTER the model's turn already spoke — no same-round collision possible (different nodes). Cross-turn: general prompt bans model verbatim repeats; engine pool is disjoint phrasing from state-prompt samples except generic "one moment". Pool line 1 "One moment please." could echo the model's earlier "One moment while I check availability" — acceptable (not verbatim-identical), but master plan tweaks pool line 1 to "One sec — finishing that up." to keep distance.

### 2.11 get_stream_writer inside `_deterministic_node`
The deterministic node is a LangGraph node invoked under `astream` in both harness and voice runtime → writer available. Hermetic unit tests calling the method directly → writer None → fillers skipped (test 5 must call through `astream`, not direct method call).
**Resolution:** U7 test 5 written against `graph.astream(..., stream_mode=["custom","updates"])`.

## 3. Audit statements the plan relies on — verified against code (preflight-in-doc)

1. `_deterministic_node` signature and return shape (builder.py:444–475) — read this session. ✅
2. `executor.execute` dispatch/gates/repeat-guard (tools.py:130–236) — read. ✅
3. response_variables for the three webhooks (llm.json pulled this session) — booking_verified written by create_livecall_booking. ✅
4. Mock contracts (mock_webhooks.py:160–235) — validate/verify/book/record all echo success shapes. ✅
5. Edge gates ConfirmSlots→VerifyLead (slot_verified), VerifyLead→Booking (data_verified, selected_time, prospect_timezone), Booking→Closing (booking_verified) — llm.json read. ✅
6. Harness tools capture = `metrics.calls` + `last_round_tool_calls` (harness.py:163–172) — read. ✅
7. Routing: engine_fired → state node (builder.py:522–523) — read. ✅

## 4. Verdict

All audit issues are addressed; 11 flaws/nuances found in the plan are resolved in `04_master_plan.md`. Plan is executable after the preflight grep (§2.9) — no open blockers.
