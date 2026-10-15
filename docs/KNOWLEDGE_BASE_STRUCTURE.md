# Knowledge Base Structure — SOP for Retell Voice Agents

> **Purpose:** Define the standard for building and referencing knowledge bases (KBs) for
> multi-prompt Retell voice agents so that **chunk retrieval is ≥90% accurate** — the way the
> Dialux SDR retrieves its Sales Psychology / Industry KBs.
> **Applies to:** any Retell LLM with `knowledge_base_ids` + `kb_config` (top_k / filter_score).
> **Worked example:** Dialux SDR (the reference design) vs the V3.1 HVAC agent (what not to do).
> **Related:** `../agents/heating_uk/multiprompt/V3.1/KB_RAG_RESTRUCTURE.md`.

---

## 1. The core principle

> **One KB per retrieval intent. One chunk (`##` section) = one thing the agent needs at one
> moment, titled with the words the customer actually says. The prompt points to the KB;
> the KB delivers.**

Retrieval accuracy is decided by **chunk granularity and intent separation**, not by total token
count. The SDR has *more* tokens than the HVAC KBs yet retrieves *better* — because its chunks are
sharp (one industry per `##`), self-contained, and named in the prospect's language.

Three non-negotiable rules:

1. **One KB per retrieval intent.** Never mix *different retrieval intents* (vocabulary + brands +
   symptoms + regulatory — four different triggers) in a single KB: the embedding space gets
   crowded and `top_k` returns unrelated chunks. **But one intent can be a LARGE KB.** The SDR
   Industry KB is **7,107 tokens across 19 `##` chunks** and retrieves flawlessly — because every
   chunk answers the same retrieval question ("what's the pitch for this industry?"), each chunk is
   ≤ ~900 tokens, self-contained, and titled in the customer's language. A monolithic KB is fine
   *as long as it is moderated by chunking*: one intent, clean ≤900-token `##` chunks, proper
   embeddings. Size is not the problem — intent mixing is.
2. **One `##` = one retrievable unit.** The chunk title must match the query. If the customer says
   "no hot water", there must be a chunk literally headed `## No Hot Water`.
3. **The prompt directs retrieval.** Every KB is referenced from a prompt with a *trigger
   condition* ("when X happens → refer to `##kb-name##`"). Passive "populates automatically" KBs
   retrieve uncontrolled.

---

## 2. Why the SDR is the reference design

| Property | SDR implementation | Result |
|---|---|---|
| One topic per KB | Industry / Pain Points / Sales Psychology / Capabilities | query targets one small space |
| Chunk = query-named unit | `## Residential Roofing` … (19 industries, each an H2) | "roofing" → correct chunk |
| Chunk self-contained | pain + solution + ROI travel together | a single chunk is usable alone |
| Prompt pointer | "when {{industry}} identified → find matching KB" | retrieval is directed, not random |
| Small intent-scoped KBs / clean chunks | focused files + ≤900-token chunks; 0.6 filter rarely drops the right chunk | reliable top_k hits |

**Failure counter-example (HVAC V3.1):** `uk-hvac-trade-kb.md` (2,967 tok) mixes vocab, brands,
7 service types, 9 symptoms, regulatory and safety in one file; symptoms were `###` under a shared
`## Symptom Categories`. A "no hot water" query competed with 5 topics for the same 3 returned
chunks → symptom chunk frequently missed.

---

## 3. Standard KB anatomy

### 3.1 File structure

```markdown
# <KB Name> — <One-line purpose>

> Retrieved when: <trigger condition — what prompt event fires this KB>

---

## <Chunk Title in Customer Words>          ← retrieval unit (the QUERY target)

<what to say / the fact — plain, complete, self-contained>

<optional sub-structure under this chunk (###) — only ever sub-details of THIS chunk>

---

## <Next Chunk Title in Customer Words>

...
```

### 3.2 Heading hierarchy = chunk boundaries

| Heading | Role in retrieval |
|---|---|
| `#` (H1) | Document identity — appears once |
| `##` (H2) | **THE RETRIEVAL UNIT.** Each H2 should be something the agent needs independently |
| `###` (H3) | Sub-detail *within* a chunk — steps, examples, sub-rules of the H2 it sits under |
| `####` (H4) | Rarely needed; avoid (invites over-fragmentation) |

**Rules:**
- Put **one retrievable fact per H2**, never several facts sharing one H2.
- H3 content only ever refines its parent H2 — never introduces a new retrievable topic.
- Keep each H2 **self-contained**: even if it is the only chunk returned, it must be usable
  (phrase + rules + constraints all together).

### 3.3 Chunk titles = the customer's words

Titles must mirror how a caller phrases the moment, **not** internal taxonomy:

| ❌ Internal label | ✅ Customer-language title |
|---|---|
| `## Symptom Category #3` | `## No Hot Water (Heating Still Works)` |
| `## Service Types` | `## Annual Boiler Service` |
| `## Industry Vertical B` | `## Residential Roofing & Solar Installers` |
| `## Names List` | `## M — Marcus, Maria, Michael …` |

