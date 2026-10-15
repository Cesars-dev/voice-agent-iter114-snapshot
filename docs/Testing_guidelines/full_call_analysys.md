# FULL CALL ANALYSIS — 4-SOP Quality Audit Protocol (CALL / SALES / HUMANIZED / LATENCY)

> **Which analysis do I run?** Identify the call type FIRST (see §0a):
> - **Harness / chat call** (llm2llm persona run, trace `llm2llm-graph-*`, has a json_log) → THIS document.
> - **Mic call** (live browser bridge, `/mic`, `sid` + `mic bridge session ... started`, NO json_log) → **`MIC-CALL-ANALYSIS-SOP.md`** (same folder) — different data plane (uvicorn log + `mic_events.py` + per-turn in-log reports), same judgment SOPs + same ledger.
>
> Audit any engine run (battery / happy / live) against FOUR Testing_guidelines SOPs
> and deliver **table first, then summary, then relevant info**. Mechanicals come
> from the json logs + the SQLite ledger; qualitative grading is done by reading a
> freshly built digest — never grade from memory. Every SOP lands in ONE SQLite
> ledger, tagged with a **session** (`<run>@<commit>`) so any row is traceable to
> the branch/commit that produced it.
>
> **Run it two ways:** *segregated* (one SOP at a time) or *aggregated* (all 4 SOPs
> in one pass into the same ledger run). Same DB, same session key.

## 0. The 4 SOPs (read them first, every time)

| # | SOP | File | SDK (write / read) | Rows land in |
|---|---|---|---|---|
| 1 | **CALL-ANALYSIS** | `CALL-ANALYSIS-SOP.md` | `live_sql.py sop-import --sop CALL` · `scripts/call.py sop <persona>` + `scripts/sop_mechanicals.py <json_logs...>` | `sops` (`category` = `prompt_following · nonsense · redundancy · repetition · bugs · tool_calls · flow · metrics`) |
| 2 | **SALES-ANALYSIS** | `SALES-ANALYSIS-SOP.md` | `live_sql.py sop-import --sop SALES` · `sop_mechanicals.py` digest | `sops` (`category` = `D1…D7`, `rule` = score `x/2`) |
| 3 | **HUMANIZED-ANALYSIS** | `HUMANIZED-ANALYSIS-SOP.md` | `live_sql.py sop-import --sop HUMANIZED` · `sop_mechanicals.py <json_logs...> --rows-out` | `sops` (`category` = `R1…R5`, `rule` = `R5.x`) |
| 4 | **LATENCY-PERCENTILE** | `LATENCY-PERCENTILE-SOP.md` | `live_sql.py import` (write) · `latency_pull.py` (read) | `rounds` + `rag` |
| 5 | **RAG-ANALYSIS** | `RAG-ANALYSIS-SOP.md` | `scripts/rag_pull.py --run <run> [--gates --rounds --zero --pin]` | `rag` plane + surgeon report |

CALL / SALES / HUMANIZED are **judgment audits** seeded as `sops` rows. LATENCY is
the **measured plane** (`rounds`/`rag`). All four share the same ledger and join by
`run_id` / `trace_id` / `session`.

## 0a. Call-type identification (chat vs mic)

| Signal | Harness/chat call | Mic call |
|---|---|---|
| Started by | `harness.py` persona | human on `/voiceNN/mic?k=<token>` |
| Log signature | json_log in `tests/llm2llm/json_logs/` | `mic bridge session <sid> started` in uvicorn log |
| Trace name | `llm2llm-graph-<persona>` | `diallux-call` / `micbridge-<sid>` |
| Analysis SOP | this file | `MIC-CALL-ANALYSIS-SOP.md` |

The judgment SOPs (CALL/SALES/HUMANIZED) are shared; the data plane and
mechanical SDKs differ. Mic-specific risks (socket staleness, adoption,
reconnect deafness, token gate) live in the mic SOP.

## 1. Where everything lives

