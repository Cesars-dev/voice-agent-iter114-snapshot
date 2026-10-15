# lf.py — Langfuse/OTEL Query SDK

> Fast observability queries for the v5 build. One command instead of throwaway scripts.
> Lives at `scripts/lf.py` in the v5 workspace. Zero dependencies (stdlib only).

## Where it lives
```
state_machine/diallux-langgraph-production-v5/scripts/lf.py     # the SDK (this doc: lf.md)
```

## Requirements
- Langfuse v3 stack healthy (`lf.py health` — web :3001 + clickhouse + zookeeper + worker UP; if ingest fails, check `docker ps` for `project-clickhouse-1`, `project-zookeeper-1`, `project-langfuse-worker-1`)
- `.env` in project root with `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` (auto-loaded; shell env overrides)
- Traces only exist if the run had `--langfuse` (harness flag) or ran through `/media` (auto-traced)

## Commands

| Command | What it answers |
|---|---|
| `lf.py health` | Is the pipeline alive? (server version + ingest check) |
| `lf.py traces [--hours 24] [--name SUBSTR] [--limit N]` | What ran? trace id, name, generations, tokens |
| `lf.py show <trace_id>` | Full observation map: every span/tool/generation in time order + counts |
| `lf.py gens <trace_id> [--state ConfirmSlots] [--last N] [--input]` | What did the model SEE and SAY per round: state sequence, spoken text, tool_calls, tokens. `--input` prints the last 3 messages (the actual prompt tail) |
| `lf.py tools <trace_id> [--name SUBSTR]` | Every webhook/tool call: payload, status, latency in ms |
| `lf.py usage [--hours 48]` | Token totals + blended cost estimate across all traces |

## Details that matter

- **Short ids work**: `lf.py gens 4365a31e` resolves against the last 7 days of traces.
- **gens output fields**: `state sequence` (the exact state walk — this is how we caught the ConfirmSlots bounce: 0 ConfirmSlots generations = never entered), `text` (spoken words), `tool_calls` (what the model chose), `tokens in/out` (inline KB mode ≈ 20k/generation; pgvector mode should drop to ~4–6k — watch this number to verify RAG is live).
- **tools output**: raw webhook JSON — response_variables values (`phone_confirmed: true` etc.), gate rejections (`status: gate_failed, missing: [...]`), slot payloads with reservation UIDs.
- **Time filters**: `--hours` is float (0.5 = 30 min).

## Recipes (the debugging moves that cracked the case)

```bash
# Did the call reach ConfirmSlots at all?
lf.py gens <tid> | grep -c ConfirmSlots          # 0 = never entered (gate starved it)

# Why did a transition bounce? (gate rejections go to the model, not the caller)
lf.py tools <tid> --name transition              # look for gate_failed + missing list

# Did the webhook actually return the value the gate wants?
lf.py tools <tid> --name record_reach            # response_variables payload

# What prompt did the model see in state X? (check {{dv}} substitution — blanks = schema hole)
lf.py gens <tid> --state ConfirmSlots --last 1 --input

# Which slots were offered — real or invented?
lf.py tools <tid> --name query_livecall_slots    # payload = ground truth vs spoken text

# Money
lf.py usage --hours 24
```

## History / why it exists
Built 2026-09-04 during the ConfirmSlots investigation after three transcript-only misdiagnoses. Lesson (see `tasks/surgeon/diallux-langgraph-v5-deploy/05_finding_confirmslots.md`): transcripts show the *conversation*; OTEL shows the *machine* — gate rejections, dropped DVS, silent normalization. The SDK makes the machine view one command away.
