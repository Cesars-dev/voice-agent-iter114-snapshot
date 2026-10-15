# call.py — Call-Analysis SDK

> One command to know everything that happened on a test call. Reads harness logs
> (`tests/llm2llm/json_logs/*.json`), applies the deterministic subset of the
> CALL-ANALYSIS-SOP, outputs JSON so an analyst (human or LLM) reads it fast.
> Lives at `scripts/call.py` next to `lf.py` (the Langfuse/OTEL query SDK).

## Where it lives & what it eats
```
state_machine/diallux-langgraph-production-v5/scripts/call.py    # this doc: call.md
tests/llm2llm/json_logs/*.json                                   # its data source (one file per call)
```
Log files are written by the harness on every run (with or without `--langfuse`).
No network, no DB — pure local file analysis. Zero dependencies.

## Commands

| Command | Answer |
|---|---|
| `call.py list [--hours 48]` | Every call: persona, outcome, pass, turns, p50 latency, gate rejections |
| `call.py show <substr>` | Full JSON dossier of the newest matching call (transcript, tools, DVS, booking, checks) |
| `call.py sop <substr>` | Dossier + SOP auto-checks (exit code 1 if any HIGH/MED finding) |
| `call.py dvs <substr>` | Final dynamic variables + contamination check only |
| `call.py turns <substr> [--frm N] [--to N] [--tool sub]` | **Verbatim transcript slice** — caller/agent text + tools per turn; `--tool` filters turns whose tool list contains the substring |
| `call.py lat <substr>` | **Latency profile** — p50/p90 from the per-turn `turn_ms[]` array + the 5 slowest turns |

`<substr>` = case-insensitive substring of the log filename (`Marcus`, `Danny`, `Sofia`...).
Newest matching file wins. Exit codes: 0 clean, 1 not-found or HIGH/MED finding.

## What's in the dossier (JSON keys)

- `persona / expect / outcome / pass / ended / turns` — identity + verdict
- `turns_p50_ms / wall_s` — latency profile (inline-KB mode ≈ 6–10s; pgvector target <3s)
- `booking` — `{fired, attempts, booked, uid, status}` (attempts>1 = hidden retry flakiness)
- `gate_rejections / blocked_transitions` — how many times the hard gates bounced the model
- `final_dvs` — the 42 typed variables at call end (the null-doctrine ground truth)
- `tools_order` — every tool in firing order (compare against the per-state required set)
- `transcript[]` — `{turn, caller, agent, tools[]}` per turn
- `auto_checks[]` — deterministic SOP findings, see below
- `rag_stats` — `{rag_turns, rag_ms_total, rag_chars}` (0s until pgvector is on)

## Auto-checks (the deterministic slice of the SOP)

| Check | Severity | Meaning |
|---|---|---|
| `echo_contamination` | HIGH | Caller said "I'm Danny" but final DVS says another name → a webhook echo overwrote per-run data. **This check caught the mock-fixture bug on 2026-09-04** |
| `required_dvs_missing` | HIGH | Booked but missing name/number/time/UID/`slot_verified` |
| `tool_called_multiple` | MED | `create_livecall_booking`, `record_booking_uid`, `end_call`, `transition_to_Booking` fired more than once |
| `booking_failed` | HIGH | BOOK persona, tool fired, no booking |
| `gate_rejections` | INFO | Bounces that self-corrected (normal when data incomplete — the gate doing its job) |
| `slow_turns` | INFO | p50 > 8s (inline-KB weight) |

The judgment categories of the SOP (prompt-following nuance, hallucination, redundancy,
flow pacing) stay with the analyst — read `transcript` in the dossier.

## Recipes

```bash
# Latest battery results at a glance
python scripts/call.py list --hours 6

# Full JSON on one call (pipe to jq / read directly)
python scripts/call.py show Danny

# Did the ghost-name bug come back?
python scripts/call.py dvs Danny | jq '.auto_checks'

# After enabling pgvector: did turns get faster?
python scripts/call.py list --hours 1
```

## History / gotchas it already caught
- 2026-09-04: `echo_contamination` flagged `first_name=Maria` on Danny's call → root cause:
  `tests/mock_webhooks.py` had Maria hardcoded as fixture; the frozen echo stamped over
  per-run DVS at VerifyLead. Mock now mirrors the live contract (echo request args;
  leak math verbatim from `validator_endpoint/validate.py` incl. 2-sig floor and
  null-leak-when-inputs-missing).
- Gotcha: log `final_dvs` from runs BEFORE that date is untrustworthy for
  `first_name / callback_number / weekly_leak / monthly_leak`.
- Gotcha: costs are not yet wired into logs (`cost: null`); use `lf.py usage` for token/cost.
- Companion: `scripts/lf.py` for the OTEL view (webhook payloads, gate reasons,
  per-round prompts) — `call.py` reads the log, `lf.py` reads the machine.
