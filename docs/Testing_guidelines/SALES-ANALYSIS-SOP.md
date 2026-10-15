# Sales Analysis SOP — VP of Sales Lens

> Framework for grading test calls as a sales leader would. Run on every call transcript alongside CALL-ANALYSIS-SOP.
> Question this answers: **"Would this call have produced revenue with a real prospect?"**

## SDK-first (do NOT rebuild)

Mechanicals/digest come from `scripts/sop_mechanicals.py <json_logs...>` (one
command, whole batch). The D1–D7 scores are the analyst's half — read the
transcript slice from `scripts/call.py turns <Persona>` / the json_log, never
grade from memory. Persist with a **SALES-only rows file** (a mixed CALL+SALES
file gets stamped under one `sop` — paid for 2026-09-22). Deliverable = **ONE
table**: D1–D7 × calls with the /14 total per call.

## Scoring Dimensions (0–2 each, max 14)

### D1. Discovery Quality
Did the agent uncover pain BEFORE pitching? Follow-up probes ("what happens when you miss those?") or single-shot interrogation?

### D2. Pain Quantification
Did numbers come from the PROSPECT (their calls/wk, close rate, deal size) and get played back? Did the leak math land conversationally ("that's $6,500 a month walking out") or read like a spreadsheet?

### D3. Objection Handling
For every objection raised: was it acknowledged → reframed → re-asked? Or dodged/argued/caved? Score highest for feel-felt-found style; zero for ignoring.

### D4. Value Framing
Benefits tied to THEIR business (their industry, their numbers), not generic features. Mentions of ROI without being asked = bonus.

### D5. Close Confidence
Did the agent assume the sale (offer day/time) vs ask permission ("would you maybe want to...")? Binary-day-first? One clear CTA at a time?

### D6. Momentum Control
Who owned pacing? Agent advanced every turn vs waited to be led. Silence-fillers that advance ("while I check that — quick question...") score high.

### D7. Recovery
When the prospect went sideways (confusion, pushback, tangent): did the agent regain the thread within 1–2 turns?

## Verdict Bands
- **12–14**: hire this agent
- **8–11**: solid SDR, coachable gaps
- **4–7**: needs retraining
- **<4**: don't let it near leads

## Output Format
```
## [Persona] — Sales Grade: X/14 (band)
D1 Discovery: x/2 — [one-line evidence]
...
D7 Recovery: x/2 — [evidence]
Notable moment: "[quote]"
Lost opportunity: "[quote]"
```

## PERSIST TO THE LEDGER (mandatory — the audit is not done until it is in SQLite)

Every SALES-SOP audit lands in the SAME ledger that holds the latency data
(`clean_diallux_SDR/research/surgeon/iter48-rag-truth/ledger.db`, table `sops`) so
results join with `calls`/`rounds`/`rag` rows by `trace_id`.

**Row shape** (one row per call × D1–D7; the score rides the `rule` field):

```json
{"run_id":"battery-iter48","trace_id":"<ledger trace_id>","sop":"SALES",
 "category":"D3","scope":"BOOK Maria","verdict":"PASS","rule":"D3 2/2",
 "evidence":"SALES band 13/14 (hire); <source run>",
 "source":"<NN>_sop_sales_report.md"}
```

- `category` = one of: `D1 · D2 · D3 · D4 · D5 · D6 · D7` (the dimensions above)
- `verdict` = `PASS` (2/2) · `PARTIAL` (1/2) · `FAIL` (0/2) · `OBSERVATION` (not recorded)
- `rule` = the dimension score, e.g. `D3 2/2`
- `evidence` = the per-call band/total (`X/14 (band)`) + which run/model it was graded on
- If a call has only a band total on record (no per-dimension breakdown), seed a
  single `OBSERVATION` row with the band in `evidence` — **never invent D-scores**.

**Import (idempotent — same rows re-import skip):**

```bash
cd /home/julio/projects/clean_diallux_SDR
python3 scripts/live_sql.py --db research/surgeon/iter48-rag-truth/ledger.db \
    sop-import --run <run_id> --sop SALES --branch <branch> --commit <sha> \
    --file research/surgeon/iterNN-<slug>/<NN>_sop_sales_rows.json
```

**Query (instant analysis — no md re-reading):**

```bash
cd /home/julio/projects/clean_diallux_SDR
python3 scripts/live_sql.py --db research/surgeon/iter48-rag-truth/ledger.db \
    sops --run battery-iter48 --sop SALES
# ... --run battery-iter48 --sop SALES --json      # machine output
```

Note: `scripts/live_sql.py` is the **stable ledger SDK on `main`** (stdlib only —
any `python3` works); `--db` is optional from the main repo but shown explicitly.
The engine worktree keeps the live dev copy
(`/tmp/opencode/wt-iter44/scripts/live_sql.py` @ `engine/iter48-rag-truth`).

**Session tracking (all 4 SOPs):** every row is stamped `session = <run_id>@<commit>`
and `sessions` carries the engine `branch`. Run SALES alongside CALL + HUMANIZED
(and the LATENCY plane) under ONE `--run`/session for an aggregated pass. Full
4-SOP protocol: `full_call_analysys.md`.
