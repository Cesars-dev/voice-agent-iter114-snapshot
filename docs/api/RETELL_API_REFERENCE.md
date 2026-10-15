# Retell API v2/v3 Endpoint Reference (Corrected)

> Generated from https://docs.retellai.com/deprecation-notice/overview — 2026-07-27.
> Supersedes deprecated endpoint paths in `RETELL-API-REFERENCE.md`, `RETELL_AI_MCP_TECHNICAL_SPECIFICATION.md`, and other docs.

Base URL: `https://api.retellai.com`
Auth: `Authorization: Bearer <RETELL_API_KEY>`
Content-Type: `application/json`

---

## Agents

| Method | Endpoint | Notes |
|--------|----------|-------|
| POST | `/v2/list-agents` | Unified list. Filter by `filter_criteria.channel: {type:"string", op:"eq", value:"voice"\|"chat"}`. Response has `items[]`, `pagination_key`, `has_more` |
| GET | `/get-agent/{agent_id}` | Get single agent |
| POST | `/create-agent` | Create draft voice agent |
| PATCH | `/update-agent/{agent_id}` | Update draft |
| DELETE | `/delete-agent/{agent_id}` | Delete |
| GET | `/get-agent-versions/{agent_id}` | Version history |
| **POST** | **`/publish-agent-version/{agent_id}`** | **Replace `POST /publish-agent/{id}` (deprecated 07/20)** |
| GET | `/get-chat-agent/{agent_id}` | Get chat agent |
| POST | `/create-chat-agent` | Create chat agent |
| PATCH | `/update-chat-agent/{agent_id}` | Update chat agent |
| DELETE | `/delete-chat-agent/{agent_id}` | Delete |

## LLMs

| Method | Endpoint | Notes |
|--------|----------|-------|
| **GET** | **`/v2/list-retell-llms`** | **Replace `GET /list-retell-llms` (deprecated 06/15)** |
| GET | `/get-retell-llm/{llm_id}` | Get single LLM |
| POST | `/create-retell-llm` | Create LLM |
| PATCH | `/update-retell-llm/{llm_id}` | Update (caching — see note below: good for small tweaks, not hard iterations) |
| DELETE | `/delete-retell-llm/{llm_id}` | Delete |

### LLM PATCH caching behavior (verified 2026-08-02)

`PATCH /update-retell-llm/{llm_id}` **does apply** and `GET /get-retell-llm/{llm_id}` reflects the change immediately. However Retell caches LLM behaviour, so a PATCH is reliable only for **small tweaks** (e.g. editing a prompt line) on a disposable test LLM.

Do **not** rely on PATCH for **hard iterations** — changing the model, adding/removing tools, restructuring states, or large prompt rewrites. For those, **create a new LLM** (prefer create new). A patched LLM can serve stale/partial behaviour for a given version, and mixing iterations on one `llm_id` makes rollback and comparison impossible.

Rule of thumb: **tweak → PATCH on the disposable test LLM; iterate → create a fresh LLM** (never patch the V3.1 or production LLM).

## Voice Calls

| Method | Endpoint | Notes |
|--------|----------|-------|
| **POST** | **`/v3/list-calls`** | **Replace `POST /v2/list-calls` (deprecated 06/15)** |
| GET | `/v2/get-call/{call_id}` | Full record (transcript, costs, latency, analysis) |
| POST | `/v2/create-phone-call` | Outbound PSTN |
| POST | `/v2/create-web-call` | WebRTC |
| POST | `/v2/register-phone-call` | Custom telephony |
| PATCH | `/v2/update-call/{call_id}` | **Ended calls only after 08/31/2026** |
| **PATCH** | **`/v2/update-live-call/{call_id}`** | **Use for ongoing calls. Fields: `fields_to_override.override_dynamic_variables`** |
| POST | `/v2/stop-call/{call_id}` | End live call |
| DELETE | `/v2/delete-call/{call_id}` | Soft-delete |
| PUT | `/v2/rerun-call-analysis/{call_id}` | Rerun QA |

## Chat Sessions

