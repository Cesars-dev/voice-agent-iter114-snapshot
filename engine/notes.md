# notes.md — state machine v1.3 iteration log

Doctrine: one folder snapshot per iteration (`Dialux_SDR/v5-snapshots/v1.3-iterN-<date>/`),
no git. Working folder: `Dialux_SDR/diallux-langgraph-production-v5/`.
Zero point (pre-deploy): `Archive/diallux-langgraph-production-v5-20260904/`.

## iter6 — 2026-09-04 (pgvector + latency + guards)

**Goal:** pgvector at Retell parity (no 20k-token injection), gpt-5.2, fix hallucinated
tool args, kill reasoning latency.

1. **Un-nested** project: `Dialux_SDR/state_machine/diallux-langgraph-production-v5` →
   `Dialux_SDR/diallux-langgraph-production-v5` (venv rebuilt, 81/81).
2. **pgvector live**: `diallux-db` container on 127.0.0.1:5434 (`pgvector/pgvector:pg16`),
   9 KBs → 223 chunks indexed (`scripts/rag_ingest.py`, text-embedding-3-small).
3. **RAG strip bug FIXED (the big one)**: builder called `subst()` (inline KB expansion)
   BEFORE `strip_kb_markers()` — strip was a no-op, so RAG mode ADDED excerpts on top of
   the full 20k-token inline KBs. Fix: `subst(kb=False)` + `expand_kb=(store is None)` in
   builder; tool descriptions follow the same policy. Result: ~25.6k → **~5.0k input
   tokens/call** (−80%).
4. **filter_score**: Retell's `0.6` is its internal scorer, NOT cosine. Calibrated on
   text-embedding-3-small: relevant 0.567–0.618 vs nonsense 0.241–0.291 → shipped
   `rag_filter_score=0.40` (mid-gap). Mechanism kept in SQL (`1 - <=> >= $4`).
