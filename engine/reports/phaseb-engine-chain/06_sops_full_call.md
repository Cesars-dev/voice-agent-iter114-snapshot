# iter33 3-SOP audit — gpt-5.2 battery (first-13, Phase B chain ON)

- Digest: `/tmp/opencode/iter33_sop_digest.txt` (built fresh from the 13 battery json_logs,
  condensed `t[state] C/A/T` — graded by READING it, never from memory).
- OTEL latency: `/tmp/opencode/iter33_lat.txt` (`state_latency.py` over 16:00:10–16:19:50Z).
  `call.py digest` / `lf.py lat` from the plan do NOT exist in this tree — adapted equivalents:
  `call.py sop <battery-file>` × 13 (dossiers) + `state_latency.py` + `silent_rounds.py`.
- Costs: UNAVAILABLE — `lf.py costs` 400s on this server, json_log `cost` empty. No cost
  line for either model (same gap carries to iter34).

## TABLE (verdicts + latency)

| Persona | CALL | SALES (/14, band) | HUMANIZED | Turn p50 | OTEL gens |
|---|---|---|---|---|---|
| Maria | PASS | 13 hire | PASS | 3.41s | 30 |
| Danny | PASS | 12 hire | PASS | 3.57s | 23 |
| Susan | PASS | 11 solid | PASS | 3.62s | 21* |
| Marcus | PASS | 11 solid | PASS | 3.82s | 30 |
| Carlos | PASS | 9 solid | PASS | 1.84s | 21 |
| Pedro | **FAIL** (over-book) | 7 retrain | PASS | 2.97s | 54 |
| Sofia | PASS | 12 hire | POLISH (R1×3) | 2.97s | 30 |
| Jorge | PASS | 9 solid | PASS | 3.28s | 19 |
| Daniel | POLISH (t10 identity flip) | 7 retrain | PASS | 2.81s | 21 |
| Brenda | PASS | 13 hire | POLISH (R1×3, stack) | 3.06s | 70 |
| Gene | PASS | 10 solid | POLISH (R5.3×2) | 3.30s | 29* |
| Frank | PASS | 8 solid | PASS | 1.79s | — (no trace) |
| Ray | PASS | 9 solid | PASS | 1.75s | 6 |

\* Susan/Gene gens counted by direct trace-ID pull (EMPTY REST names — meters skip them).
OTEL aggregate line: **ALL (10 named traces): calls=304 p50=1.52s p90=2.15s max≈6s**,
slow states ConfirmSlots p50 1.71s (validate walk) and Closer p50 1.63s (leak math) —
matches the expected profile. Per-state table: Intake 43/1.58 · Discovery 68/1.59 ·
Closer 36/1.63 · Offer 11/1.62 · contact_details 63/1.32 · ConfirmSlots 51/1.71 · Closing 32/1.33.

## Summary

**CALL: 11 PASS / 1 POLISH / 1 FAIL.** Flow discipline holds across all 13: states advance
extract→complete→transition, one question per turn, goodbyes/end_call clean, zero 48-turn
runs. The chain books invisibly (0 VerifyLead/Booking LLM gens on chain-booked calls,
`chain_done=True`, single attempts). Two demerits: Pedro FAIL = the known deferred
over-book (booked a hesitant no-book persona — outcome FAIL, not a flow break); Daniel
POLISH = t10 identity contradiction ("you're talking with Linda, a real person") after
honest AI disclosure in t1/t5/t7 — honesty breaks under repeated challenge.

**SALES: 3 hire (Maria 13, Brenda 13, Danny/Sofia 12) · 6 solid · 2 retrain (Pedro 7,
Daniel 7).** Discovery-before-pitch everywhere; leak math lands conversationally with
prospect numbers played back ($6.5k/wk Maria, $65.4k/mo Marcus, $12.1k/mo Brenda).
Objection handling is the strength (Carlos scam-deflection ×N, Brenda cost/HIPAA/accuracy
gauntlet, Ray callback-boundary held 4×). Binary day-first closes, assumed sales on all
8 books. Pedro's 7 is the steamroll signature: 3 explicit hesitations (t16/t17/t19
"make sure I understand before we go ahead") plowed past — the over-book and the
retrain band are the same bug. Daniel's 7: robot-loop ate the middle (caller-led pacing).

**HUMANIZED: 10 PASS / 3 POLISH / 0 FAIL.** Zero R3 (tool-talk) / R4 (brackets) /
R5.1 (bloat) / R5.5 (verbatim repeats) battery-wide. Agent medians 20–35w vs caller
8–18w — no R5.7. R2 opener variance healthy (Yeah/Got it/Totally/Makes sense/Love it/
Oof/Ugh). POLISH items: Sofia + Brenda R1 "I hear you" ×3; Brenda + Danny filler
stacking (1–2 turns); Gene R5.3 "those nine missed calls" ×3 + slot echo ×3.
DISCOUNTED (stated rules): phone-digit recycling (required readback), R1 singletons
(one idiom ≠ pattern), and the cross-call booking filler triple ("One sec — finishing
that up…") — designed engine-filler behavior surfacing in speech on all 8 books,
low user impact, prompt-track carry-forward (prompts frozen this plan).

## Relevant info

1. **Daniel t10 (quote):** t1 "Yeah, I am — I'm Linda, an AI assistant" → t10 "you're
   talking with Linda, a real person, right now." Direct contradiction after 3 honest
   disclosures. Fix track: identity-consistency rule (prompts frozen — Phase B v2).
2. **Pedro steamroll (quotes):** t16/t17/t19 "Just want to make sure I understand
   everything before we go ahead" → agent books anyway t26. Hesitation-acknowledge
   missing. Same deferred retrain item as the over-book.
3. **Pedro phone-loop:** caller dictates 555-123-45678 (t21), agent reads back the
   DEFAULT 15123120001 (t21), corrected t22. One misreadback, recovered — echo-hygiene note.
4. **Brenda/Marcus tomorrow-loops:** mock offers today-only; callers ask tomorrow 4–6×;
   agent holds gracefully without hallucinating tomorrow slots (Brenda t31 booked TODAY
   9/4 while caller said tomorrow — then negotiated honestly t32–t35, no fake slot).
   Mock-fidelity artifact, correct behavior.
5. **Carlos t16 `end_call,end_call`:** duplicate end_call in one round — harmless
   (call ended), dedupe note for executor.
6. **Sofia t8 timezone waffle** ("Central? Mountain? No, Central!") handled cleanly;
   **Frank 5t / Ray 6t** textbook hostile exits (de-escalate → honor → spoken goodbye).
7. **Gate to T4: OPEN.** No CALL/SALES/HUMANIZED FAIL requires engine changes — Pedro
   FAIL is the deferred craft item (unchanged scope), Daniel/Brenda items are
   speech-style (prompt track, frozen). Nothing in `diallux/graph/*` implicated.
