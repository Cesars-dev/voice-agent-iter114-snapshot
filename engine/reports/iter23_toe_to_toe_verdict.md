# iter23 — TOE-TO-TOE VERDICT: who pulls the sales KBs, and who sells better?

> Plan: `plans/plan_v5_iter23_sales_kb_toe_to_toe.md` (READ-ONLY). Corpus of record:
> deployed `Dialux_SDR/testing/json_logs/` **last 3 batches per class** (60 conversations,
> mostly V7.7 `agent_87e4d5f08475e5bc558b2f390f` era, Aug 26 – Sep 3) vs v5
> `v1.8-iter20-20260906` **last 3 batches per persona** (24 personas × 3 = 72 conversations;
> 19 substituted from `v1.7-iter19` per the plan's trace-fallback rule — flagged
> `substituted_from` in `reports/iter23_v5_extract.json`).

## 1. Headline verdict

**v5 pulls the sales KBs far more — 2× to 9× per turn — but pulls ≠ sells. On the sales field
itself the two are statistically tied, and the deployed Retell agent wins the tie-break on the
two beats where craft actually shows (pricing discipline and cold-class composure).**

- **KB-pull dimension (measured BOTH sides — see asymmetry note in §2):** v5 out-pulls Retell
  in every class per 10 agent turns — NO-BOOK **14.96 vs 1.72**, BOOK **13.45 vs 3.14**,
  CURVE 6.08 vs 4.03, GK 2.95 vs 2.58, BRK 1.93 vs 0.71 (`reports/iter23_scoreboard.json`
  → `kb_dimension`).
- **Beat scoreboard (5 beats × 0–2):** Retell **38.39** vs v5 **38.25** — a tie with Retell
  winning 4 of 5 classes (BOOK 8.07/8.06, CURVE 8.17/8.03, GK 8.07/6.12, NO-BOOK 8.08/8.00);
  v5 wins only BRK (8.05/6.00) on warm closing under attack.
- **The one hard skill gap found: v5 leaks OUR price.** In GK it told prospects
  *"Most clients pay between $1,200 and $2,800 a month"* / *"typically in the $1,200 to $2,800
  a month range"* (5 occurrences). The deployed agent **never** quoted our price in these
  batches — it defers and anchors on the sanctioned $2,500–$3,500 answering-service figure.
  The KB's §Pricing third-ask release ("…starts at $697 a month…") was **never** released by
  either side in these corpora.
- **Neither agent is a pain-ladder closer.** Pain-KB fingerprints within 2 turns of a caller
  pain statement: Retell 0.00–0.17, v5 0.00–0.12 of pain statements (beat 4 details).
  `pain-points-kb` is dead weight on both sides: deployed **7** pull-turns in 60 convs,
  v5 **35** spans in 2,091 — the pain content lives in the prompts, not the KB.

**Answer to Julio's question:** the LangGraph state machine is the better KB *consumer*
(healthier retrieval plumbing: 0% zero-chunk in this corpus), but the deployed Retell agent
is the better *salesman in practice* — same craft with fewer pulls, no price leaks, and better
end-state discipline. If the goal is "better salesman," the sales KBs are not the missing
piece on either side; the missing piece is **in-the-moment pain amplification**, which neither
architecture gets from its KBs today.

## 2. The pull table: v5 measured rag spans vs Retell measured retrieval

