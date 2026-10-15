# 10 — REPORT: owner phone-call forensics — the LONG TURN + the LEAK CALCULATOR (first iter114-line phone call)

**Call:** trace `102a0c973152f409c7c615dd56e236ba` · Twilio SID `CA38d51c76714afa90cc3b994772c34a5a` · OREGON phone `:8026` · **2026-10-15 10:29:25 → 10:39:02 UTC (9m37s)**
**Build:** `bb6d216` (`engine/114-speech-auth`) + `SPEAK_FIRST_PIPELINE=1` + `SPEAK_MODEL=gpt-5.6-luna` + caps `MAX_CALL_TURNS=128`/`MAX_CALL_SECONDS=1200` (not hit) · tier OFF · tools `gpt-5.4` · TTS EL Hannah · `speak_diet=True`/`speak_fact_guard=True`
**End mode:** caller hangup (`stream_closed` 10:39:02.877) — **no `end_call`, no goodbye** (caller never re-engaged after "Take your time. What's the email?" 10:37:57 + 65 s silence).
**Evidence:** `01_owner_call_1029_digest.txt` · `02_owner_call_1029_transcript.txt` · `03_latency_findings.md` · `04_leak_math_findings.md` · OR journal `owner_1029_journal.log` · ledger run `owner-1029-bb6d216` (36 rounds / 50 rag spans).
**Session:** zero-code (no engine/deploy/env changes). Owner ear-verdict: **pending** (mechanicals below stand regardless).

## Scorecard

| # | symptom | verdict | evidence (numbers first) |
|---|---|---|---|
| 1 | "one turn taking a looong time" | **PARTIAL — 2 mechanicals + 1 perceived stall** | worst turn 30: **3,353 ms** EOT→first token (2,347 ms of it = M2 await-on-arrival) · turn 60: e2eT **3,423 ms** by design (double tools round; first response 1,435 ms) · **±15 s dead air** 10:37:13→31 after "let me check five PM" (slots defer loop) |
| 2 | "leak calculator a bit off / never a correct estimate" | **FAIL (loop open)** | heard $160,000/wk (engine derive @80%, faithful to caller's own 80%) · corrected 5% attempt guard-cut ×2 (`%10`, `%5`) · **no re-derive ever ran** — dvs stayed 160,000; recovery line promised "run those numbers" twice, numbers never ran |
| 3 | slot booking flow (observed, not asked) | **FAIL** | 4 `query_livecall_slots` calls → **deferred 4/4** (ask-hold S1), **0 executed**, slots prefetch (2+2) never spoken, caller prompted "Are you there?", call ended un-booked |
| 4 | RAG plane (flag-only) | **FAIL on 3 gates** | served 98% PASS · await p50 59.4 PASS · land 60% / degraded 40% / query-empty 24% FAIL — separate plane, report-only |
| 5 | FIND-116C leak race | **HELD** | derive 10:33:36.405 vs speaking gen 10:33:40.0 — figure pre-existed, authorized, no race |

## ① The long turn(s) — full decomposition in `03_latency_findings.md`
- **Turn 30 @ 10:32:57 (Closer) = THE worst: 3.43 s e2eT / 3.58 s e2eR.** Composition: EOT→dispatch **2,347 ms** (M2 await-on-arrival consumed the in-flight round's pending tools future, which settled only at 10:32:53.150) + 783 + 222 + 226. Mouth/tools model speeds were normal (ttft 808/839 ms). Class: **`split M2 … awaited` stalls dispatch uncapped** — same class hit turns 17 (1,226 ms), 48 (1,201), 57 (1,167), 15 (518), 45 (327).
- **Turn 60 @ 10:37:04: e2eT 3,423 ms is a double-round turn, not a stall** (check_current_date followup; first response at 1,435 ms).
- **The perceived long wait = 15 s dead air**: "Got it. Let me check around five PM today for you." (10:37:11) → nothing (no speech/result/error/followup) until the caller prodded at 10:37:28. Rooted in the slots defer loop (below).
- p50/p90: e2eR **1,488/2,355** · e2eT **1,532/2,450** (fallback basis: stt+l2t; the line lacks the audio-out stamp). Per-stream ttft p50: mouth 909 / tools 872. Ledger TTFT p50 906 p90 1,220.

## ② The leak calculator — every figure in `04_leak_math_findings.md`
- Engine truth (single derive, 10:33:36.405): **weekly 160,000 / monthly 692,800** from `{20 calls, 80%, $10,000}` — the **80% was the caller's own words**; formula verified (`floor(w×4.33/100)×100`). The heard "$160,000" was **correct-from-engine**.
- Owner pushback → caller corrects to 5% → mouth's corrected math (20×$10k×5% = $10k/wk — arithmetically right) → **guard cut both attempts** (`number:%10` @10:34:32.666, `number:%5` @10:34:42.229; recovery "Let me run those numbers real quick — one moment." spoken; t39 was otherwise silent). Guard behaved exactly per v1.2 — the caller's word-spoken "five percent" never authorizes a `%5` token, and dvs never ingested 5%.
- **No `extract_leak_inputs` / `derive:leak_math` ever ran again** — dvs stayed 80% → the corrected estimate was never computed. "Never gave me a correct estimate" = literal.
- **NEW finding (candidate PT): corrected-inputs never re-derive** — the missing loop: caller correction → re-ingest → re-derive → speak. NOT a guard loosening job.
- FIND-116C (race): **verified HELD** on this line. iter112-e5 (fabricated math): not this. iter112-e1 (unanchored mouth math): same family, distinct root (un-ingested correction).

## ③ NEW vs KNOWN
| finding | class |
|---|---|
| M2 await-on-arrival uncapped dispatch stall (turn 30 + class) | NEW live evidence (design-known "await-on-arrival, no caps"; first owner-call quantification) |
| **Corrected leak inputs never re-derive** (stale dvs + guard-cut corrections + unfulfilled recovery promise) | **NEW** — candidate PT row |
| ask-hold S1 defer loop on `query_livecall_slots` (4/4 deferred, 0 executed, followup never fired, 15 s dead air, no booking) | **NEW live evidence** — candidate PT row (iter105 design; first phone-call proof of non-convergence) |
| Leak-figure race on the 114 line | FIND-116C — **HELD** |
| End mode: caller hangup, no `end_call` | expected; no PT-94-class empty-`end_call` dead air observed |

## References
- Digest/transcript/findings: `research/surgeon/iter114-phone-call/01…04`, this report `10_report.md`; parsers `/tmp/opencode/owner1029_*.py` (scratch).
- Precedents: `research/surgeon/iter116b-call-analysis/10_report.md` (0949, FIND-116B/C), `plans/PENDING_TASKS.md` PT-94/95/96/97/98.
- Code reads (bb6d216, read-only): `builder.py` `_DEFER_SET`/`_DEFER_S1_MSG` L574-600, ask-hold gate L3586-3607, guard `_speak_guard_cut` L446+, recovery L250, M2 comment L601-606.

## ASK (owner)
1. **GO/NO on PT-99** — leak corrected-inputs re-derive (follow-through on symptom ②).
2. **GO/NO on PT-100** — ask-hold slots defer-loop live fix (symptom ③; without it the phone can't reach a spoken slot list via this path).
3. Optional: ear-verdict on which moment felt "looong" (turn 30 silence vs the 15 s dead air vs the 65 s tail) — tunes the fix priority between M2-cap and slots-loop.
