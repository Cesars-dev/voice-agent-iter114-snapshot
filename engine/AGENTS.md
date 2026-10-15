# Dialux SDR v5 (LangGraph production build) — AGENTS.md

> **⚠️ DOCTRINE SUPERSEDED (2026-09-08, git-era restructure). Read repo-root
> `DOCTRINE.md` + `AGENTS.md` first. Corrections to everything below:**
> - **Snapshots are NOT folders** — iteration state = git branches (`engine/*` in the
>   parent repo); the photocopy era is archived as `engine/snap-*` + `_cold_archive/`.
> - **⚖️ LAW 0 (v2): CODE = branch + owner-gated merge (no code merges without Julio's
>   say-so, ever); DOCS/md/notes = straight to main, no ceremony.**
> - Project root is now `/home/julio/projects/clean_diallux_SDR/engine` (this folder).
> - Suite is **129** (not 93); run via `engine/.venv/bin/python -m pytest tests -q`.
> - Old absolute paths below (`…/Retell_AI_MCP_connection/...`) point at the frozen
>   original — MIGRATION_MAP.md translates them.
> Everything below this banner is the historical iter21-era manual, kept verbatim.

> Operating manual for this folder. Read before touching anything.
> Parent project: `/home/julio/projects/Retell_AI_MCP_connection` (its `AGENTS.md` is the master index).

## What this is

The LangGraph port of the deployed Dialux SDR chat agent ("Linda"): a 9-state
state machine with hard-gated transitions, typed dynamic variables, pgvector RAG,
deterministic leak math, and an LLM-to-LLM acceptance harness. Same prompts/tools/
webhooks as the deployed agent, ported 1:1 and hardened. **Snapshots = FOLDERS** in
`/home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/v5-snapshots/` (never git).

## Current status (2026-09-06, evening)

- **Latest: iter21 IN PROGRESS — browser-mic bridge.** Plan `plans/plan_v5_iter21_browser_mic_bridge.md`
  (in the PARENT plans/ dir). Baseline of record: `v1.8-iter20-20260906` (87 green); working folder =
  baseline + iter21. Built this session: `AUDIO_TRANSPORT=twilio|browser` codec layer (Deepgram
  linear16@16k / Cartesia pcm_s16le@16k for browser; 8k mulaw for Twilio — zero graph/prompt edits),
  `GET /mic` + `WS /mic/ws` + `diallux/static/mic/index.html`, `micbridge-<session>` Langfuse traces,
  SSML per-sentence jitter, RAG keep-alive embed client + greeting warmup + `rag_min_query_chars`
  filler skip, eager EOT ON (fixed dormant `settings.deepgram_eager` missing-field bug), turn-report
  race fix (`:final` spans), `lfpull` SDK (`diallux/observability/lf_sdk.py` + `scripts/lfpull.py`).
  Voice: Cartesia Linda `829ccd10-f8b3-43cd-b8a0-4aeaa81f3b30` @ speed 1.05. Suite **93 passed**.
- **T4 (live booking) NOT passed yet:** the calendar endpoint `slots.diallux-ai.site/check_availability`
  500s on everything — `cal_slots_endpoint/slots.db-shm/-wal` owned by wrong user (julio vs service
  user `2nd_workspace`, created Sep 5 10:30); fix = one chown (ops, pending Julio). This ALSO breaks
  the deployed Retell agents' slot checks. Snapshot `v1.9-iter21-*` NOT cut.
- **Code on GitHub: `Cesars-dev/dialux-sdr-langgraph-v5`** (public, keyhound-clean, commit 2dc13ff).
- **Live-call evidence driving iter22:** gpt-5.2 TTFT uncached ~1.0-1.6s vs 843-952ms with
  byte-identical prefix (dvs+RAG mutate our prompt prefix → 0% prompt cache); input tokens balloon
  2.7k→9.6k (full history per turn, doubled by the eager duplicate-turn bug); RAG starves sales KBs
  (sales-psychology 1 hit in 116 turns; 79/102 queries → 0 chunks). Plan:
  `plans/plan_v5_iter22_retell_parity_toe_to_toe.md` (parent plans/).
- **iter20 (2026-09-06):** `sales-language-kb.md` §Pricing third-ask releases the sanctioned string
  ("…starts at $697 a month…") ONLY under real push; prompts/ladder untouched; 233 chunks embedded.
  Bail-out lane PARKED (PT-32). Full log: `notes.md` iter20 entry.
- **Prior validated: iter15 + iter15b** (`last_name` optional, MAX_TURNS 48, full read-back).
  12/13 original personas expect-match. NOT ported to the deployed V7.7 agent
  `agent_87e4d5f08475e5bc558b2f390f`.
- **Suite: 93 passed** (`pytest tests/`): 87 (iter20) + 6 iter21 (pcm16 transport, browser TTS
  format, transport/trace-name, /mic page+ws, SSML jitter shape, speak-clock routing).

## Key paths (all absolute)

