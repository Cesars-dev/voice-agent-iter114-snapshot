# 03 — LATENCY FINDINGS: the slow turn(s) — owner call 1029 (bb6d216 split line)

Call: trace `102a0c973152f409c7c615dd56e236ba` · SID `CA38d51c76714afa90cc3b994772c34a5a` · 10:29:25–10:39:02 UTC
Build: `bb6d216` + SPEAK_FIRST_PIPELINE=1 + SPEAK_MODEL=gpt-5.6-luna + caps 128/1200 (not hit). End: caller hangup (`stream_closed`), no `end_call`.
Evidence: `01_owner_call_1029_digest.txt` (per-turn table), `02_owner_call_1029_transcript.txt`, OR journal `owner_1029_journal.log`, trace spans.

**VERDICT (mechanical, ear-verdict pending owner):**
1. **THE single worst turn = turn 30 @ 10:32:57 (Closer)** — caller waited **3.35 s EOT→first token / 3.58 s e2eR / 3.43 s e2eT**. Root leg: **EOT→dispatch = 2,347 ms**, caused by the split line's **M2 await-on-arrival** consuming the pending tools future of the in-flight round (settled 10:32:53.150; the new turn's dispatch could not start until it did). NOT mouth/tools model latency (mouth ttft 808 ms, tools 839 ms are normal).
2. **Second worst = turn 60 @ 10:37:04 (ConfirmSlots) — e2eT 3,423 ms but first response at 1,435 ms**: a **double-round tools-followup turn** (check_current_date → spoken round 2 + query_livecall_slots). Perceived as two back-to-back agent lines, not dead air.
3. **The biggest PERCEIVED stall is not a turn: 10:37:13→10:37:31 (±15 s dead air)** after "Got it. Let me check around five PM today for you." — the slots followup never fired; the caller had to prompt "Okay. Are you there?". Mechanism = the ask-hold defer loop (all `query_livecall_slots` calls deferred 4/4; no tool ever executed; no followup line for turn 60). See "Slots dead-air" below.
4. The **M2-await stall is a CLASS**, not a one-off: every `split M2 … how=awaited` turn paid ~1.2 s (turns 48/57/17) and the uncapped turn-30 case paid 2.3 s. Correlation: awaited waits land almost 1:1 on that turn's `stt_eot_to_llm_dispatch_ms`.

