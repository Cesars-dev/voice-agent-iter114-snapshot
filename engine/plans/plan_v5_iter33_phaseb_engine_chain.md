# V5 iter33 — Phase B: engine-owned booking chain + interstitial fillers (kill the 15–24s booking-turn dead air + hit the ≤30%/≤300 gates)

## Meta
- Date: 2026-09-08
- Project root (v5 repo): `/home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/dialux-langgraph-production-v5`
- Scope: implement Phase B on a NEW branch off iter32 — the engine (deterministic node) runs the verify→book chain itself (0 LLM round-trips), streaming canned filler TTS lines between slow webhooks; abort → model does conversational repair; one battery to prove the gates.
- Status: PLAN ONLY (not started — awaits Julio approval). Master plan: `Dialux_SDR/tasks/surgeon/v5-phaseb-engine-chain/04_master_plan.md` (airtight; audit/action/cross-ref archived in same folder).

## Compaction Context
(Fresh agent: trust this as fact. It replaces the entire prior conversation.)

**Project.** LangGraph port of the Dialux SDR chat agent ("Linda"): 9-state machine (Intake→Discovery→Closer→Offer→contact_details→ConfirmSlots→VerifyLead→Booking→Closing), hard-gated transitions, typed dvs, pgvector RAG, LLM-to-LLM acceptance harness. Model gpt-5.2, OPENAI_VERBOSITY=medium, reasoning_effort=none. Venv `.venv/bin/python` (3.12).

