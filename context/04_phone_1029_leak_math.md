# 04 — LEAK-CALCULATOR FINDINGS: every figure vs engine truth — owner call 1029 (bb6d216 split line)

Call: trace `102a0c973152f409c7c615dd56e236ba` · Closer 10:32:22–10:35:02 · build `bb6d216` (guard v1.2 speak_fact_guard=True, derive layer, Closer WIT).
Owner symptoms: "the leaking calculator was a bit off" + "it never gave me a correct estimate".

**VERDICT (mechanical):**
- The engine computed **weekly_leak=160,000 / monthly_leak=692,800** ONCE (10:33:36.405, `derive:leak_math`) from inputs `{missed_calls_weekly:20, close_rate_pct:80, avg_job_value:10000}` — the 80% was **the caller's own words** (10:33:33-36, "eighty percent of them book"). Formula verified: 20×10,000×0.80 = 160,000; floor(160,000×4.33/100)×100 = 692,800. **The number was faithful to the caller's inputs — but never revised after the caller corrected them.**
- The caller HEARD `$160,000` (t37, authorized by dvs) → pushed back ("way way off") → corrected to **5%** → the mouth's corrected math was **guard-cut twice** (unauthorized `%10`, `%5`) → **no re-derive ever ran** → dvs stayed at 160,000 through call end. **"Never gave me a correct estimate" is literally true: the corrected estimate was never computed or spoken; the guard even promised "Let me run those numbers real quick — one moment." — and nothing ran.**
- **FIND-116C (derive race) HELD**: derive finished 10:33:36.405, the t37 mouth gen started 10:33:40.0 (~3.6 s later) — the value was pre-existing/authorized; NO race.
- **NEW finding: the corrected-inputs-never-re-derive loop** (see classification).

