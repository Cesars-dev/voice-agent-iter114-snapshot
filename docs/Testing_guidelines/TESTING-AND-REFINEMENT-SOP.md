# Voice Agent Build & Test SOP

> This is the COMPLETE playbook for building, testing, and shipping a voice agent. Follow it start to finish. Do NOT skip steps. Do NOT stop until exit criteria are met.


---

## HOW THIS WORKS (Architecture)

Retell voice agents have two layers: the **brain** (an LLM with your prompt) and the **voice** (speech-to-text + text-to-speech). You test each layer separately.

```
1. BUILD the voice agent (LLM + agent + settings + publish)
2. TEST THE BRAIN — use chat testing (text-based) to verify prompt behavior
3. FIX the prompt, redeploy, iterate until clean
4. TEST THE VOICE — make real phone calls to verify audio quality
5. CLEAN UP — delete all temporary chat agents
```

**The brain** is tested via text — either through the Chat API (you control the caller) or Batch Simulation (Retell simulates the caller). Both test the same LLM prompt without phone calls.

**The voice** is tested by calling the agent. Chat tests can't verify interruption handling, pacing, or speech-to-text accuracy — that requires a real call.

**What text testing CAN verify:** Prompt behavior, tone, conciseness, tool call decisions, edge cases, hallucination, rule compliance.

**What text testing CANNOT verify:** Voice quality, interruption handling, backchannel timing, speech-to-text accuracy, ambient sound.

You have **4 different testing methods** available — see the Testing Toolkit section below for when to use each one.

---

## STEP 1: BUILD THE VOICE AGENT

### 1a. Write the Meta Orienter

Before writing a single line of prompt, create a Meta Orienter document that defines:

- **What this agent is** — one paragraph, what it does and what it's NOT
- **Who calls it** — describe the actual callers, their mindset, their needs
- **How it sounds** — specific voice character, what it sounds like AND what it does NOT sound like
- **What it does** — step-by-step call flow with target script feel
- **Decision logic** — when to transfer, when to handle directly, when to escalate
- **Success metrics** — what does a good call look like?
- **Anti-patterns to test for** — failure modes you expect to see in testing

See `legal-pi-intake/PI-INTAKE-AGENT-META-ORIENTER.md` for a real example.

### 1b. Write the Prompt

Follow the structure in `CLAUDE.md` and study `PROMPT-REFERENCE-EXAMPLE.md` for a real production example. Every prompt must have:

```
1. Critical Overrides (CAPS, top of prompt — strongest behavioral rules)
2. Role — Who you are, who you work for (2-3 lines)
3. Personality — How you sound, what you're NOT
4. Primary Objective — One line
5. Available Tools — What functions exist, with exact JSON format examples
6. Rules — Numbered, one line each
7. Conversation Flow — Numbered steps with IF/THEN branching and inline dialogue
8. Example Calls — Full start-to-finish sample conversations for EVERY scenario
9. Error Handling — Edge cases inline or as a separate section
```

Key prompt techniques:
- Write like you're briefing a real employee, not programming a bot
- Keep responses to 1-2 sentences. "This is a phone call, not a pitch deck."
- Use CAPS for critical instructions — the LLM pays more attention
- Include full sample conversations for every scenario (strongest behavior signal)
- Bake tool call JSON examples directly into the prompt
- Add speech normalization rules if the agent reads back numbers/addresses
- Include standard handlers: "hold on" → `NO_RESPONSE_NEEDED`, "how are you" → ask it back
- NEVER VOLUNTEER INFORMATION UNLESS EXPLICITLY ASKED

### 1c. Deploy to Retell

Create the agent via API:

```python
# 1. Create the LLM
POST create-retell-llm
{
  "model": "gpt-4.1",
  "general_prompt": "<YOUR_PROMPT>",
  "general_tools": [...],
  "begin_message": "<OPENING_LINE>"
}

# 2. Create the voice agent
POST create-agent
{
  "agent_name": "<NAME>",
  "response_engine": { "type": "retell-llm", "llm_id": "<LLM_ID>" },
  "voice_id": "minimax-Nia",
  "voice_speed": 1.2,
  "enable_backchannel": true,
  "backchannel_frequency": 0.8,
  "backchannel_words": ["mm-hmm", "uh-huh"],
  "interruption_sensitivity": 0.8,
  "responsiveness": 1.0,
  "end_call_after_silence_ms": 60000,
  "normalize_for_speech": true,
  "denoising_mode": "noise-and-background-speech-cancellation",
  ...
}

# 3. Publish
POST publish-agent-version/<AGENT_ID>
{ "version_description": "Initial Build" }
```

