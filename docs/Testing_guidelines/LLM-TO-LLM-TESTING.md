# LLM-to-LLM Testing

> Preferred first-pass testing method. Test the agent's brain by having another LLM play the caller. Faster and more adaptive than scripted tests.

## Why

Scripted Python tests follow fixed paths — they miss how a real caller adapts. Batch simulation is black-box. LLM-to-LLM lets you control the caller persona while keeping natural conversation flow.

## How It Works

```
1. Create a Retell chat session with the test chat agent (POST /create-chat)
2. Give GPT-4o (or another LLM) a system prompt built from a persona definition:
     - Persona TYPE (happy path, mean, dumb, problematic, ...)
     - The persona's narrative (who they are, their situation, goals)
     - Seeded context the agent expects (address, product, account, etc.)
     - Behavioral rules + expected outcome (book / no-book)
3. Loop:
     a. GPT-4o replies as the customer
     b. Send that reply to Retell's create-chat-completion (POST /create-chat-completion)
     c. Print the agent's response
     d. Feed agent's response back to GPT-4o for next turn
     e. Repeat until end_call, the booking tool fires, or goodbye
4. Pull the full transcript + tool calls via GET /get-chat/{id} for analysis
```

## Personas are per-agent — always built from scratch

Persona **types** are reusable categories, but each **concrete persona is agent-specific**. You must build it from scratch for the agent under test, based on:

- **What the agent does** (its purpose, its channel — voice vs chat).
- **The expected conversation shape** (what the agent is designed to ask for, in what order).
- **The success criteria** (what "done" looks like — a booking, an answer, an escalation, a hangup).

Never copy a persona written for another agent. A persona that makes sense for an HVAC booking agent is meaningless for a sales or support agent. The generic types stay constant; the content is authored per agent.

### How to build a persona for your agent