If the title contains the words the caller uses, semantic + lexical similarity both fire, and the
`filter_score` threshold is cleared reliably.

### 3.4 What belongs together in one chunk

- A **phrase the agent must say** + when to say it + what not to say. (SDR symptom chunks carry
  approved phrase + questions + do-not-say; SDR industry chunks carry pain + solution + ROI.)
- A **rule** + its trigger + its exception.
- A **lookup group** (e.g., names by first letter) stays together as one chunk per letter.

---

## 4. Chunking by content type

### 4.1 Procedural / decision-tree content (KEEP as ONE chunk)
If the content is a *sequence* the agent must follow start-to-finish (a 3-path name-confirmation
flow, a 4-step closing structure), keep the whole flow under **one H2** with `###` steps inside.
Do **not** split the steps into separate H2s — retrieval would return a fragment of the procedure.

### 4.2 Lookup-table content (names, brands, enums)
Semantic retrieval of a lookup is the wrong mechanism unless groups are chunk-sized:
- Split the lookup into **alphabetical / categorical H2 groups** (`## M — …`, `## Brands A–F`).
- Then a query like "Marcus" lands on the `## M` chunk, and the agent does the 100% match check
  against the retrieved group.
- If the whole list must be checked every time, consider a **tool** (lookup) instead of a KB.

### 4.3 Numbered-fact content (prices, fees, hours)
Small and exact — keep the whole thing in 1–3 chunks, one H2 per fact cluster
(`## Call-out fees`, `## Opening hours`). Facts are retrieved poorly under a 0.6 filter if they
live in prose; put them in short, labelled chunks.

### 4.4 Tables
Avoid retrieval-critical tables. Embeddings chunk tables poorly and the text cell may not match the
query. Convert "Customer says X → Classify as Y" tables into H2 chunks titled with X.

---

## 5. KB sizing, `kb_config`, and why big KBs are safe

Retrieval settings (per LLM): `kb_config = { "top_k": N, "filter_score": F }`. Project default:
`top_k 3, filter 0.6`.

- **Chunk cap: ≤ ~900 tokens per `##` chunk.** This is the hard-ish ceiling. Above it a chunk's
  embedding dilutes — its semantic centre drifts across sub-topics, its similarity to any single
  query drops, and it can slip below `filter_score`. At ≤900 tokens a chunk stays one coherent unit.
  (SDR industry chunks average ~370 tokens — comfortably inside.)
- **`filter_score 0.6` runs on CHUNKS, not on the KB — so total KB size is irrelevant to retrieval
  success.** A 10,000-token KB structured as 20 × 500-token intent-aligned chunks behaves like 20
  small KBs for scoring. A query like "no hot water" is embedded and compared against **each
  chunk's** embedding. The `## No Hot Water` chunk matches at high similarity (≥0.6) because its
  title and body share the query's words, so it is returned regardless of how big the file is.
  Token count never enters the similarity math — **chunk homogeneity and title↔query overlap do.**
  That is precisely why the 7,107-token SDR Industry KB is "well within parameters": 19 clean
  chunks, each scoring independently.
- **Big KBs are only a problem when they mix intents.** A 3,000-token KB with 5 topics → top_k 3
  samples across topics and the right chunk is starved. A 3,000-token KB with ONE intent and clean
  ≤900-token chunks → behaves as a set of focused retrievals. Same size, different outcome.
- **Prefer small intent-scoped KBs** for maintainability, but **do not split a single intent
  artificially** — a monolithic intent KB is legitimate.
- **Re-tune `top_k`/`filter_score` only after the structure is fixed** — never tune config to
  compensate for bad chunking.

### 5.1 When to merge vs keep separate

The decision rule is: **"do I need the whole thing when this triggers, or is a slice enough?"**

- **Keep tiny KBs separate (whole-file value).** A 100–200 token KB is below chunk size — retrieval
  returns it as one whole chunk, a binary switch. If the trigger fires, the ENTIRE content loads.
  That is the highest-accuracy retrieval possible. Merging several tiny KBs converts guaranteed
  whole-file loads into a top_k 3 sampling competition and can only lower fidelity.
  → Keep separate: fees (96 tok), company facts (73 tok), gas script (~200 tok), sample phrases,
  interaction rules, closing (970 tok).
- **Merge only a related family triggered together** — e.g. sample-phrases + interaction-rules +
  closing could become one "conversation-style" KB (~1,400 tok) *only if* each topic stays its own
  `##` chunk and you accept top_k slicing. Never merge different-intent topics (fees + symptoms).
- **Chunked KBs stay per-intent.** Symptoms, industries, address, names — big slice-based content —
  earn their own KB per retrieval intent.

---

## 6. Naming convention (critical hygiene)

- **KB name === the marker referenced in prompts.** A prompt that says `##v2-gas-mention-kb##`
  must reference a KB actually named `v2-gas-mention-kb`. (HVAC bug: prompt referenced
  `##v2-gas-mention-kb##` while the file was `kb_v2_emergency_gas.md`.)
