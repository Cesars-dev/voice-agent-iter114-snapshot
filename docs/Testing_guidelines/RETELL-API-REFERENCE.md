# Retell AI API Quick Reference

> Every endpoint, payload, and gotcha you need to build and test voice agents via the Retell API. No fluff.
>

**Base URL:** `https://api.retellai.com`
**Auth:** `Authorization: Bearer <YOUR_API_KEY>`
**Content-Type:** `application/json` on all requests

---

## 1. CREATE AN LLM (The Agent's Brain)

This is the prompt, model, and tools. The LLM is the brain — the agent is the body.

```
POST /create-retell-llm

{
  "model": "gpt-4.1",
  "general_prompt": "<YOUR FULL PROMPT TEXT>",
  "begin_message": "Hi, thanks for calling. How can I help you?",
  "general_tools": [
    {
      "type": "end_call",
      "name": "end_call",
      "description": "End the call when conversation is complete or caller is hostile."
    },
    {
      "type": "transfer_call",
      "name": "transfer_call",
      "description": "Transfer the caller to live staff.",
      "transfer_destination": {
        "type": "predefined",
        "number": "{{transfer_number}}",
        "ignore_e164_validation": true
      },
      "transfer_option": {
        "type": "warm_transfer",
        "cold_transfer_mode": "sip_invite"
      }
    },
    {
      "type": "custom",
      "name": "your_custom_function",
      "description": "What this function does.",
      "url": "https://your-webhook-url.com/endpoint",
      "speak_during_execution": true,
      "speak_after_execution": true,
      "parameters": {
        "type": "object",
        "properties": {
          "param_name": {
            "type": "string",
            "description": "What this param is"
          }
        },
        "required": ["param_name"]
      }
    }
  ]
}

Response: { "llm_id": "llm_abc123..." }
```

### Tool Types
| Type | What It Does | Required Fields |
|------|-------------|-----------------|
| `end_call` | Hangs up | `name`, `description` |
| `transfer_call` | Transfers to a phone number | `name`, `description`, `transfer_destination`, `transfer_option` |
| `custom` | Calls your webhook URL | `name`, `description`, `url`, `parameters` |

---

## 2. CREATE A VOICE AGENT

The agent wraps the LLM in voice settings — voice, speed, interruption handling, etc.

```
POST /create-agent

{
  "agent_name": "My Agent",
  "response_engine": {
    "type": "retell-llm",
    "llm_id": "<LLM_ID>"
  },
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
  "post_call_analysis_data": [
    {
      "type": "string",
      "name": "caller_name",
      "description": "Name of the caller"
    },
    {
      "type": "boolean",
      "name": "appointment_booked",
      "description": "Was an appointment booked?"
    },
    {
      "type": "enum",
      "name": "call_outcome",
      "description": "How the call ended",
      "choices": ["booked", "transferred", "not_interested", "wrong_number"]
    }
  ],
  "post_call_analysis_model": "gpt-4.1"
}

Response: { "agent_id": "agent_abc123..." }
```

### Default Voice Settings (Use These)
| Setting | Value | Why |
|---------|-------|-----|
| `voice_id` | `minimax-Nia` | Natural female voice |
| `voice_speed` | `1.2` | Slightly faster = more natural |
| `responsiveness` | `1.0` | Maximum — no delay |
| `interruption_sensitivity` | `0.8` | High — lets caller interrupt |
| `enable_backchannel` | `true` | Agent says "mm-hmm" while listening |
| `end_call_after_silence_ms` | `60000` | Hangs up after 1 min silence |
| `normalize_for_speech` | `true` | Converts numbers to spoken words |
| `denoising_mode` | `noise-and-background-speech-cancellation` | Removes background noise |

---

## 3. PUBLISH THE AGENT

**You MUST publish after every change.** Unpublished changes don't go live.

```
POST /publish-agent-version/<AGENT_ID>

{
  "version_description": "Initial Build"
}
```

Use a 2-word description every time (e.g., "Fixed Hallucination", "Added Examples").

---

## 4. CREATE A CHAT AGENT (For Testing)

Creates a text-based interface to the same LLM. Used for testing without making phone calls.

```
POST /create-chat-agent

{
  "response_engine": {
    "type": "retell-llm",
    "llm_id": "<SAME_LLM_ID>"
  }
}

Response: { "agent_id": "agent_chat_abc123..." }
```

---

## 5. CHAT API (Testing Conversations)

### Start a new conversation
```
POST /create-chat

{
  "agent_id": "<CHAT_AGENT_ID>"
}

Response: { "chat_id": "chat_abc123..." }
```

### Send a message (caller turn)
```
POST /create-chat-completion

{
  "agent_id": "<CHAT_AGENT_ID>",
  "chat_id": "<CHAT_ID>",
  "content": "Hi, I need to schedule an appointment"
}

Response: {
  "messages": [
    { "role": "agent", "content": "Sure thing. Are you a new or existing customer?" },
    { "role": "tool_call_invocation", "name": "check_availability", "arguments": "..." },
    { "role": "tool_call_result", "content": "...", "successful": true }
  ]
}
```

**NOTE:** This is NOT OpenAI's chat completion format. The request uses flat `agent_id` + `chat_id` + `content`, NOT a `messages` array. The response returns `messages` array directly, NOT OpenAI's `choices` format.