## Per-turn table (36 reports; e2eR = fallback stt+l2t — this build has no audio-out stamp)
```
turn time     st  stt  disp  dEv  ev1  l2t   e2eR  e2eT  note
   3 10:29:42 In 1305   31 1216   58  309  1614  1538
   5 10:29:56 In 2131   87 1812  232  211  2342  2254  first user turn
   7 10:30:11 In 1440   88 1202  149  186  1626  1569
  12 10:30:27 In 1113   91  897  124  182  1295  1242  state-entry (tools won)
  13 10:30:40 In  944   45  780  119  172  1116  1247
  15 10:30:54 Di 1401  549  738  115  175  1576  1529  M2 awaited 518ms
  17 10:31:05 Di 2329 1250 1003   77  222  2551  2371  M2 awaited 1226ms
  19 10:31:31 Di 1236  106  893  238  213  1449  1404
  20 10:31:44 Di 1188   90  872  225  228  1415  1233
  22 10:32:03 Di 1302  102  892  308  214  1516  1334
  26 10:32:27 Di 1073   47  756  270  181  1255  1542  transition→Closer
  28 10:32:40 Cl 1861 1018  645  198  211  2072  1914  M2 awaited (w0)
  30 10:32:57 Cl 3353 2347  783  222  226  3579  3433  ← WORST: M2 await-on-arrival 2.3s
  32 10:33:17 Cl 1069  174  708  188  225  1294  1189
  35 10:33:44 Cl 1370  119  908  344  228  1598  1577
  37 10:34:08 Cl 1219  119  733  367  240  1460  1275
  40 10:34:35 Cl 1124  128  794  203  179  1303  1258  guard cut #1
  41 10:34:45 Cl 1238  122  785  331  185  1423  1244  guard cut #2 (turn silent+recovery)
  42 10:34:54 Cl 1092   54  772  266  234  1326  1141  transition→Offer
  43 10:35:05 Cl 1109   74  756  280  292  1402  1225
  45 10:35:16 Of 1806  399  906  502  236  2042  1980  M2 awaited 327ms
  46 10:35:31 cd  977  124  830   22  206  1183  1031
  48 10:35:37 cd 2463 1246 1027  190  194  2657  2486  M2 awaited 1201ms
  49 10:35:44 cd 1091  118  773  200  186  1277  1101
  50 10:35:54 cd 1111   97  890  123  188  1299  1188
  52 10:36:07 cd 1499  132  949  418  211  1711  2450  double-round turn
  54 10:36:15 cd 1494  506  866  123  214  1708  1536  M2 awaited (w0)
  55 10:36:24 cd 1266  117 1149    0  293  1558  1657
  57 10:36:37 cd 2157 1215  781  160  198  2355  2178  M2 awaited 1167ms
  58 10:36:46 cd 1526  620  864   43  303  1830  1905  M2 awaited 564ms
  59 10:36:53 cd  938   95  713  129  171  1108   958
  60 10:37:04 Cs 1096  132  848  116  339  1435  3423  double-round (e2eT covers both)
  63 10:37:15 Cs  950  138  776   36  302  1253  1223
  65 10:37:34 Cs 1368  126 1195   48  298  1667  1853  after 15s dead air (caller prompted)
  66 10:37:47 Cs 1537  127  938  472  194  1732  1781
  67 10:38:00 Cs  915  121  789    5  266  1180  1027  head_start 968
```
Totals: e2eR p50 **1,488** / p90 **2,355** / worst **3,579** (t30) · e2eT p50 1,532 / p90 2,450.
State split of e2eT: Closer p50 ~1,258 · contact_details ~1,536 · ConfirmSlots ~1,780 (w/ slots drama) · Intake/Discovery ~1,400-1,570.

## THE slow turn — turn 30, decomposed (10:32:57, Closer)
Exact legs (report JSON + journal anchors):
| leg | ms | anchor |
|---|---|---|
| EOT (adopted eager epoch 46) | — | 10:32:50.879 `eot adopted running eager turn`; t_eager 1817287.8405 |
| **EOT → LLM dispatch** | **2,347** | dispatch = 10:32:53.23; the M2 await consumed the pending tools future exactly at 10:32:53.150 (`split M2: turn=28 how=awaited`; `stream=tools turn=27` settles same ms) |
| dispatch → first event | 783 | 10:32:54.0 |
| first event → first token | 222 | mouth round logged 10:32:54.262 (ttft 808 → gen start ~10:32:53.45) |
| first token → TTS req | 226 | t_tts_req 10:32:54.43… (t_tts_req−t_eager = 3.353 s = stt→tts) |
| + TTS first byte | ~250-350 (unmeasured; no audio-out stamp) | tts_first_byte span ~10:32:54.7 |
**Caller heard: ~3.8-3.9 s of silence after their EOT. The composition is 65% one leg: the M2 await-on-arrival** (design comment: "awaits it at the next turn's ingest (await-on-arrival, no caps)"). The awaited future was the tools task of the in-flight/speculative round (graph turn 27); it settled 2.27 s after this turn's EOT and gated dispatch. `await_warm`/RAG/warm were NOT it (pre_llm: entry 2.8 ms, warm 0, rag 78.8/0).

## The M2-awaited class (all turns where a tools future was still pending at ingest)
| turn | state | waited_ms (logged) | stt | disp |
|---|---|---|---|---|
| 15 | Discovery | 518 | 1,401 | 549 |
| 17 | Discovery | 1,226 | 2,329 | 1,250 |
| 28 | Closer | (w0) | 1,861 | 1,018 |
| **30** | Closer | **unlogged (~2,270 — inferred: future settled 10:32:53.150)** | **3,353** | **2,347** |
| 32 | Closer | (w0) | 1,069 | 174 |
| 45 | Offer | 327 | 1,806 | 399 |
| 48 | contact_details | 1,201 | 2,463 | 1,246 |
| 54 | contact_details | (w0) | 1,494 | 506 |
| 57 | contact_details | 1,167 | 2,157 | 1,215 |
Pattern: **disp ≈ waited + 50-80 ms** on every awaited row — the await IS the dispatch delay. Any turn arriving while the previous round's tools stream is still generating pays its remainder, uncapped.

