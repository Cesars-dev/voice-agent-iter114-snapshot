# How to Build a Multi-Prompt Voice Agent in Retell AI

> Build SOP — derived from the `heating_uk` production agent and Mark's Voice AI Starter Kit.
> For testing, see `TESTING-AND-REFINEMENT-SOP.md`. For platform reference, see `docs/platform/`.


---

## Overview

A multi-prompt (state machine) agent uses:

```
general_prompt (shared context, always loaded)
  └── states:
        ├── [starting_state]
        │     state_prompt: per-state instructions
        │     tools: extract_dv, custom webhooks, etc.
        │     edges: [{destination, description}]
        ├── [state_2]
        └── [state_3] (terminal — no edges)
```

Transitions are **deterministic** and driven by the **edge JSON schema** on each state. Each edge carries a
`description` stating the transition condition (e.g. *"transition when X, Y, Z are complete"*), its `required`
lists the state's data vars **plus a `*_completed` boolean**, and the state prompt ends with a small string
telling the LLM to fire `transition_to_<next>` once that completion flag is set. The `*_completed` boolean is
the gate that makes the transition deterministic — it is set true only after the state's work is done, so the
LLM can never skip a step or fire the transition early.

> ## ⚠️ DEPRECATED — NEVER use the "LLM freely decides when to transition" design
>
> There are **two** ways to drive state transitions in Retell:
>
> | | Mechanism | Verdict |
> |---|---|---|
> | **1. LLM decides the transition freely** (no deterministic `*_completed` gate; the LLM picks when to move from a loose prompt) | Retell's newer/suggested way | ❌ **DEPRECATED — DO NOT USE.** Probabilistic; the LLM can skip state work or fire `end_call` mid-flow. |
> | **2. Edge JSON transition schema, gated by a `*_completed` boolean** | ✅ **REQUIRED — deterministic, works every time. This is how we build.** |
>
> **Why mechanism 1 is forbidden:** a transition with no deterministic gate is **probabilistic** — the LLM
> *decides* when to move and can skip state work or fire `end_call` mid-flow (the exact root cause of the Dialux
> 6.1 "no-booking" failures). It also pairs with `end_call` in `general_tools`, so the agent can end the call
> from any state.
>
> **The only accepted transition design (mechanism 2):**
> 1. Each non-terminal state has its `extract_*_details` tool and an **edge** whose `required` lists the data
>    vars **plus a `*_completed` boolean** (e.g. `intake_completed`).
> 2. The edge carries a **`description`** that states the transition condition, e.g.
>    `"When intake_completed, inbound_channel, interest_topic, pain_frame are all filled, transition to Discovery."`
>    This description **must match the prompt**.
> 3. The state prompt ends with a small transition string that mirrors the description, e.g.
>    `"Then call transition_to_Discovery once the completion flag is set."` This tells the LLM it is time to fire
>    the transition and to read the edge's description (its conditions) before doing so.
> 4. The `*_completed` flag is set to true **only after all the state's required data is gathered** (per the
>    state prompt). Because the edge's `required` includes that boolean, the transition fires **deterministically**
>    once the boolean + data vars are present — the LLM cannot fire it early and skip state work.
> 5. `end_call` lives only in `general_tools` / the main prompt — never a per-state tool.
> 6. Reference pattern: `heating_uk` V3.1 and `Dialux V6.3` / `V6.4`.

---

## ⛔ CRITICAL RULE — KNOWLEDGE BASES (PERMANENT)

> **NEVER create a knowledge base without explicit user approval. Never. Ever.**
>
> - Do **not** create new KBs or inject unregistered ones at deploy time on your own. The S3 bucket backing Retell KBs
>   charges an insane amount of money, and unused/duplicate KBs bleed the account.
> - The **only** exception: the user explicitly authorizes it — e.g. *"we are deploying a new knowledge base,
>   this is the name"* — then (and only then) create it.
> - Otherwise: **reuse existing KBs only** (via the registry / `knowledge_base_ids` references). Never add a KB
>   that isn't actively used. Deploying an LLM points at existing `knowledge_base_ids` — that is free; it does
>   **not** re-upload or re-charge the KB.

