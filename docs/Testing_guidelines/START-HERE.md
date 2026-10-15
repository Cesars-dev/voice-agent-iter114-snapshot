# Voice Agent Starter Kit

Everything you need to build, deploy, and test a production voice agent using Claude Code + Retell AI.

---

## What's In This Folder

| File | What It Is |
|------|-----------|
| **START-HERE.md** | You're reading it |
| **LLM-TO-LLM-TESTING.md** | First-pass testing: GPT-4o plays the caller against your agent. Preferred over scripted tests. |
| **TESTING-AND-REFINEMENT-SOP.md** | The complete build + test playbook. Follow it start to finish. |
| **PROMPT-WRITING-RULES.md** | How to write voice agent prompts that work. Structure, tone, techniques, default settings. |
| **PROMPT-REFERENCE-EXAMPLE.md** | A real production prompt (sanitized). Study the patterns. |
| **META-ORIENTER-TEMPLATE.md** | Template for defining what your agent is before you write the prompt. |
| **RETELL-API-REFERENCE.md** | Every Retell API endpoint, payload format, and gotcha in one place. |

---

## How To Use This

### Prerequisites
- A Retell AI account + API key (retellai.com)
- Claude Code installed (claude.ai/claude-code)
- Python 3 installed
- OpenAI API key (for LLM-to-LLM testing)

### Testing Flow — LLM-to-LLM First, Then Scripts

**First pass:** Use `LLM-TO-LLM-TESTING.md` — have GPT-4o play the caller against your agent via the Retell Chat API. This catches tone issues, hallucinations, and rule violations faster than any script. You see the actual conversation flow, not just pass/fail.

**Regression:** After prompt fixes pass LLM-to-LLM, write scripted Python tests for reproducible regression. See `RETELL-API-REFERENCE.md` for the chat API format.

**Final:** Make a real phone call. Chat tests verify the brain — the phone call verifies the voice.

### The Process

**Step 1:** Read `META-ORIENTER-TEMPLATE.md`. Fill it in for your client/use case. This is your north star — what the agent does, who calls it, how it sounds, what it should NOT do.

**Step 2:** Read `PROMPT-WRITING-RULES.md` and study `PROMPT-REFERENCE-EXAMPLE.md`. These teach you how to write prompts that actually work on voice.

**Step 3:** Build and deploy the agent to a chat agent for testing. Follow `RETELL-API-REFERENCE.md` for API calls.

**Step 4:** Test with `LLM-TO-LLM-TESTING.md` — GPT-4o drives the caller, you observe the agent's behavior. Fix issues, redeploy, retest.

**Step 5:** When LLM-to-LLM passes clean, write scripted regression tests per `TESTING-AND-REFINEMENT-SOP.md`.

**Step 6:** Make a real phone call. Ship.

---

## Key Concepts

**Meta Orienter** — A spec document that defines what your agent is before you write the prompt. Think of it as the brief you'd give a new employee on their first day.

**LLM-to-LLM Testing** — GPT-4o plays the customer. Your agent gets tested through realistic, adaptive conversation. The full conversation history is fed to GPT-4o each turn so it remembers context. This catches things scripts miss.

**Chat Testing** — Retell lets you test the agent's brain (the LLM) via text instead of phone calls. Same prompt, same tools, same decision-making — just text instead of voice.

**The Caching Bug** — When you update a Retell LLM via API, the change doesn't actually propagate. You have to create a brand new LLM every time you change the prompt.

**Prompt Escalation Ladder** — When the agent breaks a rule during testing, fix it in this order: (1) add a sample conversation showing correct behavior, (2) add CAPS + stronger language, (3) add explicit "NEVER do X", (4) add to CRITICAL OVERRIDES at the top of the prompt.

---

## File Reading Order

1. `START-HERE.md` (this file)
2. `META-ORIENTER-TEMPLATE.md`
3. `PROMPT-WRITING-RULES.md`
4. `PROMPT-REFERENCE-EXAMPLE.md`
5. `RETELL-API-REFERENCE.md`
6. `LLM-TO-LLM-TESTING.md`
7. `TESTING-AND-REFINEMENT-SOP.md`
