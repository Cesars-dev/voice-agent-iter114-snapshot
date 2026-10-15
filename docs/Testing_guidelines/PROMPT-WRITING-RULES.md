# CLAUDE.md — Voice Agent Building Rules

> THESE RULES ARE MANDATORY FOR ALL VOICE AGENTS BUILT IN THIS FOLDER. NO EXCEPTIONS.


---

## PROMPT WRITING STYLE (Learned from Winning Prompts)

### Tone & Structure
- Write prompts like you're briefing a real human employee. Conversational, direct, no corporate speak.
- The agent is "just an employee working a job" — NOT an overly enthusiastic bot. Real people don't sound thrilled to be at work.
- Use dashes for contrast: "Warm, professional — NOT a bot"
- Use contractions: I'm, you're, we've, that's, don't. NEVER write formal/stiff language.
- Keep rules to ONE LINE each. If it takes more than one line, you're over-explaining.
- Number your rules. Number your steps. Clear hierarchy.
- Use IF/THEN branching in steps: "IF confirmed → STEP 2" / "IF not interested → graceful exit"
- Conversation flow steps should have INLINE dialogue examples showing exactly what to say — the flow IS the example.
- NEVER VOLUNTEER INFORMATION UNLESS EXPLICITLY ASKED FOR IT. Bake this into every prompt.

### Emphasis & Formatting
- **CAPITAL LETTERS for critical instructions.** Caps matter — the LLM pays more attention.
- Use markdown (headers, bold, bullets) for structure.
- If the agent won't follow an instruction after testing, **add CAPS, stronger verbiage, and re-emphasize**. Escalate language before adding more words.

### Multi-Shot Prompting — FULL SAMPLE CONVERSATIONS (NON-NEGOTIABLE)
- For EVERY distinct scenario the agent handles, include a COMPLETE example conversation from opening to close.
- Agent says X, caller says Y, agent says Z — the ENTIRE call, concisely.
- If there are 5 scenarios, there must be 5 full sample conversations.
- Keep turns short and realistic. No filler. But they MUST cover the full flow start to finish.
- Sample conversations are the single most powerful tool for controlling agent behavior. The agent mimics what it sees.

### Conciseness
- **BE SUPER CONCISE NEVER VERBOSE.** Every sentence must earn its place.
- "Keep responses SHORT — this is a phone call, not a pitch deck."
- 1-2 sentences before expecting input from the caller. Keep the conversation MOVING.
- Sound curt in a professional way. Never fluffy or extra.

### What Prompts CAN Control
- What the agent says (word choice, tone, structure)
- Questions asked and their order
- Edge case handling and guardrails
- Call flow branching and decision logic
- Verbal fillers (e.g., "use um and uh in every other sentence")
- Confusion triggers (e.g., "if confused say 'um, I think I lost you for a second, you there?'")

### What Prompts CANNOT Control
- Call duration / timing (platform setting)
- Voice characteristics, breathing, pacing (voice engine)
- Audio quality, background noise, silence detection (platform settings)
- These are TEXT-BASED voice agents at the end of the day

---

## PROMPT STRUCTURE TEMPLATE

Every voice agent prompt should follow this structure:

```
1. Role — Who you are, who you work for, what's your job (2-3 lines max)
2. Personality — How you sound, your vibe, what you're NOT
3. Primary Objective — One line: what is the goal of this call
4. Available Tool Calls — What functions you have access to (if any)
5. Rules — Numbered, one line each, strongest language
6. Conversation Flow — Numbered steps with IF/THEN branching and inline dialogue
7. Example Calls — Full start-to-finish sample conversations for EVERY scenario
8. Error Handling — Edge cases inline in the flow OR as a separate section
```

---

## STANDARD OPERATING PROCEDURES (Copy-Paste Snippets)

### Collecting Email (EXACT FORMAT — use in every prompt)
```
Verbally confirm the full name and email by spelling it out:
Listen and collect the email spoken by the customer and spell it back exactly for clarity.

## How to spell out email
The possible email format is name@company.com
To spell out an email address is n-a-m-e-@-c-o-m-p-a-n-y-dot-com.
@ is pronounced by "at".
If the customer is using gmail don't spell out gmail just say "at gmail dot com".
If the customer is using yahoo don't spell out yahoo just say "at yahoo dot com".
If the customer is using hotmail don't spell out hotmail just say "at hotmail dot com".
```