## Every figure on the table (spoken vs engine truth)
| # | figure (as heard/generated) | turn/gate | producer | engine truth at that moment | guard | verdict |
|---|---|---|---|---|---|---|
| 1 | "roughly 20 missed after-hours calls a week" | t14/t5 input round | caller ("ten, twenty") → mouth echo → dvs `missed_calls_weekly=20` (extract_leak_inputs #1 @10:32:50) | 20 | pass | ✓ correct-from-engine |
| 2 | "$10,000 average job" | t27 10:32:36 | caller ("let's say ten thousand") → dvs `avg_job_value=10000` | 10000 | pass | ✓ correct-from-engine |
| 3 | "80%" (caller) | 10:33:33-36 | caller → dvs `close_rate_pct=80` (extract_leak_inputs #2 @10:33:36.1) | 80 | n/a (caller's own words; note: word-percent does NOT authorize a `%` token in the guard) | ✓ entered engine |
| 4 | **"about $160,000 in potential weekly revenue"** | t37 10:33:40 | **engine derive 10:33:36.405 → spoken verbatim-as-returned** | weekly_leak=160,000 | pass (authorized) | ✓ correct-from-engine — **this is the "a bit off" figure the owner heard** |
| 5 | "completed jobs are closer to 5–10%, not 80%" | t38 10:34:31 | mouth recompute (NOT in dvs/tooltext) | dvs still {20,80,10000} | **CUT `number:%10`** + recovery "Let me run those numbers real quick — one moment." | mouth-recompute → guard-cut (by design) |
| 6 | **"Using 5% … about $10,000 a week from those missed calls"** | t39 10:34:41 | mouth recompute — arithmetically **CORRECT** (20×10,000×5% = 10,000) but unanchored | dvs still {20,80,10000}; no extract_leak_inputs with 5 ever ran | **CUT `number:%5`** (whole sentence = whole turn) + recovery line | mouth-recompute-WRONG-ANCHOR → guard-cut; **turn silent except recovery** |
| 7 | "I hear you, though **that number** is still only potential revenue" | t40 10:34:49 | mouth (no figures — guard passed) | — | pass | dangling referent: "that number" = the never-spoken $10,000/week |
| 8 | monthly 692,800 | never | derive | monthly_leak=692,800 | — | never spoken (no complaint) |

Guard-cut detail (spans + journal): cut #1 @10:34:32.666 `speak_guard_cut {reason: number:%10, sentence: "You're right, completed jobs are closer to 5–10%, not 80%."}`; cut #2 @10:34:42.229 `{reason: number:%5, sentence: "Using 5% as the completed-job rate, the opportunity is about $10,000 a week from those missed calls—does that feel closer?"}`. Recovery text (builder.py L250): `"Let me run those numbers real quick — one moment."` — emitted once per round after a number-cut.
Authorization semantics (builder.py `_speak_guard_cut` + `_spk_tokens`): money `$…`, percent `%…`, times, `#3+digit` numbers are checked against dvs values + caller words + tool text. `5–10%` tokenizes to `%10`; `5%` to `%5`; dvs authorizes `%80`. The caller's spoken "five percent" does NOT authorize `%5` (word-digit conversion only fires on runs ≥3 digit-words — iter114d rule) — so even caller-originated corrections cannot be re-spoken unless re-ingested via tools.

## Engine timeline (anchored)
```
10:32:37.909 tools: extract_leak_inputs #1 {missed_calls_weekly:20, avg_job_value:10000}   -> written {20, 10000}
10:32:50.595 M2 turn=27 consumed it (waited 1ms)
10:33:33.019 tools: extract_leak_inputs #2 {20, close_rate_pct:80, 10000}                 -> written {20, 80, 10000}
10:33:36.435 M2 turn=32 consumed it (waited 5ms)
10:33:36.405 span derive:leak_math -> {weekly_leak:"160,000", monthly_leak:"692,800"}
10:33:40.335 mouth gen t37 speaks $160,000 (authorized, heard)
10:34:04 caller: "No. It feels way way off."
10:34:31 caller: "five to ten percent"      -> mouth tries 5–10%/80%  -> CUT (%10)
10:34:41 caller: "Let's see five percent."  -> mouth tries 5%/$10k-wk -> CUT (%5); turn = recovery only
10:34:49 caller: "Yeah. Sure." -> mouth t40 (no figures)
             *** no extract_leak_inputs, no derive, no dvs write with 5% — ever ***
10:35:12 Closer closed (transition_to_Offer) -> dvs weekly_leak still "160,000" / close_rate_pct 80
```
Evidence: trace spans `tool:extract_leak_inputs` ×2 (inputs/outputs above), `derive:leak_math` ×1, `speak_guard_cut` ×2; journal has NO leak_math/dvs lines (spans only); no third `extract_leak_inputs` anywhere.

## Classification (NEW vs KNOWN)
| item | verdict |
|---|---|
| FIND-116C leak-figure race | **HELD** — derive pre-dated the speaking gen by 3.6 s; figure authorized from the start |
| iter112-e5 (fabricated leak math + invented 2:30 slot) | **NOT this** — arithmetic correct (20×10k×5%=10k); failure was authority/ingestion, not fabrication |
| iter112-e1 (unanchored mouth math pre-derive layer) | same FAMILY (mouth computes instead of engine) — but distinct root: here the inputs were **caller-corrected data the engine never ingested** |
| **NEW — corrected-inputs never re-derive** | caller corrects close rate (80%→5%) → **no tool re-call, no re-derive** → dvs stale → the mouth's corrective sentence is guard-cut (correctly) → recovery line promises a recompute that never comes → caller hears no corrected estimate. The loop is open at the **re-ingest/re-derive** junction; the guard did exactly what v1.2 says |
| NEW secondary — silent correction turn | t39's single-sentence output was 100% cut → the caller heard only "Let me run those numbers real quick — one moment." (a promise), then t40's dangling "that number" |
| Recommended fix direction (follow-up plan, owner-gated) | on caller-facing corrections to leak inputs (close rate / calls / job value), the tools stream must re-call `extract_leak_inputs` (or the engine must re-derive on corrected caller text), so the WIT card + mouth carry the corrected figure; then the guard authorizes it naturally. NOT a guard loosening |