## Second worst — turn 60 (10:37:04, ConfirmSlots) — double-round, NOT a stall
e2eT 3,423 ms = round 1 ('Got it, today works. Let me see what's open later today.' — check_current_date, first response e2eR 1,435) + tools followup → round 2 ('I can check that. What time today were you hoping for?' — query_livecall_slots → deferred). Caller heard both lines ~2 s apart; the 3.4 s e2eT is the sum of a designed two-round turn. No await stall (disp 132 ms).

## The 15-second dead air (10:37:13 → 10:37:31) — the perceived "loooong" wait
- 10:37:11 gen: "Got it. Let me check around five PM today for you." + `query_livecall_slots` (tools turn=60, logged 10:37:12.953).
- **No `query_livecall_slots` tool span exists in the trace — the call never executed. No `split followup` line for turn 60. No M2 line.** The pipeline produced NOTHING: no speech, no result, no error log, for ~15 s.
- 10:37:28.343 caller re-engages: "Okay. Are you there?" → 10:37:29.932 gen: "I'm here. You asked about five PM today, and I'm checking that now. What's the best email for the invite?" + re-call (deferred again, S1).
- Mechanism (from `builder.py@bb6d216`): `_DEFER_SET = {query_livecall_slots, create_livecall_booking}` with ask-hold gate — **S1**: defer when the round's spoken text asked a question (chop fired); **S2**: day-guard. All 4 live-call attempts were deferred (`ask_hold:defer` spans 10:37:01.362 + 10:37:31.710, arm=S1; deferred tool results observed for call_JZ9/Zer8/SYA7; turn-60's call never resolved at all). The prefetched slots (`slots_prefetch` @10:36:52: ok, 2+2 slots for 10-15/10-16) were NEVER spoken.
- Result: the caller heard ZERO slot information, the flow never reached a booking attempt, the model kept re-calling and looping ("wait for their answer, then call again") which the caller experienced as the agent going away.
- NEW vs KNOWN: the ask-hold defer gate itself = iter105 design (tested); its **live-call S1 loop in ConfirmSlots with zero executions + unfired followup = NEW live evidence** (candidate PT row; see report).

## Per-stream usage (split line)
| stream | rounds | ttft p50 | ttft p90 | max | in-tokens | out-tokens |
|---|---|---|---|---|---|---|
| speak (mouth, luna) | 31 | 909 | 1,205 | 1,828 | 122,056 | 838 |
| tools (gpt-5.4) | 50 | 872 | 1,613 | 2,483 | 337,238 | 2,558 |
Neither stream's model latency is the problem: even the worst mouth ttft (1,828 ms) and tools ttft_event (2,483 ms) sit inside normal EOT→first-token budgets. The stalls came from ORDERING (M2 await), not generation speed.

## Other observations (flag-only)
- 26 eager discards / 26 resumes / 5 barge-ins — turn-15-era resume churn is high but paid only in eager re-runs (no user-visible stall).
- `ask_hold` T4 in-flight filler ('One moment while I check availability') never fired (round already spoke) — by design.
- head_start_ms mostly 0-2 ms except turns 7/19/20/26/35/43/46/58/67 (84-1,200 ms) — eager-reuse working where it fires.
- Ledger cross-check (`latency_pull --run owner-1029-bb6d216`): TTFT p50 906 / p90 1,220; TTFT-steady p50 891; LLM-dur p50 1,181; turn-1 1,226. Per-state TTFT p50: Closer 808 · ConfirmSlots 913 · contact_details 909 · Discovery 921 · Intake 1,226.
- RAG gates (additive, from `rag_pull --gates`): served 98% PASS; await_p50 59.4 PASS; land_rate 60% FAIL / degraded 40% FAIL / query_empty 24% FAIL — separate plane, noted for the report, not part of the two symptoms.