See `CLAUDE.md` for full default settings. Always publish after creation.

### 1d. Register the Agent

Add it to the Agent Registry table in `CLAUDE.md` with agent ID, LLM ID, subfolder, and status.

---

## TESTING TOOLKIT — Pick the Right Method

You have 4 ways to test a voice agent. Each serves a different purpose. Use the right one for the job.

| Method | Best For | Cost | Speed |
|--------|----------|------|-------|
| **Batch Simulation** | Wide coverage — simulating many diverse callers at scale | FREE | Fast (parallel) |
| **Direct Claude Testing** | Surgical — testing specific behaviors, tool calls, edge cases turn-by-turn | ~$0.017/msg | Medium |
| **Scripted Python Tests** | Repeatable — hardcoding exact conversation paths to skip to the parts that matter | ~$0.017/msg | Fastest |
| **Live Phone Call** | Final check — verifying voice quality, interruptions, real-world audio | Per-minute | Slow |

### 1. Batch Simulation (`create-batch-test`)

Retell's built-in batch testing. You define test cases, Retell simulates the caller and runs them all automatically.

**When to use:**
- After a major prompt change — run 20-50 simulated calls to catch issues you didn't think of
- Regression testing — re-run a full suite after fixes to make sure nothing broke
- Wide exploration — finding failure modes across many random caller styles

**Limitations:** You don't control what the simulated caller says. Good for finding surprises, not for testing specific scenarios precisely.

```python
# Define test cases
POST create-test-case-definition
{
  "test_case_name": "New customer booking",
  "scenario_description": "A new customer calling to book an appointment for a general checkup"
}

# Run batch (1-200 cases)
POST create-batch-test
{
  "agent_id": "<AGENT_ID>",
  "test_case_ids": ["<TEST_CASE_ID_1>", "<TEST_CASE_ID_2>", ...]
}
```

### 2. Direct Claude Testing (Claude Plays the Caller)

Tell Claude Code to test the agent by playing the caller — improvising turn-by-turn through the Chat API. Claude reads each agent response and decides what a real caller would say next.

**When to use:**
- Testing tool/function calling — seeing if the agent calls `check_availability` or `create_booking` at the right moment
- Edge case exploration — "what happens if the caller asks about a service not in the prompt?"
- Stress testing a specific behavior — "test medical advice deflection 10 different ways"
- Realistic conversation flow — Claude improvises naturally, catching issues a script wouldn't

**How to do it:** Tell Claude Code something like:
> "Play a caller who wants to book an appointment. Test the full flow through the Chat API — improvise each message based on what the agent says back."

Claude will create chats, send messages, read responses, and report what it finds. You can also run multiple tests in parallel using subagents:
> "Spin up 10 subagents — each one plays a different caller persona and tests the booking flow."

### 3. Scripted Python Tests

Write a Python script that sends pre-written caller messages through the Chat API. You hardcode exactly what the caller says at each turn.

**When to use:**
- You know the exact conversation path and want to skip straight to the part that matters
- Consistency testing — send the same sequence 10x to see if the agent responds the same way every time
- Automated regression — run a saved script after every deploy to verify nothing broke
- Speed — no improvisation overhead, just fire messages and check results

**How to do it:** Tell Claude Code:
> "Write a Python test script that walks through a new customer booking — send the messages, check that the agent calls the right tools, and report pass/fail."

Claude writes the script. You can rerun it anytime.

```python
# Example: scripted test pattern
chat = api_call("POST", "create-chat", {"agent_id": CHAT_AGENT_ID})
chat_id = chat["chat_id"]

# Skip straight to the turn you care about
send(chat_id, "Hi I need an appointment")    # opener
send(chat_id, "New")                          # new customer
send(chat_id, "general checkup")              # service → should trigger check_availability
send(chat_id, "Thursday at 2pm")              # slot pick
# ... check tool calls, verify behavior
```

### 4. Live Phone Call

Call the actual voice agent from a real phone. The only way to verify voice, interruptions, pacing, and audio quality.