- Use **`topic-kb`** style slugs: `symptom-questions-kb`, `service-types-kb`, `call-closing-kb`.
- The marker in the prompt and the KB name must match **exactly** — case-sensitive.
- Avoid alarming words in names (e.g., "emergency") when a neutral term works; the name is
  surfaced to the model in some contexts.

---

## 7. How to reference a KB from a prompt

### 7.1 The trigger pattern (use in main and state prompts)

```markdown
# When to use each knowledge base

## <What the trigger is>
When the caller <condition>, refer to ##<kb-name>##.

## <Next trigger>
When the caller <condition>, refer to ##<kb-name>##.
```

### 7.2 Good trigger lines (from the Natural Speech main prompt)

```
## Fees & call-out charge
When the caller asks about a fee or call-out charge, refer to ##v2-fees-kb##.
Don't volunteer the fee unless asked.

## Gas
If the caller mentions gas, smell, or a leak, refer to ##v2-gas-mention-kb##.
```

### 7.3 Trigger rules
- **One trigger per line**, with a *discrete event* condition ("when they ask for a fee", "when
  they mention gas") — pull-only-when-needed.
- Put **global triggers** (phrasing, off-script, fees, closing) in the **main/general prompt**.
- Put **state-specific triggers** (symptom KB in triage, address KB in the address state, day-logic
  in slot selection) in the **state prompt** that owns that step.
- If the model must *form the query*, say so explicitly: "query the KB for the exact symptom the
  caller described." (~15 tokens, worth it.)

### 7.4 The prompt↔KB contract
- **Prompt = voice + flow + triggers** (light, ~700–1,000 tokens ideal).
- **KB = domain content**, one retrieval intent per file, chunked per Section 3.
- Prompt points *when*; KB answers *what*. Neither tries to do the other's job.

---

## 8. Anti-patterns checklist (do not do these)

- [ ] **Mega-KB with MIXED intents:** vocabulary + brands + symptoms + regulatory in one file —
      four different triggers competing for the same top_k slots. (A large SINGLE-intent KB, e.g.
      the 7,107-token 19-industry SDR KB, is fine.)
- [ ] **Buried H3 retrievables:** putting distinct retrievable facts under one shared H2.
- [ ] **Internal-label titles:** "Symptom Category #3" instead of "No Hot Water".
- [ ] **Lookups in prose:** a names/brands list not grouped into retrievable H2 chunks.
- [ ] **Retrieval-critical tables** instead of titled chunks.
- [ ] **Marker/name mismatch:** prompt references a KB name that doesn't exist.
- [ ] **Passive KBs:** "retrieved automatically" with no trigger line directing the query.
- [ ] **Splitting decision trees** into separate H2 chunks (breaks the procedure).
- [ ] **Merging tiny whole-file KBs into one chunked KB** (converts guaranteed whole-file loads into
      top_k sampling competition — a fidelity downgrade, not cleanup).
- [ ] **Chunks over ~900 tokens** (diluted embeddings → scores fall under `filter_score`).
- [ ] **Tuning `kb_config` to fix bad structure** (fix structure first).
- [ ] **Patching KBs in place on a live LLM** — create new KBs → new LLM → wire → test.

---

## 9. New-KB build checklist (SOP)

1. **Name it** `topic-kb` (slug matching the prompt marker you'll write).
2. **One retrieval intent** per file — if a second, differently-triggered topic appears, split it
   out. (A single intent may be large: 19 industries in one KB is legitimate.)
3. **Write each retrievable unit as its own `##`** titled in customer language.
4. **Self-contain each chunk** (what to say + when + do-not-say together).
5. **Group lookups** into retrievable categories (alphabet/category H2s).
6. **Keep decision trees as one chunk**, steps as `###`.
7. **Keep every `##` chunk ≤ ~900 tokens**; keep tiny KBs (≤ ~300 tokens) standalone so they
   retrieve whole-file.
8. **Reference it from the prompt** with a discrete trigger line; use the exact KB name.
9. **Verify the KB name exists** in the account (`list-knowledge-bases`).
10. **Rebuild as a NEW LLM** (never patch), point at the new KBs, run LLM-to-LLM probes.

---

## 10. Acceptance test (how we know retrieval is ≥90%)

For each prompt trigger, an LLM-to-LLM run must show:

1. The **right chunk content** surfaces in the agent's behaviour — e.g., caller says
   "no hot water" → agent speaks the "No Hot Water" approved phrase verbatim.
2. The agent **never improvises** content that should come from a KB (fee, gas script, closing
   structure, symptom phrase).
3. Edge lookups land: rare name → spelling path fires; brand mention → `boiler_make` captured.
4. Every prompt marker resolves to a real, correctly-named KB.
5. No chunk *below* `filter_score` causes silence — tune titles/size, not the filter.

---

## 11. Quick reference — SDR-style layout template

```markdown
# <Topic> Knowledge Base

> Retrieved when: <trigger> | KB name: <topic>-kb

## <Chunk 1 — customer-language title>
- what to say / fact (self-contained)
- constraints / do-not-say
### <sub-detail only of chunk 1> (optional)

## <Chunk 2 — customer-language title>
- ...
```