| What | Path |
|---|---|
| Project root | `/home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/diallux-langgraph-production-v5` |
| **This AGENTS.md** | `…/v5/AGENTS.md` |
| State machine source (9 states, edges, gates, tools, dvs) | `…/v5/agent/llm.json` — loaded directly (config.py:129); **no builder script** |
| Prompts (live source of truth; read at runtime) | `…/v5/diallux/prompts/*.md` |
| Typed dynamic variables + server-owned protection | `…/v5/diallux/schema.py` |
| Graph/tool executor (gates, webhooks, transition engine) | `…/v5/diallux/graph/{builder,llm,tools,subst}.py` |
| Langfuse tracer (traces, tool/gen spans, scores) | `…/v5/diallux/observability/tracer.py` |
| .env (secrets: OPENAI_API_KEY, LANGFUSE_HOST, DATABASE_URL) | `…/v5/.env` |
| Tests (93, `pytest tests/`) | `…/v5/tests/` |
| **LLM-to-LLM harness** | `…/v5/tests/llm2llm/harness.py` |
| **Personas (25)** | `…/v5/tests/llm2llm/personas.py` |
| Mock webhooks (hermetic tests) | `…/v5/tests/mock_webhooks.py` |
| Run transcripts (one json per call) | `…/v5/tests/llm2llm/json_logs/*.json` |
| Langfuse REST CLI (health/traces/gens/tools/costs) | `…/v5/scripts/lf.py` |
| Langfuse dataset + eval ladder runner | `…/v5/scripts/lf_eval.py` |
| Call transcript SDK | `…/v5/scripts/call.py` |
| Reports (surgeon folder) | `…/Dialux_SDR/tasks/surgeon/v5-pgvector-gpt52-argfix/` |

## Services & ports (running on this VPS)

| Service | Endpoint / port | Notes |
|---|---|---|
| Langfuse (self-hosted) | `http://localhost:3001` | docker; `LANGFUSE_HOST` in `.env`; ingest lag ~5–10 s |
| Postgres (pgvector RAG) | `postgresql://diallux:diallux@localhost:5432/diallux` | container `diallux-db` |
| Live validator webhook (read-only ref) | `…/validator_endpoint/` (:8003) behind Caddy `slots.diallux-ai.site` | systemd `validator-service`; iter14 decoupled `phone_confirmed` |

## The 25 personas (personas.py)

- **13 original:** 4 `happy-path` (Maria, Danny, Susan, Marcus — expect book),
  5 `stress` (Carlos mean, Pedro dumb, Sofia problematic, Jorge enquiry-only,
  Daniel ai-question), 4 `curve` (Brenda, Gene, Frank, Ray).
- **12 adversarial (ported from `testing/runners/adversarial_suite.py`):**
  5 `gatekeeper` `(GK)` Sam, Priya, Boris, Bianca, Dave (expect book);
  7 `breaker` `(BRK)` Larry, Jamie, Rita, Nick, Suzy, Alan, Wendy (expect no-book).
- **Larry (question-loop) = the ASSASSIN** — tagged `"assassin": True`. His ONLY
  purpose is to break the agent. **NEVER run in a routine batch**; gated behind
  `lf_eval.py ladder --assassin`, runs once on purpose. The `all` group includes
  him — do NOT run `--personas all` as a normal battery without the gate.
- Persona schema: `name/type/expect/dynvars/opener/system`. `expect` ∈ `("book","no-book")`.
- New personas are PER-AGENT, authored from scratch (see `tests/llm2llm/README.md`).

## How to run

```bash
cd /home/julio/projects/Retell_AI_MCP_connection/Dialux_SDR/diallux-langgraph-production-v5
.venv/bin/python -m pytest tests/ -q          # expect 93 passed

# single persona / group (gpt-4.1 is the shipped model)
set -a; . ./.env; set +a
.venv/bin/python tests/llm2llm/harness.py --personas Maria --rag --langfuse --max-turns 48 --agent-model gpt-4.1

# SOP ladder (happy -> stress -> curve -> gatekeepers -> breakers; skips assassin)
.venv/bin/python scripts/lf_eval.py ladder --agent-model gpt-4.1
# OPT-IN assassin (Larry), once, on purpose:
.venv/bin/python scripts/lf_eval.py ladder --assassin
```

SOP: happy first; if a happy-path run fails, fix the agent before stress
(`--no-happy-gate` to override). Cancel all mock bookings after a batch
(mock bookings are fixtures — no real calendar).

## Langfuse SDK (langfuse 4.15.1, OTEL-based v4)

```bash
set -a; . ./.env; set +a
python scripts/lf.py health
python scripts/lf.py traces --name marcus --hours 24
python scripts/lf.py show <trace_id>            # short id OK (>=8 chars)
python scripts/lf.py tools <trace_id>
python scripts/lf.py costs --hours 24 [--name marcus]   # exact per-model USD
python scripts/lf_eval.py bootstrap             # idempotent dataset 'diallux-personas' (25 items)
python scripts/lf_eval.py status --since-min 40 # json_logs vs Langfuse harness_result scores
```

- Every `--langfuse` harness run: trace named `llm2llm-<transport>-<persona-slug>`
  (e.g. `llm2llm-graph-bookmaria`), plus a `harness_result` score (1.0/0.0 + comment).
- SDK gotcha: langfuse 4.15.1 SDK is stricter than the self-hosted server — dataset
  reads break on response parse; `lf_eval.py` uses plain REST for datasets, SDK for
  traces/scores. `get_trace_url(trace_id=...)` needs the keyword arg.
- Prices in `lf.py costs` for gpt-5.x are placeholders — set `LANGFUSE_MODEL_PRICES`.

## Rules

- 84 tests must stay green after any change; rerun `pytest tests/ -q`.
- Cross-call repetition (R5.6) between different calls is ACCEPTABLE for now (fix later);
  repetition INSIDE one call (R5.3/R5.5) is a defect. Per Julio, 2026-09-05.
- The 3-SOP quality-audit protocol lives in
  `…/docs/Testing_guidelines/full_call_analysys.md` (opencode skill `sop_call_analysis`).
- Snapshots are folders under `Dialux_SDR/v5-snapshots/`, never git. No `rm -rf` —
  `mv` to `/tmp/opencode/`. Before new harness runs check `df -i /` (abort if free < 50000).
- Never touch the production voice agent `agent_16985b5d087e56c35141983396`, the
  deployed chat agent `agent_87e4d5f08475e5bc558b2f390f`, or the live
  `validator_endpoint/` service unless explicitly approved.
- Secrets live in `.env`; never print or commit them.