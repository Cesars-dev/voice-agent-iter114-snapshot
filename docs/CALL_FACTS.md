# CALL FACTS — one-line ledger of tracked calls

Purpose: a light, append-only index of calls we care about, so we can go back and
forth between *"great latency / dumb agent"* and *"better agent / not fast enough"*.
One line per call — what it was, where the engine came from, and the pointer to the
SQL database. The heavy facts live in SQLite, not here.

**SQL database (facts):** `research/surgeon/call-facts/call_facts.db`
(tables `calls` / `turns` / `rag` / `warm` / `facts`; view `call_compare`).
Import a call:
`research/surgeon/call-facts/import_call.py --log <uvicorn log> --sid <sid> --branch <b> --commit <sha> --port <p> --model <m> --trace <id>`

**Where the engine writes the numbers** (read the source when unsure):
- `diallux/media/session.py:543-546` — turn report → stdout + Langfuse span `turn:N`
- `diallux/media/session.py:270` — `tracer.finish(turn_reports)` at call end
- `diallux/observability/latency.py:34-43` — the report dict (`stt_eot_to_llm_first_ms`, …)
- `diallux/observability/metrics.py:50` — Prometheus histograms
- `diallux/observability/tracer.py` — Langfuse spans/generations

## Calls
| call_id | date | engine origin | model | turns | latency | agent | notes |
|---|---|---|---|---|---|---|---|
| `1480fbd1f29d` | 2026-09-09 | `engine/iter38b-gpt54` @ `491a7a3` | gpt-5.4 | 47 | cold turn-1 1966 ms; ~16/17 rounds <1.2 s; one 2.9 s state-transition spike | blind/lazy: missing KB/tools where it mattered, 9/10 date bug, 2pm loop, **no booking** | **the "great latency / dumb agent" reference.** trace `ecbc42943d42`; report `research/surgeon/iter39-spike-kill/01_analysis_call_1480fbd1f29d.md` |
| `56766c0e5a58` | 2026-09-19 | `engine/iter56-state-delta-payload` @ `abde308` | gpt-5.4 | 52 (cap) | TTFT p50 843; e2e p50 1330; 2.4–3.5 s barge-in spikes | RAG alive; dumb Intake opener; spike tail | current baseline; server `:8020` wt-iter56; trace `2372a40f4e82` |

<!-- add one row per tracked call: call_id | date | branch @ commit | model | turns | latency | agent | notes -->
| `56766c0e5a58` (redo pending) | 2026-09-19 23:21 UTC | `engine/iter56-state-delta-payload` @ `abde308` | gpt-5.4 | 52 (cap) | mic bridge starved 73-198 ms/chunk (nominal 50) → breakup; EOT median 2203 ms, 4 fires ≤1 s; eager 39/34 adopted/5 abandoned; pre-greeting server 2.7 s | agent dumb on opener; no wasted LLM calls (not the cause) | the "breaking up / EOT too sensitive / slow first phrase" call; facts → `call_facts.db` |

## Observations (append-only)
<!-- one line: date — what we noticed — evidence (file:line / trace id / log snippet) -->
- 2026-09-19 — the "great" call was `1480fbd1f29d` on `engine/iter38b-gpt54 @ 491a7a3` (gpt-5.4), i.e. **131 commits behind** current HEAD `abde308`; great consistency, awful agent.
- 2026-09-19 — iter56 voice call `56766c0e5a58` **was** iter56 code with RAG alive (53 `state_in_delta` markers, 0 RAG failures) — the "stupid" was real behaviour, not a stale build.
