# 00 — SESSION SUMMARY & SETTLED DECISIONS (iter117 round 2, base = iter 114 split)

Date: 2026-10-15 · Owner (Julio) + agent session · Status: **all amendments A1–A7 settled by owner** ·
Base decision: `bb6d216` (`engine/114-speech-auth` tip — the mouth/brain split, live on the OR phone units).
Purpose: merge-ready record of this session's decisions, to be combined with the round-2 research model's
plan ("amendments applied to the split code") before any code is cut.

> The round-1 deliverable (`TECH_REPORT.md` + `ITERATION_INSTRUCTIONS.md`, in this folder) validated the
> in-house split and proposed 7 amendments (M0 stamps, eager=speak-only, join budgets, empty-speak,
> prompt role tail, parity/fuzz pins, guard replay). This session reviewed each against the REAL 114
> code, the 1029 phone-call forensics, and the owner's product judgment — and settled them as below.
> The owner also REJECTED basing on the iter116 merge-law line ("discard the iter 116 edits, focus on
> what's mostly already working") — 114 is the phone-tested build.

---

## 1. Where we are (facts the plan must assume)

- OR phone + mic units run `bb6d216` + `SPEAK_FIRST_PIPELINE=1` + `SPEAK_MODEL=gpt-5.6-luna` (verified
  `or_deploy.sh --check` 2026-10-15; per ENV-PRODUCTION §5, owner-ordered iter114 phone test).
- The 1029 owner call (10:29–10:39 UTC, trace `102a0c973152f409c7c615dd56e236ba`) = first phone evidence
  on the split. p50 e2eR 1,488 ms / p90 2,355 ms. Its three wounds: (a) M2 await-on-arrival stalls
  dispatch (worst 2,347 ms); (b) corrected leak inputs never re-derive (PT-99); (c) ask-hold slots
  defer loop → 15 s dead air (PT-100).
- The split mechanics verified in code: `_fire_split_tools` (builder.py L1106) background TOOLS stream
  (text discarded; `tool_call_start`→prewarm; final.tool_calls = only source); SPEAK arm
  `astream(speak_messages, [], …)` L3174; speak diet view (`_speak_view` L2427, `_speak_delta` L2415);
  M1 adopt-in-round L3456 / M2 next-turn ingest await L3955 / M3 close cancel L1221; guard
  `_speak_guard_cut` L446 (+ v1.1/v1.2, FIND-114C word-digit auth); per-state mouth `_speak_model_for`
  L2530.
- KEY EVIDENCE for the join discussion (owner asked "was the worst turn a tools-carry-weight case?"): NO.
  The 2,347 ms turn-30 wait consumed a future whose result was `tool_calls=[]` (journal
  `split M2: turn=28 how=awaited — pending future consumed with no tool calls` @10:32:53.150). The one
  value-carrying future that day (`extract_leak_inputs`) was consumed `already_done waited_ms=1`. The
  await blocks on the background BRAIN STREAM finishing, not on tool execution; observed stalls were
  empty/fire-and-forget results (waits 518 / 1,226 / 2,347 ms; 4/36 turns ≥0.5 s).

## 2. Settled decisions (owner-ratified, with exact semantics)

| # | Amendment | Decision | Exact semantics (as settled) |
|---|---|---|---|
| A1 | M0 stamps (first event / first **text** / first **flush** + flush reason + ms-since-first-token; bench aliases `eou_delay`/`llm_ttft`/`tts_ttfb`/`e2e_latency`) | **GO** | Pure observability. On the split path the tool-first gap only exists on the flag-OFF (merged) path — the stamps serve B decomposition on the split + the regression tripwire. |
| A2 | Eager round = speak-only (speculative round fires the SPEAK call only; tools wait for confirmed EOT) | **GO** | Halves speculative cost; kills wasted tool generations on discarded eager turns. |
| A3 | Join budgets (research: M1 2,500 ms + tools-never-block) | **AMENDED BY OWNER — no drop, ever** | (1) Cap the **M2 ingest wait** (≤ ~1 s, flag-configurable): on timeout PROCEED with the build; the future keeps running; when it lands, execute (dvs patch + derive) and deliver its results at the next build via the **existing `held_facts` ENGINE-note channel**. Nothing dropped, dispatch never blind. (2) **NO M1 round-close wait** — a turn task waiting at close blocks the next eager fire (relocates the stall). (3) Scope principle: normal turns never block; **value turns** (money math, calendar slots, booking confirmation) keep authoritative-value discipline (see A7). Evidence: 1029 t30 (empty future, pure waste) + A6-class ops risk of dropping load-bearing results. |
| A4 | Empty-speak pin + never-silent continuation | **PARKED (owner will assess)** | Owner's restrained shape, if ever built: speak only when silence ≥ X ms AND no tools in flight — never on "tool-only turns" ("otherwise we will have a very loud mouth"). Owner note: the prompt was only disobeyed when we forced speech-before-tools; this is a nice-to-have, review later. |
| A5 | Speak prompt role tail ("spoken reply only") | **PARKED — keep exploring** | NOT a prompt split: the payload split already ships (`speak_diet`+`speak_card`, phone-tested). A5 = ONE added line in the already-custom speak tail. Cache analysis (owner question): the tail sits AFTER the history → the cached prefix bytes are untouched; no collision with prompt caching; the latency lever (smaller mouth payload) is already live. Candidate future quality/battery item. |
| A6 | History-parity + token-order pins | **GO** | Offline suite tests only (no call behavior): (1) same mock streams, flag OFF vs ON → history bytes sha256-identical (cache/wire-format protection, iter43 class); (2) evil mocked streams (tools-first/interleaved/tool-only) → first-flush timing depends ONLY on the speak stream (tripwire: re-merging the streams fails CI before any phone can stall). |
| A7 | Guard replay + value authority | **SETTLED as unified value-turn policy** | Owner idea assessed: same-round "swap mouth output for brain output" is impossible by construction (the brain stream completes BEFORE tools execute; results feed the NEXT round — no computed value exists in either context during the round). What stands: at value turns the engine owns the value — guard cuts unauthorized figures (live, v1.2); the correct value must be re-derived and spoken via continuation/next round. **PT-99 (corrected inputs never re-derive) is the missing half** (1029 proved it: corrected 5% attempt guard-cut twice, dvs stayed 80%, recovery line promised numbers that never ran). Owner: "my idea … is the exact 'ignore M2' we are discussing … we just scope it to turns that matter." → One policy: value turns = authoritative-value discipline; everything else = never block. |

## 3. Base & discard decisions

- **Base = `bb6d216`** (owner: "focus on what's mostly already working"; port/dry-run receipts: the union
  alternative `08321d0 ← bb6d216` merges cleanly with 2 conflicts, but was REJECTED as base).
- **Discard (do not port):** the iter115 merge-law prompt block (`general_prompt.md` top #CRITICAL) + its
  `llm.json` mirror — dead/harmful on the split path (produced F1/F2 on the merged line; redundant here).
- **Carry-list (each cherry-picks cleanly onto bb6d216, zero conflicts — verified):** temp guard
  (`08321d0` llm.py delta — RECOMMENDED, makes temperature 0.3 actually reach gpt-5.4; owner to confirm);
  pt15 EL voice_settings (`4efaa38` — RECOMMENDED, env globals currently silently dropped with
  `TTS_DELIVERY_PROFILES=false`); pt16 transition-hold OFF (`c932fcf` — owner call; 114 was phone-tested
  with the filler ON); pt14 warm-kwargs (`13546bc` — skip until tier=fast returns).
- One branch, sequenced commits, instrumentation first; LAW 0 (ASK before merge); SWITCH LAW #4 (code
  defaults = tested-on values); env edits owner-only.

## 4. Open questions for the round-2 research plan (what the plan must answer)

1. A3 exact mechanics: cap value, late-execute wiring, held_facts delivery, interaction with eager epochs
   and turn cancellation; how to scope "value turns" (state? pending tool names? both?).
2. A7 continuation design vs PT-99's re-derive loop (one mechanism or two?) + guard `false_cut_rate` /
   `guard_trip_rate` pins + the adversarial replay set (FIND-116C $3,750 transcript; word-form fixtures).
3. Speak-lane cache: research said `[LITE_NOOP_TOOL]` is load-bearing; the code sends `tools=[]`; repo's
   own iter110b probe says the FIND-5 rule is stale (bare no-tools cached 3,840/4,426 on its own
   lineage). Plan should specify the measurement (per-lane `cache_read`) + the decision rule.
4. Brain output cap (bounded `max_completion_tokens` on the tools lane — observed wasted generations:
   34–46 tokens building discarded text, `tool_calls=[]`).
5. A2 plumbing: how the round knows it is speculative (session currently passes no marker).
6. M0 stamp exactness: first-text vs first-event on a no-tools speak stream; flush-reason taxonomy.

## 5. Evidence index

- Plan of record: `plans/plan_v5_iter117_deep_research_fixes.md` (main).
- Round-1 deliverable: `research/surgeon/iter117-deep-research/{TECH_REPORT.md,ITERATION_INSTRUCTIONS.md}`.
- T1 mapping + base eval: `research/surgeon/iter117-deep-research/01_fix_mapping.md`.
- 0949 (merged line disease): `research/surgeon/iter116b-call-analysis/12_latency_forensics_0949.md`.
- 1029 (split line phone forensics): `research/surgeon/iter114-phone-call/{10_report.md,03_latency_findings.md,04_leak_math_findings.md,owner_1029_journal.log}`.
- This session's chat decisions → this file. Round-2 snapshot (public): `voice-agent-iter114-snapshot` (bb6d216 tree + ARCHITECTURE_SNAPSHOT.md + context/).