### KB lifecycle — reuse / upsert / create (enforced by `kb_registry.json`)

> Reference of truth: `config/kb_registry.json` next to each `config/llm.json`.
> Format: `{"<kb_slug>": {"kb_id": "knowledge_base_...", "sha256": "<sha256 of content>"}}`.
> Full policy with audit commands: [`AGENTS.md`](../AGENTS.md) → "⛔ KB POLICY".

| Situation | Action | API cost |
|---|---|---|
| slug in registry + content unchanged | **REUSE** — reference registered `kb_id`, touch nothing | zero |
| slug in registry + content changed (`sha256` mismatch) | **UPSERT** the SAME id via the deploy script: delete its sources (`delete-knowledge-base-source/{kb_id}/source/{source_id}`), then `add-knowledge-base-sources/{kb_id}` | content storage only |
| slug NOT in registry | **STOP. Do not create.** Ask the user. Create only on explicit authorization — see validation gate below | new S3 + KB |

**Validation gate before ANY first-time creation (mandatory, no exceptions):**

1. **Dedupe check:** `GET /list-knowledge-bases` — confirm no existing KB covers the same content under a
   different slug (this is how ~171 duplicate KBs / ~$250 were wasted before 2026-08-21).
2. **Content review:** show the user the exact `.md` that will be uploaded; get sign-off on name + content.
3. **Necessity check:** confirm the content cannot live in an existing KB as a new section instead.
4. Only after all three: create via the deploy script (which registers slug + `sha256` immediately) — never
   via a hand-rolled `POST /create-knowledge-base`.

**Never** hand-write KB calls outside `deploy_chat.py`'s registry resolution. Modifying KB *content* is an
upsert of the same id and is always allowed; creating a *new* KB id is what requires explicit authorization.

---

## Phase 0: Spec

Fill in a [Meta-Orienter Template](testing/META-ORIENTER-TEMPLATE.md) first. You need:

- Who calls (caller persona, context)
- What the agent does (scope, out-of-scope)
- How it sounds (voice character, tone, verbal fillers)
- Call flow (high-level steps, decision logic, transfer triggers)
- Client info (business name, hours, calendar API key, services)
- Success metrics
- Anti-patterns to test for

Do not skip this. The spec drives everything downstream.

---

## Phase 1: Decompose the Conversation into Nodes

Break the conversation into discrete nodes (Retell "states"). Each node is one self-contained step in the flow.

### Constraints

| Dimension | Target | Pushing it | Hard ceiling | Failure mode |
|-----------|--------|------------|--------------|--------------|
| Token budget per prompt | 1200 | 1500 | 2000 | Hallucination, drift |
| Steps per node | 3-5 | 5-8 | 8+ | Hallucination |
| Complex tools per node | 1-2 | 3 | 4 | Wrong tool called |
| Simple extract_dv per node | 2-3 | — | 4 | Latency, wrong extraction |

### Split rule

If any prompt exceeds 2000 tokens, it MUST be split. BUT each half must be **functionally self-contained** — it must serve a clear step in the conversational flow.

Better: one 500t specific node + one 1700t focused node.
Worse: two 1100t lobotomized nodes that can't operate independently.

### Variable inventory

Before writing prompts, create the full variable table:

```
name              type    set-in-state   used-in-state
───────────────   ─────   ─────────────  ──────────────
customer_name     text    greeter        address, booking
intent_confirmed  boolean greeter        greeter
address_postcode  text    address        booking
...
```

Walk every variable: is it set BEFORE it's used? If not, restructure.

### Edge schema

Define the edges between states. The edge carries a `description` that states the transition condition, plus a
`*_completed` boolean in `required`:

