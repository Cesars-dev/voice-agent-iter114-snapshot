# Mark's Voice AI Starter Kit — What's Here

These 6 files are from Mark's Voice AI Starter Kit (Tech Tomlet). They provide the framework for building and testing Retell AI voice agents programmatically — no dashboard needed.

## The Files

| File | Size | What It Is |
|------|------|------------|
| `START-HERE.md` | 3 KB | Entry point. Explains the kit structure and how to use it. |
| `RETELL-API-REFERENCE.md` | 10 KB | **Key file.** Every Retell API endpoint, payload shape, and gotcha — LLM creation, agent creation, chat testing, cleanup. The reference we use for programmatic testing. |
| `TESTING-AND-REFINEMENT-SOP.md` | 17 KB | **Key file.** The complete testing playbook — how to test an agent, fix issues, and iterate. Covers 4 testing methods (batch simulation, direct Claude testing, scripted Python tests, live calls). |
| `PROMPT-WRITING-RULES.md` | 12 KB | Rules and structure for writing effective voice agent prompts. |
| `PROMPT-REFERENCE-EXAMPLE.md` | 9 KB | A real production prompt example (sanitized). Reference for prompt structure. |
| `META-ORIENTER-TEMPLATE.md` | 8 KB | Template for defining an agent's spec before writing the prompt — what it does, who calls, how it sounds, success metrics. |

## How To Use

1. Read `RETELL-API-REFERENCE.md` for API endpoints
2. Follow `TESTING-AND-REFINEMENT-SOP.md` for the test workflow
3. The HVAC test scenarios (`../hvac_test_scenarios.md`) provide the 18 caller scripts to run through the chat API

## Why This Matters

Before this kit, testing a Retell agent meant manually calling it in the dashboard. Now the whole loop is automated:

```
Build via API → Test via Chat API → Find issues → Fix prompt → Redeploy → Retest
```
