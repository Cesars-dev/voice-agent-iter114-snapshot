# RAG-ANALYSIS-SOP — "Did retrieval deliver, was it the right ammo, did the model use it?"

> Companion to `full_call_analysys.md` (4-SOP protocol). Grades the RAG plane as a
> QUALITY audit, not just latency: fired → retrieved → returned → used. Feeds the
> same SQLite ledger, same session key. Chat-sim and mic calls share this SOP;
> timing verification stays with LATENCY-PERCENTILE-SOP + the mic SOP.

## What this SOP answers (owner's questions, iter63)

1. **Fired** — did the lanes fire (laneA refer-tags / laneB utterance, hybrid)?
2. **Retrieved** — what queries ran, did the query carry the caller's utterance?
3. **Returned** — how many chunks, which KBs, which pin block? Was the RIGHT chunk
   served vs the candidate pool ("picking from six to one": per-lane top-k merged
   through `merge_lane_chunks` with quota)?
4. **Used** — did the agent's reply actually wield the chunk (adapted, not
   verbatim = R4 territory), or did it talk past the knowledge?
5. **Fail-clean visibility** — short-window rounds must show `degraded=true,
   zero_hit=true` — expected, not a regression (iter62 E5 owner decision).

## Data planes and the accounting trap (learned iter63 — read this first)

**iter64 (PT-59 DONE):** the ledger `rag` table now stores `kbs`, `pinned`,
`lanes`, `zero_hit` natively (import reads them straight off the Langfuse span
output — `_ensure_rag_cols` migrates old DBs idempotently). `rag_pull.py`
prefers the ledger columns and only falls back to a sidecar for pre-iter64
rows. **No sidecar collection needed for new runs.**

Legacy trap (kept for reading OLD runs pre-iter64): the import used to drop
`kbs`/`pinned`/`lanes`, so pin-only rounds read `chunks=0, degraded=1` while
the model DID receive the pinned industry block. Treat
`chunks=0 ∧ pinned≠∅` as SERVED, not failed. TRUE clean-fail = `query` empty.
`zero_hit` (span-emitted since iter62) is the honest empty flag — `rag_pull`
uses it when present instead of inferring from `chunks=0`.

- **Query truncation:** the span stores `query[:200]` — long multi-tag laneA
  queries are truncated AT EMIT; byte-exact replay of laneA from the ledger is
  impossible. laneB (the utterance) is always intact.

## Where everything lives

| Thing | Path |
|---|---|
| SOP | `/home/julio/projects/clean_diallux_SDR/docs/Testing_guidelines/RAG-ANALYSIS-SOP.md` |
| Read-side SDK | `/home/julio/projects/clean_diallux_SDR/scripts/rag_pull.py` (stdlib `python3`) |
| SQLite ledger | `research/surgeon/iter48-rag-truth/ledger.db` (`rag` plane + `sops`) |
| Langfuse span enrichment sidecar | **LEGACY (pre-iter64 runs only)** — `research/surgeon/iterNN-slug/rag_span_enrichment_<run>.json`; `rag_pull.py` globs any iteration folder. New runs need NONE (ledger carries kbs/pinned/lanes/zero_hit). If you ever must hand-collect one: `lf.py`'s `_traces` breaks on Langfuse ≥3.17x (`limit` param → HTTP 400) — call `lf._get(f"/api/public/traces?fromTimestamp={lf._iso(hours)}")` directly and slice client-side |
| Chunk-text replay SDK | **engine branch copy `scripts/rag_replay.py`** — run it from the ACTIVE iteration worktree: `cd /tmp/opencode/wt-iterNN/engine && set -a && . ./.env && set +a && .venv/bin/python scripts/rag_replay.py --run <run> --out /tmp/<run>_truth.jsonl` (needs engine venv + .env; older runs: `git show engine/iter63-rag-fire-sim:engine/scripts/rag_replay.py`). Deterministic read-only; mirrors the sim EXACTLY: laneA tags batch in ONE `retrieve_lanes` call, laneB in its own — batch-padding shifts borderline 0.24-filter scores. iter64+: the replay mirrors the pre-pin industry scope gate |
| Transcripts (user/agent turns) | engine worktree `tests/llm2llm/json_logs/*.json` (keys: `transcript[].caller/agent/state/turn`) |

## Usage

```bash
cd /home/julio/projects/clean_diallux_SDR
python3 scripts/rag_pull.py --run chat-iter63-sim-b              # per-call table + gates
python3 scripts/rag_pull.py --run chat-iter63-sim-b --gates      # gate line only
python3 scripts/rag_pull.py --run chat-iter63-sim-b --rounds --state Discovery
python3 scripts/rag_pull.py --run chat-iter63-sim-b --zero       # zero-chunk rounds
python3 scripts/rag_pull.py --run chat-iter63-sim-b --pin        # pin coverage per call
```

Enrichment sidecar (pre-iter64 runs only): if absent, `rag_pull.py` degrades
to ledger-only. **iter64+: the ledger columns make the sidecar unnecessary —
import and analyze, nothing else.**

Deliverable = **ONE gates table** (drilling adds one per-round table max). The
chunk-USE judgment is the analyst's half (see below), saved to the surgeon
report — the chat answer shows the one table + summary + relevant info.

## Gates (chat-sim battery; mic gates live in the config + LATENCY SOP)

| Gate | Threshold | Notes |
|---|---|---|
| `served_rate` | ≥ 95% | chunks>0 OR pinned-block rendered — the TRUE knowledge-delivery rate |
| `land_rate` (span-degraded inverse) | ≥ 90% | inflated DOWN by pin-only rounds (accounting artifact) |
| `query_empty_rate` | 0% | >0 = fire/consume contract broken |
| `zero_hit_rate` | informational | investigate KB gaps if >15% of rounds land empty-handed |
| `await_p50` | ≤ 80 ms | hybrid consume cap 60 ms; aw=0 expected on chat sim (embed beats the 500 ms tail) |
| `pin_coverage` | >0 once industry dv known | verticals: Dental/Auto Repair/Remodeling/Law/Plumbing verified iter63 |

## Chunk-quality judgment (the analyst's half — never automated away)

For each substantive round (Intake/Discovery/Closer/Offer — the persuasion arc):
- read caller turn → query → served chunks (recon) → agent reply;
- verdicts: `RIGHT+USED` (chunk on-topic and the reply wields it) /
  `RIGHT+IGNORED` / `WRONG-CHUNK` / `NO-AMMO-NEEDED` (mechanical states) /
  `KB-GAP` (zero-hit where content should exist);
- cross-check R4 (template leakage): KB phrasing should be adapted, not verbatim;
- "better choice" check: compare the pre-merge pool (per-lane candidates) vs the
  served set — if a clearly better on-topic candidate was dropped by quota/filter,
  note it with chunk id + score.

## Session/persistence conventions

Same as `full_call_analysys.md` §2/§4: every pass is `--run --branch --commit`
(session `<run>@<commit>`), `rounds`/`rag` are the LATENCY plane (no `sops` rows),
the chunk-usage judgment rides the surgeon report, and HUMANIZED/CALL/SALES rows
land in `sops` via `live_sql.py sop-import`. ~~Mandatory fix back-ported to the
engine import backlog: store `kbs`/`pinned`/`lanes` in the `rag` import~~
**DONE iter64 (PT-59): the import stores `kbs`/`pinned`/`lanes`/`zero_hit`.**