```json
{
  "name": "intake",
  "edges": [{
    "destination_state_name": "discovery",
    "description": "When intake_completed, inbound_channel, interest_topic, pain_frame are all filled, transition to Discovery.",
    "parameters": {
      "type": "object",
      "properties": {
        "inbound_channel": { "type": "string" },
        "interest_topic": { "type": "string" },
        "pain_frame": { "type": "string" },
        "intake_completed": { "type": "boolean" }
      },
      "required": ["intake_completed", "inbound_channel", "interest_topic", "pain_frame"]
    }
  }]
}
```

The state prompt mirrors the edge `description` with a small transition string, e.g.
`"Then call transition_to_Discovery once the completion flag is set."` The `*_completed` boolean is set true only
after the state's work is done; because it is in the edge `required`, the transition fires **deterministically**
once the boolean + data vars are present.

> **⚠️ Edge schema + `*_completed` gate = THE transition mechanism (mandatory).**
> - Include the `*_completed` boolean in the edge's `required`, and have the state prompt tell the LLM to fire
>   `transition_to_<next>` once the completion flag is set. The edge `description` must match that prompt string.
> - List ONLY the variables actually required at that transition. Never add not-yet-known data vars (e.g.
>   `first_name`, `callback_number`, `industry`) to an early edge's `properties` — the LLM fills them with `null`,
>   which clobbers `collected_dynamic_variables`. Each edge declares only what it truly needs.
> - Do NOT use `trigger_*_transition` tools. Do NOT let the LLM decide the transition with no boolean gate
>   (deprecated mechanism 1 — probabilistic; see the DEPRECATED warning in Overview).

---

## Phase 2: Write Prompts

You write 1 general prompt + 1 state prompt per node. Below are the three archetypes with **real production sizes**.

### Prompt #1: General Prompt (shared context)

**Purpose:** Loaded in every state. Contains ONLY shared context — identity, voice, rules, company briefing, KB references.

**Real production size:** ~2700 tokens — this violates the 1500 rule and MUST be split in practice.

**What goes here (when split properly, ~1000-1200 tokens):**

```
SECTION              TOKENS    CONTENT
───────              ───────   ────────────────────────────────────────
Core Principle        ~120     Listen → Acknowledge → Execute loop.
                               "Your job has three steps, in order,
                               on every turn."

Identity               ~80     Name, company, role. "You are Tom,
                               virtual receptionist at British Heat
                               Services. You never flap."

Voice                  ~100    Accent, sentence length (max 15 words),
                               one question per turn.
                               Use "right/lovely/brilliant" sparingly.

Smart Interaction      ~100    Mirror before you ask. Match energy.
Rules                          Roll with off-script. Let them finish.

RULES (numbered)       ~350    Knowledge boundary: NO general knowledge.
                               Use KBs. Diagnostic restraint: name
                               problem category, NEVER name parts.
                               Fee rules: only if asked. Emergency
                               scripts: gas, hostility, hold-on.
                               UK trade vocabulary: say/never-say.
                               What You Never Do list.

Company Briefing       ~150    Business name, address, Gas Safe number,
                               engineer names, hours, years in business.

KB References           ~40    ##trade-kb##, ##address-kb##
```

**What moves OUT of general and INTO state prompts or KBs:**

| Content | ~Tokens | New home |
|---------|---------|----------|
| Variable handling table (all 19 vars) | ~200 | Each state prompt gets only its relevant subset |
| Sample phrases (< > library) | ~250 | KB or each state prompt as needed |
| Address field mapping | ~80 | Address state prompt |
| Tone target elaboration | ~100 | Identity section (keep it tight) |
| Multi-shot sample conversations | ~400 | KB (not prompt) |

### Prompt #2: Simple State Prompt (e.g., "greeter")

**Purpose:** Minimal extraction, few steps, light branching.

**Real production size:** ~760 tokens — fits 1200 target easily.