1. **Read the agent first.** Pull its config / response-engine prompt (`GET /get-agent` or the chat agent's LLM) so you know the fields it collects, the tools it can call (`book_calendar`, `check_availability`, `transfer`, `end_call`, etc.), the order it asks things, and its guardrails / scope boundaries.
2. **Define the expected behavior** for the persona type — how the conversation *should* flow and what the pass condition is.
3. **Write the persona definition** (fields below).
4. **Seed `dynvars`** with exactly the context the agent's prompt expects (e.g. address, product, account number). Seed it honestly — the persona answers with these values when asked, so collected fields stay consistent.
5. **Set `expect`** to the intended outcome so the run can be scored.
6. **Review rules** to make sure they exercise the agent, not a script. The caller must be an adaptive LLM, not a fixed script.

### Persona definition template

```python
{
  "name":     "(BOOK) <Caller name>",          # display label incl. expect
  "type":     "happy-path | mean | dumb | problematic | ...",
  "system":   "<system prompt the caller LLM receives>",
  "opener":   "<the caller's actual first spoken line>",
  "dynvars":  {"<agent field>": "<seeded value>", ...},   # context the agent expects
  "expect":   "book | no-book",
  "phone":    "<E.164 test number>",
}
```

The `system` prompt must contain the narrative, the seeded details the caller will reveal when asked, and the behavioral rules.

## Persona types

These are the reusable categories. Build one or more concrete personas of each type per agent. **Always include at least one Happy Path.**

### Happy Path (REQUIRED — smoke test)

> The cooperative caller. Follows the agent's intent exactly, answers every question fully, raises no objections, and gives the agent no trouble. Purpose: verify the agent's **main functions** work end-to-end on the intended happy route.

**Rules to build it:**
- The persona has exactly what the agent needs (full address, valid postcode, etc.) and volunteers / confirms it when asked.
- No curve-balls, no cost questions, no identity challenges, no mid-call changes.
- Only confirms a booking once the agent reads back a specific day + time and asks for a clear confirmation (the same real-world bar as the other types).
- Clear affirmative sentences only (`Yes, that's perfect — please book it.`) — a bare "okay"/"mm-hmm" is not a confirmation.
- `expect: "book"` (or the agent's happy-path success outcome).

**Watch for:** the whole main flow succeeds — all required fields collected in order, correct tool calls fired, correct booking / closing.

### Mean Customer (stress — resilience)

The impatient, dismissive, hostile caller. Tests emotional resilience and rule adherence under pressure.

**Watch for:** agent stays calm, does NOT match hostility, keeps one question per turn, does not get flustered or give up prematurely.

### Dumb Customer (stress — patience & edge cases)

The confused, hesitant caller who gives slightly-off or incomplete answers. Tests patience, re-asking, and edge cases (e.g. unknown postcode, house name instead of number).

**Watch for:** agent does NOT rush, does NOT stack questions, rephrases, handles "I don't know" gracefully, and uses the edge-case fallbacks correctly.

### Problematic Customer (stress — adaptability)

The chaotic caller who changes their mind, contradicts themselves, or volunteers data in the wrong order. Tests the agent's ability to roll with chaos without resetting.

**Watch for:** agent adapts without getting stuck, handles intent changes and mid-call corrections, and keeps the conversation bookable.

> Other types you may add per agent: **Safety/Emergency** (reports a hazard the agent must not book through), **Enquiry-only** (asks questions, never intends to book), **Specific-request** (insists on a person/product the agent may not guarantee). Author these from scratch against the agent's guardrails, same as above.

### A complete example — the Heating UK agent

This repo's `agents/heating_uk/performance_tests/personas/18/hvac_18_personas.py` is a worked example of the method: 18 personas authored from scratch for the HVAC booking agent, each with narrative context, opener, seeded `dynvars`, an `expect` flag, and per-persona phones. It covers all types above (happy paths S1–S6/S9/S12/S15–S17, mean/dumb/problematic via S4/S5/S7/S10/S11/S17, plus safety S13, non-UK postcode S8, and heat-pump enquiry S18). Read it as a template, then rebuild equivalent personas for the agent you are testing.

## Key Technique — Full Context

On every turn, GPT-4o receives the **entire conversation history** — everything it said before plus the agent's latest reply. This prevents the caller from contradicting itself or forgetting details it already gave.

## Full Python Architecture

```python
import json, urllib.request, os

# ── Config ──
RETELL_KEY = "key_..."
OPENAI_KEY = "sk-..."
CHAT_AGENT = "agent_..."  # test chat agent ID (channel: chat)

# ── OpenAI Caller ──
def call_gpt(system, history, agent_last=None):
    """Generate the customer's next line using GPT-4o."""
    msgs = [{"role": "system", "content": system}] + list(history)
    if agent_last:
        msgs.append({
            "role": "user",
            "content": f"Agent said: \"{agent_last}\". Reply in <=20 words as customer."
        })
    data = json.dumps({
        "model": "gpt-4o",
        "messages": msgs,
        "temperature": 0.7,
        "max_tokens": 80
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=data,
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()

# ── Retell API ──
def retell(method, endpoint, data=None):
    """Call Retell REST API."""
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        f"https://api.retellai.com/{endpoint}", data=body, method=method,
        headers={"Authorization": f"Bearer {RETELL_KEY}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# ── One Test Run ──
def test_scenario(persona):
    """Run a single scenario: GPT-4o caller vs Retell chat agent."""
    print(f"\n=== {persona['name']} ===")

    # Create chat session
    create_data = {"agent_id": CHAT_AGENT}
    if persona.get("dynvars"):
        create_data["retell_llm_dynamic_variables"] = persona["dynvars"]
    chat = retell("POST", "create-chat", create_data)
    cid = chat["chat_id"]
    print(f"Chat: {cid}")

    caller = persona["opener"]
    hist = []
    system = persona["system"]
    expect = persona.get("expect", "book")

    for turn in range(20):
        # Send caller message to Retell
        resp = retell("POST", "create-chat-completion", {
            "agent_id": CHAT_AGENT, "chat_id": cid, "content": caller
        })

        # Extract agent speech
        agent_text = ""
        for msg in resp.get("messages", []):
            if msg.get("role") == "agent":
                agent_text = msg.get("content", "")
                break

        if not agent_text:
            # Agent is mid-tool (e.g. check_availability) and hasn't spoken.
            caller = "Mm-hmm."  # neutral backchannel — not a confirmation
            continue

        print(f"A: {agent_text[:200]}")

        # Detect conversation end by tool invocation
        fired = [m.get("name") for m in resp.get("messages", [])
                 if m.get("role") == "tool_call_invocation"]
        if "end_call" in fired:
            print("[end_call fired]")
            break
        if any(w in agent_text.lower() for w in ["goodbye", "bye for now", "take care"]):
            print("[Agent closed]")
            break

        # Get next caller response from GPT-4o
        hist.append({"role": "assistant", "content": caller})
        caller = call_gpt(system, hist, agent_text)
        print(f"C: {caller[:200]}")

    return cid  # return chat ID for transcript analysis

# ── Load personas (agent-specific, built from scratch) ──
# personas = [ ...your per-agent persona dicts... ]

# ── Run ──
for persona in personas:
    chat_id = test_scenario(persona)

    # Fetch full transcript + tool calls for analysis
    chat = retell("GET", f"get-chat/{chat_id}")
    print(f"\nTranscript:\n{chat.get('transcript', '')[:500]}")
    print(f"Cost: {chat.get('chat_cost', {}).get('combined_cost', '?')}c")
```

## Notes

- Retell's `create-chat-completion` uses `agent_id` + `chat_id` + `content` (NOT OpenAI-style `messages` array).
- The response contains a `messages` array — iterate to find `role: "agent"` for speech.
- Check `role: "tool_call_invocation"` for `end_call`, `book_calendar`, `transfer`, etc. to detect conversation end / completion. When reading a tool's `arguments`, `json.loads` it first (see the arguments note below).
- **Never `break` when the agent returns no speech on a turn.** `create-chat-completion` can return empty `agent` content while the agent is mid-tool (e.g. `check_availability`). A bare `break` there aborts the conversation before it finishes. Retry the same turn (small sleep + `continue`) or send a neutral backchannel — but bound the retry so you can't loop forever. (Bug 1 from the first `test_happy_path_today.py` run.)
- **Tool-call `arguments` are a JSON string, not a dict.** Inspecting a `book_calendar` / `check_availability` call returns `msg.get("arguments")` as serialized JSON, e.g. `'{"time":"2026-08-21T13:00:00"}'`. `json.loads` it (guarded) before reading fields — calling `.get("time")` on the raw string raises `AttributeError`. (Bug 2 from the first run.)
- **Assert a booking by non-empty parsed `time`, never a fixed timestamp.** Live availability varies by date, so a hard-coded expected time fails. Confirm `book_calendar` fires with a non-empty `time` instead.
- Seed the agent's expected context via `retell_llm_dynamic_variables` on `create-chat` (`dynvars`).
- After testing, use `GET /get-chat/{id}` to pull transcript, variables, tool calls, and cost.
- Create one chat agent per test session. Delete when done.
- **Your target must be a chat agent** (`channel: "chat"`). A voice agent cannot be driven by `create-chat-completion` — either create a chat agent bound to the same Retell LLM (`llm_id`) to test the brain, or clone the voice agent and test via the audio panel / `create-web-call` to test voice behavior.

## Run Order

```
1. Happy Path (always first)  → smoke test: main functions work
2. Mean Customer  (1-2)       → stress emotional resilience
3. Dumb Customer  (1-2)       → stress patience + edge cases
4. Problematic Customer (1-2) → stress adaptability
5. Any extra types (safety, enquiry-only, specific-request, ...) authored for this agent
```

If the agent fails the Happy Path, fix the agent before running stress tests — there is no point stressing a flow that does not work. If any stress persona fails, fix the prompt before running the rest.

## When to Use

- **After any prompt change** — validate core flows before scripted tests.
- **Edge case exploration** — stress-test specific behaviors with varied callers.
- **Regression check** — run the happy path + 3-5 stress personas to confirm nothing broke.

## When to Move to Python Scripts

After LLM-to-LLM passes, write scripted Python tests for regression (reproducible, no API costs for the caller LLM). See `RETELL-API-REFERENCE.md` for the chat API format.
