# 12 — LATENCY FORENSICS: owner call 0949 (`08321d0`, iter116 line) — "why zero gain?"

> Owner ask (2026-10-15): "I called the iter 116 yesterday, once… we gained ZERO latency, why? What is 'luna guard off' and luna + guard (waiting on the brain?)… do deep forensics."
> Call: trace `3c1bd550a7b928ef9a14a84445d97b75` · `08321d0` (`engine/116-realmf-merge-law`) · 2026-10-14 09:49:06–09:54:58 UTC · Twilio phone · tier=fast ON (predates the ~10:00 tier-off) · 20 turns / 0 chained / 0 booked (test hangup).
> Sources (frozen): `01_owner_call_0949_digest.txt` · `owner_0949_journal.log` · ledger run `owner-0949-08321d0` · compare run `owner-0737-2e5646e` + `owner_0737_journal.log` · Langfuse traces `3c1bd550…` / `9b1fdf02…` · code @ `bb6d216` (`builder.py`) · ancestry: `10_report.md` §LINEAGE.
> **ERRATA (v1 of this file, same day):** v1 attributed most of the heard gap vs 0737 to "caller churn after the speculative trigger". **Wrong** — the perf stamps reconcile exactly: `head_start_ms ≡ t_final − t_eager` and `stt ≡ t_tts_req − t_eager` (t1: 639.7 / 1189.65 ✓ to 0.15 ms). The speculative window is small (head_start p50 **160 ms**), and discarded fragments are NOT in the turn metrics (anchor = the adopted epoch). Corrected decomposition below.
> **ERRATA v2 (2026-10-15, on-box verification):** the 0737-vs-0949 comparison is **tier-contaminated**. `OPENAI_SERVICE_TIER` was switched fast → **off** at **09:48:38** (backup `.env.bak-tieroff-iter112test-20261014`, owner comment "fast tier OFF for this test…") — **28 s before the call**, with a unit restart. 0737 ran tier=**fast** (2e5646e boot, 115 deploy note); 0949 ran tier=**off**. `10_report.md`'s "tier=fast ON — predates the ~10:00 tier-off" is WRONG → errata appended there. Also verified: `TTS_GATE_FAST_FIRST_FLUSH=false` on BOTH boots and never edited during the day — the flush slice is NOT an env-flag flip.

## 1. THE BUILD CARRIED NO LATENCY WORK — zero gain is structural, not mysterious

`08321d0` = `c932fcf` (THE REAL-MF = iter109 tip + iter110 driver tooling + iter113 pt14/15/16) + iter115's 4-file merge-law port. Verified absent: the whole 112 speak-first pipeline, the 114 mouth/speech-auth layer, and also `bedecb3`/`63d7f6d` (113b barge-fix + caps).

| Latency work on the board | In `08321d0`? | Measured value |
|---|---|---|
| 112 speak-first staggered pipeline (`SPEAK_FIRST_PIPELINE`) | ❌ absent (0 hits in config) | mic: −261…−667 ms turn p50 on E-runs |
| 114 mouth layer (luna per-state, prewarm, guard) | ❌ absent (16 commits missing) | E10: 1,284 turn / luna split 1,132 |
| iter115 merge-law (prompt rule + temp) | ✅ present | 0 chained / 0 silent on this call |
| 113b barge-fix (`bedecb3`) + caps (`63d7f6d`) | ❌ absent | 0737 had them; this call ran the old barge machinery |

## 1b. THE ENV-SWITCH FORENSICS (owner ask: "EL param? deepgram param? ours?")

**`TTS_GATE_FAST_FIRST_FLUSH` is OUR engine param** (Settings `tts_gate_fast_first_flush`, `config.py`). Semantics: ON = first chunk flushes early — first `,` (≥12 chars) or 28 chars total; OFF = first flush waits for a full **sentence boundary**. Owner PT-71 order keeps it OFF (fast flush chopped clauses). It is not an ElevenLabs or Deepgram parameter.

**It did NOT change between the two calls** (on-box, verified): `TTS_GATE_FAST_FIRST_FLUSH=false` in both env files (`engine/.env:36`, `/opt/diallux-production/.env:50`), absent from every Oct-14 changed-key diff (backups 06:49/07:33/09:48). The flag is clean.

**But an env switch DID land 28 s before the call — on `OPENAI_SERVICE_TIER`** (the switch the owner smelled, different key):