| Thing | Path |
|---|---|
| The 4 SOPs | `/home/julio/projects/clean_diallux_SDR/docs/Testing_guidelines/{CALL,SALES,HUMANIZED}-ANALYSIS-SOP.md`, `LATENCY-PERCENTILE-SOP.md` |
| The mic-call SOP (5th plane) | `/home/julio/projects/clean_diallux_SDR/docs/Testing_guidelines/MIC-CALL-ANALYSIS-SOP.md` |
| SQLite ledger | `/home/julio/projects/clean_diallux_SDR/research/surgeon/iter48-rag-truth/ledger.db` (main repo `research/` — gitignored, survives branch switches) |
| Ledger SDK (`live_sql.py`) | **`/home/julio/projects/clean_diallux_SDR/scripts/live_sql.py` (main — stable, stdlib)**: `sop-import` · `sops` · `sessions` · `gates`. The Langfuse write path (`import`, needs `LANGFUSE_*` in `.env`) runs the engine worktree copy. |
| Latency percentile SDK (`latency_pull.py`) | `/home/julio/projects/clean_diallux_SDR/scripts/latency_pull.py` (main repo — Python 3 stdlib only) |
| Transcript/digest + Langfuse SDKs | engine worktree `scripts/call.py`, `scripts/lf.py`, `scripts/lf_quick.py`, `scripts/latency.py` |
| Mic-call mechanical puller | engine worktree `scripts/mic_events.py` (`--sid` / `--window` / `--tag ERR,DROP` / `--langfuse`) — mic plane ONLY |
| Harness (generate new runs) | engine worktree `tests/llm2llm/harness.py --personas <Name> --rag --langfuse --max-turns 28` |
| Call logs (one json per call) | engine worktree `tests/llm2llm/json_logs/*.json` |
| Reports | `research/surgeon/iterNN-<slug>/<NN>_<name>.md` (+ the seed JSON next to it) |
| Persona registry (13 REAL names) | `tests/llm2llm/personas.py`: Maria, Susan, Danny, Marcus, Carlos, Pedro, Sofia, Jorge, Daniel, Brenda, Gene, Frank, Ray |
| Eval runner | `scripts/lf_eval.py {bootstrap,ladder,status}` |
| Langfuse SDK details | skill `langfuse-call-analysis` |

**Persona-name trap:** only those 13 names exist. Inventing personas (David, Ethan…)
fails with `no persona matches '<Name>'` and silently produces empty logs.

**SDK homes:** `scripts/live_sql.py` (sops/gates plane) + `scripts/latency_pull.py`
are stable on `main` and stdlib-only (`python3`). The engine worktree keeps the live
dev copy of `live_sql.py` **and the Langfuse `.env`** (needed for the `import` write
step) plus the other SDKs (`call.py`, `lf.py`, `lf_quick.py`, `latency.py`). If a
worktree is gone, recreate it (`git worktree add /tmp/opencode/wt-<iter> engine/<iter>`).

## 2. Data model — ONE SQLite ledger

```
research/surgeon/iter48-rag-truth/ledger.db
  runs      run_id · commit_sha · window · n_calls · imported_at · session
  sessions  session · run_id · branch · commit_sha · window · created_at      ← tracking key
  calls     trace_id · run_id · trace_name · ts · persona · outcome · turns · passed
  rounds    trace_id · run_id · state · ttft_ms · cache_read · input · output · llm_ms   ← LATENCY plane
  rag       trace_id · run_id · kind · state · ms · chunks · kbs · query · cosine …      ← LATENCY plane
  findings  known-bug register (FIND-N)
  sops      run_id · trace_id · sop · category · scope · verdict · rule ·
            failure_reason · failure_assessment · evidence · source · session         ← CALL/SALES/HUMANIZED
```

### `session` — branch/commit tracking (mandatory)

`session = "<run_id>@<commit_sha>"`, e.g. `battery-iter48@033f742`. It is stamped on
every `runs` row and every `sops` row; `rounds`/`rag`/`calls` join to it via
`run_id`. The `sessions` table carries the engine `branch` + `commit_sha` + window.

- Default when importing: `session` = `--session`, else `<run_id>@<--commit>`, else `<run_id>`.
- Always pass `--branch` and `--commit` so the pass is traceable:
  `--branch engine/iter49-rag-parity --commit $(git -C /tmp/opencode/wt-iter44 rev-parse --short HEAD)`.
- Old ledgers migrate automatically on first `live_sql.py` call: the `session`
  column is added and backfilled from `runs.commit_sha`, and `sessions` is seeded.

### Segregated vs aggregated

| Mode | What it means | How |
|---|---|---|
| **Segregated** | Audit one SOP at a time | one `sop-import --sop <X>` (or `latency_pull.py`) per invocation, all under the same `--run`/session |
| **Aggregated** | All 4 SOPs in one pass, one row-set | one `import` (latency) + three `sop-import` calls, same `--run`/`--branch`/`--commit` → the whole audit is `SELECT * FROM sops WHERE run_id=?` + `latency_pull.py --run ?` |

## 3. Workflow

