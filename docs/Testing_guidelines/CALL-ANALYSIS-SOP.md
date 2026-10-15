# Call Analysis SOP

> Standard framework for analyzing every test run. Run through ALL categories on every analysis.

## SDK-first (do NOT rebuild — proven pipeline)

```bash
# 1. mechanicals + condensed data for the whole batch, one command:
python3 scripts/sop_mechanicals.py /tmp/opencode/wt-iterNN/engine/tests/llm2llm/json_logs/<glob> 
# 2. per-persona dossier + verbatim slices (engine worktree):
.venv/bin/python /tmp/opencode/wt-iterNN/engine/scripts/call.py sop <Persona>
.venv/bin/python /tmp/opencode/wt-iterNN/engine/scripts/call.py turns <Persona> --frm 10 --to 13
# 3. persist (one row per call x category, 8 categories; own file, never mixed with SALES):
python3 scripts/live_sql.py --db research/surgeon/iter48-rag-truth/ledger.db \
    sop-import --run <run> --sop CALL --branch <branch> --commit <sha> --file <NN>_sop_call_rows.json
```

Deliverable = **ONE table**: 8 categories × calls (verdict per cell). Only the
cells that deviate carry scope/turn references; the rest are PASS.

## Data Sources

1. **GET `get-chat/{chat_id}`** — full transcript, message_with_tool_calls, dynamic_variables, cost
2. **Test script stdout** — real-time turn-by-turn output
3. **Booking confirmation** — Cal.com API check if booking was created

---

## Analysis Categories

### 1. PROMPT FOLLOWING

Check every state's prompt steps against what the agent actually did. For each state:

- Did the agent follow the prescribed conversation flow steps?
- Were filler phrases used before tool calls?
- Were required questions asked (boiler brand, error code, postcode, etc.)?
- Were forbidden actions avoided (stacking questions, diagnosing, etc.)?
- Did the agent wait for confirmation before transitioning?

**Mark each state:** ✅ followed / ⚠️ partial / ❌ violated

### 2. NONSENSE / HALLUCINATION

- Agent says things it cannot know ("I don't have access to X" when tools exist)
- Raw JSON or technical output leaked into agent speech
- Agent contradicts itself between turns
- Agent claims to do something it cannot
- Agent fabricates tools, policies, or capabilities

### 3. REDUNDANCY

- Same extract tool called multiple times with same data
- Same question asked to customer more than once (without cause)
- Trivial or wasted tool calls (e.g., trigger with `false`)
- State transitions attempted without new data

### 4. REPETITION

- Same phrase or sentence structure repeated across turns
- Agent loops on the same response without progression
- Same slot offered after customer already responded

### 5. BUGS (Code/Config)

- Missing variable values passed as empty strings
- Incorrect tool parameters (wrong format, wrong enum)
- `speak_after_execution` misconfiguration
- Edge schema / tool enum mismatches
- API errors from tools (check_availability, book_calendar)
- Missing required fields in downstream API calls

### 6. TOOL CALL ANALYSIS

- Were all required tools called for each state?
- Were transition tools called in correct order (extract → trigger → transition)?
- Were tool parameters populated with real values (not empty)?
- Were tools called at the right time (not before data collected)?

### 7. CONVERSATION FLOW

- Natural pacing: agent doesn't rush or stall
- One question per turn (no stacking)
- Agent responds to what customer actually said
- Agent re-asks when customer didn't answer
- Goodbye/end_call handled properly

### 8. LOGS & METRICS

- Total turns
- Cost
- Number of agent messages
- Number of tool calls
- Number of state transitions
- Dynamic variables at end of call

---

## 9. PERSIST TO THE LEDGER (mandatory — the audit is not done until it is in SQLite)

Every CALL-SOP audit lands in the SAME ledger that holds the latency data
(`clean_diallux_SDR/research/surgeon/iter48-rag-truth/ledger.db`, table `sops`)
so results join with `calls`/`rounds`/`rag` rows by `trace_id`.

**Row shape** (one row per call × category; failures carry reason + assessment + evidence):

```json
{"run_id":"happy-b","trace_id":"<ledger trace_id>","sop":"CALL",
 "category":"prompt_following","scope":"contact_details t12-t28",
 "verdict":"FAIL","rule":"P0/FIND-1",
 "failure_reason":"…","failure_assessment":"…","evidence":"…",
 "source":"07_call_analysis_sop_happy_b.md"}
```

- `category` = one of: `prompt_following · nonsense · redundancy · repetition · bugs · tool_calls · flow · metrics` (§1–§8 above)
- `verdict` = `PASS | PARTIAL | FAIL | OBSERVATION`
- `rule` = the ranked finding id it belongs to (e.g. `P0/FIND-1`, `P2/DV-bookkeeping`)
- `failure_reason` / `failure_assessment` / `evidence` = verbatim from the analysis (what broke, why it broke, quoted proof)

**Import (idempotent — same rows re-import skip):**

```bash
cd /home/julio/projects/clean_diallux_SDR
python3 scripts/live_sql.py --db research/surgeon/iter48-rag-truth/ledger.db \
    sop-import --run <run_id> --sop CALL --branch <branch> --commit <sha> \
    --file research/surgeon/iterNN-<slug>/<NN>_callsop_report.json
```

**Query (instant analysis — no md re-reading):**

```bash
cd /home/julio/projects/clean_diallux_SDR
python3 scripts/live_sql.py --db research/surgeon/iter48-rag-truth/ledger.db \
    sops --run battery-iter48                 # full audit
# ... --run happy-b --verdict FAIL           # only failures
# ... --run happy-b --sop CALL --json        # machine output
```

Note: `scripts/live_sql.py` is the **stable ledger SDK on `main`** (stdlib only —
any `python3` works). `--db` is optional from the main repo (it defaults to the
main-repo ledger) but shown explicitly. The engine worktree keeps the live dev
copy (`/tmp/opencode/wt-iter44/scripts/live_sql.py` @ `engine/iter48-rag-truth`);
edit there and commit to `main` to publish.

**Session tracking (all 4 SOPs):** every imported row is stamped
`session = <run_id>@<commit>`, and the `sessions` table carries the engine
`branch`. Run CALL + SALES + HUMANIZED (and the LATENCY plane) under ONE
`--run`/session for an aggregated pass; query `live_sql.py sessions`. Full
4-SOP protocol: `full_call_analysys.md`.

## Output Format

```
## Test [#]: Summary

### State Transitions
greeter → triage: ✅/❌
triage → address: ✅/❌
address → slot_selection: ✅/❌
slot_selection → confirmation: ✅/❌
confirmation → booking: ✅/❌

### Booking
book_calendar: ✅/❌ (booking_id: X)
end_call: ✅/❌

### 1. Prompt Following
[per-state breakdown]

### 2. Nonsense/Hallucination
[issues found]

### 3. Redundancy
[issues found]

### 4. Repetition
[issues found]

### 5. Bugs
[issues found]

### 6. Tool Call Analysis
[issues found]

### 7. Conversation Flow
[issues found]

### 8. Logs & Metrics
Turns: X | Cost: X¢ | Agent msgs: X | Tool calls: X | Transitions: X
```
