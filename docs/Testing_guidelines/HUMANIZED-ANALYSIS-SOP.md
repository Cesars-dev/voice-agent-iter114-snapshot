# Humanized Analysis SOP — "Are We Screaming AI?"

> Companion to CALL-ANALYSIS-SOP. Grades how NATURAL the agent sounds to a human ear.
> The point: detect robotic, monotone, template-y, or tool-aware speech before a prospect does.

## SDK-first (do NOT rebuild — the mechanicals are ONE command)

```bash
python3 scripts/sop_mechanicals.py /tmp/opencode/wt-iterNN/engine/tests/llm2llm/json_logs/<glob> --rows-out <NN>_sop_humanized_rows.json
```

It emits the full R1–R5 mechanicals per call (R1 regex, R3 tool-talk, R4
brackets, R5.1 bloat, R5.4 filler stack, R5.5 verbatim repeats, R5.7 mirroring)
plus a ready `sop-import` rows file. The analyst only: reads the flagged turns
verbatim, does the ear check, and adjusts verdicts. Deliverable = **ONE table**:
R1–R5 × calls with the call verdict per row.

## Red Flags Checked

### R1. Robotic markers
- Corporate filler: "I completely understand", "I hear you", "Certainly", "I'd be happy to"
- Over-perfect grammar: zero contractions across 10+ turns
- List-speech: "First... Second... Third..." in a phone conversation

### R2. Monotone / rigidity
- Sentence-length variance near zero (all turns same rhythm)
- Same opener structure turn after turn ("Got it," ×N)
- Identical transition phrases between every state

### R3. Verbatim tool references
- Agent says tool/variable names aloud ("validate_lead", "slot_verified", "dynamic variables", "extract...")
- Narrates mechanics ("let me run the validator") instead of natural speech ("let me check that")

### R4. Template leakage
- KB sample phrases used word-for-word with no adaptation
- Bracket/placeholder syntax spoken ([OPTION 1], [their timezone])
- Same sentence reused across different personas' calls (cross-call fingerprinting)

### R5. Verbosity / redundancy — "is it talking past the caller?" (added 2026-09-04, iter12)

A caller answers in 5 words; the agent answers in 80. That is not conversation — it is speech
playing over the caller. Check every call for:

- **R5.1 Turn bloat vs. caller** — agent turn >40 words while the caller's last answer was ≤5 words
  AND the caller asked nothing (reference caps: default turn ≤25 words; mirror short answers ≤15;
  >45 words only when the caller's question required it).
- **R5.2 Re-explaining** — same claim / benefit / number delivered 2+ times in one call
  (re-pitching after acknowledgment, repeating the leak math, re-describing the walkthrough).
- **R5.3 Phrase recycling** — the same distinctive (non-acknowledgment) phrase used 3+ times in
  one call (e.g., "20-minute walkthrough — I'll show you exactly how it works and what your
  numbers look like").
- **R5.4 Filler stacking** — ≥2 acknowledgments stacked in a single turn ("Got it — I hear you —
  that makes sense").
- **R5.5 Verbatim sentence repeats** — any full sentence repeated word-for-word across turns in
  one call.
- **R5.6 Cross-call fingerprinting** — the same distinctive sentence appearing in ≥3 different
  calls (template leaked into "natural" speech).
- **R5.7 No mirroring** — caller's average answer length ≤6 words across a call while the
  agent's average turn stays >25 words (energy/length adaptation failure; the short-answer
  caller test).

Severity: R5.5/R5.6 are hard flags (verbatim = template, never accidental). R5.1–R5.4/R5.7 are
graded: ≥2 hits of one kind = 1 flag; 3+ kinds hit = automatic verbosity FAIL regardless of count.

## Scoring
- Count R1–R5 hits (R5 counted per the severity rules above).
- **0 flags**: passes as human
- **1–2**: minor polish pass
- **3+**: rewrite needed before production

## Output per call
```
## [Persona] — Humanized: PASS/POLISH/FAIL
R1 robotic: n hits [examples]
R2 monotone: opener-variance x, top repeated opener "..."
R3 verbatim tools: n
R4 templates: n [examples]
R5 verbosity: n flags [R5.x hits with examples; caller-len vs agent-len medians]
Ear check quote: "[most robotic line]"
```

## PERSIST TO THE LEDGER (mandatory — the audit is not done until it is in SQLite)

Every HUMANIZED-SOP audit lands in the SAME ledger that holds the latency data
(`clean_diallux_SDR/research/surgeon/iter48-rag-truth/ledger.db`, table `sops`) so
results join with `calls`/`rounds`/`rag` rows by `trace_id`.

**Row shape** (one row per call × R1–R5; the sub-rule rides the `rule` field):

```json
{"run_id":"battery-iter48","trace_id":"<ledger trace_id>","sop":"HUMANIZED",
 "category":"R5","scope":"t10","verdict":"FAIL","rule":"R5.5",
 "failure_reason":"in-turn echo duplication: round-1 sentence re-emitted verbatim",
 "failure_assessment":"verbatim repeat = hard flag (PT-48/FIND-8)",
 "evidence":"t10: '<sentence>' emitted twice",
 "source":"tests/llm2llm/json_logs (battery-iter48) + PT-48"}
```

- `category` = one of: `R1 · R2 · R3 · R4 · R5` (the rules above)
- `rule` = the specific sub-rule for R5 hits (`R5.1`…`R5.7`), else `R1`…`R5`
- `verdict` = `PASS` · `PARTIAL` (minor polish) · `FAIL` (hard flag / 3+ kinds) · `OBSERVATION`
- `R5.5` / `R5.6` are **hard flags** (verbatim = template/round-concat, never accidental)
- `scope` = the turn/state the hit landed on (e.g. `t10`); put the quoted sentence in `evidence`

**Import (idempotent — same rows re-import skip):**

```bash
cd /home/julio/projects/clean_diallux_SDR
python3 scripts/live_sql.py --db research/surgeon/iter48-rag-truth/ledger.db \
    sop-import --run <run_id> --sop HUMANIZED --branch <branch> --commit <sha> \
    --file research/surgeon/iterNN-<slug>/<NN>_sop_humanized_rows.json
```

**Query (instant analysis — no md re-reading):**

```bash
cd /home/julio/projects/clean_diallux_SDR
python3 scripts/live_sql.py --db research/surgeon/iter48-rag-truth/ledger.db \
    sops --run battery-iter48 --sop HUMANIZED --verdict FAIL
```

Note: `scripts/live_sql.py` is the **stable ledger SDK on `main`** (stdlib only —
any `python3` works); `--db` is optional from the main repo but shown explicitly.
The engine worktree keeps the live dev copy
(`/tmp/opencode/wt-iter44/scripts/live_sql.py` @ `engine/iter48-rag-truth`).

**Session tracking (all 4 SOPs):** every row is stamped `session = <run_id>@<commit>`
and `sessions` carries the engine `branch`. Run HUMANIZED alongside CALL + SALES
(and the LATENCY plane) under ONE `--run`/session for an aggregated pass. Full
4-SOP protocol: `full_call_analysys.md`.