### Get full chat data (transcript, variables, tools, cost)
```
GET /get-chat/<CHAT_ID>

Response contains:
- transcript: full conversation text
- collected_dynamic_variables: all extracted variables and their values
- message_with_tool_calls: full array of every message and tool call
- chat_cost: combined_cost and per-product breakdown
- chat_analysis: summary, sentiment, success status
```

Use this after testing to analyze the agent's behavior in detail.

### End a conversation
```
PATCH /end-chat/<CHAT_ID>
```

### How to read responses
The `messages` array contains everything the agent did on that turn:
- `role: "agent"` — What the agent said (the text response)
- `role: "tool_call_invocation"` — Agent decided to call a tool (check the `name` and `arguments`)
- `role: "tool_call_result"` — The tool's response (`successful: true/false`)
- `role: "state_transition"` — State machine transition (`former_state_name` → `new_state_name`)

---

## 6. CLEANUP ENDPOINTS

### List all agents (unified)
```
POST /v2/list-agents

Response: { "items": [ { "agent_id": "...", ... } ], "pagination_key": "...", "has_more": false }
```
Use `filter_criteria.channel: {type:"string", op:"eq", value:"chat"}` for chat-only results.

### Delete a chat agent
```
DELETE /delete-chat-agent/<AGENT_ID>
```

### Delete a voice agent
```
DELETE /delete-agent/<AGENT_ID>
```

**Always clean up after testing.** Chat agents pollute your dashboard. Delete them all when done. Verify with a second `POST /v2/list-agents` (with `channel: "chat"` filter) — should return empty `items[]`.

---

## 7. OTHER USEFUL ENDPOINTS

### List all voice agents
```
POST /v2/list-agents
```
Use `filter_criteria.channel: {type:"string", op:"eq", value:"voice"}` for voice-only results.

### Get a specific agent
```
GET /get-agent/<AGENT_ID>
```

### Get a specific LLM
```
GET /get-retell-llm/<LLM_ID>
```

### Update an LLM (USE WITH CAUTION)
```
PATCH /update-retell-llm/<LLM_ID>

{ "general_prompt": "<UPDATED_PROMPT>" }
```

**WARNING — CACHING BUG:** This endpoint stores the update but does NOT propagate it to runtime. New chat agents created after the update will still use the OLD cached prompt. See the gotcha below.

---

## 8. DYNAMIC VARIABLES

Variables you can use in your prompt with `{{double_curly_braces}}`:

### System Variables (Available Automatically)
| Variable | Description |
|----------|-------------|
| `{{current_time_America/new_york}}` | Current time (change timezone as needed) |
| `{{current_calendar_America/new_york}}` | 14-day calendar view |
| `{{from_number}}` / `{{user_number}}` | Caller's phone number (inbound) |
| `{{call_id}}` | Unique call identifier |
| `{{direction}}` | "inbound" or "outbound" |

### Custom Variables
Set these per-agent in the Retell dashboard or via API. Reference them in the prompt:
- `{{transfer_number}}` — where to transfer calls
- `{{business_name}}` — inject the client's business name
- `{{agent_name}}` — the AI receptionist's name

---

## 9. CRITICAL GOTCHAS

### The LLM Caching Bug
`PATCH /update-retell-llm` returns success but the runtime keeps using the old prompt. Even new chat agents created after the update get the cached version.

**Fix:** ALWAYS create a brand new LLM (`POST /create-retell-llm`) instead of updating. Then create a new agent pointing to the new LLM. Delete the old ones.

### Chat Agents Pollute Your Dashboard
Every `POST /create-chat-agent` adds an agent to your account. They don't auto-delete. After every test session, run `POST /v2/list-agents` (with `channel: "chat"` filter) and delete all of them.

### Can't Swap Response Engine After Publish
Once an agent is published with a specific LLM, you can't change its `response_engine` to point to a different LLM. You have to create a new agent entirely.

### Custom Tool Webhook Format
When Retell calls your webhook, it sends the tool arguments in the request body. Your endpoint should return a JSON response — that response gets passed back to the LLM as the tool result. The agent reads your response and decides what to say next.

### Transfer Tool Requires Specific Structure
The `transfer_call` tool MUST have `transfer_destination` (not just `number`) and `transfer_option`. See the exact format in Section 1 above. Getting this wrong returns a 400 error.

### Publish After Every Change
If you create or modify an agent and don't publish, the changes don't go live for phone calls. Always `POST /publish-agent-version/<ID>` after any change.

---

## 10. COST REFERENCE

| Action | Cost |
|--------|------|
| Chat API message | ~$0.017 per message (GPT-4.1) |
| Batch simulation | FREE |
| Voice call (inbound/outbound) | Per-minute pricing (see Retell dashboard) |
| Agent creation | Free |
| LLM creation | Free |

---

## 11. PYTHON HELPER PATTERN

Every deploy/test script in this kit uses this same pattern:

```python
import json
import urllib.request

API_KEY = "your_api_key_here"
BASE_URL = "https://api.retellai.com"

def api_call(method, endpoint, data=None):
    url = f"{BASE_URL}/{endpoint}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {API_KEY}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        print(f"API Error {e.code}: {e.read().decode()}")
        raise

# Example: Create an LLM
llm = api_call("POST", "create-retell-llm", {
    "model": "gpt-4.1",
    "general_prompt": "Your prompt here",
    "general_tools": [],
    "begin_message": "Hi, how can I help?"
})
print(f"LLM ID: {llm['llm_id']}")
```

No external dependencies. Uses only Python's built-in `urllib`. Works on any machine with Python 3 installed.