**Where we stand.** `iter32-silent-round-gate` @ `596f1bb` (branch, NOT merged; parent `48d6ad3` = iter31 C3b). Suite 133. Battery 2026-09-08 10:19:30–10:27:00Z: 12/13 PASS (Pedro over-book = known accepted FAIL), ended 13/13, agent-silent tool turns 0/230, machine-gun end_call DEAD (Frank 13t/15 gens, Ray 6t/7 gens — were 179/163), silent% 66.0→54.2, gens 480, cache-hit 58.9%, BOOK SALES avg 13.1/14, HUMANIZED 0 FAIL. Full analysis in `tasks/surgeon/v5-iter32-silent-round-gate/02_changes_review.md`, `03_latency.md`, `04_sops_full_call.md` (READ FIRST — this session's product).

**The problem Phase B solves (measured this session).** The booking turn ("yes, book it") = the model walks 8–10 tool-only LLM rounds (validate_lead→verify_lead_data→create_livecall_booking→record_booking_uid→record_booking_outcome→transitions, 4 state hops) at ~1.6–2s/gen → caller waits 15–24s in near-silence (Maria t12 23.6s, Susan t22 19.9s). Chain-state gens = 101/480 (21%); chain-involved silence = 158/260 silent gens (60.8%). TTFT: 55% of turns open with silent bookkeeping rounds before first speech (first word 3.4–5.0s instead of 1.7s). Latency gates NOT met: silent% 54.2 vs ≤30, gens 480 vs ≤300, BOOK gens/turn 2.44 vs ≤2.0.

**Design (Julio-approved direction, 2026-09-08 chat).** "Phase B = plumbing": engine owns the RESULT-DEPENDENT chain; LLM keeps meaning (extract_*, slot dialogue, booking_intent reschedule/cancel, end_call, goodbye, all speech). Engine tells the LLM errors/lacks (existing coaching-message pattern: "Transition NOT taken: first_name is empty — ask for it…"). Filler phrases between chain steps ("one moment please", "alright, getting that locked in…") so the 4–6s remaining wait isn't creepy — engine-emitted via tts_token, ZERO LLM cost, never verbatim twice per call. TTFT target ~1.7s (first round becomes pure speech). verify_lead_data caught nothing in 13 battery calls (mocks always valid) — its webhooks stay for PROD parity; only the LLM round-trips die.

**Audit facts (line-verified, see master plan §1/§5):**
- `_deterministic_node` (builder.py:444–475) already does engine-side leak-math + today-prefetch, sets `engine_fired=True` → routes model back to speak. Phase B extends THIS (proven pattern iter29/30).
- `ToolExecutor.execute` (tools.py:130–236) = single door: repeat-guard (already_captured; end_call exempt), extract/custom(HMAC+retry)/code dispatch, hard-gated transitions (`_gate_check_edge`), end_call truth gates. All response_variables→dvs.
- Edge gates: ConfirmSlots→VerifyLead needs `slot_verified`; VerifyLead→Booking needs `data_verified`+`selected_time`+`prospect_timezone`; Booking→Closing needs `booking_verified` (written by create_livecall_booking's response_variables — mock_webhooks.py:206–212; `record_booking_outcome` extract REJECTS manual booking_verified write — server-owned).
- Harness captures tools via `metrics.calls` + `last_round_tool_calls` (harness.py:163–172); `get_stream_writer` TTS channel at builder.py:43,242,345–347 (`{"tts_token": …}`).
- `tests/test_v2.py::test_gated_full_happy_path` scripts the model walking verify/book turns — MUST be edited (turns 7–8 tool rounds become engine-side; final dvs assertions unchanged).
- Battery gate bounce evidence (self-corrected): Susan missing first_name, Jorge missing livecall_agreed — coaching messages work in 1 round.
- `stash@{0} phaseB-wip` (on iter29-30-one-trip) is DEAD — pre-C3 layout; never pop; branch fresh.
- Mock booking UID `qeTqHuZ1EDzH8bxEdhPQ6H`; create args pinned in master plan U3 `_chain_args` (11 fields, byte-equal to today's model templates).

**Phase B targets (from measured baseline):** booking turn ≤6s; gens ≤300/battery; silent% ≤30; BOOK gens/turn ≤1.3; turn p50 ≤2.0s; TTFT p50 ≤1.7s; 12/13+ PASS; suite 141 (133+8 new `tests/test_phaseb_chain.py`); cache-hit should RECOVER (engine rounds don't mutate the request prefix).

## Resolved Decisions (DO NOT revisit)
| Decision | Rationale |
|---|---|
| Branch `iter33-phaseb-engine-chain` NEW off `596f1bb`; NO merge/push/main commits without Julio | Julio's git discipline: new branch → edit → test → eval |
| Stash `phaseB-wip` stays dead | Pre-C3 conflicts; master plan re-derives |
| Engine chain = result-dependent POST-verify tools only | extract_*/slot dialogue/booking_intent/end_call are meaning, stay LLM-owned (Julio: "we are generating the conversation from certain values") |
| Fillers engine-emitted via tts_token, pool of 6, anti-repeat per call | Zero LLM cost; not creepy; F10 pool line 1 = "One sec — finishing that up." (no collision with model's "One moment while I check availability") |
| record_booking_outcome fired engine-side with {"booking_verified": True} on success path | tools_order parity; extract rejects manual write anyway (server-owned); create's response_variables write the truth |
| Abort statuses {gate_failed, incomplete, unfixable_format, book_failed, error, invalid_uid, uid_not_found, request_failed}; continue {ok, already_captured} | Cross-ref F4 |
| `chain_done` dvs = the ONLY idempotency latch (no GraphState chain_ran) | Cross-ref F1 |
| NO edits to prompts/*.md, agent/llm.json, tools.py, mock_webhooks.py in v1 | Drift test + parity; dead prompt text harmless; F12 = only test_v2 edit |
| Feature flag `Settings.phaseb_chain` (env `PHASEB_CHAIN=0` disables) | A/B + instant rollback |
| One battery (first-13, SOP order) + ledgers after; Pedro retrain NOT this plan | Measured gates; craft item separate |

## BLOCKED / NEEDS INPUT
| Item | Where to get it |
|---|---|
| None | — |

## Environment & Dependencies
- Repo `/home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/dialux-langgraph-production-v5`, branch base `596f1bb`, expect clean tree at start (T1).
- Python `.venv/bin/python` (3.12); suite gate `.venv/bin/python -m pytest tests/ -q` → 133 before, 141 after.
- Langfuse self-hosted `http://localhost:3001`, keys in `.env` (LANGFUSE_*); meters: `scripts/silent_rounds.py`, `scripts/state_latency.py`, `scripts/latency.py` (all existing, read-only REST).
- Harness: `tests/llm2llm/harness.py --personas <Name> --rag --langfuse --max-turns 48 --agent-model gpt-5.2` (mock slots default; battery script shaped like `/tmp/opencode/iter32gate_battery.sh`).
- Master plan (BINDING execution detail incl. full code units U1–U10, file map, gates): `…/tasks/surgeon/v5-phaseb-engine-chain/04_master_plan.md`. Audit 01–03 archived in `…/archive/`.
- NEVER touch: prod agents `agent_16985b5d087e56c35141983396` / `agent_87e4d5f08475e5bc558b2f390f`, `eval/llm2llm_report.json` (restore with `git checkout --` if harness ran), `v5-snapshots/`, `_archive/`, `diallux/graph/tools.py`, `diallux/prompts/**`, `agent/llm.json`, `tests/mock_webhooks.py`, `tests/llm2llm/**` (except running the harness).

## Architecture (Phase B flow)
```
caller: "yes book it"
  └─ state node (ConfirmSlots): model speech + extract_confirm_details + validate_lead
       └─ _deterministic_node  ──trigger: slot_verified ∧ selected_time ∧ tz ∧ number ∧ name ∧ intent=∅ ∧ ¬booking_verified ∧ ¬chain_done──
            engine pass (0 LLM calls):
              [filler]→ verify_lead_data → transition_to_Booking
              [filler]→ create_livecall_booking (writes booking_uid/verified/booked_human)
              [filler]→ record_booking_uid → record_booking_outcome → transition_to_Closing
            ok   → engine_fired=True → state node (Closing): ONE LLM round speaks "you're booked"   (turn done: ~1.6s + fillers)
            abort→ chain_aborted_{step,msg} dvs → state node: model speaks the repair (Say-lines / honest failure)
          PHASEB_CHAIN=0 → whole block skipped (iter32 behavior)
```

## File Map
| File (absolute) | What changes | New/Edit/Delete |
|---|---|---|
| `…/v5/diallux/config.py` | `phaseb_chain: bool = True` | Edit |
| `…/v5/diallux/schema.py` | `chain_done`, `chain_aborted_step`, `chain_aborted_msg` | Edit |
| `…/v5/diallux/graph/builder.py` | `_ENGINE_FILLERS`/`_PHASEB_ABORT`/`_PHASEB_SLOW`/`_chain_args`; chain-runner in `_deterministic_node`; abort-clear in state_node; tail render of abort fields if the generic dvs render misses them | Edit |
| `…/v5/tests/test_phaseb_chain.py` | 8 hermetic tests (end-to-end 0-LLM, resumable, intent-guard, no-double-book, abort→repair, fillers streamed, flag-off, trigger-guard) | New |
| `…/v5/tests/test_v2.py` | `test_gated_full_happy_path` trim (engine walks turns 7–8; assertions unchanged) | Edit |
| `…/v5/ITERATIONS.md` + `git_tree.md` | §23 + row (own commit, post-battery) | Edit |
| `…/tasks/surgeon/v5-phaseb-engine-chain/05_battery.md`, `06_verdict.md` | battery report + verdict | New (post-battery) |

## Deploy Rules
- No service deploys/restarts. No commits to main. Commit per unit on the branch ONLY (U1–U3, U4–U5, U6–U7, then battery+ledgers). Julio reviews before any merge.
- One Maria smoke + ONE first-13 battery (SOP ladder order), meters after. NO Pedro changes, NO prompt changes, NO extra harness runs.
- Langfuse pulls read-only. Never print/commit secrets.

## Tasks (in order — full code in 04_master_plan.md §2)

### T1 — VERIFY + BRANCH
Goal: pin start state; cut the branch.
Commands: `git status --short` (empty) · `git log --oneline -3` (596f1bb/5ac026b/48d6ad3) · `git branch --show-current` (iter32-silent-round-gate) · `.venv/bin/python -m pytest tests/ -q` (133) · `git checkout -b iter33-phaseb-engine-chain`.
Verification: all five match; branch created.

### T2 — UNITS U1–U3 (flag, schema, filler pool + _chain_args)
Goal: compile-green foundation.
Files: config.py, schema.py, builder.py (module-level additions only).
Verification: `.venv/bin/python -m py_compile` all three; suite still 133.

### T3 — UNITS U4–U5 (chain-runner + abort-clear)
Goal: the engine chain in `_deterministic_node` + repair-clear in state_node.
Verification: compile; suite 133 (chain inert until tests exercise it — trigger needs slot_verified etc.).

### T4 — UNITS U6–U7 (test_v2 trim + 8 new tests)
Goal: hermetic proof: 0-LLM chain, resumable, abort→repair, fillers distinct streamed, flag-off, no-double-booking, trigger guard.
Verification: `.venv/bin/python -m pytest tests/ -q` → **141 green**.

### T5 — MARIA SMOKE (live LLM, mock slots)
Goal: end-to-end truth: PASS book, `phaseb:chain_ok` span, booking turn ≤6s, VerifyLead/Booking silent LLM gens ≈0.
Commands: `set -a; . ./.env; set +a; .venv/bin/python tests/llm2llm/harness.py --personas Maria --rag --langfuse --max-turns 48 --agent-model gpt-5.2` then `scripts/state_latency.py --from <window> --to <now>`.
Verification: PASS + gates above; if chain aborts on live path → STOP, report the span payload (do NOT band-aid).

### T6 — FIRST-13 BATTERY + METERS
Goal: the measured gates. Battery script (SOP ladder order happy→stress→curve), then `silent_rounds.py` + `state_latency.py` + `latency.py --hours 2`.
Verification: 12/13+ PASS · ended 13/13 · gens ≤300 · silent% ≤30 · booking turns ≤6s · turn p50 ≤2.0s · cache-hit recovered ≥~60% · tools_order shows the 8 chain tools.
Writes: `05_battery.md` + ledgers (own commit).

### T7 — VERDICT TO JULIO
Goal: table + summary + relevant info in chat; merge decision his.
Verification: numbers cite T6 pulls, not memory.

## Validation Plan (end-to-end)
1. T1 five checks green; branch `iter33-phaseb-engine-chain`.
2. T2–T4: compile green after each unit; final suite 141.
3. T5: Maria PASS, chain_ok span, ≤6s booking turn, 0 silent chain LLM rounds.
4. T6: all measured gates; meters reconcile (state_latency TOTAL == battery gens).
5. T7: verdict cites measured numbers.

## Deferred / Not In This Plan
| Item | Why |
|---|---|
| Merge of iter32 OR iter33 to main | Julio's call, after verdict |
| Pedro over-book retrain | Craft, orthogonal |
| Cache-hit re-tune beyond measurement | Decide post-Phase-B numbers (expected to self-recover) |
| Prompts cleanup of dead chain text (v2) | Only after battery proof; drift risk not worth it now |
| History window re-tune | After Phase B numbers |
| Phase B for non-chain states (Intake…contact_details) | Not latency material; extract layer is meaning (LLM-owned by design) |