> **SDK-FIRST RULE (mandatory — do not rebuild).** Every mechanical step has a
> proven SDK; run it, never hand-roll a fresh script mid-session (that is how a
> pass burns an hour). The pipeline for ANY audit is exactly:
>
> ```bash
> # 0. env + locate the run's json_logs in the engine worktree
> ls -t /tmp/opencode/wt-iterNN/engine/tests/llm2llm/json_logs/*.json | head -8
> # 1. transcripts + mechanics (one command, all calls):
> python3 scripts/sop_mechanicals.py /tmp/opencode/wt-iterNN/engine/tests/llm2llm/json_logs/<glob> --rows-out <nn>_sop_humanized_rows.json
> # 2. per-persona verbatim slices / auto-dossier (worktree SDK):
> .venv/bin/python /tmp/opencode/wt-iterNN/engine/scripts/call.py sop <Persona>
> # 3. RAG plane:
> python3 scripts/rag_pull.py --run <run> --gates   # + --rounds/--zero/--pin to drill
> # 4. latency plane:
> python3 scripts/latency_pull.py --run <run>
> # 5. persist judgment rows (one file per SOP — never a mixed file):
> python3 scripts/live_sql.py --db research/surgeon/iter48-rag-truth/ledger.db \
>     sop-import --run <run> --sop CALL --file <...>.json   # row's sop field wins over --sop
> ```
>
> Gotchas paid for in blood (2026-09-22): a MIXED CALL+SALES rows file imports
> everything under one `sop` — always split files per SOP. `sqlite3.Row` is not
> a dict — materialize `dict(zip(cols, row))` alignment carefully. ~~The ledger
> `rag` import drops `kbs/pinned/lanes` (PT-59)~~ **RESOLVED iter64 (PT-59
> DONE): the import stores `kbs/pinned/lanes/zero_hit` natively — no sidecar
> needed; `rag_pull.py` reads the ledger columns first (sidecar = legacy
> fallback for pre-iter64 runs only).**

### Step 0 — Environment
```bash
cd /tmp/opencode/wt-iter44          # engine worktree with the SDKs
set -a; . ./.env; set +a            # only if generating new runs (Langfuse keys)
ls -t tests/llm2llm/json_logs/*.json | head -30
```
If Langfuse is involved, check ingest first (`scripts/lf.py health`); json_logs work
regardless.

### Step 0a — Switch audit (MANDATORY since 2026-10-02 — register: `ENV-SWITCHES.md`)
Before ANY verdict: snapshot the RESOLVED engine switch set of the checkout that
produced the run (the stdlib command in `ENV-SWITCHES.md` §2 — resolves code
defaults + `.env` overrides), save it as `<run>_env_snapshot.txt` beside the run
evidence, and DIFF it against (a) the prod env snapshot, (b) the previous passing
run's snapshot. Any behavioral-switch diff (warm hold `WARM_AWAIT_DEST_MS`, rag
fire mode / live-retrieve, TTS gate / dedupe pair, deepgram eager, verbosity /
reasoning effort) **invalidates cross-run comparison until explained in the
verdict**. Paid lesson this encodes: iter87's deterministic-hold battery ran the
design ON; the knob's default was 0 and nothing set it — three iterations of later
tests measured an engine state that was never running, and the drift surfaced as
"mystery regressions". One snapshot query; don't relive that.

### Step 1 — Transcript + mechanical digest
The transcript SDK is `scripts/call.py` (subcommands `list · show · sop · dvs ·
turns · lat`). It emits the per-call transcript + CALL auto-checks. The richer
batch `digest` subcommand exists on the upstream v5 tree — if it is missing on your
branch, build the digest from `tests/llm2llm/json_logs/*.json` (e.g.
`/tmp/opencode/build_digest.py`) or read one call at a time:

```bash
.venv/bin/python scripts/call.py sop <persona>                        # auto-checks + dossier
.venv/bin/python scripts/call.py turns <persona> --frm 10 --to 13     # verbatim slice
.venv/bin/python scripts/call.py lat <persona>                        # turn_ms p50/p90
```
The digest (however built) must emit: header (pass/expect/outcome/turns/
gate_rejections/uid/p50), **latency** (`turn_ms` p50/p90 — the *round* plane),
state chain, HUMANIZED mechanicals (R1–R5.7), opener variance (R2), CALL
auto-checks, condensed transcript. The SDK does the MECHANICAL half only;
judgments stay with the analyst.

### Step 1b — Latency plane (LATENCY SOP — REQUIRED in every report)
Two distinct planes, both reported:
- **round** (`rounds.ttft_ms`, per LLM round) — from the ledger percentiles.
- **LLM duration** (`rounds.llm_ms`) — full generation wall time.