**When to use:** After all chat testing passes. The chat tests verify the brain — the phone call verifies the voice.

---

### Which to use when? (Quick Guide)

- **Just deployed a new agent?** → Direct Claude Testing for core flows, then Batch Simulation for wide coverage
- **Found an issue and fixed the prompt?** → Scripted test for the exact failing scenario (3 clean reps)
- **Want to stress-test a guardrail?** → Direct Claude Testing with 10 varied approaches
- **Running regression after fixes?** → Batch Simulation or saved Scripted tests
- **Everything passes in chat?** → Live Phone Call for final sign-off

You can mix and match. Use all four in a single session if needed.

---

## STEP 2: TEST THE AGENT

### 2a. Create a Temporary Chat Agent

Before using Direct Claude Testing or Scripted Tests, clone the voice agent's LLM into a chat agent:

```python
POST create-chat-agent
{ "response_engine": { "type": "retell-llm", "llm_id": "<SAME_LLM_ID_AS_VOICE_AGENT>" } }
```

This creates a text interface to the exact same prompt. **Create ONE per test session. Reuse it for all scenarios. Delete it when done.**

(Batch Simulation uses the voice agent directly — no chat agent needed.)

### 2b. Phase 1 — Scenario Inventory

Read the prompt top to bottom. List EVERY testable scenario:
- Happy paths (the main flows the agent handles)
- Edge cases (unexpected but plausible inputs)
- Deflection cases (things the agent should NOT answer)
- Error handling (API failures, unknown questions)
- Out-of-scope (wrong business, wrong intent)
- Standard handlers (hold on, how are you, hostile caller)
- Hallucination traps (questions where the answer is NOT in the prompt)

Each scenario = a list of user messages that simulate a realistic call.

**Output:** Numbered list of scenarios with a 1-line description and pass criteria each.

### 2c. Phase 2 — Broad Testing (One Pass Each)

Pick your method based on the testing toolkit above. Recommended approach:

**Option A — Direct Claude Testing (Recommended for first pass):**
Tell Claude to play the caller for each scenario, improvising turn-by-turn. This catches natural conversation issues a script would miss.

**Option B — Scripted Tests:**
If you already know the exact conversation paths, write scripts for each scenario. Faster, but won't catch unexpected issues.

**Option C — Batch Simulation:**
Run all scenarios through Retell's batch tester for wide automated coverage. Good for finding surprises.

However you test, score each response against what the prompt SHOULD produce:
- **Correct content?** Right info, no hallucination
- **Correct tone?** Concise, human, not robotic
- **Correct structure?** 1-2 sentences, one question at a time
- **No rule violations?** No made-up info, no stacking questions, etc.

Mark each scenario: **PASS / ISSUE FOUND / BORDERLINE**

**Output:** Scenario scorecard.

### 2d. Phase 3 — Issue Confirmation (Smart Retesting)

For each ISSUE FOUND or BORDERLINE:

```
1. Run the same test a 2nd time
   ├─ If it fails again → CONFIRMED ISSUE. Go to Phase 4.
   └─ If it passes → Run 3 MORE times (5 total)
       ├─ If 0-1 failures out of 5 → INTERMITTENT (low priority, note it)
       └─ If 2+ failures out of 5 → CONFIRMED ISSUE. Go to Phase 4.
```

Do NOT blindly run 10 reps on every test. Only escalate when needed.

**Output:** Confirmed issue list with failure frequency (e.g., "3/5 reps failed").

### 2e. Phase 4 — Prompt Fix

For each confirmed issue:

1. Identify the EXACT part of the prompt that governs this behavior
2. Apply the fix using the escalation ladder:
   - **First try:** Add/improve the relevant sample conversation
   - **Second try:** Add CAPS + stronger language to the rule
   - **Third try:** Add explicit negative instruction ("NEVER do X")
   - **Fourth try:** Add to the CRITICAL OVERRIDE section at the top of the prompt
3. Isolate the fix — do NOT change anything else in the prompt

### 2f. Deploy the Fix

**CRITICAL — RETELL LLM CACHING BUG:** Updating an existing LLM via `PATCH update-retell-llm` stores the change but does NOT propagate to runtime. The API returns success, but the agent keeps using the cached prompt.

**Workaround:** Always create a NEW LLM with the fixed prompt:

