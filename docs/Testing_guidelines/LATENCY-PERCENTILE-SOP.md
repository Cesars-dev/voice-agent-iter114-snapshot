# LATENCY-PERCENTILE-SOP — instant P50/P90 on TTFT, LLM, e2e, cache, RAG

> How to turn any engine run (battery, happy, live) into a latency table in ONE
> command. No LLM needed to read the output. Percentile method: nearest-rank
> over the sorted sample (`v[min(int(q*n), n-1)]`) — same as `latency.py`.

## 0. Metric definitions (know what you're reading)

| Metric | Source | Meaning |
|---|---|---|
| **TTFT** | ledger `rounds.ttft_ms` | time-to-first-token of the LLM call, per round |
| **TTFT-steady** | same, turn-2+ per call | cached steady-state TTFT (the number that should sit ~700-950ms) |
| **turn-1** | first TTFT-bearing round per trace | first response of the call (lite round) — read WITH its `cache_read` |
| **LLM duration** | `rounds.llm_ms` | full generation wall time (TTFT + streaming) |
| **cache_read** | `rounds.cache_read` | prompt-cache hit tokens; floor values today: 2688 (tools+head), 3712 (state entries), 1664 (lite turn-1) |
| **RAG ms / chunks / cosine** | `rag.ms, chunks, cosine` | retrieval cost + yield per span |
| **tokens in/out** | `rounds.input/output` | payload size; input p50 ~4.6k is the healthy band |
| **e2e (LIVE ONLY)** | json_logs turn reports + Langfuse | `e2e = STT-EOT→LLM-first + TTFT + LLM→TTS gate`. The llm2llm harness is text-native: STT/TTS planes are INVISIBLE to it. Never compare harness rounds to live e2e. |

## 1. Data sources (pin these — tooling lives on engine branches/worktrees)

| Source | Path (today) | Lives on |
|---|---|---|
| SQLite ledger | `clean_diallux_SDR/research/surgeon/iter48-rag-truth/ledger.db` | main repo `research/` (gitignored — survives branch switches) |
| **Percentile puller (this SOP's SDK)** | `clean_diallux_SDR/scripts/latency_pull.py` | main repo `scripts/` |
| Langfuse span SDK | `scripts/latency.py` (json_logs + Langfuse spans: LLM p50/p90, RAG, tool, tokens, cache estimate) | engine branch worktree (currently `/tmp/opencode/wt-iter44` @ `engine/iter48-rag-truth` `033f742`) |
| Ledger import/gates/sops | `clean_diallux_SDR/scripts/live_sql.py` | **main repo `scripts/` (stable)** · live dev copy in the engine worktree |
| Quick ledger pulls | `scripts/lf_quick.py runs\|bugs\|rounds` | same engine worktree |
| Live e2e parser (ref impl) | `research/surgeon/iter45-latency-audit/extract_turns.py` (uvicorn "turn N final report" → per-turn STT/TTFT/TTS/e2e) | evidence folder, gitignored |
| SQL SOP (ledger design) | `research/surgeon/iter48-rag-truth/SQL_ANALYSIS_SOP.md` | evidence folder |

**Branch rule:** the write-side engine scripts (`latency.py`, `call.py`, `lf_quick.py`)
live on the engine worktree/branch; recover them from `engine/iterNN-*` if a worktree
is gone. The two SDKs this SOP depends on are **stable on `main`** and need only
stdlib `python3`: `scripts/live_sql.py` (ledger import/gates/sops) and
`scripts/latency_pull.py` (percentiles).

**Session tracking (all 4 SOPs):** the write step (`live_sql.py import`) stamps
`runs.session = <run_id>@<commit>` and writes a `sessions` row carrying the engine
`branch`; `rounds`/`rag` join to it via `run_id`. Pass
`--branch engine/iterNN-<slug> --commit <sha>` on import, then
`live_sql.py sessions` lists every pass (branch/commit + sops count). This SOP is
the LATENCY plane of the 4-SOP protocol — run it under the SAME `--run`/session as
CALL/SALES/HUMANIZED for an aggregated pass. Full protocol: `full_call_analysys.md`.

## 2. THE one-command pull (ledger plane — batteries + happy runs)

```bash
cd /home/julio/projects/clean_diallux_SDR
python3 scripts/latency_pull.py --run happy-f --run battery-iter48   # any run_ids in the ledger
# filters: --state Discovery   ·   machine output: --json
# all runs: omit --run
```

Output per run: rounds, TTFT p50/p90, TTFT-steady p50/p90, turn-1 (ttft/cache
per call), LLM p50/p90, cache-0 round count, cache-hit floor, token p50s,
RAG ms p50/p90 + chunks + cosine avg, per-state TTFT table, per-call turn counts.

Sanity anchor (2026-09-12, verified): `happy-f` = TTFT p50 **900** / p90 **1143**,
LLM p50 **1164**, turn-1 = (792, 810, 796, 1143) **all cache 2688**;
`battery-iter48` = 357 rounds, TTFT p50 **874** / p90 **1037**, cache-0 = 30.

## 3. Live-call e2e (the planes the harness cannot see)

```bash
# spans + turn composition (last N hours, name filter):
.venv/bin/python scripts/latency.py --hours 6 [--name bookmarcus] [--json]
# per-turn STT→LLM / TTFT / LLM→TTS / e2e tables (uvicorn turn reports):
#   see extract_turns.py pattern in research/surgeon/iter45-latency-audit/
```

Read order for a live call: TTFT (LLM plane) + STT-EOT→first (+100-450ms over
TTFT = drift embed + graph hop) + TTS gate (100-250ms). e2e ≈ sum. Compare only
against live, same verbosity (medium verbosity historically +150-350ms/turn).

## 4. Thresholds in force (gates, not vibes)

| Gate | Threshold | Origin |
|---|---|---|
| G4 steady TTFT p50 | ≤ 950ms | iter48 gates (`live_sql.py gates`) |
| G4 turn-1 TTFT | ≤ 1000ms (battery: ≤1300 first heavy round from iter49) | owner: <1000ms must-have |
| e2e target (live, post iter50 region move) | ≤ 1000-1400ms | `10_latency_findings.md` projection |
| RAG hot-path addition | ≤ +60ms/round vs battery-iter48 | iter49 plan |
| local embed p50 | ≤ 30ms | iter49 plan T2 |
| cache floors | 2688/3712 steady · 1664 lite turn-1 | FIND-5/iter44 — drop below = placement bug, STOP |

## 5. Interpretation pitfalls (paid for in blood)

1. **Always split turn-1 from steady** — one cold round inside a p50 hides the
   regression; one cached turn-1 hides a missing prewarm.
2. **cache-0 rounds cluster at Closing** (allow-list 0 chunks; FIND-4) — a cache-0
   count is only alarming if it is NOT Closing/transition-heavy.
3. **Transition entries with cache 0** = prewarm lost the race — cold re-prefill
   900-2115ms. Compare per-state p90, not just p50.
4. **Verbosity is a config variable, not noise** — re-run comparisons only on
   same-verbosity runs (iter45 §0, SUSPECT #1).
5. **Offer/low-n states** (n<10) have unstable p90 — flag n in the table
   (the puller prints n per state).
6. **RAG ms is async drift cost in iter48** — with live-retrieve (iter49) it sits
   ON the hot path; watch the ≤+60ms gate, not the raw number.