Write side — Langfuse → ledger (needs `LANGFUSE_*`; runs from the engine worktree):
```bash
cd /tmp/opencode/wt-iter44 && set -a && . ./.env && set +a
.venv/bin/python scripts/live_sql.py \
    --db /home/julio/projects/clean_diallux_SDR/research/surgeon/iter48-rag-truth/ledger.db \
    import --window "HH:MM-HH:MM" --run <run_id> --branch <branch> --commit <sha>
```
Read side (percentile SDK — main repo, no LLM/keys needed):
```bash
cd /home/julio/projects/clean_diallux_SDR
python3 scripts/latency_pull.py --run <run_id>
# filters: --state Discovery  ·  --json  ·  omit --run = all
```
Gates (main repo, ledger-only):
```bash
cd /home/julio/projects/clean_diallux_SDR
python3 scripts/live_sql.py --db research/surgeon/iter48-rag-truth/ledger.db \
    gates --a <run_a> --b <run_b>
```
Report must carry the aggregate line, the per-call p50/p90 table, and the
slow-state note. Details + thresholds live in `LATENCY-PERCENTILE-SOP.md`.

### Step 2 — Grade (4 SOPs)
- **CALL**: 8 categories from the digest + dvs dumps.
- **SALES**: D1–D7 (0–2 each, /14) by READING the condensed transcripts.
  Band 12–14 hire · 8–11 solid · 4–7 retrain · <4 don't ship.
- **HUMANIZED**: R1–R5 verdict PASS/PARTIAL/FAIL (R5 severity: verbatim = hard flag;
  3+ R5 kinds = automatic FAIL).
- **LATENCY**: round + LLM percentiles, turn-1 vs steady split, cache-0 rounds.

### Step 3 — Deliver (owner's required order)
1. **TABLE first — ONE table per SOP (the rule).** CALL = one 8-category × call
   table. SALES = one D1–D7 × call table with the /14 total. HUMANIZED = one
   R1–R5 × call verdict table. RAG = one gates table (+ one per-round table when
   drilling). Do NOT invent extra tables per call — the digest lives in the
   surgeon report file, the chat answer gets exactly one table per SOP.
2. **Then summary** — the story, not the rows.
3. **Then relevant info** — quotes, worst offenders, fix suggestions + latency aggregate.
4. **Latency-comparison table** when auditing ≥2 batches.
5. **Full path of the plan/report file** at the end (one copy-paste).
Save full per-call detail to `research/surgeon/iterNN-<slug>/<NN>_<name>.md`.

### Step 4 — PERSIST TO THE LEDGER (mandatory — the audit is not done until it is in SQLite)

One ledger, one session. `sops` holds CALL/SALES/HUMANIZED; `rounds`/`rag` hold LATENCY.

**Row shapes** (one row per call × category):

```json
{"run_id":"<run>","trace_id":"<ledger trace_id>","sop":"CALL",
 "category":"prompt_following","scope":"contact_details t12-t28","verdict":"FAIL",
 "rule":"P0/FIND-1","failure_reason":"…","failure_assessment":"…","evidence":"…",
 "source":"<NN>_callsop_report.md"}

{"run_id":"<run>","trace_id":"<ledger trace_id>","sop":"SALES",
 "category":"D3","scope":"BOOK Maria","verdict":"PASS","rule":"D3 2/2",
 "evidence":"SALES band 13/14 (hire)","source":"<NN>_sop_sales_report.md"}

{"run_id":"<run>","trace_id":"<ledger trace_id>","sop":"HUMANIZED",
 "category":"R5","scope":"t10","verdict":"FAIL","rule":"R5.5",
 "failure_reason":"in-turn echo duplication","failure_assessment":"verbatim = hard flag",
 "evidence":"t10: '<sentence>' emitted twice","source":"<NN>_sop_humanized_report.md"}
```
- `verdict` = `PASS | PARTIAL | FAIL | OBSERVATION` (uniform across SOPs).
- CALL `category` = §1–§8; SALES = `D1…D7` (`rule` = `Dn x/2`); HUMANIZED = `R1…R5`
  (`rule` = `R5.x`); LATENCY is the `rounds`/`rag` plane (no `sops` row required —
  report it from `latency_pull.py` / `gates`).
- Missing per-dimension data (only a band total recorded) → one `OBSERVATION` row
  with the band in `evidence`. **Never invent numbers or trace_ids.**

