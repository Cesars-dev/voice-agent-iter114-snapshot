# 04 — Phase B MASTER PLAN (airtight — all cross-ref flaws resolved)

- Date: 2026-09-08
- Status: AIRTIGHT pending Julio approval to execute
- Inputs: `01_audit.md`, `02_action_plan.md`, `03_cross_reference.md` + preflight grep of tests (flaw F12 below)
- Branch plan: `iter33-phaseb-engine-chain` NEW, cut from `iter32-silent-round-gate` @ `596f1bb`. The old `stash@{0} phaseB-wip` is DEAD (audit §9) — never pop.

## 0. Final design (one paragraph, zero ambiguity)

After every state round, the `_deterministic_node` checks the chain trigger. Trigger = `state_name ∈ {ConfirmSlots, VerifyLead, Booking}` AND dvs hold `slot_verified` + `selected_time` + `prospect_timezone` + `callback_number` + `first_name` (the exact booleans today's edges gate on) AND `booking_intent` empty AND `booking_verified` false AND `chain_done` false. When true, the node runs the REMAINING chain steps engine-side in ONE pass (each step skipped if its dvs flag is already set — resumable): `validate_lead` (skip if `data_verified` or already past ConfirmSlots… skip if `slot_verified` already true → skip), `transition_to_VerifyLead` (skip if state_name already VerifyLead/Booking/Closing), `verify_lead_data` (skip if `data_verified`), `transition_to_Booking` (skip if state already Booking/Closing), `create_livecall_booking` (skip if `booking_uid` set), `record_booking_uid` (arg = uid from create's live response), `record_booking_outcome` `{"booking_verified": True}` (SUCCESS PATH ONLY), `transition_to_Closing`. Between the three slow webhooks it emits canned filler lines through the TTS stream writer (pool of 6, never verbatim twice per call). All steps go through `executor.execute` (HMAC, retries, gates, repeat-guard preserved). On any failure status → abort: set `chain_aborted_step/msg` dvs, stop, `engine_fired=True` routes the model into the state where it stopped, the STATE BLOCK tail shows the failure, the model does conversational repair (same coaching pattern as today's gate messages). On success: `state_name=Closing`, `engine_fired=True`, model gets ONE round to speak "you're booked" from the tail (`booked_human`, `booking_uid`). Idempotency via `chain_done` dvs (set at chain start; popped on abort). Feature flag `Settings.phaseb_chain` (env `PHASEB_CHAIN`, default true) wraps the whole block for A/B and rollback.

## 1. Resolved flaws (from 03_cross_reference.md §2 + preflight)

| # | Flaw | Resolution (binding) |
|---|---|---|
| F1 | `chain_ran`/`chain_done` duplicate flags | Keep ONLY dvs `chain_done` (schema field, engine-write). No `chain_ran` GraphState field. |
| F2 | Abort return shape inconsistent | One clean return dict both paths (code below). |
| F3 | `_IN_STREAM` invented | `try: writer = get_stream_writer() except Exception: writer = None` inside the runner. |
| F4 | record_booking_outcome / abort statuses | Abort statuses = `{"gate_failed","incomplete","unfixable_format","book_failed","error","invalid_uid","uid_not_found","request_failed"}`. Continue = `"ok"`, `"already_captured"`. `record_booking_outcome` fires ONLY after `record_booking_uid` ok. |
| F5 | Maria all-in-one model round must not double-book | Trigger requires `booking_verified` false + resumable skips; U7 test 9 covers. |
| F6 | verify ISO selected_time injection | No change (pre-existing contract, engine passes stored value verbatim). |
| F7 | Fillers invisible to harness transcript | Verification via `fillers_said` final state + `phaseb:chain_ok` span + hermetic stream test. |
| F8 | `fillers_said` merge semantics | Runner returns the COMPLETE cumulative list. |
| F9 | Test grep was missing | DONE (preflight): `test_v2.py::test_gated_full_happy_path` scripts model-fired verify/book rounds → MUST be edited (F12). `test_units.py:122`, `test_v2.py:63-201` call `execute()` directly — unaffected. `test_iter32_gate.py` — gate only, unaffected. `test_graph.py:75`, `test_iter31_c3_prefix.py:190` — early transitions, unaffected. |
| F10 | Filler/model phrase collision | Pool line 1 changed to `"One sec — finishing that up."` (keeps distance from the model's ConfirmSlots "One moment while I check availability"). |
| F11 | Writer availability in hermetic tests | Filler test drives the graph via `astream(stream_mode=["custom","updates"])`, not direct node calls. |
| F12 (preflight) | `test_gated_full_happy_path` FakeLLM script | Turn 6 becomes the last tool round (extract_confirm_details + validate_lead + transition_to_VerifyLead); turns 7-8 tool rounds DELETED (chain is engine-side now); their speech rounds collapse into Closing speech + goodbye. Final-dvs assertions UNCHANGED (engine writes the same flags). |

## 2. Final code units (binding — compile-checked in order)

### U0 — branch
```
cd /home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/dialux-langgraph-production-v5
git status --short                          # expect EMPTY
git checkout -b iter33-phaseb-engine-chain  # from iter32-silent-round-gate @ 596f1bb
```

### U1 — `diallux/config.py`: feature flag
After `max_tool_rounds: int = 12` (config.py:43):
```python
    phaseb_chain: bool = True        # iter33 Phase B: engine-owned booking chain (env PHASEB_CHAIN=0 disables)
```
(Settings is pydantic BaseSettings — env override automatic; verify field name maps `PHASEB_CHAIN`.)

### U2 — `diallux/schema.py`: 3 typed fields (after `booking_confirmed: bool = False`, ~line 91)
```python
    chain_done: bool = False            # iter33: engine chain ran (engine-write only; not in any extract tool)
    chain_aborted_step: str = ""        # iter33: abort surfacing (engine-write, tail-rendered)
    chain_aborted_msg: str = ""          # iter33: abort response summary (tail-rendered)
```

### U3 — `diallux/graph/builder.py`: filler pool + helper (after `_MECHANICAL_RE`, ~line 75)
```python
# iter33 Phase B: engine-owned interstitial fillers. Spoken via tts_token —
# never an LLM call. Anti-repeat within call (mirrors general_prompt's rule).
_ENGINE_FILLERS = [
    "One sec — finishing that up.",
    "Alright, getting that locked in for you.",
    "Just confirming those details now.",
    "Great — booking that for you as we speak.",
    "Almost there, locking in your time.",
    "Perfect, just a moment while I finish this.",
]
_PHASEB_ABORT = {"gate_failed", "incomplete", "unfixable_format", "book_failed",
                 "error", "invalid_uid", "uid_not_found", "request_failed"}
_PHASEB_SLOW = {"verify_lead_data", "create_livecall_booking", "record_booking_uid"}


def _chain_args(step: str, dvs: dict, booking_uid: str = "") -> dict:
    """Deterministic chain args — byte-equal to the model's templates today."""
    if step == "validate_lead":
        return {"first_name": dvs.get("first_name", ""), "last_name": dvs.get("last_name", ""),
                "company_name": dvs.get("company_name", ""),
                "callback_number": dvs.get("callback_number", ""),
                "prospect_timezone": dvs.get("prospect_timezone", ""),
                "selected_time": dvs.get("selected_time", ""),
                "missed_calls_per_week": dvs.get("missed_calls_weekly", ""),
                "avg_job_value": dvs.get("avg_job_value", ""),
                "close_rate_pct": dvs.get("close_rate_pct", "")}
    if step == "verify_lead_data":
        return {"first_name": dvs.get("first_name", ""), "last_name": dvs.get("last_name", ""),
                "company_name": dvs.get("company_name", ""),
                "callback_number": dvs.get("callback_number", ""),
                "prospect_timezone": dvs.get("prospect_timezone", ""),
                "selected_time": dvs.get("selected_time", ""),
                "slot_options": dvs.get("requested_slot", "")}
    if step == "create_livecall_booking":
        name = f"{dvs.get('first_name', '')} {dvs.get('last_name', '')}".strip()
        return {"account_id": "diallux_live", "timezone": dvs.get("prospect_timezone", ""),
                "time": dvs.get("selected_time", ""), "name": name or dvs.get("company_name", ""),
                "email": "jaydiallux@gmail.com", "attendeePhoneNumber": dvs.get("callback_number", ""),
                "title": f"Dialux Live call for {dvs.get('company_name', '')}",
                "notes": f"Industry: {dvs.get('industry', '')} | Pain points: {dvs.get('pain_points', '')}",
                "slot_reservation_uids": dvs.get("slot_reservation_uids", ""),
                "booking_intent": dvs.get("booking_intent", ""), "preferred_time": ""}
    if step == "record_booking_uid":
        return {"booking_uid": booking_uid}
    return {}
```

### U4 — `diallux/graph/builder.py`: the chain-runner (appended INSIDE `_deterministic_node`, after the today-prefetch block, before the final return)
```python
        # iter33 Phase B: engine-owned booking chain. The model confirmed the
        # slot in conversation; everything past slot_verified is fixed-order
        # plumbing. One deterministic pass, resumable (each step skips if its
        # flag is set), fillers between the slow webhooks, abort -> model repair.
        if (self.settings.phaseb_chain
                and state_name in ("ConfirmSlots", "VerifyLead", "Booking")
                and not dvs.get("chain_done") and not dvs.get("booking_verified")
                and not dvs.get("booking_intent")
                and dvs.get("slot_verified") and dvs.get("selected_time")
                and dvs.get("prospect_timezone") and dvs.get("callback_number")
                and dvs.get("first_name")):
            try:
                writer = get_stream_writer()
            except Exception:
                writer = None
            fillers: list[str] = list(state.get("fillers_said") or [])
            executed: list[dict] = []
            uid = dvs.get("booking_uid", "")
            plan = [("validate_lead", "ConfirmSlots", not dvs.get("slot_verified")),
                    ("transition_to_VerifyLead", "ConfirmSlots", state_name == "ConfirmSlots"),
                    ("verify_lead_data", "VerifyLead", not dvs.get("data_verified")),
                    ("transition_to_Booking", "VerifyLead", state_name in ("ConfirmSlots", "VerifyLead")),
                    ("create_livecall_booking", "Booking", not uid),
                    ("record_booking_uid", "Booking", not dvs.get("booking_verified")),
                    ("record_booking_outcome", "Booking", not dvs.get("booking_verified")),
                    ("transition_to_Closing", "Booking", True)]
            # normalize: verify args come from dvs; record_booking_outcome is engine-side only on success
            cur_state = state_name
            dvs_patch["chain_done"] = True        # idempotency latch (popped on abort)
            for step, home, needs in plan:
                if not needs:
                    continue
                if step == "validate_lead" and dvs.get("slot_verified"):
                    continue                       # model already validated this pass
                if writer and step in _PHASEB_SLOW:
                    for line in _ENGINE_FILLERS:
                        if line not in fillers:
                            writer({"tts_token": line + " "})
                            fillers.append(line)
                            break
                args = _chain_args(step, dvs, uid)
                if step == "record_booking_outcome":
                    args = {"booking_verified": True}   # engine asserts only on this path
                outcome = await self.executor.execute(step, args, dvs, home)
                status = outcome.response.get("status", "ok" if outcome.response.get("ok") else "error")
                executed.append({"name": step, "ok": status == "ok"})
                dvs_patch.update(outcome.dvs_patch)
                dvs.update(outcome.dvs_patch)
                if outcome.new_state:
                    cur_state = outcome.new_state
                if step == "create_livecall_booking" and outcome.response.get("booking_uid"):
                    uid = outcome.response["booking_uid"]
                if status in _PHASEB_ABORT:
                    dvs_patch.pop("chain_done", None)
                    dvs_patch["chain_aborted_step"] = step
                    dvs_patch["chain_aborted_msg"] = json.dumps(outcome.response)[:600]
                    if self.tracer:
                        self.tracer.span("phaseb:chain_abort", output={"step": step, "resp": outcome.response})
                    return {"dvs": dvs_patch, "engine_fired": True, "state_name": cur_state,
                            "fillers_said": fillers, "last_round_tool_calls": executed,
                            "chain_aborted": {"step": step, "response": outcome.response}}
            if self.tracer:
                self.tracer.span("phaseb:chain_ok", output={"steps": [e["name"] for e in executed]})
            return {"dvs": dvs_patch, "engine_fired": True, "state_name": "Closing",
                    "fillers_said": fillers, "last_round_tool_calls": executed,
                    "metrics": {"phaseb_chain": [e["name"] for e in executed]}}
```
Notes (binding):
- `plan` tuples carry the HOME state each tool legally executes from (executor looks tools up in `states[state_name]` — mock/lve webhooks are state-tools; passing `home` keeps the lookup exact).
- The `validate_lead` skip: trigger already requires `slot_verified` true, so validate is ALWAYS skipped in practice (the model validated); it stays in the plan for the resumable shape (trigger relaxations later).
- `transition_to_Closing` unconditional (plan tuple `True`): Booking→Closing gate reads `booking_verified` — set by create's response_variables — gate passes; if it somehow fails (status gate_failed) → abort branch (model repair), safe.
- `_state_block` renders `chain_aborted_step/msg` automatically? NO — they're plain dvs; `_state_block` renders dvs lines generically (verify in T-compile; if the block renders only whitelisted keys, add the two lines explicitly after the SPEAK FIRST directive).

### U5 — `diallux/graph/builder.py` state_node: clear abort surface after repair speech
In state_node's tool-execution branch, after `updates["last_round_tool_calls"] = executed` (~line 410):
```python
                if content.strip() and dvs.get("chain_aborted_step"):
                    dvs_patch.update({"chain_aborted_step": "", "chain_aborted_msg": ""})
```
(the pure-speech branch already ends the turn; abort repair happens on a tool round or the next speech round clears it — same line added to the else-branch's updates["dvs"] if needed; verify which branch runs after engine_fired routing: state node WITH speech, no tools → else-branch → add the same clear there):
```python
            else:
                ...
                if content.strip() and (state.get("dvs", {}).get("chain_aborted_step")):
                    updates["dvs"] = {"chain_aborted_step": "", "chain_aborted_msg": ""}
```

### U6 — `tests/test_v2.py::test_gated_full_happy_path` (F12 edit)
- Turn 6 FakeLLM round: keep `extract_confirm_details` + `validate_lead` + `transition_to_VerifyLead` (round ends, state → VerifyLead, then the ENGINE chain completes deterministically).
- DELETE the turn-7 and turn-8 tool rounds; keep speech rounds: turn 7 = speech only (model in Closing speaks booked), turn 8 = goodbye round unchanged (turn 9 in today's file).
- Texts list shortens accordingly (remove "yes confirm", "go ahead").
- Final assertions unchanged (engine writes identical dvs flags; `booking_uid == BOOKING_UID`, `booking_verified` True, `gate_rejections == []`).

### U7 — `tests/test_phaseb_chain.py` (NEW — hermetic, FakeLLM + mock_client, pattern from test_v2.py)
1. `test_chain_end_to_end_zero_llm`: FakeLLM scripted: round 1 fires extract_confirm_details+validate_lead+transition_to_VerifyLead (speech token "Locked in."); then ONE speech round; then goodbye round. Drive 3 turns via astream. Assert: final `state_name == "Closing"`, `dvs.booking_uid == BOOKING_UID`, `dvs.booking_verified is True`, `dvs.chain_done is True`, `dvs.data_verified is True`; FakeLLM call count == 3 (NO engine LLM rounds); tracer-less.
2. `test_chain_resumable_skips`: same but FakeLLM ALSO fires verify_lead_data in round 1 (model went further) — engine skips verify; still completes; no gate rejections.
3. `test_chain_not_fired_when_intent_set`: seed booking_intent="reschedule" (via a Booking-state round) → chain does not fire; model keeps control (assert no `chain_done`).
4. `test_chain_not_fired_when_model_books_all`: FakeLLM fires the ENTIRE chain itself in one round (Maria t12 shape) → engine never fires (chain_done False — engine didn't run; booking_verified True from model's tools); no double booking (executor repeat-guard would answer already_captured anyway).
5. `test_chain_aborts_to_repair`: monkeypatch mock transport so verify_lead_data returns data_verified False + unfixable_format (edit mock fixture via custom httpx.AsyncClient handler wrapping mock_client) → chain aborts at verify_lead_data; `state_name` stays VerifyLead; `dvs.chain_aborted_step == "verify_lead_data"`; the NEXT FakeLLM round receives a tail containing the abort note (FakeLLM captures system blocks — assert the string appears); model repair round clears the fields.
6. `test_fillers_streamed`: run through `astream(stream_mode=["custom","updates"])`; collect custom `tts_token` events during the booking turn; assert ≥2 filler lines, all distinct, all from `_ENGINE_FILLERS`.
7. `test_chain_flag_off`: `Settings(openai_api_key="test", retell_api_key="test", phaseb_chain=False)` → chain never fires; behavior = iter32 (model walks; long-existing test_v2 path effectively).
8. `test_trigger_guard`: direct `_deterministic_node` invocation with slot_verified true but selected_time "" → no chain (returns engine_fired False / no chain_done).

### U8 — compile + suite ladder
```
.venv/bin/python -m py_compile diallux/graph/builder.py diallux/schema.py diallux/config.py
.venv/bin/python -m pytest tests/ -q          # expect 141 (133 + 8 new; test_v2 edited in place)
```

### U9 — live smoke + battery (AFTER suite green)
```
set -a; . ./.env; set +a
.venv/bin/python tests/llm2llm/harness.py --personas Maria --rag --langfuse --max-turns 48 --agent-model gpt-5.2
# expect: PASS book, turns ≈ 13, booking turn wall ≤ 6s, trace shows phaseb:chain_ok span,
# VerifyLead/Booking gens ≈ 0 silent LLM rounds
# then the first-13 battery per the SOP ladder order (happy → stress → curve):
.venv/bin/python /tmp/opencode/phaseb_battery.sh   # created at execution time, same shape as iter32gate_battery.sh
```
Meters after battery:
```
.venv/bin/python scripts/state_latency.py --from <battery window start> --to <end>
.venv/bin/python scripts/silent_rounds.py --from <battery window start> --to <end>
.venv/bin/python scripts/latency.py --hours 2
```
Gates: 12/13+ PASS · ended 13/13 · gens ≤300 · silent% ≤30 · booking turn ≤6s · turn p50 ≤ ~2.0s · suite 141.

### U10 — ledgers (separate commit, after battery)
`ITERATIONS.md` §23 + `git_tree.md` row + surgeon `05_battery.md` + `06_verdict.md` in `tasks/surgeon/v5-phaseb-engine-chain/`.

## 3. File Map (final)

| File (absolute) | Change | New/Edit |
|---|---|---|
| `…/v5/diallux/config.py` | `phaseb_chain: bool = True` flag | Edit |
| `…/v5/diallux/schema.py` | `chain_done`, `chain_aborted_step`, `chain_aborted_msg` fields | Edit |
| `…/v5/diallux/graph/builder.py` | `_ENGINE_FILLERS`, `_PHASEB_ABORT`, `_PHASEB_SLOW`, `_chain_args`, chain-runner in `_deterministic_node`, abort-clear in state_node, tail render of abort fields (if needed) | Edit |
| `…/v5/tests/test_phaseb_chain.py` | 8 hermetic tests | New |
| `…/v5/tests/test_v2.py` | `test_gated_full_happy_path` script trim (F12) | Edit |
| `…/v5/ITERATIONS.md`, `git_tree.md` | ledgers (U10, own commit) | Edit |
| **NEVER touched** | `diallux/graph/tools.py`, `diallux/prompts/*.md`, `agent/llm.json`, `tests/mock_webhooks.py`, `tests/llm2llm/**`, prod agents, `v5-snapshots/`, `_archive/`, stash | — |

## 4. Deploy / git discipline (Julio's rules, binding)

1. Branch `iter33-phaseb-engine-chain` off `596f1bb`; work ONLY there. NO merge, NO push, NO main commits without Julio's explicit approval.
2. Commit per unit (U1+U2+U3 one commit; U4+U5 one; U6+U7 one; battery/ledgers last).
3. No deploys, no service restarts, no harness re-runs beyond the smoke + ONE battery. Pedro/others unchanged.
4. Rollback: `git checkout iter32-silent-round-gate` — or runtime-off via `PHASEB_CHAIN=0`.

## 5. Verification summary (end-to-end)

1. Suite 141 green (compile after each unit).
2. Hermetic: chain 0 LLM rounds, resumable, abort→repair, fillers distinct streamed, flag-off = iter32 behavior, no-double-booking.
3. Maria smoke: PASS + `phaseb:chain_ok` span + booking turn ≤6s.
4. Battery 12/13+ with gates: gens ≤300, silent ≤30%, turn p50 ≤2.0s, TTFT first word ≤1.7s p50.
5. Langfuse meters reconcile (state_latency totals == battery gens).

## 6. Open items (none blocking)

- Cache-hit re-tune (post-Phase-B decision — engine rounds don't touch the prefix at all, so cache should RECOVER toward 69.9%).
- Pedro retrain (unchanged, separate).
- Prompts cleanup of dead chain text (v2 of Phase B, only after battery proof).
- History window re-tune (after Phase B numbers).
