# DEPLOYMENT-SOP.md — Retell API deployment rules

> Written 2026-08-23 after a session of trial-and-error deploy failures.
> Every rule below was learned from a real 400/404 error this session.
> READ THIS BEFORE ANY deploy script. Check `docs/api/MCP_SPECIFICATION.md` §6–7 for full schemas.

## 0. Golden rules

1. **Check `docs/api/` FIRST.** Every failure today had its answer in MCP_SPECIFICATION.md or RETELL_API_REFERENCE.md. Decode the 400 message fully — Retell lists every missing property per schema branch before you guess.
2. **Never patch LLMs in production lineage.** PATCH caching makes hard iterations unreliable (RETELL_API_REFERENCE.md:38–44). Iterate = create new LLM. Tweak-on-disposable-only.
3. **KB upsert order:** add new source FIRST, poll until registered (10× 1.5 s), THEN delete old source. Retell forbids deleting an LLM's last KB source.
4. **Create agents fresh per test matrix; never mutate deployed agents mid-experiment.**

## 1. API keys — path resolution

- Keys live ONLY in the repo root `.env`: `/home/julio/projects/<repo>/.env`.
- **NEVER resolve `.env` via relative `../..` chains from nested script folders** — depth miscounting silently loads the WRONG `.env` (or none). This caused real `401 Invalid API Key` responses ("Bearer None").
- Correct pattern:
  ```python
  ROOT = "/home/julio/projects/Retell_AI_MCP_connection"   # absolute, no ".."
  KEY = None
  for line in open(os.path.join(ROOT, ".env")):
      if line.strip().startswith("RETELL_API_KEY="):
          KEY = line.strip().split("=", 1)[1]
  ```
- Sanity-check before first call: expected prefix `key_2f615e…`. If unset → do NOT send `"Bearer None"`; fail loudly.
- `os.environ` fallback is fine but must not mask a missing-file condition.

## 2. Endpoints — use these exact routes

| Purpose | Route | Notes |
|---|---|---|
| Create LLM | `POST /create-retell-llm` | Returns **`llm_id`** (there is NO `retell_llm_id` key in response) |
| List LLMs | `POST /v2/list-retell-llms` | ✅ preferred |
| ~~Get LLM~~ | ~~`GET /get-retell-llm/{id}`~~ | ❌ deprecated path-style — **do not use for LLM**; filter the `/v2/list` call by id instead |
| Update LLM | `PATCH /update-retell-llm/{id}` | disposable tweaks ONLY (see golden rule 2) |
| Create chat agent | `POST /create-chat-agent` | requires `response_engine` OBJECT (see §4) |
| Start chat | `POST /create-chat` | body `{"agent_id": …}` → returns `chat_id` |
| Send turn | `POST /create-chat-completion` | ❌ NOT `/create-chat-turn` (404). Body `{"agent_id", "chat_id", "content"}` |
| Fetch transcript | `GET /get-chat/{chat_id}` | includes collected dynamic vars |
| List chats | `POST /v3/list-chats` | replaces v2 list-chats |

Deprecated-path rule of thumb: if RETELL_API_REFERENCE.md shows an endpoint in **bold**, that's the replacement to use; the unbolded path-style twin is legacy.

## 3. `create-retell-llm` payload contract

Top level (all verified required or strongly recommended):

```json
{
  "model": "gpt-5.1",
  "general_prompt": "...",
  "starting_state": "main",
  "start_speaker": "agent",
  "begin_message": "Hello! ...",
  "states": [ ... ]
}
```

- `starting_state` **required** — omitting it → `400 Have defined states, but no starting state defined`.
- Recommended parity with production builds: `model_temperature: 0.1`, `tool_call_strict_mode: true`.

### States
- State with no own prompt: **OMIT the `state_prompt` key entirely**. `"state_prompt": null` → `400 state_prompt must be string`.
- Edge object shape:
  ```json
  {"description": "When X is true, move on.", "destination_state_name": "verify"}
  ```
  - Field is **`destination_state_name`** (not `destination_state`) → else `400 must have required property 'destination_state_name'`.
  - No `conditions` array needed for simple transitions; the `description` carries the routing intent.

### Tools inside states
- **`extract_dynamic_variable`**: every entry in `variables[]` **requires a `description`** — omission → `400 variables/N must have required property 'description'` (+ oneOf cascade noise).
  ```json
  {
    "type": "extract_dynamic_variable",
    "name": "capture_details",
    "description": "...",
    "speak_after_execution": false,
    "variables": [{"name": "first_name", "type": "string", "description": "Caller's first name"}]
  }
  ```
- **`code` tool**:
  ```json
  {"type": "code", "name": "check_details", "description": "...",
   "code": "<js body, uses dv.*, return {...}>",
   "timeout_ms": 5000, "speak_during_execution": false, "speak_after_execution": true}
  ```
- **`end_call`**: needs `name`, `description`, `speak_after_execution`, optional `execution_message`.
- Tool `type` strings are strict enums — a typo produces a giant oneOf dump listing every allowed alternative (`end_call`, `transfer_*`, `press_digit`, `sms_content`, `code`, `mcp`, …). Read it; the answer is in the dump.

## 4. `create-chat-agent` payload contract

```json
{
  "agent_name": "SANDBOX repair R1",
  "response_engine": {"type": "retell-llm", "llm_id": "llm_xxx", "version": 0}
}
```

- `response_engine` must be an **object**, never a bare string id → else `400 response_engine must be object` (MCP_SPECIFICATION.md:513).
- Chat agents work immediately for `create-chat-completion` without a separate publish step (verified).

## 5. Driving chats (test harness)

- Turn loop:
  ```python
  api("POST", "create-chat-completion", {"agent_id": A, "chat_id": C, "content": user_text})
  ```
- Response parsing: assistant text lives in `resp["messages"]` — take last entry with `role in ("agent","assistant")` and non-empty `content`. Tool invocations appear as entries with `"type": "tool_call"` carrying `name`.
- Session record with final dynamic variables: `GET /get-chat/{chat_id}` → `collected_dynamic_variables`.
- GPT caller personas: system prompt + trailing user turn quoting the agent line, ≤25-word replies, temperature ~0.7.

## 6. Pre-flight checklist (run before EVERY deploy)

1. `.env` resolved absolutely; key prefix printed & matches expectation.
2. Payload passes local JSON-schema sanity: `starting_state` present, no null `state_prompt`, all `variables[]` have `description`, edge fields say `destination_state_name`.
3. Code-tool JS smoke-tested locally in node with dirty + clean fixtures (free — catches syntax + logic before spending API calls).
4. After deploy: print agent_id + llm_id into `sandbox_agents.json`-style registry immediately.
5. First live call = cheapest possible probe (one completion turn), verify tool fires, THEN run the full matrix.

## 7. Known account facts

- Current workspace key prefix `key_2f615e…`; dead workspace ids (e.g. `agent_74b34a77…`) 404 forever — ignore, don't chase.
- Verify which account sandbox artifacts landed on via `POST /v2/list-agents` filtered to your SANDBOX names before burning test credits.

## KB Upsert Payload Gotcha (2026-08-25)
`add-knowledge-base-sources/{kb_id}` takes ONE multipart part: field `knowledge_base_texts`
= JSON array `[{"title": slug, "text": text}]`. Do NOT pre-wrap the array in its own multipart
before nesting it in the form — nested boundaries make Retell see zero sources
(400 "no knowledge base sources provided"). Path 2 (content-changed upsert) is rare:
it only fires when sha256 differs, so test it by editing a KB before trusting a new deploy script.