```
SECTION              TOKENS    CONTENT
───────              ───────   ────────────────────────────────────────
MISSION                ~40     "Greet, listen, mirror, classify,
                               prepare for transition. Read the room:
                               brisk → get to point, flustered →
                               give a beat."

Variable Extraction    ~60     "Call extract_user_details when user
                               volunteers info. Critical: don't set
                               intent_confirmed until mirrored + agreed."

Step 1: Greet         ~100     CHECK {{greeting_exchanged}}?
                                 YES → skip
                                 NO → greet + CALL extract_dv (EXTRACT NOW)

Step 2: Listen &      ~200     Mirror in ≤10 words. Inline dialogue:
Mirror                         <Right, no heating since morning —
                                that's a pain>
                               CALL extract_dv for intent_clear
                               CALL extract_dv for classified_intent
                               SCAN for any volunteered address/symptom
                               → extract immediately

Step 3: Confirm       ~80      CALL extract_dv for intent_confirmed.
Intent                         Silent via tool. Don't restate back.

Edge Cases            ~100     "Are you a real person?"
                               "Asked about the company"
                               Inline dialogue for each.

CRITICAL RULES        ~150     Always / Never list.
                               Check before ask. Extract incrementally.
                               Never stack. Don't diagnose — next state.
```

### Prompt #3: Complex State Prompt (e.g., "booking")

**Purpose:** Multiple tools, branching, hard limits, edge cases.

**Real production size:** ~1860 tokens — over 1500 target but under 2000 ceiling. Borderline; keep unified if functionally coherent.

```
SECTION              TOKENS    CONTENT
───────              ───────   ────────────────────────────────────────
MISSION                ~50     1. Pull slots  2. Offer 1-2  3. Confirm
                               4. Book  5. Close
                               CRITICAL LINE: "The address is already
                               captured. Use it as-is. Do NOT re-ask."

Time Context           ~60     {{current_time_Europe/London}}
                               {{current_calendar}}
                               Relative time: tomorrow, next week

Time Format Rules      ~40     UK-friendly. "Tuesday morning", "between
                               8 and 12". Never 24-hour.

TOOL:                  ~150    Exact payload. "First call: today +1 day
check_availability             window. If rejected, ask caller. Function
                               returns slots — pick 1-2 closest, don't
                               dump all 20."

TOOL:                  ~150    Exact field mapping with {{var}}
book_calendar                  interpolation. Notes format includes
                               customer + address + symptoms.

Steps 1-6             ~600    Step 1: CALL check_availability
                               Step 2: Offer 1-2 closest slots
                               Step 3: Hard rule — MAX 5 offers, then
                                       callback (CAPS, non-negotiable)
                               Step 4: Confirm — readback full booking
                               Step 5: CALL book_calendar. Fail? Retry
                                       once → "Mike will call"
                               Step 6: Close warmly. CALL end_call

Fee Rules (if asked)  ~100    Exact scripts. Weekday £75+£50.
                               Evening £120+£50. Never volunteer.

CRITICAL Reminders     ~100    "MAX 5 offers, then callback"
                               "Address already captured — do NOT re-ask"
                               "Engineer confirms on site"
```

### General prompt split strategy

When your general prompt exceeds 2000 tokens (heating_uk hits ~2700):

1. **Trim the general prompt** to ~1000 tokens — keep only: Identity, Voice, Core Principle, RULES (condensed), Company Briefing, KB references.

2. **Distribute** the removed sections into each state prompt:
   - Variable table → each state gets only its relevant variables
   - Sample phrase library → KB (referenced via `##phrases-kb##`)
   - Multi-shot conversations → KB
   - Address mapping → address state prompt only

3. **Result:**
   ```
   Before:  general (2700) + greeter (760) + address (1775) + booking (1860) = ~7100
   After:   general (1000) + greeter (900) + address (1500) + booking (1500)  = ~4900
            + KB (phrases + conversations) = ~1500 (loaded on demand, not always)
   ```

### Every prompt — mandatory rules