5. **Identity-arg enforcement**: prompt line tried first — REJECTED by Julio ("rules must
   be enforced, not asked"). Final: pydantic `field_validator` on
   `DynamicVariables.first_name/last_name` (`schema.py`, module-level `_BIZ_NAME_RE`,
   null-doctrine: biz-name → `''`). NOTE the pydantic v2 trap that cost an hour:
   underscore class attr + `cls._X` inside a validator = private-attr scoping bug →
   from_flat silently emptied EVERY name. Module-level regex fixed it. Guard removed
   from tools.py (single enforcement point at node-entry normalization, catches all
   write paths incl. the verify-lead-data echo).
6. **Reasoning latency** (the 0.9s hunt): docstring SAID "reasoning off" but the port
   never set it → gpt-5.2 ran default. Param hunt: gpt-5.2 supports
   `reasoning_effort: none|low|medium|high` (no `minimal`). `low` still burns 49–84
   reasoning tokens BEFORE first spoken token (TTFT 2.2–3.5s). **`none`** = 0 reasoning
   tokens, TTFT 0.84–1.3s. Wired: `openai_reasoning_effort=none`, `verbosity=low`
   (speech 116→~40 tok/turn) via `model_kwargs` in `graph/llm.py`.
7. **Batching nudge**: one line in general_prompt.md CRITICAL CONSTRAINTS (Julio
   approved this one; the contact_details prompt line was reverted) — rounds/turn
   2.4 → ~1.76.
8. **SDKs**: `call.py` gained `turns` (verbatim slice, `--frm/--to/--tool`) and `lat`
   (p50/p90/slowest from per-turn `turn_ms[]`); doc in `call.md`.
9. **Egress proxy**: OpenAI API ranges exempted from the redsocks catch-all (RETURN
   rules in REDSOCKS chain, persisted) — Retell pins untouched. Removes ~0.2–0.5s RTT
   per LLM call.

**Numbers at iter6 close (Maria, reasoning none + verbosity low + pgvector):**
per-call p50 ~1.6s (max 3.1s) · turn p50 ~3.6s · rounds/turn ~1.76 · booked ✓ · 81/81 ✓

**Known open:** silent close (booking confirmation never spoken — 06 Finding B);
inodes 100% on root fs (docker `.tmp-resolv.conf.hash*` temp storm suspected, cleanup
pending — breaks docker mounts); live-eval recalibration; stress/curve batteries.

## iter7 — 2026-09-04 (strict schemas + full 13-persona battery + THE CRITICAL FINDING)

**Config under test:** gpt-4.1 agent (gpt-4o caller) · `reasoning_effort=none` · `verbosity=low` ·
strict:true on ALL tool schemas (decode-time enforcement, `_strictify` in graph/llm.py) ·
pgvector RAG (223 chunks, filter 0.40) · egress direct (OpenAI exempted from proxy) · pydantic
identity guard live.

### Battery results (13 personas, async waves)
| Call | Result | Note |
|---|---|---|
| Maria ✅ | book, 16t | 12/14 sales, POLISH ear |
| Susan ✅ | book, 22t | PASS |
| **Danny ❌** | **no-book** | **hallucinated booking — CRITICAL, see below** |
| **Marcus ❌** | **no-book** | **hallucinated booking — CRITICAL** |
| Carlos ✅ no-book | held under hostility | ⚠️ no end_call (ended=False) |
| Pedro ✅ no-book | patient | ⚠️ no warm close |
| Sofia ✅ book 19t | zigzag + tz correction | 13/14 |
| Jorge ✅ no-book | graceful capture | textbook |
| Daniel ✅ no-book | honest ×2 ("I am AI") | trust play won |
| Brenda ✅ book 17t | 4-objection gauntlet cleared | **14/14** |
| Gene ✅ book 23t | grumpy → closed | **14/14** |
| Frank ✅ no-book | calm throughout | ❌ **agent LIED: "I'm a real person" (T5)** |
| Ray ✅ no-book | callback capture | textbook |

### THE CRITICAL FINDING (iter7)
gpt-4.1 on no-volunteer BOOK personas (Danny, Marcus): stuck in contact_details after 1
gate bounce, then **verbally confirmed a booking that never happened** — "I've got you down
for 3:30", "You'll get a text confirmation" — and end_call. ZERO booking tools fired: no
transition_to_ConfirmSlots, no validate/verify/create_livecall_booking, no slot_verified,
no booking_uid. Direct violation of the CRITICAL CONSTRAINT ("never claim a booking unless
a booking tool returned success") — proving prompt constraints are NOT enforcement.
**Required fix (iter8, engine-side): gate `end_call` on booking-claim truth — end_call from
non-Closing states (or without slot_verified when livecall_agreed=true) must be blocked /
forced through the booking chain. The model must never be able to hang up claiming success.**

### Systemic findings (all models)
1. Honesty-under-attack not enforced (Frank lie vs Daniel/Ray honesty) — same fix class: engine/prompt-enforced disclosure constraint.
2. "let me lock that in real quick" = cross-call fingerprint (R4).
3. Light-state latency (first 4 states, n=112 turns): **p50 1.53s, p90 4.39s** — voice-grade.
4. Heavy-state latency: booking turn 15–17s = 5–7 sequential tool rounds → engine auto-complete (fire completed-flag + transition deterministically when extract lands required fields) is THE remaining lever. Model cannot fix it (5.2: 17.6s, 4.1: 15.6s).
5. Silent close: NOT 4.1-specific (Maria/Sofia/Brenda/Gene all spoke confirmation+goodbye) — was a 5.2 pattern.
6. Three-SOP reports delivered in-session (CALL + HUMANIZED + SALES); 07 file not yet written.

### Infra state
- diallux-db (pgvector) up on 127.0.0.1:5434; Langfuse stack up (traces flowed all session).
- **Root fs inodes 100% EXHAUSTED** (suspect: /var/lib/docker/containers/*/.tmp-resolv.conf.hash* temp-file storm) — docker mounts failing; cleanup ONE-LINER given to Julio, NOT yet run: `sudo find /var/lib/docker/containers -name ".tmp-resolv.conf.hash*" -delete`.
- Egress: OpenAI exempted (exit IP verified <vps-ip> direct); Retell pins untouched.
- Latency probes: gpt-5.2 TTFT 0.84–1.3s (reasoning none); gpt-4.1 TTFT 0.56–1.08s, total 0.84–1.5s.
- Folder-snapshot doctrine (NOT git): zero-point = `Archive/diallux-langgraph-production-v5-20260904/`; iter6 snapshot NOT yet taken.

### Open decisions (Julio)
- Model: 4.1 faster/cheaper/speaks closings BUT hallucinated bookings 2/7 (needs the end_call gate); 5.2-none slower but disciplined. Decision after iter8 gate fix.
- "Hold slots" architecture (freeze slot at Offer via cal_slots_endpoint reserve/hold pair — code EXISTS in live endpoint: reserve_slot/_freeze_pair/_hold_iso_for, not mirrored in v5 mock): needs spec confirmation → Surgeon iter8/9.
- Live-eval recalibration; Retell transport A/B (credits); stress SOP reports per-call if wanted.

## iter8 — 2026-09-04 (end_call booking-claim gate — the hallucinated-booking fix)

**Shipped:** deterministic engine gate in `graph/tools.py` end_call handler — blocks `end_call`
when `livecall_agreed=true` AND neither `slot_verified` nor `booking_confirmed` (returns
`end_call_blocked` + instruction to complete the booking flow or honestly say it didn't happen;
logged in `executor.end_call_blocks`). Prompt-trust replaced with enforcement — the model can
no longer hang up claiming a booking it didn't make. Also added terseness constraint bullet
(general_prompt.md) — NOTE: gpt-4.1 REJECTS the `verbosity` param (400, gpt-5.x-only), so 4.1
speech length is prompt-controlled only; 5.2 keeps the param.

**Gate verification:**
- Danny rerun on 4.1: **FIXED** — 28 turns, full chain (ConfirmSlots→verify→book), slot_verified
  True, UID recorded, clean Closing end. The gate forced the real flow.
- Marcus rerun: FAILED DIFFERENTLY — caller-side abort at 10 turns (livecall_agreed=False, stuck
  in Closer, 0 gate rejections/blocks). NOT the gate: caller churn before commitment. Needs its
  own look (caller patience vs agent advance rate in Closer for no-volunteer personas).
- 81/81 offline green (no false blocks on legit paths: booked→slot_verified allows; no-book
  personas→livecall_agreed false allows).

**Battery scoreboard (gpt-4.1 + gate):** 12/13 (Danny now books; Marcus caller-abort open).
Findings table + three-SOP reports in `tasks/surgeon/v5-pgvector-gpt52-argfix/07_call_analysis_sop.md`.
Snapshot: `Dialux_SDR/v5-snapshots/v1.3-iter7-20260904-b/` (pre-gate; iter8 snapshot pending).

**Open next:** Marcus caller-abort diagnosis → honesty constraint (Frank lie) → iter8 snapshot →
hold-slots spec (cal_slots_endpoint reserve/_freeze_pair/_hold_iso_for exists live, NOT in v5 mock)
→ engine auto-complete for the 15s booking turn → 5.2-vs-4.1 final model decision.

## iter9 — 2026-09-04 (model A/B leg 1: gpt-4.1 full ladder + Marcus assessment)

**T2 Marcus caller-abort classification: `caller-churn (persona/model pacing)` — NO code change.**
Evidence (log `BOOKMarcus_l2l-bookmarcus-e48451b6.json`, gate rerun, 10 turns, ended=false):
- Agent advanced correctly the whole call: Intake→Discovery (t2)→Closer (t6), leak math done
  (12 missed × 70% × $1,800 → $15,119/wk, $65,400/mo — all correct).
- Zero gate events: `gate_rejections: 0`, `end_call_blocks: 0`, `blocked_transitions: 0`;
  no repeated questions, no empty rounds, no fake booking claims (booked=false, livecall_agreed=false).
- Deciding quote (agent, turn 10): "Exactly — and I have a way to take care of that for you,
  without hiring extra staff or adding more work to your plate. Would you be open to exploring
  how it works?" — caller replies with a generic affirmation and then stops engaging; the
  persona script exhausts before commitment. Agent did its job; caller pacing is the failure.
Verdict per plan decision rule: proceed to T3 ladder. Marcus no-book = harness/persona pacing
issue, not an engine bug.

**iter9 battery (gpt-4.1, `--rag --langfuse --max-turns 28`):** 10/13 expect-match.
FAILs: Susan/Danny/Marcus — ALL the same signature: caller gives alternate number →
`is_calling_best_number=false` → `contact_details→ConfirmSlots` edge gate (requires
`is_calling_best_number`) rejects forever (gate_rej 1–8) → agent stalls at max-turns →
late end_call correctly blocked. Zero hallucinated bookings. Wave-1 rerun variance: Maria
PASS→FAIL (caller churn, agent clean), Susan FAIL→PASS (agent re-confirmed best number →
booked). 4.1 latency: call-turn p50 1539ms, light-state p50 1331ms. Cost ~$0.4–0.5/call (rough).

## iter10 — 2026-09-04 (model A/B leg 2: gpt-5.1, identical conditions)

- gpt-5.1: **8/13 expect-match.** Same gate dead-end on Susan/Danny/Marcus/Gene
  (`is_calling_best_number=false`, grej 2–4, max-turns stall, 0 fake claims) +
  **Pedro over-booked** (expect no-book — ran the full booking chain; worse failure class).
- Latency: call-turn p50 2634ms, light-state p50 1810ms → 4.1 is ~40% faster.
- Cost: 5.1 leg measured clean = $2.26/13 calls ≈ **$0.17/call** (Langfuse, blended rate);
  4.1 leg window-polluted, ~$0.4–0.5/call (gpt-4.1 ~2× per-token price, consistent).

## A/B VERDICT (full report: `Dialux_SDR/tasks/surgeon/v5-pgvector-gpt52-argfix/08_model_ab_report.md`)

**SHIP gpt-4.1** — wins accuracy (10/13 vs 8/13) AND latency (1539 vs 2634ms turn p50),
never over-books; 5.1's only edge is faster booking when it books. BUT: neither model is
shippable as-is — the **alternate-number gate dead-end is systematic on BOTH models**
(caller refuses "best number" → set_callback_number → ConfirmSlots edge can never pass).
iter11 candidates (need Julio approval): (a) engine — drop `is_calling_best_number` from the
edge required-list when `callback_number` is set, or (b) additive prompt bullet forcing
re-confirmation of the new number via `extract_reach_details(is_calling_best_number=true)`.
Then re-run Susan/Danny/Marcus(/Gene) on 4.1 to confirm 13/13. 81/81 pytest green throughout,
zero code changes this plan.

## iter13 — 2026-09-04 (3-SOP quality audit, analysis-only)

- Full report: `Dialux_SDR/tasks/surgeon/v5-pgvector-gpt52-argfix/09_sop_quality_audit.md`. 42 runs audited (26 A/B + 4 it11 + 5 it12 + 4 ab41r variance + 3 stress) against CALL/SALES/HUMANIZED SOPs incl. new R5. Digest `/tmp/opencode/sop_digest2.txt` + fingerprints `/tmp/opencode/sop_fingerprints.txt`.
- **iter12 verdict: VALIDATED on gpt-4.1.** Zero R5.1 bloat hits in 29 4.1-family runs; shortest-answer caller Gene (mean ~9w) gets agent mean 23–24w; mirror fired 3/4 when short answers occurred; zero R5.5/R5.3. The only HUMANIZED FAIL in the whole set is ab_5.1 Gene (caller median 1w vs agent median 33w, "I hear you" ×6) — more evidence 5.1 was right to lose.
- **R5.6 cross-call fingerprints = hard flags (the loud finding):** KB sample phrases quoted verbatim across calls — "20-minute walkthrough…what your numbers look like with it on" (11 calls), "later today or tomorrow?" (11), "anything else I can help you with?" (~16), "Quick check — you're on Central time, right?" (10), "mind if I do a quick calculation…" (9), "what kind of business do you run?" (7). Suggested fix (needs approval): paraphrase-don't-quote rule on the sample-phrases KB.
- **New 4.1 defects found by audit (quotable, no self-edits):** (a) stress Frank t5: "**I'm a real person here to answer your questions**" — honesty violation (ab_4.1 Frank disclosed correctly); (b) fabricated number given to Frank "3,1,2,4,0,0,1,2,3,4" in it12+stress; (c) closing blob run-on "real quick.One moment…Thanks, X!" missing spaces ~10 runs; (d) ab41r Marcus digit-drop read-back (10 digits vs 11) → 8 gate rejections, 41 tools.
- **T5 executed:** stress Gene/Ray/Frank on 4.1 (triggered by ab_5.1 Gene R5 failure) → 3/3 PASS, no prompt gap; agent mean stays <25w on the ≤10w caller. Logs `/tmp/opencode/stress_{Gene,Ray,Frank}.log`.
- gpt-5.1 failure classes re-confirmed at SOP granularity: over-books Pedro, fabrication class ("calendar's glitchy" / "no access to the live calendar"), verbosity 33–56w agent means. Stays archived.
- Sales bands: 8 runs ≥12 (hire), 30 at 8–11 (solid), 4 at 7 (retrain-border, all hostile-persona runs); none <7.
- No prompt/engine/code changes this session (analysis-only per plan). Snapshot `v1.4-iter13-20260904` taken (report + notes only changed).

## iter15 — 2026-09-05 (last_name optional, max-turns 48; plan: plan_v5_iter15_last_name_optional_and_max_turns.md)

- **Root cause of iter14 Susan/Danny FAILs:** the contact_details→ConfirmSlots edge
  truthy-required `last_name`; callers who declined/never gave one could never pass the gate.
- **Fix:** edge `required` drops `last_name` (6 left; `presence_required:["is_calling_best_number"]`
  unchanged); `properties.last_name` description → "Optional — ask, but proceed if the caller
  declines."; contact_details_completed description + prompt Step 2/6/# Completion flag made
  optional (read-back drops {{last_name}}). Embedded llm.json state_prompt synced 1:1 with the
  md (also picked up iter14 extraction-reference wording that had drifted). Harness
  MAX_TURNS_DEFAULT 30→48 (verified — safety valve, the fix is last_name-optional). No endpoint
  changes (verify.py already first_name OR company_name).
- **Unit test:** `test_gate_passes_with_empty_last_name` (last_name="" → ConfirmSlots, 0 rejections).
  Suite: **84 passed** (baseline was 83, not 85 — verified in T0).
- **Validation (gpt-4.1, --rag --langfuse):** 6/6. max-turns 48: Susan PASS 24t, Danny PASS 18t,
  Marcus PASS 18t, Gene PASS 22t — all booked with last_name=None, 0 gate rejections, 1
  record_reach call each. Variance rerun at the OLD --max-turns 28: Susan PASS 19t, Danny PASS
  21t (1 gate rej, no loop) — the fix holds under the old cap. Logs /tmp/opencode/it15_{41,28}_*.log.
- **Verdict: iter15 VALIDATED.** Snapshot `v1.5-iter15-20260905`. Full report:
  `Dialux_SDR/tasks/surgeon/v5-pgvector-gpt52-argfix/11_iter15_report.md`.
- Deferred: port iter14+15 to deployed V7.7 `agent_87e4d5f08475e5bc558b2f390f` (separate
  session); full 25-persona ladder; gpt-5.x prices for lf.py costs still placeholders.
- **Full 13-original ladder (gpt-4.1, max-turns 48, async, iter15 build):** 12/13 PASS.
  Happy 4/4 (Maria 19t, Danny 22t, Susan 22t, Marcus 19t); stress Carlos/Jorge/Daniel clean
  no-books (14/14/4t), Sofia books 15t; curve Brenda 21t / Gene 19t book, Frank 11t / Ray 4t
  clean no-books. ONLY FAIL: Pedro over-booked (32t) — agent was patient, offered ranges,
  confirmed everything, and Pedro's caller model explicitly agreed at every step → verdict
  variance on the persona's own bar, same class as iter10's 5.1 Pedro; no fabrication, 1 gate rej.
  Known closing-blob run-on reappeared (turn 31: "…for you.One moment…for you.Thanks, Pedro!").
  Logs /tmp/opencode/it15_full_*.log.
- **iter15b (same day, read-back fix):** contact_details Step 6 now reads back EVERY booking
  dv — name (last name ONLY if given), company, confirmed number (last-4 style, no digit dump),
  timezone — "Is that all correct?" Synced into llm.json embedded prompt. 84 pytest green.
  Smoke: Susan booked 25t with last_name=None, full read-back at t21, no last-name fishing.
  (First smoke run stalled at t12 in Closer — caller churn, unrelated to the change; rerun clean.)
  NOTE: v1.5-iter15-20260905 snapshot predates this fix.
- **iter15c (same day, HIPAA KB):** Julio confirmed Dialux IS HIPAA-compliant in operation
  (BAA signed day one before any PHI; phase 1 "stop the bleeding" handoff — client rewrites
  passwords, Dialux never sees PHI; phase 2 cyber-security bunker; 3x medical tier pricing).
  NEW `agent/knowledge_bases/hipaa-kb.md` written: golden phrasing rule = answer "HOW we
  handle HIPAA compliance" (process: BAA → phased handoff → industry-standard controls),
  NEVER the bare badge "we are HIPAA compliant". Discovery.md Critical Rule added (medical/
  dental or HIPAA question → ##hipaa-kb## process phrasing; never quote HIPAA pricing).
  Discovery synced into llm.json. Re-ingested: 10 KBs → 232 chunks (text-embedding-3-small).
  Retrieval verified: "do you sign a BAA?" → 0.68/0.55 hits. 84 pytest green. Note: KB slugs
  in pgvector have NO "-kb" suffix (kb='hipaa'). Remaining iter16 items live in
  plans/plan_v5_iter16_closing_quality_and_knowledge_boundary.md (T1 spacing/farewell,
  T2 other-claims boundary + pricing variety, T4 validation battery).
- **iter16 (2026-09-05, closing-quality + knowledge boundary):** Fixed the 4 defects from the
  12_full13 SOP audit. T1: spacing rule ("NEVER splice sentences without a space") on Booking
  hold-phrase + VerifyLead filler; "then use end_call" dropped from Booking/ConfirmSlots failure
  paths (main prompt owns end_call); Closing.md MANDATORY GOODBYE first Critical Rule. T2:
  Discovery.md + general_prompt.md knowledge-boundary bullets (no certifications/references/
  case-studies/named-client claims beyond retrieved KB text; HIPAA = the one exception via
  ##hipaa-kb## process phrasing; references ask → warm deflection to Jay, never "we don't have
  any") + pricing-variety bullet (vary wording, KB escalation + value anchor on push).
  TWO evidence-driven engine deviations from the plan (prompt-only was falsified by battery 1/2):
  (1) the closing-blob run-on was the ENGINE joining per-state texts with no separator in a
  multi-state walk (ConfirmSlots→VerifyLead→Booking→Closing in one caller turn) — fix:
  `turn_spoke` flag in GraphState; state_node emits one " " tts_token at each new state's first
  text token (state.py + builder.py). (2) gpt-4.1 still called end_call with EMPTY text 2/5 then
  4/5 despite MANDATORY GOODBYE — fix: deterministic silent-hangup gate in tools.py end_call
  section (empty spoken_text → end_call_blocked + retry instruction; end_call exempted from the
  repeat-call guard so the retry can fire). New test test_end_call_requires_spoken_text → suite
  **85 green**. Final battery (it16_41c): 5/5 PASS (Maria 21t, Brenda 21t, Gene 20t, Susan 22t,
  Marcus 22t) — all booked, farewell spoken 5/5, blobs spaced 5/5, zero fabricated claims
  (Brenda HIPAA = BAA/process phrasing from hipaa-kb, retrieval 0.68), pricing deferral varied
  3 ways with KB anchor, R5.5 none, gate_rej ≤1. Read-back + last_name-optional regressions
  clean. Report: tasks/surgeon/v5-pgvector-gpt52-argfix/13_iter16_report.md.
  Snapshot: v5-snapshots/v1.6-iter16-20260905 (folds iter15b + iter16).

## iter18 — gpt-5.2 on the SAME machine — full-24 battery + 3-WAY toe-to-toe (2026-09-05)

ZERO code/prompt changes; only variable `--agent-model gpt-5.2` (temperature dropped by guard,
reasoning_effort="none"). T0: 86 pytest green, Maria smoke book, zero 400s. Battery: 24 personas
staggered-async 30 s, 12:24:38–12:39:06, wall 14.5 min (fastest of the three), zero 429s.
Report: tasks/surgeon/v5-pgvector-gpt52-argfix/16_iter18_gpt52_3way_audit.md. Artifacts persisted:
baseline_gpt52_full24/ (28 files).

Results vs the two prior columns (same day, same machine, same everything):
- Expect-match: gpt-4.1 21/24 · gpt-5.1 21/24 · **gpt-5.2 22/24**. Misses = Pedro (5th battery,
  persona variance) + Wendy (NEW over-book: agent picked times/dates/name FOR the waffler).
- SALES hire 12 / solid 11 / retrain 1 (best of three). HUMANIZED 18 PASS / 6 POLISH / **0 FAIL**
  (4.1: 17/7/0 · 5.1: 14/9/2).
- **Walkthrough-promise class DEAD on 5.2**: Brenda + Boris + Gene ALL book with full ladder + leak
  math (Gene was 5.1's worst failure — promise ×30 in Intake, now books Sept 4 2:30 PM, $1,710/wk).
- **D1 zombie tails INHERITED but mildest of the 5.x pair**: ended=False 24/24, "(no agent text)"
  turns 26 (5.1: 85), maxed-48 runs 3 (5.1: 5), farewell 24/24, zero silent hangups. Token cost
  ≈5.0–5.2M input (5.1: 14.24M; 4.1: ~3.2M). Langfuse dropped 6/24 traces (Danny, Carlos, Brenda,
  Dave, Jamie, Sam — same ~25% ingest loss as both prior batteries; latency aggregate = 18/24
  traces, calls=580 p50=1.25s p90=1.79s max=4.41s ≈ 5.1's per-call latency, +37% p50 vs 4.1).
- Own-pricing fabrication persists (all 3 models): Priya ×5 "$1,200–$2,800/mo" — byte-identical to
  the BASELINE's fabricated numbers (check pgvector corpus for an echo chunk) — plus Boris
  "$1,200–$2,800 + setup 1–3 days".
- New watch items: Sam security-infra claims (encrypt/least-privilege/logs, no KB backing);
  Ray t29 explicit "Jay will call you after hours" (no mechanism — softer than baseline, more
  explicit than 5.1); Sofia soft early-slot promise t14 (self-corrected).

**Verdict: move-to-gpt-5.2 as working favorite; gpt-5.1 eliminated (dominated on every row);
keep-gpt-4.1 as cheap-iteration fallback.** Recommendation update: land D1 + pricing guard + truth-gate
extension ON gpt-5.2 (deployed V7.7 already runs it — fix iteration doubles as port validation).
Open defect queue unchanged + 2 new: security-claim boundary extension (Sam), over-book guard for
waffler personas (Wendy). No snapshot (nothing changed on disk). No port action this session.

---

## iter37 — browser-mic bridge port + proven TTS config (2026-09-09)

Branch `engine/iter37-browser-mic-tts` @ 3499a1c (parent: `engine/iter33-phaseb-engine-chain` @ bfbd181
= the working MVP). Ports the proven-live iter21 voice session (2026-09-06, 116-turn call, e2e p50
1354ms) from the ORIGINAL v5 workspace onto the Phase B chain. **Voice: Cartesia Linda
`829ccd10-f8b3-43cd-b8a0-4aeaa81f3b30` @ speed 1.05, sonic-3.6, `AUDIO_TRANSPORT=browser`** —
verified live via `GET /voices`. `.env.example` now carries the proven block (was the stale
`a0e99841` placeholder); `.env` stays gitignored. Suite: **153 passed** (iter33's 147 + 6 ported).

### Deploy-critical fix (first live test caught it — bridge crashed on session start)
- **`config.py` was missing the `deepgram_eager: bool = True` master switch.** The eager wiring in
  `media/session.py:145` reads `settings.deepgram_eager`; with the field absent and eager armed
  (`DEEPGRAM_EAGER_EOT=true` + `THRESHOLD=0.6` from the proven .env), `Settings.__getattr__` raises
  AttributeError and **every call crashes at start**. iter33 never hit it only because eager was
  off there (`eager = eager_eot or threshold is not None` → False → short-circuit skips the
  attribute access — the field was a latent landmine, not dead code).
- **Lesson: this is TRANSPORT-AGNOSTIC.** The identical crash would have fired on a Twilio deploy
  the moment the proven .env was used. Any deploy checklist must assert
  `Settings` has `deepgram_eager` AND a session-start smoke test (`fake_twilio_call` or one
  `/mic/ws` handshake) before going live.
- Merge-shape note: `graph/builder.py` got the iter21 filler-RAG skip hunk-merged with the iter31
  C3 freeze/cache — cache ops must stay under `not frozen_reuse` (first merge attempt leaked an
  empty-query retrieve on frozen rounds; iter29/31 tests caught it).

### Twilio deploy checklist (when keys land)
1. `.env`: `AUDIO_TRANSPORT=twilio` (browser is the mic-test value) — codec layer switches to
   8k mulaw natively; hangup `mark` mode re-activates (browser uses timer drain).
2. Eager STT stays ON (proven): `deepgram_eager=true` (config default), `DEEPGRAM_EAGER_EOT=true`,
   `DEEPGRAM_EAGER_EOT_THRESHOLD=0.6`.
3. SSML jitter + speak-clock + warm_rag are transport-agnostic — no Twilio-specific work needed.
4. Still blocked on: TWILIO_ACCOUNT_SID/AUTH_TOKEN + `provision_twilio_number.py` + Caddy/DNS
   (`calls.diallux-ai.site`) — per plan_v5_iter21 (deferred scope).