| Method | Endpoint | Notes |
|--------|----------|-------|
| **POST** | **`/v3/list-chats`** | **Replace `POST /v2/list-chats` / `GET /list-chat` (deprecated 06/15)** |
| GET | `/get-chat/{chat_id}` | Full transcript + variables + cost |
| POST | `/create-chat` | Start session |
| POST | `/create-chat-completion` | Send message |
| PATCH | `/end-chat/{chat_id}` | End session |
| PATCH | `/update-chat/{chat_id}` | Update metadata |

## Phone Numbers

| Method | Endpoint | Notes |
|--------|----------|-------|
| **GET** | **`/v2/list-phone-numbers`** | **Replace `GET /list-phone-numbers` (deprecated 06/15)** |
| GET | `/get-phone-number/{number}` | Get config |
| POST | `/create-phone-number` | Purchase |
| PATCH | `/update-phone-number/{number}` | **Use `inbound_agents: [{agent_id, agent_version, weight}]` instead of `inbound_agent_id` (deprecated 03/31)** |
| DELETE | `/delete-phone-number/{number}` | Release |
| POST | `/import-phone-number` | Import SIP |

## Knowledge Bases

| Method | Endpoint | Notes |
|--------|----------|-------|
| POST | `/create-knowledge-base` | **`multipart/form-data`** (not JSON). Fields: `knowledge_base_name`, `knowledge_base_texts` (JSON array of `{title, text}`), `knowledge_base_files`, `knowledge_base_urls` |
| GET | `/get-knowledge-base/{kb_id}` | |
| GET | `/list-knowledge-bases` | |
| DELETE | `/delete-knowledge-base/{kb_id}` | |

## Voices

| Method | Endpoint |
|--------|----------|
| GET | `/list-voices` |
| GET | `/get-voice/{voice_id}` |
| POST | `/add-voice` |
| POST | `/clone-voice` |
| POST | `/search-voice` |

## Conversation Flows

| Method | Endpoint | Notes |
|--------|----------|-------|
| **GET** | **`/v2/list-conversation-flows`** | **Replace `GET /list-conversation-flows` (deprecated 06/15)** |
| **GET** | **`/v2/list-conversation-flow-components`** | **Replace legacy (deprecated 06/15)** |
| GET | `/get-conversation-flow/{id}` | |
| POST | `/create-conversation-flow` | Deprecated: `tools`/`tool_ids` on `type:"conversation"` nodes — use `subagent` node type instead |
| PATCH | `/update-conversation-flow/{id}` | |
| DELETE | `/delete-conversation-flow/{id}` | |

## Testing

| Method | Endpoint | Notes |
|--------|----------|-------|
| **GET** | **`/v2/list-batch-tests`** | **Replace `GET /list-batch-tests` (deprecated 06/15)** |
| **GET** | **`/v2/list-test-case-definitions`** | **Replace legacy (deprecated 06/15)** |
| **GET** | **`/v2/list-test-runs/{job_id}`** | **Replace legacy (deprecated 06/15)** |
| POST | `/create-test-case-definition` | |
| POST | `/create-batch-test` | |
| GET | `/get-batch-test/{id}` | |
| GET | `/get-test-run/{id}` | |

## Account & Utilities

| Method | Endpoint |
|--------|----------|
| GET | `/get-concurrency` |
| POST | `/create-batch-call` |
| POST | `/list-export-requests` |

## Post-Call Analysis

Replace deprecated fields with `post_call_analysis_data` system presets:

| Deprecated field (removed 06/15) | Replacement `type:"system-presets"` entry |
|---|---|
| `analysis_summary_prompt` | `{ "type": "system-presets", "name": "call_summary", "description": "..." }` |
| `analysis_successful_prompt` | `{ "type": "system-presets", "name": "call_successful", "description": "..." }` |
| `analysis_user_sentiment_prompt` | `{ "type": "system-presets", "name": "user_sentiment", "description": "..." }` |

## Transfer Call

| Deprecated field (removed 01/23) | Replacement |
|---|---|
| `show_transferee_as_caller` | `cold_transfer_mode` parameter |

## MCP Server

- **URL:** `https://mcp.retellai.com` (not `retell.stlmcp.com`)
- **Auth:** Bearer token (unchanged)
- **MCP tools internally use current API versions** — REST endpoint paths in this doc are for direct HTTP calls only