### Collecting Phone Numbers (EXACT FORMAT — use in every prompt)
- For INBOUND calls: phone is available via `{{from_number}}` or `{{user_number}}`. Confirm: "Is this number you're calling from the best one to reach you?"
- For OUTBOUND calls: phone is already in dynamic variables. Confirm it works for texts: "Is this number good for a text?"
- NEVER ask someone to slowly recite their phone number. Confirm what you already have from the system.

### Collecting Addresses (EXACT FORMAT — use when needed)
```
Ask for the address verbally first: "What's the property address?"
If the caller gives the address and it is clear, confirm once and continue.
If the address is unclear or you are unsure on spelling, then say:
"Got it. To make sure I get this correct, I'm going to send you a quick text so you can reply with the exact spelling."

NEVER start with texting by default.
Only use texting when clarity would be lost if spoken.
```

### Confirmation Pattern (EXACT FORMAT)
```
Always confirm back what the caller just said for critical information but don't confirm the same information twice.
If the caller gives info that could be misheard over the phone, spell it back:
(e.g., "okay, so your address is 332 Cabot Road, that's C-A-B-O-T, correct?")
Only do a FINAL full confirmation right before taking the main action
(e.g., "Let me make sure I've got everything right — [summary]. Correct?")
```

### Speech Normalization Rules (Copy into any prompt that reads back numbers/addresses)
```
Speech Normalization Rules:
When speaking information back to the caller, convert numbers and abbreviations to natural speech.
Convert prices like 875 to "eight hundred seventy five dollars" not "eight seventy five".
Convert square footage like 2100 to "two thousand one hundred square feet" not "two one zero zero".
Convert street abbreviations:
Rd say "Road"
St say "Street"
Ave say "Avenue"
Dr say "Drive"
Blvd say "Boulevard"
Ln say "Lane"
Always speak numbers the way a human would say them. Never read them digit by digit unless the caller is spelling.
```

### Verbal Fillers & Realism
- ALWAYS include in personality section: `use um and uh in every other sentence.`
- ALWAYS include confusion trigger: `if there are any moments of confusion say only once: "um, I think I lost you for a second, you there?"`
- Use verbal confirmations: "Got it", "Perfect", "Makes sense", "Okay"

### "Hold On" Handler (Copy into every prompt)
```
If user says "hold on", reply exactly the following: "NO_RESPONSE_NEEDED".
```

### "How Are You" Handler (Copy into every prompt)
```
If the caller asks "how are you" — always ask it back:
"I'm doing alright today. Uh, how are you?"
```

### Graceful Exits
- If not interested: thank them, wish them well, end call. ONE sentence.
- NEVER push back, convince, or ask "are you sure?"
- Example: "Totally understand. Thanks for letting me know. Have a great day!"

### Transfer Protocol
- Be ASSERTIVE: "Let me connect you with [name] right now."
- Do NOT ask "Would you like me to transfer you?" — just do it.
- If transfer fails: "Looks like they're on another call. I'll make sure they call you back within the hour. Is this the best number?"

### Hostile/Rude Callers
- Do NOT engage. One sentence: "I apologize for the interruption. Have a great day!" → end_call.

---

## RETELL AI DYNAMIC VARIABLES

### System Variables (Available Automatically)
| Variable | Description |
|----------|-------------|
| `{{current_time_America/new_york}}` | Current time in specified timezone (change city as needed: `America/chicago`, `America/los_angeles`, `America/denver`) |
| `{{current_calendar_America/new_york}}` | 14-day calendar view in specified timezone |
| `{{session_type}}` | "voice" or "chat" |
| `{{session_duration}}` | Running call duration |
| `{{call_id}}` | Unique call identifier |
| `{{direction}}` | "inbound" or "outbound" |
| `{{from_number}}` / `{{user_number}}` | Caller's phone number (inbound) |

### Custom Dynamic Variables
- Set per-agent based on use case (e.g., `{{first_name}}`, `{{agent_name}}`, `{{transfer_number}}`)
- Reference in prompt with double curly braces
- For outbound agents: always include lead context variables (name, source, original interest)
- For inbound agents: use `{{from_number}}` for caller ID
- ALWAYS include `Current date and time is {{current_time_America/new_york}}` near the top of every prompt that deals with scheduling

---

## DEFAULT RETELL AI SETTINGS (Apply to ALL agents)