| Time (UTC, Oct 14) | Event | Receipt |
|---|---|---|
| 05:27 / 06:41 / 09:15 / 09:44 | checkouts 63d7f6d → 2e5646e → c932fcf → 08321d0 | `git reflog` on OR |
| 07:28 boot | 2e5646e, process env `OPENAI_SERVICE_TIER=fast` | 115 deploy note |
| 07:33 | EL tune (speed 1.1→1.0, stab 0.7→0.8) — EL only | `.bak-elbars-20261014-073343` diff |
| **09:48** | **`OPENAI_SERVICE_TIER=fast` → empty** + comment "fast tier OFF for this test (iter112-line build); fast tier belongs to the iter113 line"; both env files edited (also `TTS_DELIVERY_PROFILES=false` in /opt) | `.bak-tieroff-iter112test-20261014`; file mtime 09:48 |
| **09:48:38** | **phone unit restarted** (still running this boot now) | `ActiveEnterTimestamp` |
| **09:49:06** | **owner call** — ran **08321d0 code + tier OFF** | process env `OPENAI_SERVICE_TIER=` (empty); journal `model_kwargs` warning lists `temperature` (merge-law guard present) |

So: 0737 ran **fast**, 0949 ran **off** → the cross-call latencies are tier-contaminated (plus caller shape); the 116b report's tier state is inverted. Also note the comment's label "iter112-line build" while the running code was `08321d0` (the 116 line) — record, don't interpret.

## 2. CORRECTED DECOMPOSITION — the call WAS genuinely slow, engine-side

Verified chain per turn (all same origin): **`t_eager` → (first LLM event) → (first TTS request) → (first audio byte)**; `e2eR = stt + l2t`; the honest "dead air after turn-end" = `e2eR − head_start` (confirm → audio).

| Slice (p50, ms) | 0949 | 0737 | Δ |
|---|---|---|---|
| head_start = speculative window (`t_eager`→confirmed EOT) | 160 (p90 640) | **1** (p90 400) | +159 (small) |
| — TTFT (round, to first event) | **848** | 934 | −86 (0949 better) |
| — first-event → first-TTS-request (flush slice) | **813** | 344 | **+469** |
| — TTS first byte (l2t) | 228 | 207 | +21 |
| **heard** (t_eager → first audio) | **1,833** | 1,505 | +328 |
| **felt** (confirmed EOT → first audio) | **1,672** (p90 3,462) | 1,447 (p90 1,918) | **+225** |
| e2e_turn p50 | 2,204 | 1,695 | +509 |

**So:**
- Eager is NOT the villain. The speculative head start bought ~160 ms p50 — and on your hesitation-heavy dictation turns it is *discarded before the adopted round even starts* (15 discards; the adopted epoch begins after your last resume → near-zero head start). The speculation is being thrown away, not abused; discards cost tokens/CPU but are invisible to the turn metrics.
- The real cost: **~1.67 s p50 of dead air after each turn was confirmed** (p90 3.46 s; best turn 773 ms). This is engine-side and real — not a metric artifact.
- The single biggest anomaly vs 0737 sits in the **flush slice: 813 vs 344 ms** — the time from first LLM event to the first sentence flushed to TTS. TTFT was actually *better* on 0949.

## 3. THE FLUSH SLICE — mechanism (verified on Langfuse traces)

First flush is gated on a **sentence boundary** (`TTS_GATE_FAST_FIRST_FLUSH=false` — the PT-71 naturalness law: no chopped clauses). The mouth's replies are frequently **one long first sentence**, so nothing is flushable until the model finishes it. Live examples:

- 0949 (@09:50:14): *"Makes sense, and that Facebook ad helps. We answer your inbound calls when you can't pick up, handle the basics, and help keep those new-customer calls from slipping away, and when"* — out=207 tok, 3 tools, sentence runs long; first aurio waits for the (single) sentence/whole-gen.
- 0737 (@07:38:31): *"Thanks, that helps, and referrals like that usually mean someone saw a real fit. When you miss those calls on delivery, what worries you most about that?"* — an **early sentence boundary** ("…fit.") → flush lands early.

So the baseline 1.4-1.5 s = TTFT + (time to first sentence boundary) + TTS byte, on both builds.

## 3b. THE FINAL MODEL — wall-clock verified (2026-10-15): the mouth does NOT wait for the brain

