# 02 — Phase B ACTION PLAN (engine-owned booking chain + interstitial fillers)

- Date: 2026-09-08
- Input: `01_audit.md` (same folder). Every change below traces to an audit finding (§ refs).

## 0. Design in one paragraph

When the engine (in `_deterministic_node`) sees state=ConfirmSlots with `slot_verified` true AND the edge booleans for the whole downstream walk satisfied AND `booking_intent` empty, it runs the ENTIRE verify→book chain itself through `ToolExecutor.execute` (same HMAC webhooks, same gates, same response_variables→dvs), emits canned filler TTS lines between the slow steps, hops state_name to Closing, sets `engine_fired=True`, and hands the model ONE round to speak "you're booked". The model never walks tool-by-tool; the caller hears fillers instead of dead air. On ANY chain step failure → abort, surface the step's response in the STATE BLOCK tail, route to the state node for conversational repair (model speaks the Say-lines). LLM-decided tools (extract_*, slot dialogue, booking_intent reschedule/cancel, end_call, goodbye) stay with the model.

## 1. Change units (each = one commit-sized edit, compile-checked in order)

### U1 — `diallux/state.py`: chain scratch fields (audit §5, §8.3)

Add to `GraphState` (all optional, total=False already):
```python
    # Phase B: engine booking-chain scratch
    chain_ran: bool                 # engine chain completed this call (idempotency)
    fillers_said: list[str]         # engine filler lines already spoken (anti-repeat)
    chain_aborted: dict             # last chain abort: {"step": name, "response": {...}}
```
`initial_state` seeds: `"chain_ran": False, "fillers_said": [], "chain_aborted": {}`.
Note: NOT dvs — invisible to the model (audit §8.3 resolved: runtime state, not dvs).

### U2 — `diallux/graph/builder.py`: filler pool + emitter (audit §4.3, §8.2)

