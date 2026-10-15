# iter24 — Deviation Audit: did Julio's `#[refer <kb> — <dv>]` design ever exist?

**Date:** 2026-09-06 · **Scope:** READ-ONLY audit, per `plans/plan_v5_iter24_kb_referencing_state_gate.md`
**Sources:** `V7.4_hard_gpt52/llm.json` (archive), `V7.5_lean/llm.json`, `V7.6_verify_state/llm.json`, `V7.7_set_callback_number/llm.json`, `diallux/prompts/*.md` · Machine-readable inventory: `reports/iter24_prompt_kb_inventory.json`

---

## 1. Headline answer

**YES — the design was executed, and it existed from the very first audited version (V7.4) through current production (V7.7), essentially unchanged.**

Every deployed version's state prompts contain inline KB references **joined to dynamic variables in the same line** — the exact pattern Julio describes. The literal `#[refer <kb> — <dv>]` grammar never existed (zero hits for `#[` in any source file), but the *semantic design* — "refer to this KB, anchored on this dv, at this instruction" — is present verbatim, using the syntax `Refer to ##<kb>## … {{<dv>}}`.

The receipts (identical lines in ALL of V7.4 / V7.5 / V7.6 / V7.7, per `llm.json` `states[].state_prompt`):

| State | The join (quoted) |
|---|---|
| **Discovery** | "Refer to ##discovery-bridge-kb## and pick the angle that matches {{interest_topic}}. Generate your question from their words + the angle — never read a question…" |
| **Discovery** | "Set {{interest_signal}} = true with `extract_discovery_details`. Use ##voice-ai-capabilities-kb## to tie the solution to {{pain_points}} and {{industry}}." |
| **Closer** | "Refer to ##sales-psychology-kb## pain-amplification techniques. Formulate 1–3 questions anchored on {{pain_frame}} and {{interest_topic}}…" |
| **Closer** | "For any objection, refer to ##sales-language-kb## and use its lines. Industry-fit objections pair with the matching {{industry}} sections of ##industry-kb##." |
| **Offer** | "If they ask price → refer to ##sales-language-kb## and defer all pricing to the live call. Never quote a number." |
| **Intake** | "For channel-specific handling, refer to ##call-context-kb##…" |
| **Closing** | "…Refer to ##call-closing-kb## for the exact goodbye…" |

Tally (from the inventory JSON): every deployed version carries **17 KB-name mentions, 5 KB↔dv same-line joins, 14 refer-style lines** across its states.

## 2. The deviation timeline — nothing was "inlined away"

- **The prompts are byte-stable across the era.** Word counts per state are identical V7.4 → V7.5 → V7.6 → V7.7 (Intake 449, Discovery 590, Closer 816, Offer 501; only ConfirmSlots/Booking/Closing grew slightly in V7.6/V7.7 for the verify/callback logic). The KB-referencing design survived every deploy untouched.
- **The craft was never copied into prompts.** Verbatim prompt↔KB overlap is **2 lines per version** (one discovery-bridge sample question, one call-closing line) — out of thousands of KB lines. There is no "the KB was pasted into the prompt" smoking gun at any version.
- **v5 inherited the design — and EXTENDED it:** v5 `diallux/prompts/` carries **28 KB mentions, 7 joins, 28 refer lines** (vs 17/5/14 deployed). v5-only additions:
  - `general_prompt.md` (a v5-only file, no deployed equivalent): "You receive inbound calls from prospects. Refer to ##call-context-kb##…", "When identifying challenges or handling objections, refer to ##sales-language-kb##. Use ##pain-points-kb## and ##sales-psychology-kb## for tailoring.", "When {{industry}} is identified, refer to ##industry-kb##…" ← a *fourth* KB↔dv join pattern.
  - `Discovery.md`: "…ANY question about HIPAA/PHI/compliance/security/privacy → pull ##hipaa-kb##…"
  - Discovery grew 590 → 727 words.

**Verdict: "executed from V7.4 onward, carried intact, extended by v5." No version inlined it away; no version deviated from it.**

## 3. Where the price-discipline language lived (count + locate only)

Identical in all 4 deployed versions and v5 (11 price lines deployed, 14 in v5):

- **Offer state** — explicit `### Pricing` block: "Defer all pricing to the live call — never quote a number." + "If they ask price → refer to ##sales-language-kb##…"
- **Closer state** — "No pricing — refer to ##sales-language-kb## and defer it all to the live call."
- **v5-only extra:** `Discovery.md` adds "Pricing deferral: vary the wording every time. If they push a second time, switch to the KB escalation line and the value anchor" — a deferral *escalation* rule the deployed lineage did not have. (Framing note for the GK `$1,200–$2,800` leak diagnosis: the never-quote rule existed in every version including v5's Offer/Closer; the leak is a compliance failure, not a lost rule. No fix proposed here per plan.)
- Note: the Closer "ballpark / $400, $650, $850 / ${{weekly_leak}}" lines are *prospect's* loss-figure math (sanctioned craft), not our pricing.

## 4. What this means for the framework

The tag framework (`#[refer <kb> for <what> — <dv>]` + per-state retrieval gate) is **NOT a restoration of a lost design — it is a formalization of a design that already ran in production since V7.4.** The follow-up implementation plan therefore:

1. **Ports the EXISTING phrasing** (`Refer to ##<kb>## … {{dv}}`), already battle-tested across 4 production versions — not improvisation. The `#[…]` grammar would be a syntax change on top of proven semantics.
2. **Closes the real gap:** the deployed design covers only Discovery/Closer/Offer/Intake/Closing. **Closer-adjacent states with no KB anchoring** (ConfirmSlots, Booking, contact_details, VerifyLead) are where the per-state *retrieval gate* (no retrieval in Closing/Booking/VerifyLead/ConfirmSlots) acts — those states have zero KB references AND zero need for them, yet iter23 measured heavy v5 pulling there.
3. **v5's general_prompt.md adds cross-state KB rules the deployed design deliberately never had** — candidate simplification target when implementing the gate (flagged only; no edits in this plan).

## 5. Raw per-state inventory

| Source | State | Words | KB-mentions | KB↔dv joins | Refer-lines | Verbatim-overlap | Price-lines |
|---|---|---|---|---|---|---|---|
| V7.4 | Intake / Discovery / Closer / Offer / contact_details / ConfirmSlots / Booking / Closing | 449/590/816/501/…/268/192 | 17 | 5 | 14 | 2 | 11 |
| V7.5 | same 8 states | identical to V7.4 | 17 | 5 | 14 | 2 | 11 |
| V7.6 | 9 states (+VerifyLead) | ConfirmSlots 414, Booking 280, Closing 192 | 17 | 5 | 14 | 2 | 11 |
| V7.7 | 9 states (+VerifyLead) | ConfirmSlots 466, Booking 295, Closing 203 | 17 | 5 | 14 | 2 | 11 |
| v5 prompts | 9 states + general_prompt | Discovery 727, Closing 277, Booking 439, general 1052 | 28 | 7 | 28 | 2 | 14 |

Per-state detail with quoted lines for every count: `reports/iter24_prompt_kb_inventory.json`.

---
*Every claim above carries a quoted line; sources are the four `llm.json` files + `diallux/prompts/*.md` on disk, extracted read-only by `scripts/iter24_audit.py`. Follow-up (tags + retrieval gate + fresh batch) is a separate plan, per Julio's sequencing decision.*