```
FORMATTING:
  - Use contractions: I'm, you're, don't, we've
  - Number all rules. Number all steps.
  - CAPS for critical instructions only
  - Inline dialogue with <angular brackets> for sample phrases
  - IF/THEN for all branching
  - One rule per line
  - "use um and uh in every other sentence"
  - "if confused: 'um, I think I lost you for a second, you there?'"

STRUCTURE:
  - Step 1 starts with CHECK: "Is {{variable}} already set?"
    YES → skip. NO → execute.
    This makes every state prompt idempotent.
  - MISSION line: "Your job in THIS state is X"
  - EXTRACT NOW markers when the LLM should call extract_dv

EVERY PROMPT MUST HAVE:
  - "Hold on" handler: "NO_RESPONSE_NEEDED"
  - "How are you" handler: ask it back
  - Graceful exit: one sentence, no convincing
  - Hostile caller: one dismiss sentence → end_call
```

---

## Phase 3: Build Knowledge Bases

Write KBs as `.md` files. Each is uploaded to Retell independently.

```
KB file structure:
  - Pure reference content. No instructions to the agent.
  - Referenced in prompts via ##kb-name##
  - Agent retrieves from KB automatically based on conversation context

Config:
  top_k: 3
  filter_score: 0.6

Use KBs for:
  - Trade/domain knowledge (boiler types, error codes, service offerings)
  - Address handling rules
  - Sample conversation library (multi-shot prompting)
  - Company policies, service areas, pricing tables
  - Anything that would bloat the prompt beyond 1500 tokens
```

---

## Phase 4: Define Tools

### Tool types available

| Type | When to use |
|------|-------------|
| `end_call` | Every agent. Global tool. |
| `transfer_call` | When escalation to a human is needed |
| `extract_dv` | Per-state variable extraction. Max 2-4 per state. |
| `check_availability_cal` | Cal.com slot query. Complex — max 2 per state. |
| `book_appointment_cal` | Cal.com booking. Complex — max 2 per state. |
| `custom` (webhook) | Any project-specific logic. Needs URL + parameters schema. |
| `send_sms` | SMS fallback for address/email collection |
| `press_digit` | DTMF input collection |
| `code` | Execute custom code snippets |

> ⚠️ **DO NOT use Retell's native `check_availability_cal` — it is broken for slot booking.**
> It returns collapsed **range windows** instead of discrete slot times, and its `time` payload
> format causes the 1-hour offset bug. We use **our own custom webhook** instead.
>
> - **Slot query:** custom function `check_availability` → `POST https://slots.diallux-ai.site/check_availability` (returns discrete `{day,time,iso}` slots)
> - **Booking:** keep native `book_appointment_cal` (name `book_calendar`) but pass the slot's **bare local `time`** + `timezone: Europe/London` — never the UTC `iso`.
>
> **Full integration guide:** [`docs/custom_endpoints.md`](custom_endpoints.md)
> Service docs: `cal_slots_endpoint/README.md`, `ARCHITECTURE.md`, `FINDINGS.md`; `time_endpoint/README.md`, `ARCHITECTURE.md`.

### Tool limits per state

| Tool category | Max per state | Reason |
|---------------|---------------|--------|
| Complex (webhook, calendar, routing, booking) | 2 | LLM gets confused which to call |
| Simple extract_dv | 4 | Token cost + latency. 4 OK if all trivial (e.g., `extract_name`, `extract_phone`). |
| Mixed (2 complex + 1-2 simple) | 3-4 | Manageable. Beyond 4 → hallucination. |

### Custom webhook tools

Each custom webhook needs its schema described in TWO places:

1. **In the prompt** (natural language):
   ```
   You have a tool called `verify_address`. Call it when the customer
   gives you a UK postcode. Pass the postcode as `postcode` and the
   house number as `house_number`. It returns "valid" or "invalid".
   ```

2. **In the tool definition** (JSON schema):
   ```json
   {
     "type": "custom",
     "name": "verify_address",
     "description": "Verify a UK postcode and house number",
     "url": "https://your-webhook.com/verify-address",
     "speak_after_execution": true,
     "parameters": {
       "type": "object",
       "properties": {
         "postcode": { "type": "string", "description": "UK postcode" },
         "house_number": { "type": "string", "description": "House number or name" }
       },
       "required": ["postcode"]
     }
   }
   ```

   Both descriptions must agree. The prompt tells the LLM WHEN and WHY.
   The schema tells the LLM HOW (parameter types, required fields).

