# Retell Multi-Prompt Agent — Complete Reference

> Scraped from `docs.retellai.com` on 2026-07-21. Covers the `states`-based multi-prompt architecture (NOT Conversation Flows).

---

## 1. Architecture Overview

Multi-prompt agents organize conversations into a **structured tree of states**, each with its own focused prompt and behavior. Unlike single-prompt agents (one monolithic `general_prompt`), multi-prompt agents use:

- `general_prompt` — shared context loaded in every state (identity, voice, rules, KBs)
- `states[]` — individual state prompts loaded one-at-a-time
- `edges[]` — transition rules between states
- `starting_state` — entry point
- `general_tools` — tools available in every state
- Per-state `tools[]` — tools only available in that state

### State Machine (Not Conversation Flow)

```
general_prompt (always active)
  └── states:
        ├── [starting_state]
        │     prompt: <state_prompt>
        │     tools: <state-specific tools>
        │     edges: [{destination_state_name, description}]
        │
        ├── [state_2]
        │     prompt: ...
        │     tools: ...
        │     edges: ...
        │
        └── [state_3]
              prompt: ...
              tools: ...
              edges: ...
```

Transitions are triggered by the LLM based on instructions in the prompt (e.g., "if user agrees, transition to next_state").

### Benefits Over Single Prompt

- **Predictable Behavior**: Each state has a clear, focused purpose
- **Easier Debugging**: Issues isolated to specific states
- **Better Function Control**: Tools available only when appropriate
- **Scalable Design**: Add new states without affecting existing ones
- **Reduced Drift**: LLM has less context to deviate from at each step

---

## 2. API Payload — `POST /create-retell-llm`

```
POST https://api.retellai.com/create-retell-llm
Authorization: Bearer <token>
Content-Type: application/json
```

### Full Schema

```json
{
  "model": "gpt-4.1",
  "s2s_model": "gpt-realtime-1.5",
  "model_temperature": 0,
  "model_high_priority": true,
  "tool_call_strict_mode": true,
  "knowledge_base_ids": ["<kb_id>"],
  "kb_config": {
    "top_k": 3,
    "filter_score": 0.6
  },
  "begin_after_user_silence_ms": 2000,
  "begin_message": "Begin message spoken by agent on answer.",

  "general_prompt": "Shared prompt loaded in every state. Identity, voice, rules, KB references.",
  "general_tools": [
    {
      "type": "end_call",
      "name": "end_call",
      "description": "End the call with user."
    }
  ],

  "states": [
    {
      "name": "information_collection",
      "state_prompt": "State-specific instructions for this phase...",
      "edges": [
        {
          "destination_state_name": "appointment_booking",
          "description": "Transition to book an appointment."
        }
      ],
      "tools": [
        {
          "type": "extract_dv",
          "name": "extract_user_details",
          "description": "Extract user details when provided",
          "variables": [
            {
              "name": "customer_name",
              "description": "The customer's full name",
              "type": "text"
            },
            {
              "name": "past_customer",
              "description": "Whether the customer has used our service before",
              "type": "boolean"
            }
          ]
        }
      ]
    },
    {
      "name": "appointment_booking",
      "state_prompt": "Instructions for booking phase...",
      "tools": [
        {
          "type": "check_availability_cal",
          "name": "check_availability",
          "description": "Check available slots",
          "cal_api_key": "cal_live_xxx",
          "event_type_id": 12345,
          "timezone": "America/Los_Angeles"
        }
      ]
    }
  ],

  "starting_state": "information_collection",
  "default_dynamic_variables": {
    "customer_name": ""
  }
}
```

### Supported Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `model` | string | ✅ | LLM model (e.g. `gpt-4.1`, `gpt-5.4`) |
| `general_prompt` | string | ✅ | Shared prompt for all states |
| `general_tools` | array | optional | Tools available in every state |
| `states` | array | optional | State definitions (multi-prompt mode) |
| `starting_state` | string | conditional | Required if `states` is provided |
| `begin_message` | string | optional | First thing the agent says |
| `knowledge_base_ids` | string[] | optional | Linked KBs |
| `kb_config` | object | optional | `{top_k, filter_score}` |
| `tool_call_strict_mode` | boolean | optional | Enforce strict tool calling |
| `model_temperature` | number | optional | 0-2 |
| `model_high_priority` | boolean | optional | Priority queue |
| `default_dynamic_variables` | object | optional | Default values for `{{vars}}` |
| `mcps` | array | optional | MCP server integrations |
| `s2s_model` | string | optional | Speech-to-speech model |