**Wall-clock trace, turn 7 (merged gen, 3 tools):** eager 09:50:14.53 → first LLM event +1,341 ms → **TTS request +1,661 / first audio ≈ +1,830** → gen keeps streaming, round closes 09:50:18.49 → **tools execute 09:50:18** — the audio left **~2.1 s BEFORE the tools even ran**. In merged gens the text streams first; tools fire only after the gen completes. The "mouth waits on the brain" hypothesis is **falsified**.

The audible wait per turn = A + B + C (from confirmed EOT):

| Call (build) | A: confirm→1st token | B: 1st token→flush | C: TTS byte | felt p50 | first-sentence p50 | gen dur p50 |
|---|---|---|---|---|---|---|
| 0117 (c932fcf) | 833 | 355 | 208 | 1,363 | 79c | 1,211 |
| 0528 (63d7f6d) | 850 | 478 | 198 | 1,617 | 50c | 1,206 |
| 0558 (63d7f6d) | 850 | 279 | 202 | 1,360 | — | — |
| 0737 (2e5646e, merge-law #1) | 901 | 335 | 206 | 1,447 | 97c | 1,510 |
| 0915 (c932fcf, dead-air call) | 919 | 1,225 | 198 | 2,171 | — | — |
| **0949 (08321d0, merge-law #2)** | **754 (best ever)** | **779 (2nd worst)** | 228 | **1,672** | **116c (longest ever)** | **2,131 (heaviest)** |

**The regression is B**: the mouth's opening sentence is now the longest in the agent's history (116c p50 vs 50–97c on every healthy previous call), and sentence-gated TTS means nothing is audible until that opener completes. The trend tracks the merge-law adoption (50c → 97c → 116c; gen dur 1,206 → 1,510 → 2,131): the rule that eliminated chained/silent rounds appears to have stretched the mouth's breath. A (entry) was best-ever, C stable. Per-turn B still varies more than sentence length alone explains (t18: 92c but B 2,612) → close the gap with 3 per-round stamps: first-event / first-text / first-flush.

Levers, ranked: **(1) mouth shape — short first sentence (ack), then the rest** (prompt-level; NOT chopped clauses — a complete short sentence is natural); **(2) the 112/114 speak-first mouth** — decouples mouth from brain; the bare speak stream can be authored short while tools run (luna TTFT 843; still unshipped on the phone); **(3) 3-stamp instrumentation** on the mic lane to nail per-turn B variance; (4) `TTS_GATE_FAST_FIRST_FLUSH` ON = banned (PT-71 chopped clauses).

## 3c. THE MIC-VS-TWILIO ALGEBRA (2026-10-15, owner ask: "why differs from llm mic calls?")

**Identity (verified ±40 ms on 4 runs): `e2e_response = ttft + B + C`** — the head-start `hs` cancels out of the metric (`stt+l2t = hs+ttft+B+C`; `e2eR` anchors at the eager; `felt = e2eR − hs`). The house metric is anchor-clean and comparable across transports.

| Run (box/build) | ttft | B | C | e2eR p50 | disc/turn | input / cache (ratio) |
|---|---|---|---|---|---|---|
| MIC arm0.3 (OR/2e5646e) | 870 | 250 | 196 | 1,311 | 0.18 | 5,343 / 3,712 (70%) |
| MIC scripted (OR/c932fcf) | 838 | 198 | 220 | 1,350 | 0.40 | 5,456 / 3,712 (71%) |
| MIC E10 (FIN/bb6d216 speak-first) | 843 | 269 | 193 | 1,378 | 0.06 | 3,515 / 2,816 (61%) |
| PHONE 0117 (OR/c932fcf) | 921 | 355 | 208 | ~1,363* | 0.54 | 7,312 / 4,736 (72%) |
| PHONE 0528 (OR/63d7f6d) | 972 | 478 | 198 | 1,660 | 0.42 | 6,679 / 4,736 (70%) |
| PHONE 0737 (OR/2e5646e) | 929 | 335 | 206 | 1,497 | 0.67 | 8,473 / 6,784 (76%) |
| PHONE 0949 (OR/08321d0) | 848 | **779** | 228 | **1,832** | 0.75 | 6,002 / 3,712 (62%) |

\* 0117 proxy (no t_audio_out stamp on c932fcf).

**Findings:** (1) `ttft` ~constant 838–972 and `C` 175–228 everywhere — mic vs phone = **+186 ms total on e2eR** (ttft +59, B +85, C +10), not transport audio. (2) B is the only moving slice: mic 198–292 (scripted content) vs phone 279–478 (owner content) vs **0949 779 — worst ever**. (3) 0949 vs its own ancestor (0737, same mouth bytes): +335 e2eR = B **+444** (−81 ttft, +22 C). (4) B on 0949 ≈ 6–8 ms/char of first sentence (normal ~50 tok/s pace) on 16/20 turns, with **4 STALL turns** (t17/t18/t25/t36: 17–28 ms/char = 2–4×; ~4.7 s of tail) — unexplained, NOT tools (t18's gen had `tool_calls=[]`), NOT reasoning (`openai_reasoning_effort="none"` on this build). (5) Mic benchmarks never saw the real B band — **the scripted caller is systemically short-openers content (B≈250); owner content hits B 450–780** — that is the entire "mic looks fast vs Twilio looks slow" mirage, plus +60 ttft and cache shape. (6) The merge-law stretched the first breath across calls: openers 50c (pre) → 97c (0737) → 116c (0949) — the silence win's visible cost.

**Solution map (engine-level):** ① 3-stamp instrumentation per round (first event / first text token / first flush + delta-gap sampler) — catches the 2–4× stalls (≈25% of 0949's tail) on the next run; ② the 114 mouth line measured on **owner-like content** (the only built mechanism that decouples the first breath; its scripted B≈270 is not evidence for hard content); ③ cache hygiene for ttft (62–76% ratios; t17/t21 mid-call re-prefills) — separate from the tier track; ④ honest floor on the current model: ttft 850 + B 250 + C 200 ≈ **1.3 s** — sub-1.2 requires ttft↓ (cache/model) and/or B↓ (mouth line + stall fix).

## 3d. THE B-TAIL HISTORY + THE "MOUTH QUEUES BEHIND TOOLS" MECHANISM (2026-10-15)

**B distribution per call (same slice):**

| Call | B p50 | B p90 | B max | turns B>1s |
|---|---|---|---|---|
| 0558 (63d7f6d, pre-merge) | 279 | 487 | 665 | **0/37** |
| 0737 (2e5646e, merge-law) | 335 | 677 | 859 | **0/48** |
| 0117 (c932fcf) | 355 | 1,524 | 3,133 | 11/39 |
| 0949 (08321d0, merge-law port) | **779** | **2,398** | 2,612 | **4/20** |
| 0528 (63d7f6d) | 478 | 1,422 | **6,467** | 13/31 |
| 0915 (c932fcf, dead-air) | 1,225 | 2,072 | 5,074 | 15/23 |
| MIC arm0.3 (scripted) | 250 | 486 | 659 | 0/17 |

Old-call stalls (0117/0528/0915) = the **chained silent-tool-round** disease (round 1 emitted only tools → no text; B spans round 1 + round 2 to flush). Merge-law killed chaining (0737/0949: 0 chained) — 0558/0737 prove a clean call is achievable. **0949's 4 stalls are a NEW flavor: the model streaming the TOOL-CALL block BEFORE its speech in the single merged gen** — out-token vs text accounting: t18 gen = 249 out-tok / 92c text (≈226 tool tokens); t36 = 231 / 141c (≈196); at ~70–80 tok/s that's 2.4–2.8 s of mouth-queueing behind tool args, matching B 2,612/2,398. Same mouth bytes on 0737 never did this (rote/short tool args). **The mouth-stream architecture (112/114 speak-first: noop-tool mouth ∥ real-tools stream) makes this ordering physically impossible for the mouth — the structural cure.** Instrumentation (first-text vs first-tool stamps) will confirm ordering per turn live.

**Muzzle analysis (owner question, 2026-10-15: "do we need to wait for tools before speaking?").** On 0949 all 16 tool spans = next-turn feeders (extract_* captures, *_completed flags, transition_to_* state switches) — **zero same-turn result dependencies**; and tool EXECUTION never blocked speech (t7: audio out 2.1 s before tools ran). So the 4 muzzled turns were functionally unnecessary. The only legit same-turn case = the caller asked a question whose ANSWER is the tool output (slots/calendar readback, leak-math readback via the deterministic lane, validator gates) — those pay tool exec + an extra speak-round (~850 ttft) by design. Everything else is punt-able to the next turn (the engine already does this for ask-hold S1: a round ending on a question defers its tool calls).

## 4. MERGE-LAW DELIVERED SILENCE/TAIL WINS, NOT p50 DIET

| Metric | 0949 | 0737 | 0915 (c932fcf, no merge-law) |
|---|---|---|---|
| Chained turns | **0/20 (0%)** | 4/48 (8.3%) | 16/23 (69.6%) |
| Silent generations | 0 | 0/56 | — (dead-air end_call loop) |
| LLM-dur p50/p90 | 2,131 / 3,498 (merged gens heavier) | 1,510 / 2,031 | 1,197 / 1,599 |
| cache-hit tokens p50 | 3,712 | 6,784 | — |
| cache-0 rounds | 3 (t1 cold, t17, t21 mid-call) | 1 (t1) | 2 |

## 5. THE GUARD QUESTION (owner asked): "luna guard off" vs "luna + guard" — is the mouth waiting on the brain?

**Guard** (`builder.py` @ `bb6d216`): before a sentence reaches TTS, `_speak_guard_cut(sent, auth_tokens, auth_nums, dvs, tooltext)` checks figures/times/status claims against engine-authorized facts (dvs, tool results, caller's own words). Unauthorized → sentence **cut**, one recovery line spoken (*"Let me run those numbers real quick — one moment."*), span `speak_guard_cut`. Fail-closed, regex-only, auth sets computed once per round (no added wait on the LLM path).

**"Waiting on the brain?"** — No hard wait; it is a **filter, not a lock**. But it *defers content* whose support the brain hasn't produced yet (the FIND-116C class: spoken "$3,750" before the engine computed $15,000) → cut + next-round delivery. Guard OFF (E6) = fastest raw mouth (1,087 turn p50) **and** the config where wrong numbers go to air; guard ON (E7) = protected. The +206 E6→E7 delta = run-arc + v1.0 false cuts (fixed v1.1/v1.2), not serialization. **Luna** = `gpt-5.6-luna`, the speak-twin mouth model; brain/tools stay gpt-5.4.

## 6. VERDICT + LEVERS (ranked, for the owner)

| # | Finding | Class |
|---|---|---|
| 1 | Zero gain = **the latency build was never on the phone** (`08321d0` has no 112/114/113b). Structural. | ✅ explained |
| 2 | Eager is **not** too aggressive: head start p50 160 ms (0737: ~1 ms); on hesitant turns the speculation is discarded → zero head start, but no audible cost and no metric inflation beyond ~160 ms. | ✅ explained |
| 3 | The call was **really ~1.67 s dead-air p50 after turn-end** (p90 3.46 s) — engine-side. Retract earlier "engine not slower" reading (mixed stamp origins). | ✅ corrected |
| 4 | `TTS_GATE_FAST_FIRST_FLUSH` = OUR flag, **unchanged** (false both calls) — NOT the env fuck-up. The env switch that DID land: `OPENAI_SERVICE_TIER` fast→off at 09:48:38 (28 s pre-call) → 0737-vs-0949 = tier-contaminated; 116b report tier claim inverted (errata filed). | ✅ corrected |
| 5 | Regression slice = **B (first token → first spoken sentence): 779 vs 279–478 historical** — opening sentences longest ever (116c p50 vs 50–97c; merge-law adoption curve) × sentence-gated TTS. Mouth does NOT wait on the brain (wall-clock: audio −2.1 s before tools ran). Levers: (1) short-first-sentence mouth shape (prompt), (2) 112/114 speak-first mouth (unshipped), (3) 3-stamp instrumentation. | ⏳ next |
| 6 | Instrument: log the sentence-gate flush (reason + ms since first token) per round — stop reverse-engineering from cross-origin stamps. Matched A/B for tier (fast vs off, same commit, scripted caller) — owner sets env, agent runs arms. | 📋 next battery |
| 7 | Live line state (2026-10-15 15:00 UTC): phone still runs the **09:48:38 boot — 08321d0 + tier OFF**; today's 06:56 `pull` to `c0ca759` did not restart (running code unchanged). | 📋 note |

**One-liner:** the mouth does **not** wait for the brain — on merged turns the audio leaves ~2.1 s before the tools even run. The felt regression is B: the spoken **opening sentence** is the longest in the agent's history (116c p50, up from 50–97c pre-merge) and sentence-gated TTS plays nothing until it completes; the merge-law fixed silence but stretched the mouth's first breath. A (entry: 754) was best-ever; C (TTS byte: 228) stable.