```python
# 1. Create NEW LLM with fixed prompt
POST create-retell-llm { ... }

# 2. Create NEW voice agent pointing to new LLM
POST create-agent { "response_engine": { "type": "retell-llm", "llm_id": "<NEW_LLM_ID>" } }

# 3. Publish
POST publish-agent-version/<NEW_AGENT_ID>  { "version_description": "<2-Word Fix Title>" }

# 4. Delete the OLD chat agent and create a new one pointing to the NEW LLM
DELETE delete-chat-agent/<OLD_CHAT_AGENT_ID>
POST create-chat-agent { "response_engine": { "type": "retell-llm", "llm_id": "<NEW_LLM_ID>" } }

# 5. Update agent registry in CLAUDE.md
# 6. Delete deprecated voice agent
DELETE delete-agent/<OLD_AGENT_ID>
```

### 2g. Phase 5 — Fix Verification

After deploying the fix:

1. Run the SAME test that failed — must pass
2. Run it 2 more times — must pass both
3. If any of the 3 fail → back to Phase 4, escalate the fix
4. If all 3 pass → move on

### 2h. Phase 6 — Regression

After ALL issues are fixed:

1. Re-run every scenario from Phase 2 once
2. If any NEW failures appear (the fix broke something else) → treat as new issue, go to Phase 3
3. If everything passes → prompt is DONE

### 2i. Bonus — Edge Case Testing

After the core scenarios pass, run a second round of 10-15 edge cases that are NOT explicitly in the prompt:
- Wrong number / wrong business
- Non-English speaker
- Asks about things not in the knowledge base (hallucination traps)
- Prompt injection attempts
- Long rambling caller
- Asks for personal info about the business owner
- Wants to leave a message

If any fail, loop back through Phases 3-6.

---

## STEP 3: CLEAN UP

**MANDATORY — Do this after EVERY test session:**

```python
# List all chat agents
POST /v2/list-agents

# Delete every one
DELETE delete-chat-agent/<AGENT_ID>

# Verify zero remain
POST /v2/list-agents  →  should return []
```

Also delete any deprecated voice agents from fix iterations:
```python
DELETE delete-agent/<DEPRECATED_AGENT_ID>
```

Only the FINAL voice agent should remain in the dashboard.

---

## STEP 4: FUNCTION/TOOL TESTING (If Applicable)

Chat API tests prove the agent DECIDES to call the right tool at the right time. But the actual tool execution (webhooks, transfers, API calls) needs separate verification:

- **Transfer calls:** Verify the transfer number is set in dynamic variables. Make a real test call.
- **Custom API tools:** Test the webhook endpoints independently. Verify payloads match what the prompt instructs.
- **End call:** Verified during chat testing (the tool fires in chat responses).

---

## ISSUE LOG FORMAT

Track every issue in an `ISSUE-LOG.md` file in the agent's subfolder:

```
ISSUE #1: [Short description]
- Scenario: [which test]
- Failure: [what the agent said wrong]
- Frequency: [X/Y reps]
- Root cause: [which part of prompt]
- Fix applied: [what changed]
- Fix version: [2-word publish title]
- New LLM ID: [id]
- New Agent ID: [id]
- Verified: [PASS/FAIL after fix]
- Regression: [PASS/FAIL]
```

---

## EXIT CRITERIA

Testing is DONE when:
- Every core scenario passes
- Every edge case scenario passes
- Every confirmed issue has a verified fix
- Regression pass is clean (no new failures from fixes)
- Issue log is fully resolved
- All temporary chat agents are deleted
- Only the final voice agent remains in the dashboard
- Agent registry in CLAUDE.md is updated with final IDs

Until then, keep looping.

---

## FILE STRUCTURE PER AGENT

```
voice-ai-agency/agents/<agent-name>/
├── <AGENT>-META-ORIENTER.md     — North Star spec
├── create-<agent>.py             — Deploy script (prompt + settings + tools)
├── run-sop-tests.py              — Phase 2/6 broad test scenarios
├── ISSUE-LOG.md                  — Testing issue tracker
└── sop-test-results.json         — Raw test data
```

---

## REFERENCE FILES

- `CLAUDE.md` — Prompt writing rules, default settings, standard SOPs, agent registry
- `PROMPT-REFERENCE-EXAMPLE.md` — Real production prompt to study (sanitized)
- `legal-pi-intake/` — Complete example agent with meta orienter, deploy script, and test scripts
