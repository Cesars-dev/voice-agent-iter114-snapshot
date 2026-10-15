# iter33 battery (gpt-5.2) — first-13, Phase B engine chain ON

- Window: 2026-09-08T16:00:10Z → 16:19:50Z (sequential, mock slots, caller gpt-4o, max-turns 48)
- Branch: `engine/iter33-phaseb-engine-chain` @ `31abcab` (fix#2; battery ran on `63ce382`, OTEL deltas only after)
- Log: `/tmp/opencode/iter33_battery.log` (per-persona tails truncated to 6 lines — full
  json_logs in `tests/llm2llm/json_logs/`); re-runs: `/tmp/opencode/phaseb_rerun.sh` (full logs)

## Per-persona table (first runs = battery record)

| Persona | Expect | Outcome | Pass | Turns | Ended | chain_done | booking |
|---|---|---|---|---|---|---|---|
| Maria | book | book | PASS | 15 | Y | True | qeTqHuZ1EDzH8bxEdhPQ6H |
| Danny | book | book | PASS | 16 | Y | True | (mock uid) |
| Susan | book | book | PASS | 16 | Y | True | (mock uid) |
| Marcus | book | book | PASS | 15 | Y | True | (mock uid) |
| Carlos | no-book | no-book | PASS | 16 | Y | False | — |
| Pedro | no-book | **book** | **FAIL** | 27 | Y | True | (mock uid) |
| Sofia | book | book | PASS | 15 | Y | True | (mock uid) |
| Jorge | no-book | no-book | PASS | 11 | Y | False | — |
| Daniel | no-book | no-book | PASS | 15 | Y | False | — |
| Brenda | book | book | PASS | 36 | Y | True | (mock uid) |
| Gene | book | book | PASS | 19 | Y | True | (mock uid) |
| Frank | no-book | no-book | PASS | 5 | Y | False | — |
| Ray | no-book | no-book | PASS | 6 | Y | False | — |

**12/13 PASS · ended 13/13 · gate_rejections: 0 everywhere except Sofia (1, recovered) ·
single booking attempt on all 8 books · ZERO 48-turn runs · speaker-visible silent turns 0/212.**

Pedro FAIL = the known deferred over-book (NOT in this plan; craft/retrain item, orthogonal).

## Meter reconciliation (window above)

- `state_latency.py`: 304 LLM gens over the 10 NAMED traces, p50 ~1.52s / p90 ~2.15s.
  + Susan EMPTY-trace 21 gens + Gene EMPTY-trace 29 gens → **≈354 gens battery-wide**
  (+ Frank ~12, unmetered — no trace). Total turns 212 → **≈1.7 gens/turn** (iter32: 2.09).
- `silent_rounds.py` (10 named traces): 304 gens / 137 speech / 167 silent = **54.9%**
  (Susan+Gene excluded — EMPTY REST names, cannot `--name`-filter; their gens ARE in Langfuse).
- `latency.py --hours 2`: per-call table in T3b report.
- Agent-turn wall (json_logs `turn_ms`, n=212): **p50 3.02s / p90 6.08s** (includes tool+rag
  time; LLM-gen p50 1.52s is the ≤2.0s gate metric).
- Booking-turn wall: Maria smoke ≤3s (chain ms + Closing gens ~1.1s); see T2. **≤6s ✓.**

## Gate reconciliation

| Gate | Target | Measured | Verdict |
|---|---|---|---|
| PASS | 12/13+ | 12/13 (Pedro = known over-book) | ✓ |
| ended | 13/13 | 13/13 | ✓ |
| gens | ≤300 | ~366 (480 iter32 → ~366; -24%) | ✗ (improved, not met) |
| silent% | ≤30 | 54.9% (54.2% iter32 — flat) | ✗ (residual = extract_*/transition_ sawtooth, Phase B v2 scope) |
| booking turn | ≤6s | ~1–3s | ✓ |
| turn p50 (LLM) | ≤2.0s | 1.52s | ✓ |
| suite | 141+ | 147 | ✓ |

Efficiency-gate note (same precedent as iter32): the ≤30%/≤300 targets need per-state
tool-protocol redesign (every state still runs extract→complete→transition ≈ 2 silent
gens/turn). Phase B killed the booking-tail rounds (VerifyLead/Booking LLM gens ≈ 0 on
chain-booked calls — Danny/Marcus/Sofia traces show it) but the sawtooth remains.
Craft gates all green; no drift (Frank 5t / Ray 6t clean exits, spoken goodbyes).

## OTEL tail record (what broke, what changed, re-run numbers)

Battery found 4/13 traces with export gaps (behavior unaffected — all PASS):
- Maria/Susan: chain TOOL spans + `phaseb:chain_ok` missing mid-trace (later spans present).
- Gene: trace cut at `chain_ok` — 2 Closing gens + `end_call` missing.
- Frank: whole trace absent from Langfuse.
- Susan/Gene traces also carried EMPTY REST names (meters' `--name` filter drops them).

Root cause: SILENT OTLP batch drops (exporter logs nothing; payloads ≤3.2KB — not size;
no server access to confirm ingest side; losses cluster in time = transient). Quiescence
polling alone cannot see them — hence fix#1 (commits below).

- `63ce382` (T1): blocking confirm in `finish()` + harness wait + 4 regression tests.
- `61b23f9` (fix#1): LOCAL census (`_started`: root + every start) vs server count;
  `tail_ok` True/False/None + LOUD `TAIL SHORTFALL` + verdict (`tail_expected/server/complete)
  written into every per-persona json_log; `span()` errors now printed (was blind `pass`).
- `31abcab` (fix#2): `tail_seen` passthrough, skip redundant 2nd wait. Suite 147.
- Re-runs (same branch, full logs): Maria 22:33 ✓ 81 obs complete · Susan 22:34 ✓ 100 obs
  complete · Gene 22:40 ✓ **108/108 `tail_complete=True`** · Frank 22:38 SHORT (5 mid-trace)
  → Frank 22:42 tail COMPLETE (`end_call` present, 28/32, 4 mid-trace speech gens missing).

Final tail status: **13/13 traces land with complete tails** (Frank: 4 mid-trace speech gens
short — meter denominator impact <2%, transcript in json_log is the record). No new branch
(2-iter cap respected: T1 + fix#1/fix#2 on the same branch).