**Aggregated import (all 4 SOPs, one session):**
```bash
cd /home/julio/projects/clean_diallux_SDR
DB=research/surgeon/iter48-rag-truth/ledger.db
LS="python3 scripts/live_sql.py --db $DB"
RUN=battery-iter49 BRANCH=engine/iter49-rag-parity
COMMIT=$(git -C /tmp/opencode/wt-iter44 rev-parse --short HEAD)
# LATENCY plane (rounds/rag) — Langfuse write, from the engine worktree (has .env):
( cd /tmp/opencode/wt-iter44 && set -a && . ./.env && set +a && \
  .venv/bin/python scripts/live_sql.py \
    --db /home/julio/projects/clean_diallux_SDR/$DB \
    import --window "14:03-14:11" --run $RUN --branch $BRANCH --commit $COMMIT )
# judgment SOPs — main-repo SDK, same run/branch/commit:
$LS sop-import --run $RUN --sop CALL      --branch $BRANCH --commit $COMMIT --file research/.../sop_rows_call.json
$LS sop-import --run $RUN --sop SALES     --branch $BRANCH --commit $COMMIT --file research/.../sop_rows_sales.json
$LS sop-import --run $RUN --sop HUMANIZED --branch $BRANCH --commit $COMMIT --file research/.../sop_rows_humanized.json
```
**Segregated import** = the same commands, one SOP per invocation (e.g. only the
`SALES` line) — still the same `--run`/session, so it lands in the same ledger run.

## 4. Queries (instant analysis — no md re-reading)

```bash
cd /home/julio/projects/clean_diallux_SDR
DB=research/surgeon/iter48-rag-truth/ledger.db
LS="python3 scripts/live_sql.py --db $DB"
$LS sessions                                                   # every pass: branch/commit + sops count
$LS runs                                                       # runs with their session
$LS sops --run battery-iter49                                  # all judgment SOPs, grouped by call
$LS sops --run battery-iter49 --sop HUMANIZED --verdict FAIL   # hard flags only
$LS sops --run battery-iter49 --json                           # machine output
$LS gates --a battery-iter49 --b happy-g                       # latency gates
python3 scripts/latency_pull.py --run battery-iter49
```
Traceability join:
```sql
SELECT s.sop, s.category, s.verdict, s.session, se.branch, se.commit_sha
FROM sops s JOIN sessions se USING (session) WHERE s.run_id='battery-iter49';
```

## 5. Report conventions
- Identify runs by `model + tag + persona` (mtime window), never filename alone.
- `gate_rejections > 0` on a book-persona = the iter11 alternate-number fix may be needed.
- Mock bookings always carry UID `qeTqHuZ1EDzH8bxEdhPQ6H` (`tests/mock_webhooks.py:14`) — a fixture, never a leak.
- Max-turns blowups (28t, `ended=false`) = engine dead-ended; call-quality FAIL even if expect-matched.
- Every number in a report must trace to a SQL row or a json_log field. Nothing from memory.

## 6. Langfuse scoring + exact costs

- **Scores:** every `--langfuse` run attaches `harness_result` (1.0 PASS / 0.0 FAIL +
  `outcome/turns/gate_rejections/agent_model`) to its trace; trace names carry the
  persona (`llm2llm-graph-bookmaria`).
- **Eval ladder:** `scripts/lf_eval.py ladder` (happy → stress → curve, stops on happy
  failure); `lf_eval.py status --since-min N` cross-walks fresh json_logs vs scores.
- **ASSASSIN GATE (permanent):** exactly one assassin — **Larry** (`(BRK) Larry —
  question-loop`, `"assassin": True`). Never in the default ladder; runs only behind
  `lf_eval.py ladder --assassin`.
- **Costs:** `scripts/lf.py costs --hours 24 [--name marcus]` (override with
  `LANGFUSE_MODEL_PRICES`). gpt-5.x prices are placeholders — verify before quoting.
- **SDK gotcha:** langfuse 4.15.1 SDK is stricter than the self-hosted server (dataset
  get/list breaks); `lf_eval.py` uses plain REST for datasets.

## 7. Scaffolding note
`scripts/live_sql.py` (sops/gates plane) and `scripts/latency_pull.py` are stable on
`main` and stdlib-only (`python3`). The engine worktree carries the live dev copy of
`live_sql.py`, the Langfuse `.env` (for the `import` write step), and the other
engine SDKs (`call.py`, `lf.py`, `lf_quick.py`, `latency.py`). Keep the 4 SOP files
+ this protocol in sync — every SOP's "persist" section documents the same `sops`
table, the same `--db` ledger path, and the same `session` convention.