---

## Phase 5: Assemble the LLM Payload

Full payload structure:

```json
{
  "model": "gpt-5.4",
  "model_temperature": 0,
  "tool_call_strict_mode": true,
  "general_prompt": "<general_prompt text>",
  "general_tools": [
    { "type": "end_call", "name": "end_call", "description": "End the call politely." }
  ],
  "begin_message": "British Heat Services, Tom speaking — how can I help?",
  "starting_state": "greeter",
  "states": [
    {
      "name": "greeter",
      "state_prompt": "<greeter prompt text>",
      "edges": [{
        "destination_state_name": "address",
        "description": "All greeter variables populated"
      }],
      "tools": [{
        "type": "extract_dv",
        "name": "extract_user_details",
        "description": "Extract caller info when volunteered",
        "variables": [
          { "name": "customer_name", "type": "text", "description": "Full name" },
          { "name": "intent_confirmed", "type": "boolean" }
        ]
      }]
    },
    {
      "name": "address",
      "state_prompt": "<address prompt text>",
      "edges": [{
        "destination_state_name": "booking",
        "description": "All address variables populated"
      }],
      "tools": [{ "type": "extract_dv", "name": "extract_address_details", ... }]
    },
    {
      "name": "booking",
      "state_prompt": "<booking prompt text>",
      "edges": [],
      "tools": [
        { "type": "check_availability_cal", "name": "check_availability", ... },
        { "type": "book_appointment_cal", "name": "book_calendar", ... }
      ]
    }
  ],
  "knowledge_base_ids": ["kb_trade_id", "kb_address_id"],
  "kb_config": { "top_k": 3, "filter_score": 0.6 }
}
```

### Critical — caching bug

**NEVER PATCH an existing LLM.** Retell's `PATCH /update-retell-llm` stores the update but does NOT propagate to runtime. Always `POST /create-retell-llm` with the full payload, then point your agent to the new LLM ID.

### Dialux SDR V6.x — canonical build + deploy chain

> The generic `build_deploy.py` pattern above is superseded for Dialux by this exact chain.
> Folders are canonical; `llm.json` is generated, never hand-edited.

```
EDIT (folders are truth)          BUILD (byte-parity)               DEPLOY (new LLM each time)
prompts/general_prompt.md    ─┐
prompts/begin_message.txt     ├─►  python3 tools/build_llm_json.py  ──►  cp llm.json
tools/*.tools.json            │    · validates JSON schemas          ─►  "6.1 iterations/MVP_agent/
edges/*.edges.json            │    · enforces token ceilings             config/llm.json"
"Knowledge bases"/*.md        │    · resolves ##kb## markers        ─►  cd MVP_agent
(prompts/states/<N>.md)       ┘    · verifies edge destinations          python3 deploy_chat.py
                                   · parity-asserts embed==folder        (reuses/upserts KBs via
                                                                          config/kb_registry.json;
                                                                          creates NEW llm + chat agent)
```

| Step | Script | Notes |
|------|--------|-------|
| Build | `Dialux_SDR/V6.6/tools/build_llm_json.py` (`--check` = validate only) | Reassembles `llm.json` from folder sources byte-for-byte; top-level settings carried over untouched. **Never hand-edit `llm.json`.** |
| Deploy | `"Dialux_SDR/6.1 iterations/MVP_agent/deploy_chat.py"` | Creates NEW LLM + NEW chat agent. KB resolution: REUSE / UPSERT per registry — never CREATE without explicit user authorization (see KB lifecycle above). |
| Test | `"…/MVP_agent/tools/test_llm_to_llm.py"` | Point at the new agent via `export CHAT_AGENT_ID=<id>` — the script stays ID-agnostic. |
| FORBIDDEN | `MVP_agent/build_llm.py` | Regenerates from V1-era `JSON/states/` sources — running it clobbers the V2+ lineage. |