### Model
- **ALWAYS use GPT-4.1** → `"model": "gpt-4.1"`

### Voice
- **Default voice: Nia (MiniMax)** → `"voice_id": "minimax-Nia"`
- **Speed: 1.2** → `"voice_speed": 1.2`

### Speech Settings
- **Responsiveness: HIGHEST** → `"responsiveness": 1.0`
- **Interruption sensitivity: 0.8** → `"interruption_sensitivity": 0.8`
- **Backchannel: ALWAYS ON** → `"enable_backchannel": true, "backchannel_frequency": 0.8`
  - Backchannel words: `"backchannel_words": ["mm-hmm", "uh-huh"]`
- **End-of-call function: ALWAYS ENABLED** (add `end_call` tool to every agent)
- **Denoising: Remove noise + background speech** → `"denoising_mode": "noise-and-background-speech-cancellation"`
- **End call on silence: 1 minute** → `"end_call_after_silence_ms": 60000`
- **Normalize for speech: ON** → `"normalize_for_speech": true`

### Post-Call Analysis
- **ALWAYS configure post-call analysis** when creating an agent
- Extract data points relevant to what the prompt is designed to collect
- **ALWAYS use GPT-4.1** → `"post_call_analysis_model": "gpt-4.1"`

---

## PUBLISH RULES

### CRITICAL: ALWAYS REPUBLISH AFTER ANY CHANGE
- **Every time you modify the prompt — REPUBLISH.**
- **Every time you change settings — REPUBLISH.**
- **Every time you change ANYTHING — REPUBLISH.**
- 2-word version title describing the change (e.g., "Fixed Deflection", "Added Examples")
- `POST /publish-agent-version/{agent_id}`

---

## TESTING METHODOLOGY

### Two Testing APIs
1. **Batch Simulation** (`create-batch-test`) — FREE, automated, wide coverage, can't control caller. Use for regression.
2. **Chat API** (`create-chat-agent` → `create-chat` → `create-chat-completion`) — ~$0.017/msg, full control. Use for surgical mid-conversation testing.

### When to Use Each
- **Batch Simulation**: After prompt changes, run all scenarios for regressions
- **Chat API**: Test a SPECIFIC response at a SPECIFIC point. Replay setup messages, fire test message 10x for consistency.

### Consistency Testing
- Test critical responses **10 times** from the same conversation context
- If it fails even 1/10, strengthen the prompt at that point
- Use CAPS, stronger verbiage, and add/improve sample conversations

---

## ITERATION LOOP

> **Full SOP: `TESTING-AND-REFINEMENT-SOP.md`** — Follow it exactly. No shortcuts.

**Summary:**
1. Write prompt aligned to meta orienter + this CLAUDE.md
2. Get user approval on the build
3. **Run the Testing & Refinement SOP** (see `TESTING-AND-REFINEMENT-SOP.md`)
   - Phase 1: Inventory every scenario from the prompt
   - Phase 2: Broad test — run each scenario once, score it
   - Phase 3: Confirm issues — smart retesting (not blind 10x reps)
   - Phase 4: Fix prompt — isolate the fix, republish
   - Phase 5: Verify fix — 3 clean reps
   - Phase 6: Regression — re-run everything, catch new breakdowns
4. Repeat until exit criteria met (every scenario passes, every fix verified, regression clean)

---

## AGENT REGISTRY

| Agent | Subfolder | Retell Agent ID | LLM ID | Status |
|-------|-----------|-----------------|--------|--------|
| PI Intake (Legal) | `legal-pi-intake/` | `agent_a3a30a67fcc03e1be7bfe2e362` | `llm_e84132673b1e896388a5bd467d62` | Testing |
| PI Intake (Legacy) | `legal-pi-intake/` | `agent_c73878398bb94c62d42d2d184c` | `llm_df299bb9617c3b507a7023f78939` | Deprecated |
| Airbnb Concierge (Mansion In The Sky) | `airbnb-mansion-in-the-sky/` | `agent_f9fe4a9f738dbed8016b3b509b` | `llm_ea8789c087ef9e6c1d52f222397d` | Tested |
| Airbnb Concierge v1 | `airbnb-mansion-in-the-sky/` | `agent_21f8a60127381dd5d7b2a70985` | `llm_1c7ef69552571055880248b88957` | Deprecated |

---

## API KEY

Retell AI: `key_8970cab8ef7afa92828075dc1280`