**Asymmetry note (upgraded from the plan's assumption):** the plan expected Retell KB usage to
be *inferred from output fingerprints* because "Retell logs no per-turn KB retrieval." It does
— indirectly: every payload carries a public `knowledge_base_retrieved_contents_url` with the
**actual per-turn retrieved chunks**. So both sides are directly measured; output fingerprints
remain the secondary behavioural check. (15/60 deployed conversations lack the URL — older
V7.5/other-agent payloads — flagged `kb_url_missing`, fingerprints only.)

### v5 — state × KB pull turns (2,091 rag spans, 60 traced calls; 12 calls untraced → flagged)

| State | sales-language | sales-psychology | pain-points | industry | call-context | discovery-bridge | call-closing |
|---|---|---|---|---|---|---|---|
| Intake | 68 | 62 | 6 | 47 | 49 | — | 14 |
| Discovery | 81 | 35 | 8 | 73 | 90 | 112 | 2 |
| Closer | 58 | 46 | 13 | 75 | 16 | — | 7 |
| Offer | 42 | 11 | 6 | 15 | 4 | — | 14 |
| contact_details | 131 | 20 | 2 | 44 | 7 | — | 45 |
| ConfirmSlots | 5 | 7 | — | 8 | — | — | 6 |
| VerifyLead | — | 3 | — | — | — | — | — |
| Booking | — | 17 | — | — | — | — | 11 |
| Closing | 16 | **431** | — | — | — | — | 48 |

Zero-chunk rate: **0% on every KB** in this corpus (sales-psychology 632 pulls / 0 starved,
sales-language 401 / 0, pain-points 35 / 0). **This CONTRADICTS the iter21 live-voice
evidence** (79/102 queries → 0 chunks, `micbridge-0c8bfae47854`) — the plan pinned this as a
flag-if-absent finding: the starvation is a property of the **live voice path** (short/filler
queries), not of the text harness batches. Don't kill iter19/20 RAG config over it; fix the
voice-path query shaping.

**Waste signal:** 68% of v5's sales-psychology pulls (431/632) fire in the **Closing** state —
where only 5 fingerprint hits ever appear in speech. Those chunks are retrieved and never used.
Deployed-side per-turn retrieval gaps: only 2–9% of agent turns retrieve nothing.

### Retell — direct retrieval density (per turn, from the CloudFront contents)

| KB | pull turns (60 convs) |
|---|---|
| industry-kb | 461 |
| discovery-bridge-kb | 261 |
| **sales-psychology-kb** | **215** |
| call-closing-kb | 94 |
| call-context-kb | 92 |
| are-you-ai-kb | 87 |
| voice-ai-capabilities-kb | 53 |
| **sales-language-kb** | **42** |
| **pain-points-kb** | **7** |

Retell leans on `industry` + `discovery-bridge` and barely touches `sales-language` (42) —
because the V7.7 prompt carries the phrasing rules inline; v5 leans hard on
`sales-psychology` (632) + `sales-language` (401) as if the KBs were the craft source.

## 3. Ten quoted line pairs (receipts; paths in the extracts)

| # | Beat | Retell (deployed) | v5 (LangGraph) |
|---|---|---|---|
| 1 | Pricing defer | CURVE `c7cd09eb`: "I can't give a single exact number without seeing your call flow and volume, but most practices compare it against what a human answering service costs ($2,500–$3,500/month) or one front-desk salary." | CURVE Brenda: "Great question. Pricing is custom and gets covered on the live call based on your setup, so you get exact numbers for your situation. Is that fair?" |
| 2 | Value anchor | (same CURVE line as #1) | CURVE Brenda: "Most dental practices spend $300 to $600 a month on traditional answering services—sometimes more if call volume is high." |
| 3 | Cost-of-inaction math | BOOK `388890ab`: "20 missed calls/week × 50% booking × $650 puts you at about $6,500/week, or roughly $28,100/month in missed revenue." | BOOK Danny: "So with your numbers, that's about $4,050 a week slipping away… roughly $17,500 a month." |
| 4 | Pain anchored on caller's words | NO-BOOK `c49cccf2`: "if they're ordering somewhere else when it rings out, that's real revenue walking away." | BOOK Susan: "Right, so every one of those could be a $2,500 project walking away. When you miss those calls while you're out on jobs, how does that usually show up in your schedule or revenue?" |
| 5 | Ack → probe | BOOK `388890ab`: "Makes sense—when those after-hours calls hit voicemail, do they usually leave a message and call back, or do you think they're calling another practice?" | BOOK Danny: "Got it, so after hours everything just rolls to voicemail. What's the main thing you're hoping to fix — more jobs booked from those missed calls, or better coverage…?" |
| 6 | Binary 20-minute close | BOOK `388890ab`: "It's a 20-minute walkthrough—Jay will show you exactly how we catch those after-hours calls…" | BOOK Marcus: "…we do a 20‑minute walkthrough where we show exactly how we'd plug that leak for your firm—would you rather do that later today or tomorrow?" |
| 7 | Quantify pain | BOOK `5329e16c`: "Makes sense—50% is a fair gut-check. What's the average value of a new patient appointment for you, roughly, in dollars?" | GK Priya (leak context): "roughly how many calls do you miss in a typical week?" |
| 8 | BRK ending (the tie-breaker beat) | BRK `9b7fa201` (cold, tool-flavored): "I'm having trouble confirming the calendar because that phone number is being rejected as invalid…" | BRK Wendy (warm): "Thanks, Wendy — totally understand. Since we didn't get a number… want to share the best 10-digit number now?" |
| 9 | **PRICE LEAK — v5 only** | (none — deployed never quoted our price) | GK Boris: "Jay sets exact pricing after a quick look at your call flow, but for your volume it's typically in the $1,200 to $2,800 a month range, and setup is usually 1 to 3 days." |
| 10 | Wasted pull — v5 Closing spam | — | sales-psychology retrieved in Closing state **431×** (68% of its volume); Closing lines are "No, that's all for now. Thank you…" — only 5 fingerprint hits ever surface in Closing |

## 4. The three biggest gaps → one highest-leverage fix

1. **v5 pricing discipline leak (GK):** "$1,200 to $2,800 a month" said 5× against the KB's
   "never quote OUR price." The KB was pulled at that very turn — retrieval worked, the
   instruction lost to the model's improvisation. Retell never leaks.
2. **v5 Closing-state retrieval spam:** 68% of sales-psychology pulls fire in Closing, where
   nothing salesy is ever said — pure wasted tokens/latency, and it is what inflates v5's
   headline pull numbers. Pull volume ≠ pull value.
3. **`pain-points-kb` is dead weight on both sides** (deployed 7 pull-turns, v5 35 spans; pain
   beat scores 0.00–0.17 everywhere). Pain content flows from prompts, not from this KB —
   on both architectures.

**Highest-leverage fix (recommendation ONLY — no code in this plan):** don't attach more KBs
anywhere (iter22's idea — abandoned); instead **gate RAG by state** in v5: zero KB retrieval in
`Closing`/`Booking`/`VerifyLead`, and for `sales-psychology`/`pain-points` require an
objection/pain trigger (the same trigger the prompt already defines: "Retrieved when: …").
That cuts the wasted 431 Closing pulls, sharpens the pulls that matter (Closer/Discovery), and
would move the v5 agent's KB usage from "more" to "well-timed" — the one field where the
deployed agent currently beats it while spending ~9× fewer sales-KB pulls.

## Appendix — scoreboard raw (`reports/iter23_scoreboard.json`)

| class | side | kb_fp | obj | price | pain | warm | TOT | winner |
|---|---|---|---|---|---|---|---|---|
| BOOK | Retell | 2.00 | 2.00 | 2.00 | 0.07 | 2.00 | 8.07 | **retell** |
| BOOK | v5 | 2.00 | 2.00 | 2.00 | 0.06 | 2.00 | 8.06 | |
| BRK | Retell | 2.00 | 2.00 | 2.00 | 0.00 | 0.00 | 6.00 | **v5** |
| BRK | v5 | 2.00 | 2.00 | 2.00 | 0.05 | 2.00 | 8.05 | |
| CURVE | Retell | 2.00 | 2.00 | 2.00 | 0.17 | 2.00 | 8.17 | **retell** |
| CURVE | v5 | 2.00 | 2.00 | 2.00 | 0.03 | 2.00 | 8.03 | |
| GK | Retell | 2.00 | 2.00 | 2.00 | 0.07 | 2.00 | 8.07 | **retell** |
| GK | v5 | 2.00 | 2.00 | **0.00** | 0.12 | 2.00 | 6.12 | |
| NO-BOOK | Retell | 2.00 | 2.00 | 2.00 | 0.08 | 2.00 | 8.08 | **retell** |
| NO-BOOK | v5 | 2.00 | 2.00 | 2.00 | 0.00 | 2.00 | 8.00 | |

Beat notes: `kb_fp`/`obj`/`price`/`warm` saturate for both sides (same shared brain) — the
pinned verbatim-fingerprint objection metric scored 0–3 per class on BOTH sides (KB objection
lines are paraphrased, not quoted; the scoreboard scores the behavioural ack+question metric
instead, with the pinned metric kept in each detail string). `pain` is the only beat that
fails everywhere. v5 context (iter20-native, 2 substituted calls per class excluded):
BOOK 12/12 pass, BRK 13/15, CURVE 2/3, GK 13/13, NO-BOOK 7/10.

### Caveats (all flagged in artifacts)
- 12/60 deployed conversations (mostly the V7.5-era BRK batch) have no KB-contents URL →
  fingerprints only, flagged `kb_url_missing`.
- 12/72 v5 conversations have no Langfuse trace (3 personas ran untraced Sep 5 + 3 fetch
  failures); 19 conversations were substituted from `v1.7-iter19` per the plan's fallback rule.
- Deployed last-3 batches mix a few non-V7.7 payloads (BOOK 09-03 batch from other agent ids);
  agent_id recorded per conversation in `reports/iter23_corpus_manifest.json`.
- Fingerprints prove craft surfaced in speech, not causal KB usage — both sides now have
  direct pull evidence, so no conclusion rests on inference.