State transition definitions live in `edges/*.edges.json` and are rebuilt verbatim by the builder —
**never edit transitions inside `llm.json` directly**; edit the folder file and rebuild.

---

## Phase 6: Deploy

```python
# build_deploy.py — minimal pattern
Step 1: POST /create-retell-llm     → llm_id
Step 2: PATCH /update-chat-agent    → point chat agent to new llm_id
Step 3: POST /create-agent          → agent_id (voice config)
         { voice_id, responsiveness, backchannel, denoising, stt }
Step 4: POST /publish-agent-version/{id}    → LIVE
Step 5: Register in AGENTS.md
```

The **chat agent** is for text-based testing (no phone minutes). The **voice agent** is for production calls. Both can point at the same LLM.

Testing flow: deploy to chat agent → test → fix → create new LLM → point chat agent → test → ... → once clean → create voice agent → publish.

---

## Phase 7: Test — HUMAN IN THE LOOP

**STOP.** Do not proceed without explicit approval.

You must present the completed agent (prompts, state machine, tools, KBs, LLM payload) to the user and get sign-off before any testing begins.

Once approved, hand off to `docs/testing/TESTING-AND-REFINEMENT-SOP.md` for the full procedure.

---

## Phase 8: Production

```
1. Create production voice agent (separate from test)
2. Publish
3. Assign phone number
4. Set up webhook (if multi-company injection)
5. Register in AGENTS.md with LIVE status
6. Delete deprecated agents
```

---

## Quick Reference Rules Card

```
┌─────────────────────────────────────────────────────────────────────────┐
│ TOKENS                                                                  │
│   1200 target  1500 max  2000 hard ceiling                              │
│   General prompt counts too — split via KB distribution if >2000       │
│                                                                         │
│ STEPS PER NODE                                                          │
│   3-5 optimal  5-8 pushing it  >8 prone to hallucination                │
│                                                                         │
│ TOOLS PER NODE                                                          │
│   Complex (webhook, calendar, routing): 2 max                          │
│   Simple extract_dv: 4 max (only if all trivial)                       │
│   Mixed: 3-4 total                                                      │
│                                                                         │
│ SPLIT RULE                                                              │
│   >2000 tokens → split. Each half must be FUNCTIONALLY self-contained.  │
│   Better: 500t + 1700t than 2 x 1100t lobotomized.                     │
│                                                                         │
│ TRANSITIONS                                                             │
│   Deterministic via EDGE JSON SCHEMA + a *_completed boolean gate.      │
│   Edge: description states the condition; required = data vars + the    │
│   *_completed boolean. Prompt: small string to fire transition_to_<next>│
│   once the completion flag is set. Edge description must match prompt.  │
│   Edge properties list ONLY the required vars (never not-yet-known ones │
│   like first_name/callback_number — they inject null and clobber vars). │
│   FORBIDDEN: LLM decides the transition with no boolean gate (prob-     │
│   abilistic). No trigger_*_transition tools.                            │
│                                                                         │
│ PROMPT STRUCTURE                                                        │
│   Every step: CHECK {{var}}? YES→skip, NO→execute                      │
│   MISSION line on every state prompt                                    │
│   EXTRACT NOW markers on every extract_dv call                          │
│   IF/THEN for ALL branching                                             │
│                                                                         │
│ LLM UPDATES                                                             │
│   NEVER patch. Create new LLM each change. Caching bug.                │
│                                                                         │
│ REPUBLISH                                                               │
│   After EVERY change.                                                   │
│                                                                         │
│ KBs                                                                     │
│   Use for: reference data, sample conversations, bulky content          │
│   Config: top_k=3, filter_score=0.6                                     │
│   Referenced as ##kb-name## in prompts                                  │
│                                                                         │
│ CUSTOM WEBHOOKS                                                         │
│   Describe in BOTH prompt (natural language) AND tool schema (JSON).   │
│   Both must agree on when/why/how.                                      │
└─────────────────────────────────────────────────────────────────────────┘
```