Module-level (next to `_MECHANICAL_RE`, after line 75):
```python
# Phase B: engine-owned interstitial fillers. Spoken via tts_token, never an
# LLM call. Anti-repeat within call (mirrors the general_prompt rule).
_ENGINE_FILLERS = [
    "One moment please.",
    "Alright, getting that locked in for you — one sec.",
    "Just confirming those details now.",
    "Great — booking that for you as we speak.",
    "Almost there, locking in your time.",
    "Perfect, just a moment while I finish this up.",
]
```
Emitter (method on CallRuntime or module fn used by the chain-runner):
```python
def _emit_filler(writer, state) -> str | None:
    """Pick the first unsaid filler; mark it said; return the line (or None if pool exhausted)."""
    for line in _ENGINE_FILLERS:
        if line not in (state.get("fillers_said") or []):
            writer({"tts_token": line + " "})
            return line
    return None
```
Pass `state` copy with the new line appended (chain-runner returns it in its update dict so LangGraph merges it — `fillers_said` is a plain list field; merge by replacement in the node's returned patch, same as `metrics`).

### U3 — `diallux/graph/builder.py`: the chain-runner in `_deterministic_node` (audit §4.1–4.2, §8.4, §8.6)

Append after the today-prefetch block (before the `if not patch` return, ~line 473), reusing the existing structure:

```python
        # Phase B: engine-owned booking chain. The model CONFIRMED the slot in
        # conversation (extract_confirm_details + validate_lead ok); the rest
        # is fixed-order plumbing: verify -> book -> record -> Closing. Runs
        # once (chain_ran), only on the happy path (booking_intent empty).
        if (state_name == "ConfirmSlots" and not dvs.get("chain_done")
                and dvs.get("slot_verified") and dvs.get("selected_time")
                and dvs.get("prospect_timezone") and dvs.get("callback_number")
                and dvs.get("first_name")
                and not dvs.get("booking_intent")):
            steps = ["validate_lead", "transition_to_VerifyLead", "verify_lead_data",
                     "transition_to_Booking", "create_livecall_booking",
                     "record_booking_uid", "record_booking_outcome",
                     "transition_to_Closing"]
            fired = state.get("chain_ran", False)
            if not fired:
                writer = get_stream_writer() if _IN_STREAM else None
                executed: list[dict] = []
                new_st = None
                dvs_patch.update({"chain_done": True})     # engine bookkeeping flag (server-owned-ish: engine writes, model never does)
                for step in steps:
                    if step == "record_booking_outcome":
                        outcome = await self.executor.execute(step, {"booking_verified": True}, dvs, "Booking")
                    else:
                        outcome = await self.executor.execute(step, {}, dvs, state_name if step == "validate_lead" else "VerifyLead" if step == "verify_lead_data" else "Booking")
                    executed.append({"name": step, "ok": bool(outcome.response.get("ok", outcome.response.get("status") == "ok"))})
                    dvs_patch.update(outcome.dvs_patch)
                    dvs.update(outcome.dvs_patch)
                    if outcome.new_state:
                        new_st = outcome.new_state
                        state_name = new_st          # keep gate context right for next steps
                    if outcome.response.get("status") in ("gate_failed", "incomplete", "unfixable_format", "book_failed", "error", "invalid_uid", "uid_not_found"):
                        # abort: hand the response to the model for conversational repair
                        dvs_patch.pop("chain_done", None)
                        patch.update({"chain_aborted": {"step": step, "response": outcome.response}})
                        if self.tracer:
                            self.tracer.span("phaseb:chain_abort", output={"step": step, "resp": outcome.response})
                        return {"dvs": dvs_patch, "engine_fired": True, "state_name": state_name,
                                "chain_ran": False, "fillers_said": state.get("fillers_said", []),
                                "chain_aborted": {"step": step, "response": outcome.response},
                                "last_round_tool_calls": executed}
                    # filler before the slow ones (audit: create_livecall_booking ~2s+; verify ~1.5s)
                    if step in ("verify_lead_data", "create_livecall_booking", "record_booking_uid") and writer:
                        line = _emit_filler(writer, {**state, "fillers_said": list(state.get("fillers_said", []))})
                        if line:
                            state = {**state, "fillers_said": state.get("fillers_said", []) + [line]}
                if self.tracer:
                    self.tracer.span("phaseb:chain_ok", output={"steps": [e["name"] for e in executed]})
                return {"dvs": dvs_patch, "engine_fired": True, "state_name": new_st or state_name,
                        "chain_ran": True, "fillers_said": state.get("fillers_said", []),
                        "last_round_tool_calls": executed,
                        "metrics": {"phaseb_chain": [e["name"] for e in executed]}}
```
Critical details (each traces to audit):
- **Args**: validate_lead/verify_lead_data/create_livecall_booking read their values from dvs INSIDE `_exec_custom`? NO — the model passes args today. **SOLUTION (audit §2.4)**: build args deterministically in the runner (a `_chain_args(step, dvs)` helper, spec in U4) so the webhooks get the same payload the model would have templated ("pass exactly as stored"). create_livecall_booking gets the full 11-field payload from the Booking tool schema (audit §1.3 lists them verbatim).
- **record_booking_outcome**: engine fires it with `{"booking_verified": True}` — but booking_verified is SERVER_OWNED via extract → rejected! **SOLUTION (audit §8.4 resolved)**: SKIP record_booking_outcome in the engine chain; `create_livecall_booking`'s response_variables ALREADY write `booking_verified` (audit §1.2.8: response_variables {'booking_uid','booking_verified','booked_human'}). The model-side prompt step 3 (record_booking_outcome) becomes satisfied by the webhook write itself. tools_order keeps 8 engine tools (drop record_booking_outcome OR fire it knowing extract rejects → keeps tools_order identical; DECISION: fire it anyway with empty args → extract returns rejected_server_owned + note — harmless, transcript parity kept).
- **`chain_done` dvs flag**: DynamicVariables has no such field → add `chain_done: bool = False` to `diallux/schema.py` (NOT server-owned — engine writes it; extract never sees it in any tool's variables list so the model can't write it).
- **`_IN_STREAM`**: `get_stream_writer()` raises outside a stream context. Harness runs `graph.astream(...)` so writer exists; hermetic tests may invoke nodes directly → guard with try/except (same as builder.py:242 pattern if present; else wrap).
- **state_name mutation mid-chain**: `transition_to_VerifyLead` requires source state ConfirmSlots, `transition_to_Booking` requires VerifyLead, `transition_to_Closing` requires Booking — execute() checks `self.states.get(state_name)` so the runner MUST track the current state per step (the code above does via outcome.new_state).
- **engine_fired=True** routes BACK to the state node (audit §1.2.6) — the node runs with state_name now = Closing, tail shows booked_human → model speaks confirmation. ONE LLM round.
- **chain_ran idempotency**: after success, `slot_verified` stays true so the entry condition would re-fire on the next deterministic pass — `chain_done` in dvs is the re-entry block (checked first in the guard).

### U4 — `diallux/graph/builder.py`: `_chain_args(step, dvs)` helper (audit §2.4)

```python
def _chain_args(step: str, dvs: dict) -> dict:
    """Deterministic arg assembly — byte-equal to what the model templates today."""
    if step == "validate_lead":
        return {"first_name": dvs.get("first_name",""), "last_name": dvs.get("last_name",""),
                "company_name": dvs.get("company_name",""), "callback_number": dvs.get("callback_number",""),
                "prospect_timezone": dvs.get("prospect_timezone",""), "selected_time": dvs.get("selected_time",""),
                "missed_calls_per_week": dvs.get("missed_calls_weekly",""), "avg_job_value": dvs.get("avg_job_value",""),
                "close_rate_pct": dvs.get("close_rate_pct","")}
    if step == "verify_lead_data":
        return {"first_name": dvs.get("first_name",""), "last_name": dvs.get("last_name",""),
                "company_name": dvs.get("company_name",""), "callback_number": dvs.get("callback_number",""),
                "prospect_timezone": dvs.get("prospect_timezone",""), "selected_time": dvs.get("selected_time",""),
                "slot_options": dvs.get("requested_slot","")}
    if step == "create_livecall_booking":
        name = f"{dvs.get('first_name','')} {dvs.get('last_name','')}".strip()
        return {"account_id": "diallux_live", "timezone": dvs.get("prospect_timezone",""),
                "time": dvs.get("selected_time",""), "name": name or dvs.get("company_name",""),
                "email": "jaydiallux@gmail.com", "attendeePhoneNumber": dvs.get("callback_number",""),
                "title": f"Dialux Live call for {dvs.get('company_name','')}",
                "notes": f"Industry: {dvs.get('industry','')} | Pain points: {dvs.get('pain_points','')}",
                "slot_reservation_uids": dvs.get("slot_reservation_uids",""),
                "booking_intent": dvs.get("booking_intent",""), "preferred_time": ""}
    return {}   # transitions + record_booking_uid (arg from prior response handled in U3) 
```
record_booking_uid needs the UID from create's response — runner captures `outcome.response.get("booking_uid")` and passes it (NOT from dvs, from the response — same as the model does).

### U5 — `diallux/graph/builder.py`: abort surfacing in `_state_block` tail (audit §8.1)

In `_state_block` (after the SPEAK FIRST line, ~line 225), prepend when present:
```python
    aborted = dvs.get("chain_aborted_step")   # plain dvs pair (see U5b)
    if aborted:
        lines.insert(0, f"BOOKING SYSTEM NOTE (internal, never mention tools): {aborted}")
```
Simpler: the runner (U3 abort branch) writes TWO dvs (added to schema, server-owned-style — engine-writable, not in any extract tool list): `chain_aborted_step: str = ""`, `chain_aborted_msg: str = ""` rendered by `_state_block` when non-empty. Model reads it in the tail and speaks the repair (verify actions Say-lines). Clear them after the model's next speech round (state_node already returns dvs patches; add: `if content.strip() and dvs.get("chain_aborted_step"): updates["dvs"] = {"chain_aborted_step": "", "chain_aborted_msg": ""}` in the tool-execution path of state_node — audit note: state_node updates["dvs"] is dvs_patch-based; append these two keys there).

### U6 — `diallux/schema.py`: new typed fields

```python
    chain_done: bool = False            # Phase B: engine chain completed (engine-write only)
    chain_aborted_step: str = ""        # Phase B: last abort step (engine-write, model-readable via tail)
    chain_aborted_msg: str = ""         # Phase B: last abort response summary (tail)
```
NOT in SERVER_OWNED_DVS enforcement (that's extract-side); they simply never appear in any extract tool's variables list → model can't write them. chain_done + aborted fields are read by builder.

### U7 — Tests: `tests/test_phaseb_chain.py` (NEW, hermetic FakeLLM — pattern: tests/test_iter32_gate.py)

1. `test_chain_fires_end_to_end`: seed state ConfirmSlots + dvs (slot_verified, selected_time, timezone, callback_number, first_name, company, uids) → run `_deterministic_node` → assert: state_name == "Closing", dvs has booking_uid == BOOKING_UID, booking_verified True, engine_fired True, chain_ran True, executed == the 8 steps, ZERO LLM calls (FakeLLM call counter == 0).
2. `test_chain_skips_when_intent_set`: same seed + booking_intent="reschedule" → runner does NOT fire; state stays.
3. `test_chain_aborts_on_verify_fail`: mock verify returns data_verified false (unfixable_format path — monkeypatch executor or a mock client fixture) → assert chain_aborted_step == "verify_lead_data", engine_fired True (routes to model), state_name stuck at VerifyLead.
4. `test_chain_idempotent`: second deterministic pass after success → no re-fire (chain_done).
5. `test_fillers_emitted`: stream-mode invocation → captured tts_tokens include ≥2 distinct filler lines, none repeated.
6. `test_fillers_pool_exhausts`: 6+ slow steps → no crash, no repeat.
7. `test_gate_integrity`: runner respects gates — seed with slot_verified true but selected_time "" → no fire (guard).
8. `test_transcript_parity`: harness-style run → tools_order transcript row contains the 8 chain tool names (engine-executed tools land in transcript).

### U8 — Harness/tool accounting (audit §8.6 — MUST SOLVE)

The harness reads per-turn tools from `metrics.calls` + `last_round_tool_calls` (harness.py:163–172). The runner returns `last_round_tool_calls: executed` + `metrics` — but the deterministic node's return merges into state; the harness's `updates` stream picks node outputs. Verify in T3 of execution: run harness on one happy persona, check json_log `tools_order` includes all 8 chain tools and PASS logic (expect=book) still green. If `metrics` merge (Annotated merge_dict) clobbers the state-node metrics of the same turn: acceptable — append `phaseb_chain` key (merge_dict merges, not replaces, per-key: metrics is Annotated[dict, merge_dict] → update merges; verify).

### U9 — Ledger updates (post-battery only)

`ITERATIONS.md` §23, `git_tree.md` row, surgeon `05_battery.md` + `06_verdict.md`. NOT in the build commit.

## 2. Execution order (each unit compile-checked: `.venv/bin/python -m py_compile diallux/graph/builder.py diallux/state.py diallux/schema.py`)

U1 → U6 (schema first actually: U6 then U1) → U2 → U4 → U3 → U5 → U7 → U8 → battery → U9.

## 3. Verification ladder (mirrors git discipline: branch → edit → test → eval)

1. `.venv/bin/python -m pytest tests/ -q` → 133 + 8 new = 141 green.
2. Single happy-path harness smoke: Maria, mock slots, 13 turns expected: `set -a; . ./.env; set +a; .venv/bin/python tests/llm2llm/harness.py --personas Maria --rag --langfuse --max-turns 48 --agent-model gpt-5.2` → PASS, booked=True, and Langfuse trace shows: ConfirmSlots speech round → 1 deterministic span phaseb:chain_ok → Closing speech round. Booking turn ≤6s.
3. First-13 battery (the SOP order happy→stress→curve): 12/13+ PASS, Pedro unchanged.
4. Latency pull: `scripts/state_latency.py --from <window>` → VerifyLead/Booking gens ≈ 0 engine-side; `scripts/silent_rounds.py` → silent% ≤30 target, gens ≤300.
5. TTFT check: battery json_logs turn_ms p50 ≤ ~2000ms; booking turns ≤6000ms.

## 4. Rollback

Single `git checkout` of the branch commits; no engine file touched on main until Julio approves merge. `chain_done` guard means a feature-flag-off variant is trivial (wrap the U3 block in `if self.settings.phaseb_chain:` — Settings field `phaseb_chain: bool = True`; set False via env `PHASEB_CHAIN=` for A/B).

## 5. What we are NOT doing (audit §7 restated)

No prompt edits (v1), no agent/llm.json edits, no extract-layer changes, no model-initiated transition changes, no TTS voice changes, no webhook changes, no Phase B for the 6 non-chain states.