---

## 3. State Object Schema

Each entry in `states[]`:

```json
{
  "name": "state_identifier",
  "state_prompt": "Instructions for this state...",
  "edges": [
    {
      "destination_state_name": "next_state",
      "description": "When to transition"
    }
  ],
  "tools": [
    { /* tool definitions scoped to this state */ }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | ✅ | Unique state identifier |
| `state_prompt` | string | ✅ | Prompt text for this state |
| `edges` | array | optional | Transition rules to other states |
| `tools` | array | optional | Tools only available in this state |

### Edge Object

```json
{
  "destination_state_name": "next_state",
  "description": "Human-readable transition condition"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `destination_state_name` | string | ✅ | Target state name |
| `description` | string | ✅ | Describes when to transition |

The prompt in each state instructs the LLM when to transition. Example:

```
7. Ask if user is interested in an in person tour.
   - if yes, transition to schedule_tour.
   - if no or hesitant, call function end_call to hang up politely.
```

---

## 4. Extract Dynamic Variables Tool (`extract_dv`)

Add the `extract_dv` tool to a state to pull values from user replies and store them as typed dynamic variables.

### Tool Definition

```json
{
  "type": "extract_dv",
  "name": "extract_user_details",
  "description": "Extract user information when they provide it",
  "variables": [
    {
      "name": "customer_name",
      "description": "The customer's full name",
      "type": "text"
    },
    {
      "name": "past_customer",
      "description": "Whether this is a returning customer",
      "type": "boolean"
    },
    {
      "name": "intent",
      "description": "Why the customer is calling",
      "type": "enum",
      "enum_options": ["booking", "quote", "other"]
    }
  ]
}
```

### Variable Types

| Type | Description | Examples |
|------|-------------|----------|
| `text` | Any word or sentence | `"John Smith"`, `"boiler broken"` |
| `number` | Numeric value | `42`, `98.6` |
| `enum` | Predefined list | `"Yes"`, `"No"`, `"Maybe"` |
| `boolean` | True or false | `true`, `false` |

### Best Practice

Include in the prompt explicitly when to invoke the extract variable function:

```
When the user states their name and phone number, call the `extract_user_details` function.
```

---

## 5. Dynamic Variables System

### Syntax

```
{{variable_name}}
```

Supports nested variables:
```
{{current_time_{{my_timezone}}}}
```

### System Variables

Available automatically in any state:

| Variable | Description |
|----------|-------------|
| `{{current_agent_state}}` | Current state name (for multi-state agents) |
| `{{previous_agent_state}}` | Previous state name |
| `{{current_time}}` | Current time in agent timezone |
| `{{current_time_[timezone]}}` | Current time in specified timezone |
| `{{current_hour}}` | Current hour as fraction |
| `{{current_calendar}}` | 14-day calendar view |
| `{{session_type}}` | `voice` or `chat` |
| `{{session_duration}}` | How long the session has been running |
| `{{user_number}}` | Caller's phone number (phone calls only) |
| `{{agent_number}}` | Agent's phone number |
| `{{call_id}}` | Current call ID |
| `{{direction}}` | `inbound` or `outbound` |

### Setting Default Values

```json
{
  "default_dynamic_variables": {
    "customer_name": "",
    "company_name": "British Heat Services"
  }
}
```

### For Outbound Calls via API

```json
{
  "retell_llm_dynamic_variables": {
    "customer_name": "John Smith",
    "company_name": "British Heat Services"
  }
}
```

---

## 6. Multi-Prompt Prompt Structure

### `general_prompt` (Shared — Always Loaded)

Should contain:
- Identity and role
- Voice and tone rules
- Knowledge boundary instructions
- Diagnostic restraint rules
- Fee rules
- Emergency scripts
- What you never do rules
- Company briefing
- Knowledge base references

### State Prompts (Loaded Per State)

Each state prompt should contain:
- **Mission**: What this state is responsible for
- **Steps**: Numbered conversation flow for this state
- **Transition instructions**: When to call edges to other states
- **Extraction instructions**: When to call `extract_dv` functions
- **State-specific rules**

### Transition Prompting Pattern

In the state prompt, include explicit transition conditions:

```
After completing [step]:
- If [condition], transition to [state_name].
- If [other condition], call [function].
```

The LLM uses the `edges` definitions to signal state transitions. The platform handles the actual state switch.

---

## 7. Example: Multi-Prompt HVAC Agent Structure

```json
{
  "starting_state": "greeter",
  "general_prompt": "# Identity\nYou are Tom...\n\n# Voice\n...\n\n# Emergency\n...",
  "general_tools": [
    { "type": "end_call", "name": "end_call", "description": "End the call politely." }
  ],
  "states": [
    {
      "name": "greeter",
      "state_prompt": "# Mission\nGreet, listen, mirror, classify intent.\n\n## Steps\n1. Greet...\n2. Listen and mirror...\n3. Confirm intent...\n...\n\n## Transition\nOnce intent is confirmed, transition to address.",
      "edges": [
        { "destination_state_name": "address", "description": "Intent confirmed, move to address capture" }
      ],
      "tools": [
        {
          "type": "extract_dv",
          "name": "extract_caller_info",
          "description": "Extract caller info when volunteered",
          "variables": [
            { "name": "customer_name", "type": "text" },
            { "name": "past_customer", "type": "boolean" },
            { "name": "intent", "type": "enum", "enum_options": ["booking", "quote", "other"] }
          ]
        }
      ]
    },
    {
      "name": "address",
      "state_prompt": "# Mission\nCapture address and symptoms.\n\n## Steps\n...",
      "edges": [
        { "destination_state_name": "booking", "description": "Address confirmed, proceed to booking" }
      ],
      "tools": [
        {
          "type": "extract_dv",
          "name": "extract_address_details",
          "description": "Extract address when provided",
          "variables": [
            { "name": "address_postcode", "type": "text" },
            { "name": "address_line1", "type": "text" },
            { "name": "address_city", "type": "text" }
          ]
        }
      ]
    },
    {
      "name": "booking",
      "state_prompt": "# Mission\nOffer slots, book, close.\n\n## Steps\n1. Call check_availability...",
      "edges": [],
      "tools": [
        { "type": "check_availability_cal", "name": "check_availability", ... },
        { "type": "book_appointment_cal", "name": "book_calendar", ... }
      ]
    }
  ]
}
```

---

## 8. Key Differences: Multi-Prompt vs Conversation Flow

| Feature | Multi-Prompt (`states`) | Conversation Flow (`nodes`) |
|---------|------------------------|-----------------------------|
| State management | LLM-managed transitions | Deterministic node graph |
| Prompt per state | `state_prompt` | Node-level prompts |
| Per-state tools | `tools[]` per state | Per-node tools |
| Edge schema | `edges[]` with description | Visual drag-and-drop |
| Complexity | Medium | High |
| Use case | Structured conversations | Compliance-heavy workflows |
| API endpoint | `POST /create-retell-llm` | `POST /create-conversation-flow` |

---

## 9. Caching Bug (Critical)

From the Retell API reference:

> **`PATCH /update-retell-llm`** stores the update but does NOT propagate to runtime. New chat agents created after the update will still use the OLD cached prompt.

**Workaround:** Always create a NEW LLM (`POST /create-retell-llm`) with the updated prompt instead of patching the existing one.

This affects both `general_prompt` and `states` arrays on existing LLMs.

---

## 10. Tool Types Available

| Type | Purpose |
|------|---------|
| `end_call` | End the call |
| `transfer_call` | Transfer to another number |
| `check_availability_cal` | Check Cal.com availability |
| `book_appointment_cal` | Book Cal.com appointment |
| `extract_dv` | Extract dynamic variables from user input |
| `custom` | Call a custom webhook |
| `press_digit` | Collect DTMF input |
| `send_sms` | Send an SMS |
| `code` | Execute custom code |
| `mcp` | Call an MCP server tool |

---

## 11. Source URLs

- https://docs.retellai.com/build/single-multi-prompt/prompt-overview.md
- https://docs.retellai.com/build/single-multi-prompt/write-multi-prompt.md
- https://docs.retellai.com/build/single-multi-prompt/extract-dv.md
- https://docs.retellai.com/build/dynamic-variables.md
- https://docs.retellai.com/api-references/create-retell-llm
- https://docs.retellai.com/api-references/update-retell-llm
- https://docs.retellai.com/api-references/get-retell-llm
