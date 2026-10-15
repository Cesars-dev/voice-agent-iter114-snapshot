# Retell AI MCP Server — Complete Technical Specification for Agent Integration

> **Document purpose.** This is a self-contained, agent-readable technical reference for connecting any MCP-capable LLM agent (Cursor, Claude Desktop, Claude Code, Codex, custom agent, etc.) to the **Retell AI MCP Server**. It covers the MCP transport, every tool surface, the underlying REST API, request/response schemas, lifecycle flows, rate limits, webhooks, error handling, and security.
>
> **Source authority.** Compiled from the official Retell docs (`docs.retellai.com`), the Retell blog (`retellai.com/blog`), and the official-grade community MCP server (`github.com/sunnysingh100/retell-mcp-server`) which achieves 100% parity with the Retell Node.js SDK (60 tools across 14 domains).


---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Architecture at a Glance](#2-architecture-at-a-glance)
3. [Authentication & Connection](#3-authentication--connection)
4. [Client Configuration Recipes](#4-client-configuration-recipes)
5. [MCP Protocol Surface](#5-mcp-protocol-surface)
6. [Complete Tool Catalog (60 Tools, 14 Domains)](#6-complete-tool-catalog-60-tools-14-domains)
7. [Detailed Tool Specifications](#7-detailed-tool-specifications)
8. [Underlying REST API Reference](#8-underlying-rest-api-reference)
9. [Core Object Schemas](#9-core-object-schemas)
10. [End-to-End Worked Examples](#10-end-to-end-worked-examples)
11. [Concurrency, Limits & Rate Limits](#11-concurrency-limits--rate-limits)
12. [Webhooks, Alerts & Incidents](#12-webhooks-alerts--incidents)
13. [Error Handling](#13-error-handling)
14. [Security Best Practices](#14-security-best-practices)
15. [Migration Path: REST ↔ MCP ↔ SDK](#15-migration-path-rest--mcp--sdk)
16. [Appendix A: Full Endpoint Reference](#appendix-a-full-endpoint-reference)
17. [Appendix B: Troubleshooting Matrix](#appendix-b-troubleshooting-matrix)

---

## 1. Executive Summary

Retell AI is a platform to build, test, deploy, and monitor AI voice (and chat) agents — inbound and outbound — with telephony, prompts, tools, knowledge bases, voices, and analytics built in. Retell exposes its platform through three equivalent surfaces:

| Surface | Endpoint | Use case |
|---|---|---|
| **REST API** | `https://api.retellai.com` | Programmatic access from any language |
| **Official SDKs** | `retell-sdk` (Node/TS) and `retell` (Python) | Typed wrappers over the REST API |
| **MCP Server** | `https://mcp.retellai.com` | Agentic access from LLM clients (Cursor, Claude Desktop, Claude Code, Codex, custom agents) |

**MCP is simply a different interface to the same core functionality** as the REST API and SDKs. Whatever you can do via REST/SDK you can do via MCP, expressed as tools rather than HTTP endpoints.

### What you get out of the box

- A single hosted MCP endpoint (no server to run).
- ~60 tools covering 14 resource domains.
- Automatic tool discovery via `tools/list` (returns name, description, JSON input schema).
- Bearer-token authentication using your existing Retell API key.
- Streamable HTTP transport (works with any MCP 2025-06-18 compatible client).

### What you need to bring

1. A Retell account and API key (Dashboard → API Keys).
2. An MCP-capable client.
3. That's it — no proxy, no self-hosting, no SDK install required (though SDKs remain useful for non-LLM code paths).

---

## 2. Architecture at a Glance

```
┌──────────────────────┐         Streamable HTTP          ┌────────────────────────┐
│  MCP Client (agent)  │  ────────────────────────────►   │  Retell MCP Server     │
│  - Cursor            │   POST https://mcp.retellai.com  │  https://mcp.retellai.com │
│  - Claude Desktop    │   Authorization: Bearer <KEY>    │                        │
│  - Claude Code       │   Content-Type: application/json │  Tools (JSON-RPC):     │
│  - Codex             │   MCP 2025-06-18 protocol        │  - tools/list          │
│  - Custom agent      │                                  │  - tools/call          │
└──────────────────────┘                                  │  - resources/list      │
        ▲                                                 │  - prompts/list        │
        │ LLM decides which tool to call                  └───────────┬────────────┘
        │ and with what args.                                          │
        │                                                              ▼
┌───────┴────────────┐                                  ┌──────────────────────────┐
│  LLM (GPT/Claude/  │                                  │  Retell REST API         │
│  Gemini/etc.)      │                                  │  https://api.retellai.com│
└────────────────────┘                                  │  - /v2/create-phone-call│
                                                        │  - /v2/create-web-call  │
                                                        │  - /v3/list-calls       │
                                                        │  - /create-agent        │
                                                        │  - /v2/list-phone-numbers  │
                                                        │  - /get-concurrency     │
                                                        │  - ... (50+ endpoints)  │
                                                        └──────────┬───────────────┘
                                                                   │
                                                                   ▼
                                                        ┌──────────────────────────┐
                                                        │  Retell Platform         │
                                                        │  - Telephony (PSTN/SIP)  │
                                                        │  - LLMs (GPT, Claude,    │
                                                        │    Gemini, Retell-LLM)   │
                                                        │  - TTS / STT providers   │
                                                        │  - Knowledge bases (RAG) │
                                                        │  - QA / Analytics        │
                                                        └──────────────────────────┘
```

The MCP server is a thin protocol adapter that translates `tools/call` JSON-RPC invocations into REST calls against `api.retellai.com`. The full surface area of the Retell Node SDK (≈60 methods) is mirrored 1-to-1 as MCP tools.

---

## 3. Authentication & Connection

### 3.1 API Key

- Generate from the Retell Dashboard → **API Keys** tab.
- A single key works for REST, SDK, and MCP — no separate MCP token.
- Keys are org-scoped. Apply **least privilege**: prefer read-only keys for exploration, restricted-scope keys for production agent workflows.

### 3.2 MCP Server URL

```
https://mcp.retellai.com
```

> **Note:** The legacy server at `https://retell.stlmcp.com` has been removed (07/20/2026). Use `https://mcp.retellai.com`.

### 3.3 Authentication Header

```
Authorization: Bearer <RETELL_API_KEY>
```

Send this header on **every** MCP HTTP request, including:
- The initial `initialize` handshake.
- The long-poll/streaming `notifications/initialized` exchange.
- Every `tools/list` and `tools/call` JSON-RPC request.
- Any `resources/*` or `prompts/*` invocations.

### 3.4 Transport

Retell's MCP server uses **Streamable HTTP** (the MCP 2025-06-18 spec's HTTP transport). Single endpoint, POST-only, server may upgrade to `text/event-stream` for streaming responses. Clients do not need to open a separate stdio process.

### 3.5 Required HTTP semantics

| Header | Value |
|---|---|
| `Authorization` | `Bearer <RETELL_API_KEY>` |
| `Content-Type` | `application/json` |
| `Accept` | `application/json, text/event-stream` |
| `MCP-Protocol-Version` (recommended) | `2025-06-18` |

JSON-RPC 2.0 envelope for every call:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "retell_create_phone_call",
    "arguments": {
      "from_number": "+14157774444",
      "to_number": "+12137774445"
    }
  }
}
```

---

## 4. Client Configuration Recipes

### 4.1 Cursor

`Command Palette → Cursor Settings → MCP → Add new global MCP server`:

```json
{
  "mcpServers": {
    "retell": {
      "url": "https://mcp.retellai.com",
      "headers": {
        "Authorization": "Bearer <RETELL_API_KEY>"
      }
    }
  }
}
```

### 4.2 Claude Desktop

`Settings → Developer → Edit Config` (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "retell": {
      "url": "https://mcp.retellai.com",
      "headers": {
        "Authorization": "Bearer <RETELL_API_KEY>"
      }
    }
  }
}
```

If your Claude Desktop version doesn't yet support remote HTTP natively, bridge via `mcp-remote`:

```json
{
  "mcpServers": {
    "retell": {
      "command": "npx",
      "args": [
        "mcp-remote@latest",
        "https://mcp.retellai.com",
        "--header",
        "Authorization:Bearer <RETELL_API_KEY>"
      ]
    }
  }
}
```

### 4.3 Claude Code

```bash
claude mcp add --transport http retell https://mcp.retellai.com \
  --header "Authorization: Bearer <RETELL_API_KEY>"
```

### 4.4 Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.retell]
url = "https://mcp.retellai.com"
bearer_token_env_var = "RETELL_API_KEY"
```

Then:

```bash
export RETELL_API_KEY="<RETELL_API_KEY>"
```

### 4.5 Custom MCP Client (TypeScript)

```typescript
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const transport = new StreamableHTTPClientTransport(
  new URL("https://mcp.retellai.com"),
  {
    requestInit: {
      headers: {
        Authorization: `Bearer ${process.env.RETELL_API_KEY}`,
      },
    },
  }
);

const client = new Client({ name: "my-agent", version: "1.0.0" });
await client.connect(transport);

// Discover available tools
const { tools } = await client.listTools();
console.log(tools.map(t => t.name));

// Call a tool
const result = await client.callTool({
  name: "retell_create_phone_call",
  arguments: {
    from_number: "+14157774444",
    to_number: "+12137774445",
  },
});
```

### 4.6 Custom MCP Client (Python)

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import os

async with streamablehttp_client(
    "https://mcp.retellai.com",
    headers={"Authorization": f"Bearer {os.environ['RETELL_API_KEY']}"},
) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print([t.name for t in tools.tools])

        result = await session.call_tool(
            "retell_create_phone_call",
            {"from_number": "+14157774444", "to_number": "+12137774445"},
        )
        print(result.content)
```

---

## 5. MCP Protocol Surface

Retell's MCP server implements the standard MCP methods. The primary ones an agent will use:

| Method | Purpose |
|---|---|
| `initialize` | Capability handshake. Declare client info, negotiate protocol version. |
| `notifications/initialized` | Sent after `initialize` to signal the client is ready. |
| `tools/list` | Enumerate all available Retell tools with names, descriptions, and JSON input schemas. |
| `tools/call` | Invoke a specific tool with arguments. Returns `content` (list of text/resource blobs) and optional `isError` flag. |
| `resources/list` | List static Retell resources (currently limited; prefer tools for everything). |
| `prompts/list` | List predefined Retell prompt templates (limited; not the main surface). |
| `ping` | Keepalive. |

### 5.1 Tool discovery pattern

Every well-behaved Retell-connected agent should:

1. Call `initialize` once at session start.
2. Call `tools/list` to refresh the tool catalog (the catalog can grow as Retell ships features).
3. For each user intent, choose the minimal tool set. **Do not pre-call tools speculatively.**
4. After destructive calls (`retell_delete_*`, `retell_publish_*`), verify with a `retell_get_*` or `retell_list_*`.

### 5.2 Tool naming convention

All Retell tools follow the convention:

```
retell_<verb>_<resource_singular_or_plural>
```

Examples: `retell_create_agent`, `retell_list_calls`, `retell_publish_agent`, `retell_get_concurrency`.

This makes it trivial to grep tool names or restrict agent access by prefix.

---

## 6. Complete Tool Catalog (60 Tools, 14 Domains)

Below is the full tool surface, grouped by domain. The community-maintained `sunnysingh100/retell-mcp-server` achieves **100% parity with the official Node SDK**, and Retell's hosted MCP server exposes the same surface area.

### 6.1 🎙 Voices (5 tools)

| Tool | Underlying REST | Purpose |
|---|---|---|
| `retell_list_voices` | `GET /list-voices` | List available voices across providers (ElevenLabs, OpenAI, Deepgram, Cartesia, MiniMax, Retell, etc.) |
| `retell_get_voice` | `GET /get-voice` | Fetch metadata for a single voice |
| `retell_add_voice` | `POST /add-voice` | Upload / register a new custom voice |
| `retell_clone_voice` | `POST /clone-voice` | Clone a voice from an audio sample |
| `retell_search_voice` | `POST /search-voice` | Search the community voice library |

### 6.2 🧠 LLMs (5 tools)

| Tool | Underlying REST | Purpose |
|---|---|---|
| `retell_list_llms` | `GET /v2/list-retell-llms` | List Retell LLM response engines |
| `retell_get_llm` | `GET /get-retell-llm` | Fetch one Retell LLM |
| `retell_create_llm` | `POST /create-retell-llm` | Create a Retell LLM (model, params, general prompt, tools, knowledge bases) |
| `retell_update_llm` | `PATCH /update-retell-llm` | Update an existing Retell LLM |
| `retell_delete_llm` | `DELETE /delete-retell-llm` | Delete a Retell LLM |

### 6.3 📚 Knowledge Bases (6 tools)

| Tool | Underlying REST | Purpose |
|---|---|---|
| `retell_list_knowledge_bases` | `GET /list-knowledge-bases` | List all KBs |
| `retell_get_knowledge_base` | `GET /get-knowledge-base` | Fetch one KB |
| `retell_create_knowledge_base` | `POST /create-knowledge-base` | Create a new KB |
| `retell_delete_knowledge_base` | `DELETE /delete-knowledge-base` | Delete a KB |
| `retell_add_knowledge_base_sources` | `POST /add-knowledge-base-sources` | Attach sources (URLs, files, text) to a KB |
| `retell_delete_knowledge_base_source` | `DELETE /delete-knowledge-base-source` | Remove a specific source from a KB |

### 6.4 🤖 Voice Agents (7 tools)

| Tool | Underlying REST | Purpose |
|---|---|---|
| `retell_list_agents` | `POST /v2/list-agents` | List voice agents (paginated, filterable by channel=`voice`) |
| `retell_get_agent` | `GET /get-agent` | Fetch a single agent |
| `retell_create_agent` | `POST /create-agent` | Create a new voice agent |
| `retell_update_agent` | `PATCH /update-agent` | Update an agent's draft |
| `retell_delete_agent` | `DELETE /delete-agent` | Delete an agent |
| `retell_get_agent_versions` | `GET /get-agent-versions` | List all versions (draft + published history) of an agent |
| `retell_publish_agent` | `POST /publish-agent-version` | Promote a draft agent version to production |

### 6.5 📱 Phone Numbers (6 tools)

| Tool | Underlying REST | Purpose |
|---|---|---|
| `retell_list_phone_numbers` | `GET /v2/list-phone-numbers` | List all phone numbers in the org |
| `retell_get_phone_number` | `GET /get-phone-number` | Fetch one number's config (agent assignment, inbound settings) |
| `retell_create_phone_number` | `POST /create-phone-number` | Purchase/provision a new number (US or international) |
| `retell_update_phone_number` | `PATCH /update-phone-number` | Update number config (reassign agent, set webhook, etc.) |
| `retell_delete_phone_number` | `DELETE /delete-phone-number` | Release a number |
| `retell_import_phone_number` | `POST /import-phone-number` | Import a number from an external SIP/provider (custom telephony) |

### 6.6 📞 Voice Calls (8 tools)

| Tool | Underlying REST | Purpose |
|---|---|---|
| `retell_list_calls` | `POST /v3/list-calls` | Paginated call list with rich filters (date range, agent, direction, status, etc.) |
| `retell_get_call` | `GET /v2/get-call` | Fetch one call's full record: transcript, analysis, metrics, costs |
| `retell_update_call` | `PATCH /v2/update-call` | Update call metadata (mutable key/value store) |
| `retell_update_live_call` | `PATCH /v2/update-live-call` | Modify an in-progress call (override agent, inject dynamic vars, end call) |
| `retell_delete_call` | `DELETE /v2/delete-call` | Soft-delete a call record |
| `retell_create_phone_call` | `POST /v2/create-phone-call` | Initiate an outbound PSTN call |
| `retell_create_web_call` | `POST /v2/create-web-call` | Create a web (browser/WebRTC) call — returns a `call_id` + WebSocket URL the client connects to |
| `retell_register_phone_call` | `POST /v2/register-phone-call` | Register a custom-telephony call (you manage audio; Retell runs the agent) |
| `retell_stop_call` | `POST /v2/stop-call` | End a live call gracefully |
| `retell_rerun_call_analysis` | `PUT /v2/rerun-call-analysis` | Re-run post-call analysis (summary, sentiment, custom fields) |

> Note: the "8 tools" heading counts the primary call tools; with `update_live_call`, `stop_call`, `rerun_call_analysis`, and `register_phone_call` the total is 10. Domain counts vary slightly by SDK version.

### 6.7 💬 Chat Agents (7 tools)

| Tool | Underlying REST | Purpose |
|---|---|---|
| `retell_list_chat_agents` | `POST /v2/list-agents` | List chat agents (filter `channel: "chat"`) |
| `retell_get_chat_agent` | `GET /get-chat-agent` | Fetch one chat agent |
| `retell_create_chat_agent` | `POST /create-chat-agent` | Create a chat agent |
| `retell_update_chat_agent` | `PATCH /update-chat-agent` | Update a chat agent's draft |
| `retell_delete_chat_agent` | `DELETE /delete-chat-agent` | Delete a chat agent |
| `retell_get_chat_agent_versions` | `GET /get-chat-agent-versions` | List versions |
| `retell_publish_chat_agent` | `POST /publish-agent-version` | Promote draft → production |

### 6.8 💬 Chat Sessions (7 tools)

| Tool | Underlying REST | Purpose |
|---|---|---|
| `retell_list_chats` | `POST /v3/list-chats` | List chat sessions |
| `retell_get_chat` | `GET /get-chat` | Fetch one chat session |
| `retell_create_chat` | `POST /create-chat` | Start a new chat session bound to a chat agent |
| `retell_update_chat` | `PATCH /update-chat` | Update chat metadata |
| `retell_create_chat_completion` | `POST /create-chat-completion` | Send a user message and get an assistant reply (OpenAI-compatible) |
| `retell_create_sms_chat` | `POST /create-outbound-sms` | Start an SMS-based chat (long-running, asynchronous) |
| `retell_end_chat` | `PATCH /end-chat` | End an active chat session |
| `retell_rerun_chat_analysis` | `PUT /rerun-chat-analysis` | Re-run post-chat analysis |

### 6.9 🔀 Conversation Flows (5 tools)

| Tool | Underlying REST | Purpose |
|---|---|---|
| `retell_list_conversation_flows` | `GET /v2/list-conversation-flows` | List all conversation flow response engines |
| `retell_get_conversation_flow` | `GET /get-conversation-flow` | Fetch one flow |
| `retell_create_conversation_flow` | `POST /create-conversation-flow` | Create a node-based conversation flow |
| `retell_update_conversation_flow` | `PATCH /update-conversation-flow` | Update a flow |
| `retell_delete_conversation_flow` | `DELETE /delete-conversation-flow` | Delete a flow |

### 6.10 🔀 Conversation Flow Subflows & Components (5 tools each)

For reusable node-graph components:

- `retell_list_conversation_flow_subflows` / `retell_get_conversation_flow_subflow` / `retell_create_conversation_flow_subflow` / `retell_update_conversation_flow_subflow` / `retell_delete_conversation_flow_subflow`
- Same CRUD pattern for "conversation flow components" (reusable building blocks).

### 6.11 🧪 Test Cases & Batch Tests (8 tools)

| Tool | Purpose |
|---|---|
| `retell_create_test_case_definition` | Define a regression test case |
| `retell_get_test_case_definition` | Fetch one definition |
| `retell_list_test_case_definitions` | List all definitions |
| `retell_update_test_case_definition` | Update a definition |
| `retell_delete_test_case_definition` | Delete a definition |
| `retell_create_batch_test` | Run a batch of test cases against an agent |
| `retell_get_batch_test` | Get batch test status |
| `retell_list_batch_tests` | List all batch tests |
| `retell_get_test_run` | Get a single test run result |
| `retell_list_test_runs` | List test runs in a batch |

### 6.12 ⚙️ Utilities & Account (4 tools)

| Tool | Purpose |
|---|---|
| `retell_create_batch_call` | Schedule a batch of outbound calls (CSV-driven or programmatic) |
| `retell_create_batch_test` | Schedule batch regression tests |
| `retell_get_concurrency` | Get current vs. limit concurrency, burst capacity |
| `retell_get_mcp_tools` | Introspect the MCP server itself (returns the tool catalog as a tool call) |
| `retell_list_export_requests` | List data export jobs (recordings, transcripts, etc.) |

### 6.13 🚨 Alerts (subset via REST)

Although the hosted MCP server's alert tools are still rolling out (per Retell's blog), the underlying REST endpoints already exist:

| Tool (planned / available) | Underlying REST | Purpose |
|---|---|---|
| `retell_create_alert_rule` | `POST /create-alert-rule` | Define a metric threshold alert (e.g. p95 latency > 2s) |
| `retell_list_alert_rules` | `GET /list-alert-rules` | List all alert rules |
| `retell_list_incidents` | `GET /list-incidents` | List fired incidents |
| `retell_test_webhook` | `POST /test-webhook` | Send a test event to your webhook URL |

### 6.14 🔌 MCP Tool (meta)

| Tool | Underlying REST | Purpose |
|---|---|---|
| `retell_get_mcp_tools` | `GET /get-mcp-tools` | Returns the catalog of MCP tools exposed by the org — useful for runtime discovery |

### 6.15 Total tool count: **~60** across 14 domains

Exact count varies slightly by SDK version (Retell occasionally adds or deprecates tools). Always run `tools/list` at session start to get the authoritative list for your account.

---

## 7. Detailed Tool Specifications

Below are the most commonly-used tools with argument schemas. Every tool returns either a JSON object (single-resource ops) or a JSON array (list ops), wrapped in MCP `content` blocks.

### 7.1 `retell_create_agent`

Creates a **voice agent**. An agent is a deployable configuration that pairs a response engine (LLM or conversation flow) with a voice, language, telephony behavior, guardrails, etc.

#### Arguments

| Argument | Type | Required | Description |
|---|---|---|---|
| `response_engine` | object | ✅ | `{type: "retell-llm", llm_id, version}` or `{type: "conversation_flow", conversation_flow_id, version}` |
| `voice_id` | string | ✅ | Primary voice ID (e.g. `retell-Cimo`) |
| `fallback_voice_ids` | string[] | optional | Voices to try if primary fails |
| `agent_name` | string | optional | Human-readable name |
| `voice_temperature` | number | optional | 0–2 (default 1) |
| `voice_speed` | number | optional | 0.5–2 (default 1) |
| `volume` | number | optional | 0–2 (default 1) |
| `voice_emotion` | string | optional | e.g. `calm`, `cheerful` |
| `enable_expressive_mode` | boolean | optional | Allow emotion tags in TTS |
| `expressive_emotion_tags` | string[] | optional | `["empathetic","excited","sigh","clear throat","emphasis"]` |
| `responsiveness` | number | optional | 0–2 — how aggressively the agent responds to user input |
| `interruption_sensitivity` | number | optional | 0–2 |
| `enable_backchannel` | boolean | optional | Enable "yeah", "uh-huh" interjections |
| `backchannel_frequency` | number | optional | 0–1 |
| `backchannel_words` | string[] | optional | Custom backchannel vocabulary |
| `reminder_trigger_ms` | number | optional | ms of silence before a reminder nudge |
| `reminder_max_count` | integer | optional | Max reminder nudges per turn |
| `ambient_sound_volume` | number | optional | 0–1 |
| `language` | string | optional | BCP-47 (e.g. `en-US`, `es-MX`) |
| `webhook_url` | string | optional | Your webhook for live call events |
| `webhook_events` | string[] | optional | Filter which events fire |
| `webhook_timeout_ms` | integer | optional | Per-event timeout |
| `boosted_keywords` | string[] | optional | STT boost words |
| `data_storage_setting` | enum | optional | `everything` \| `essential` \| `none` |
| `data_storage_retention_days` | integer | optional | Retention window |
| `opt_in_signed_url` | boolean | optional | Return signed URLs for recordings |
| `signed_url_expiration_ms` | integer | optional | URL TTL |
| `pronunciation_dictionary` | object[] | optional | `[{word, alphabet: "ipa"|"nimfa", phoneme}]` |
| `end_call_after_silence_ms` | integer | optional | Auto-hangup after silence (default 600000) |
| `max_call_duration_ms` | integer | optional | Hard cap (default 3600000) |
| `voicemail_option` | object | optional | `{action: {type: "static_text"|"dynamic_text"|"hangup", text}}` |
| `ivr_option` | object | optional | Behavior when IVR is detected |
| `call_screening_option` | object | optional | `{agent_identity, call_purpose}` for outbound screening |
| `post_call_analysis_data` | object[] | optional | Custom fields to extract post-call |
| `post_call_analysis_model` | string | optional | e.g. `gpt-4.1-mini` |
| `begin_message_delay_ms` | integer | optional | Delay before first agent utterance |
| `ring_duration_ms` | integer | optional | Outbound ring timeout |
| `stt_mode` | enum | optional | `fast` \| `accurate` |
| `custom_stt_config` | object | optional | `{endpointing_ms, ...}` |
| `vocab_specialization` | enum | optional | `general` \| `medical` \| `finance` |
| `allow_user_dtmf` | boolean | optional | Accept DTMF input |
| `allow_dtmf_interruption` | boolean | optional | DTMF interrupts agent |
| `user_dtmf_options` | object | optional | `{digit_limit, termination_key, timeout_ms}` |
| `denoising_mode` | enum | optional | `noise-cancellation` \| `off` |
| `guardrail_config` | object | optional | `{input_topics, output_topics}` for topic guardrails |
| `handbook_config` | object | optional | Behavioral toggles (default_personality, ai_disclosure, scope_boundaries, etc.) |
| `timezone` | string | optional | IANA tz, e.g. `America/Mexico_City` |

#### Returns

```json
{
  "agent_id": "oBeDLoLOeuAbiuaMFXRtDOLriTJ5tSxD",
  "agent_name": "Jarvis",
  "version": 0,
  "response_engine": { "type": "retell-llm", "llm_id": "llm_234sdertfsdsfsdf", "version": 0 },
  "voice_id": "retell-Cimo",
  "language": "en-US",
  "status": "draft",
  "created_at": "2026-07-21T01:00:00.000Z",
  "updated_at": "2026-07-21T01:00:00.000Z"
}
```

Newly created agents are **drafts**. Use `retell_publish_agent` to make them callable from production phone numbers.

### 7.2 `retell_create_phone_call`

Initiates an outbound phone call.

#### Arguments

| Argument | Type | Required | Description |
|---|---|---|---|
| `from_number` | string | ✅ | E.164 (e.g. `+14157774444`) — must be a number you own in Retell |
| `to_number` | string | ✅ | E.164 destination |
| `override_agent_id` | string | optional | Use a different agent than the one assigned to `from_number` |
| `override_agent_version` | string | optional | `latest_published` (default) or specific version |
| `agent_override` | object | optional | Per-call override of any agent field (voice, prompt, etc.) |
| `metadata` | object | optional | Arbitrary key/value (stored on the call) |
| `retell_llm_dynamic_variables` | object | optional | Inject variables into the LLM prompt at call start |
| `custom_sip_headers` | object | optional | Custom SIP headers (custom telephony only) |
| `ignore_e164_validation` | boolean | optional | Bypass number format validation |

#### Returns

```json
{
  "call_id": "Jabr9TXYYJHfvl6Syypi88rdAHYHmcq6",
  "agent_id": "oBeDLoLOeuAbiuaMFXRtDOLriTJ5tSxD",
  "call_type": "phone_call",
  "call_status": "ongoing",
  "direction": "outbound",
  "from_number": "+14157774444",
  "to_number": "+12137774445",
  "start_timestamp": 1753056000000
}
```

### 7.3 `retell_create_web_call`

Creates a **web call** — a browser/WebRTC call. Returns a `call_id` and a `call_url` your frontend opens to stream audio.

#### Arguments

| Argument | Type | Required | Description |
|---|---|---|---|
| `agent_id` | string | ✅ | Voice agent to talk to |
| `agent_version` | string | optional | `latest_published` or specific |
| `agent_override` | object | optional | Per-call override (same shape as `create_phone_call`) |
| `metadata` | object | optional | Custom metadata |
| `retell_llm_dynamic_variables` | object | optional | Inject prompt variables |
| `current_node_id` | string | optional | Start a conversation-flow call at a specific node |
| `current_state` | string | optional | Initial state label |

#### Returns

```json
{
  "call_id": "Jabr9TXYYJHfvl6Syypi88rdAHYHmcq6",
  "agent_id": "oBeDLoLOeuAbiuaMFXRtDOLriTJ5tSxD",
  "call_type": "web_call",
  "call_status": "ongoing",
  "call_url": "https://call.retellai.com/?call_id=Jabr9TXYYJHfvl6Syypi88rdAHYHmcq6"
}
```

### 7.4 `retell_list_calls`

Paginated call search.

#### Arguments

| Argument | Type | Description |
|---|---|---|
| `filter_criteria` | object | `{agent_id, call_status, direction, disconnection_reason, before_start_time, after_start_time, ...}` |
| `limit` | integer | Page size (max 1000) |
| `pagination_token` | string | Cursor from previous response |
| `sort_order` | enum | `ascending` \| `descending` (by start time) |

#### Returns

```json
{
  "calls": [ /* up to `limit` call objects */ ],
  "pagination_token": "next-cursor-or-omitted",
  "total_count": 12345
}
```

### 7.5 `retell_get_call`

Fetch one call by ID. Returns the full transcript, disconnection reason, latency percentiles, cost breakdown, and post-call analysis.

#### Returns (selected fields)

```json
{
  "call_id": "Jabr9...",
  "agent_id": "oBeDL...",
  "call_type": "phone_call" | "web_call",
  "call_status": "ongoing" | "registered" | "ended",
  "direction": "inbound" | "outbound",
  "disconnection_reason": "agent_hangup" | "user_hangup" | "timeout" | "...",
  "from_number": "+14157774444",
  "to_number": "+12137774445",
  "transfer_destination": "+12137771234",
  "start_timestamp": 1753056000000,
  "end_timestamp": 1753056600000,
  "transcript": [ /* list of {role, content, timestamp} */ ],
  "transcript_object_url": "https://retellai.s3.../transcript.txt",
  "recording_url": "https://retellai.s3.../recording.mp3",
  "realtime_breakdown": {
    "p50": 800, "p90": 1200, "p95": 1500, "p99": 2500,
    "stt": { /* latency stats */ },
    "llm": { /* latency stats */ },
    "tts": { /* latency stats */ },
    "knowledge_base": { /* latency stats */ },
    "s2s": { /* end-to-end speech-to-speech latency */ }
  },
  "call_analysis": {
    "call_summary": "The agent called the user to ask ...",
    "in_voicemail": false,
    "user_sentiment": "Positive" | "Negative" | "Neutral",
    "call_successful": true,
    "custom_analysis_data": { /* your custom fields */ }
  },
  "call_cost": {
    "product_costs": [
      { "product": "elevenlabs_tts", "cost": 60, "unit_price": 1, "is_transfer_leg_cost": false }
    ],
    "total_duration_seconds": 60,
    "total_duration_unit_price": 1,
    "combined_cost": 70
  },
  "llm_token_usage": {
    "values": [123, 456],
    "average": 289,
    "num_requests": 2
  }
}
```

### 7.6 `retell_create_phone_number`

Purchase / provision a new number.

#### Arguments

| Argument | Type | Required | Description |
|---|---|---|---|
| `area_code` | string | one of | US/Canada 3-digit area code (e.g. `415`) |
| `country` | string | one of | ISO country code (e.g. `US`, `MX`, `GB`) |
| `is_toll_free` | boolean | optional | Buy a toll-free number |
| `outbound_call_capability` | boolean | optional | Enable outbound dialing |
| `inbound_agents` | object[] | optional | Weighted agent list for inbound calls (replaces `inbound_agent_id`) |
| `nickname` | string | optional | Friendly label |

### 7.7 `retell_get_concurrency`

#### Returns

```json
{
  "current_concurrency": 10,
  "concurrency_limit": 100,
  "base_concurrency": 20,
  "purchased_concurrency": 80,
  "concurrency_purchase_limit": 100,
  "remaining_purchase_limit": 20,
  "reserved_inbound_concurrency": 10,
  "concurrency_burst_enabled": true,
  "concurrency_burst_limit": 60
}
```

Field semantics:
- `base_concurrency`: free concurrent calls included with your plan.
- `purchased_concurrency`: additional capacity you've bought.
- `concurrency_limit`: `base_concurrency + purchased_concurrency` — your soft cap.
- `concurrency_burst_enabled` / `concurrency_burst_limit`: allow temporary spikes above `concurrency_limit`.
- `reserved_inbound_concurrency`: capacity reserved so inbound calls always get a slot.

### 7.8 `retell_create_knowledge_base` and source attachment

**⚠️ CORRECTED 2026-07-30:** The API uses `multipart/form-data`, not `application/json`.
The endpoint `/add-knowledge-base-sources` does NOT exist (returns 404).
Sources must be included at creation time via `knowledge_base_texts` / `knowledge_base_files` / `knowledge_base_urls`.

```bash
curl --request POST \
  --url https://api.retellai.com/create-knowledge-base \
  --header 'Authorization: Bearer <token>' \
  --form 'knowledge_base_name=Sample KB' \
  --form 'knowledge_base_texts=[{"title": "Some Title", "text": "Content goes here..."}]'
```

**Key fields (multipart/form-data):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `knowledge_base_name` | string | ✅ | KB name (used in `##name##` prompt references). Max 40 chars. |
| `knowledge_base_texts` | string | optional | JSON array of `{title, text}` objects |
| `knowledge_base_files` | file[] | optional | Files (max 25, 50MB each) |
| `knowledge_base_urls` | string | optional | JSON array of URL strings to scrape |

**Example with all source types:**
```python
import requests
r = requests.post(
    "https://api.retellai.com/create-knowledge-base",
    headers={"Authorization": "Bearer <token>"},
    data={
        "knowledge_base_name": "my-kb",
        "knowledge_base_texts": '[{"title": "Doc title", "text": "..."}]',
        "knowledge_base_urls": '["https://example.com"]',
    },
    files={"knowledge_base_files": open("./doc.pdf", "rb")},
)
print(r.json()["knowledge_base_id"])
```

### 7.9 `retell_create_chat_completion`

OpenAI-compatible chat completion for a chat agent.

#### Arguments

```json
{
  "chat_id": "chat_abc123",
  "messages": [
    {"role": "user", "content": "What's my order status?"}
  ],
  "stream": false,
  "temperature": 0.7
}
```

#### Returns

OpenAI-style response:

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "Your order #12345 is..."}, "finish_reason": "stop"}
  ]
}
```

---

## 8. Underlying REST API Reference

For non-MCP code paths (cron jobs, webhooks handlers, server-side scripts), use the REST API directly.

### 8.1 Base URL

```
https://api.retellai.com
```

Most call/chat endpoints live under `/v2/`. Configuration endpoints (agents, phone numbers, KBs, LLMs, voices, conversation flows) live at the root.

### 8.2 Authentication

```
Authorization: Bearer YOUR_API_KEY
```

Send on every request. Missing/invalid → `401` with `{"status":"error","message":"API key is missing or invalid."}`.

### 8.3 SDKs

| Language | Package | Install |
|---|---|---|
| Node.js / TypeScript | `retell-sdk` | `npm install retell-sdk` |
| Python | `retell` | `pip install retell` |

Both SDKs auto-inject the base URL and `Authorization` header.

```typescript
import Retell from 'retell-sdk';
const client = new Retell({ apiKey: process.env.RETELL_API_KEY });
const call = await client.call.createPhoneCall({ from_number: '...', to_number: '...' });
```

```python
from retell import Retell
client = Retell(api_key=os.environ['RETELL_API_KEY'])
call = client.call.create_phone_call(from_number='...', to_number='...')
```

### 8.4 Common REST patterns

- **List endpoints** are `POST` (not `GET`) — they accept a filter JSON body.
- **Pagination** uses `pagination_token` cursors.
- **IDs** are opaque strings (e.g. `oBeDLoLOeuAbiuaMFXRtDOLriTJ5tSxD`).
- **Versions**: draft agents and chat agents are versioned. `override_agent_version: "latest_published"` is the safe default for production calls.

### 8.5 Audio WebSocket

Real-time audio for web calls uses a separate host:

```
wss://api.retellai.com/audio-websocket/{call_id}
```

Your client (browser SDK or backend) connects after `create_web_call` returns. Bidirectional 24kHz PCM audio frames. **Not** exposed via MCP — MCP is control-plane only.

---

## 9. Core Object Schemas

### 9.1 Agent (full)

```jsonc
{
  "agent_id": "oBeDLoLOeuAbiuaMFXRtDOLriTJ5tSxD",
  "agent_name": "Jarvis",
  "version": 0,
  "version_title": "Production hotfix",
  "version_description": "Customer support agent for handling product inquiries",
  "status": "draft" | "published",

  "response_engine": {
    "type": "retell-llm" | "conversation_flow",
    "llm_id": "llm_234sdertfsdsfsdf",         // if retell-llm
    "conversation_flow_id": "cf_xxx",          // if conversation_flow
    "version": 0
  },

  "voice_id": "retell-Cimo",
  "fallback_voice_ids": ["cartesia-Cimo", "minimax-Cimo"],
  "voice_temperature": 1,
  "voice_speed": 1,
  "volume": 1,
  "voice_emotion": "calm",
  "enable_expressive_mode": true,
  "expressive_emotion_tags": ["empathetic","excited","sigh","clear throat","emphasis"],
  "expressive_mode_prompt": "Use [sigh] for thoughtful pauses and [excited] for good news.",

  "responsiveness": 1,
  "interruption_sensitivity": 1,
  "enable_backchannel": true,
  "backchannel_frequency": 0.9,
  "backchannel_words": ["yeah","uh-huh"],
  "reminder_trigger_ms": 10000,
  "reminder_max_count": 2,
  "ambient_sound_volume": 1,

  "language": "en-US",
  "webhook_url": "https://webhook-url-here",
  "webhook_events": [],
  "webhook_timeout_ms": 10000,
  "boosted_keywords": ["retell","kroger"],

  "data_storage_setting": "everything" | "essential" | "none",
  "data_storage_retention_days": 30,
  "opt_in_signed_url": true,
  "signed_url_expiration_ms": 86400000,

  "pronunciation_dictionary": [
    {"word":"actually","alphabet":"ipa","phoneme":"ˈæktʃuəli"}
  ],
  "end_call_after_silence_ms": 600000,
  "max_call_duration_ms": 3600000,

  "voicemail_option": { "action": {"type":"static_text","text":"Please give us a callback tomorrow at 10am."} },
  "ivr_option": { "action": {"type":"hangup"} },
  "call_screening_option": {
    "agent_identity": "Acme Health scheduling team",
    "call_purpose": "confirming your appointment for tomorrow"
  },

  "post_call_analysis_data": [
    {
      "type": "string",
      "name": "customer_name",
      "description": "The name of the customer.",
      "examples": ["John Doe","Jane Smith"],
      "required": true,
      "conditional_prompt": "<string>"
    }
  ],
  "post_call_analysis_model": "gpt-4.1-mini",

  "begin_message_delay_ms": 1000,
  "ring_duration_ms": 30000,
  "stt_mode": "fast" | "accurate",
  "custom_stt_config": { "endpointing_ms": 123 },
  "vocab_specialization": "general" | "medical" | "finance",
  "allow_user_dtmf": true,
  "allow_dtmf_interruption": false,
  "user_dtmf_options": { "digit_limit": 25.5, "termination_key": "#", "timeout_ms": 8000 },
  "denoising_mode": "noise-cancellation" | "off",
  "guardrail_config": { "input_topics": ["platform_integrity_jailbreaking"], "output_topics": [] },
  "handbook_config": {
    "default_personality": true,
    "conversational_personality": true,
    "natural_filler_words": true,
    "high_empathy": true,
    "echo_verification": true,
    "nato_phonetic_alphabet": true,
    "speech_normalization": true,
    "smart_matching": true,
    "ai_disclosure": true,
    "scope_boundaries": true
  },
  "timezone": "America/New_York"
}
```

### 9.2 Retell LLM (response engine for single/multi-prompt agents)

```jsonc
{
  "llm_id": "llm_234sdertfsdsfsdf",
  "model": "gpt-4.1",
  "s2s_model": "gpt-realtime-1.5",
  "model_temperature": 0,
  "model_high_priority": true,
  "tool_call_strict_mode": true,
  "general_prompt": "<system prompt>",
  "tools": [ /* Retell tool defs — call transfer, calendar booking, etc. */ ],
  "knowledge_base_ids": ["<kb_id>"],
  "kb_config": { "top_k": 3, "filter_score": 0.6 },
  "begin_after_user_silence_ms": 2000,
  "begin_message": "Hey I am a virtual assistant calling from Retell Hospital."
}
```

### 9.3 Conversation Flow (response engine for flow-based agents)

```jsonc
{
  "conversation_flow_id": "cf_xxx",
  "model_temperature": 0.7,
  "tool_call_strict_mode": true,
  "knowledge_base_ids": ["kb_001","kb_002"],
  "kb_config": { "top_k": 3, "filter_score": 0.6 },
  "start_speaker": "agent" | "user",
  "begin_after_user_silence_ms": 2000,
  "nodes": [ /* node-graph definition — see conversation flow docs */ ]
}
```

### 9.4 Phone Number

```jsonc
{
  "phone_number": "+14157774444",
  "nickname": "Main support line",
  "country": "US",
  "is_toll_free": false,
  "outbound_call_capability": true,
  "inbound_agents": [
    { "agent_id": "oBeDLoLOeuAbiuaMFXRtDOLriTJ5tSxD", "agent_version": "latest_published", "weight": 1 }
  ],
  "status": "active"
}
```

### 9.5 Call

See [7.5](#75-retell_get_call).

### 9.6 Chat Agent

Same shape as voice agent, but:
- `response_engine.type` is always `retell-llm`.
- No `voice_id`, `voicemail_option`, `ivr_option`, `call_screening_option`, etc.
- Adds `greeting_message`, `system_message`, etc.

### 9.7 Knowledge Base Source

```jsonc
{
  "source_id": "src_xxx",
  "type": "url" | "text" | "file" | "qa" | "sitemap",
  "content": "<url-or-text-or-base64>",
  "metadata": { /* arbitrary */ }
}
```

### 9.8 Voice

```jsonc
{
  "voice_id": "elevenlabs-Adam",
  "provider": "elevenlabs" | "openai" | "deepgram" | "cartesia" | "minimax" | "retell",
  "name": "Adam",
  "language": "en",
  "gender": "male",
  "metadata": { /* provider-specific */ }
}
```

---

## 10. End-to-End Worked Examples

### 10.1 "Create and publish a voice agent, then make a test call" — pure MCP

```text
User:  "Set up an inbound sales agent for my clinic and dial me at +12137774445 to test it."

Agent:
  1. retell_list_voices({ provider: "retell" })
     → picks "retell-Cimo"

  2. retell_create_llm({
       model: "gpt-4.1",
       general_prompt: "You are a friendly intake assistant for Acme Health...",
       begin_message: "Hi, thanks for calling Acme Health. How can I help?",
       knowledge_base_ids: []
     })
     → llm_id: "llm_xxx"

  3. retell_create_agent({
       agent_name: "Acme Health Intake",
       response_engine: { type: "retell-llm", llm_id: "llm_xxx", version: 0 },
       voice_id: "retell-Cimo",
       language: "en-US",
       timezone: "America/Mexico_City",
       end_call_after_silence_ms: 600000,
       max_call_duration_ms: 1800000
     })
     → agent_id: "agent_yyy", status: "draft"

  4. retell_publish_agent({ agent_id: "agent_yyy" })
     → status: "published"

  5. retell_list_phone_numbers({})
     → finds "+14157774444" already in account

  6. retell_update_phone_number({
       phone_number: "+14157774444",
       inbound_agents: [{ agent_id: "agent_yyy", weight: 1 }]
     })

  7. retell_create_phone_call({
       from_number: "+14157774444",
       to_number: "+12137774445",
       override_agent_id: "agent_yyy"
     })
     → call_id: "call_zzz", call_status: "ongoing"

  8. (After ~30 seconds) retell_get_call({ call_id: "call_zzz" })
     → returns transcript, summary, sentiment, cost
```

### 10.2 "Rerun QA on the last 20 calls with low call_successful scores"

```text
Agent:
  1. retell_list_calls({
       filter_criteria: {
         before_start_time: <now>,
         after_start_time: <now - 7 days>
       },
       limit: 1000
     })

  2. (Filter client-side for call_analysis.call_successful === false)
     → 12 calls

  3. For each: retell_rerun_call_analysis({ call_id })
     → re-runs LLM analysis

  4. retell_list_alert_rules({})
     → check if any rule fires on the new scores

  5. Return a summary table to the user.
```

### 10.3 "Add this pricing doc to my agent's knowledge base"

```text
Agent:
  1. retell_list_knowledge_bases({})
     → finds "Sales KB" → knowledge_base_id: "kb_xxx"

  2. retell_add_knowledge_base_sources({
       knowledge_base_id: "kb_xxx",
       sources: [
         { type: "url", content: "https://acme.com/pricing.pdf" },
         { type: "text", content: "Consultation fee: $120. Insurance accepted: Aetna, Cigna, UnitedHealth." }
       ]
     })

  3. retell_list_agents({ filter_criteria: { channel: "voice" } })
     → finds "Acme Health Intake"

  4. retell_update_agent({
       agent_id: "agent_yyy",
       retell_llm: { knowledge_base_ids: ["kb_xxx"] }
     })

  5. retell_publish_agent({ agent_id: "agent_yyy" })
```

---

## 11. Concurrency, Limits & Rate Limits

### 11.1 Concurrency

Use `retell_get_concurrency` (or `GET /get-concurrency`) to inspect:

```json
{
  "current_concurrency": 10,        // active calls right now
  "concurrency_limit": 100,         // soft cap = base + purchased
  "base_concurrency": 20,           // free tier
  "purchased_concurrency": 80,      // paid add-on
  "concurrency_purchase_limit": 100,// max you can buy
  "remaining_purchase_limit": 20,
  "reserved_inbound_concurrency": 10,
  "concurrency_burst_enabled": true,
  "concurrency_burst_limit": 60     // max simultaneous burst above cap
}
```

Exceeding `concurrency_limit` (with no burst capacity remaining) returns `HTTP 429` on `create_phone_call` / `create_web_call` / `register_phone_call`. Inbound calls are accepted as long as `reserved_inbound_concurrency` allows.

### 11.2 REST API rate limits

Retell enforces account-level rate limits on REST endpoints. The exact thresholds scale with plan tier. Symptoms:

- `HTTP 429` with body `{"status":"error","message":"Account rate limited, please throttle your requests."}`.
- MCP tool calls that wrap a rate-limited REST endpoint return the same error in their `isError` payload.

Mitigations:
- Exponential backoff with jitter.
- Batch where possible (`create_batch_call`, `create_batch_test`).
- Cache `list_*` results for 60+ seconds.

### 11.3 Audio WebSocket limits

- 24kHz mono PCM, 16-bit, 50ms frames (1200 samples/frame).
- Inbound audio must arrive within ~500ms of capture for sub-2s end-to-end latency.
- Idle WebSocket disconnect after 60s of inactivity.

### 11.4 Data retention

Controlled per-agent via `data_storage_setting` and `data_storage_retention_days`:
- `everything`: transcripts, recordings, AI analysis, metrics — retained `N` days.
- `essential`: transcripts + metrics only.
- `none`: no PII stored (real-time only).

---

## 12. Webhooks, Alerts & Incidents

### 12.1 Webhook events

Configure per-agent via `webhook_url` + `webhook_events`. Common events:

| Event | When |
|---|---|
| `call_started` | Call leg connected |
| `call_ended` | Call terminated, before post-call analysis is ready |
| `call_analyzed` | Post-call analysis complete (transcript, summary, sentiment, custom fields) |
| `call_error` | Error during the call |
| `chat_started` / `chat_ended` / `chat_analyzed` | Chat equivalents |
| `batch_call_started` / `batch_call_ended` | Batch progress |
| `agent_published` | Agent version went live |
| `incident_detected` | Alert rule fired |

Webhook payloads are JSON, signed (verify with your Retell webhook secret). Use `retell_test_webhook` to send a synthetic event.

### 12.2 Alert rules

Create via `retell_create_alert_rule` (`POST /create-alert-rule`). Typical rules:

- p95 end-to-end latency > 2500ms sustained over 5 minutes.
- Call success rate < 90% over 1 hour.
- Concurrency utilization > 85% for 10 minutes.

Triggered rules create **incidents** — list via `retell_list_incidents`.

### 12.3 QA

QA (Quality Assurance) runs are post-call scoring runs:

- `retell_list_qa_runs` — list recent QA runs.
- `retell_rerun_qa` — re-score a past call against new criteria.
- Submit human scores back via the QA scoring API (when available).

These are exposed via MCP when the agent workflow needs them, e.g. "Rerun QA on this call and summarize the failure reasons."

---

## 13. Error Handling

### 13.1 Standard error envelope

REST errors return:

```json
{
  "status": "error",
  "message": "<human-readable>"
}
```

MCP wraps these into tool-call results with `isError: true`:

```json
{
  "content": [{ "type": "text", "text": "{\"status\":\"error\",\"message\":\"...\"}" }],
  "isError": true
}
```

### 13.2 Error catalogue

| HTTP | Message | Cause | Fix |
|---|---|---|---|
| 400 | `Invalid request format, please check API reference.` | Bad JSON / missing required field | Validate against the tool's input schema (returned by `tools/list`) |
| 401 | `API key is missing or invalid.` | Missing/expired `Authorization` header | Re-issue key in dashboard, restart MCP session |
| 402 | `Trial has ended, please add payment method.` | Billing issue | Add card in dashboard |
| 404 | `Cannot find requested asset under given api key.` | Wrong ID, or asset in different workspace | Verify ID; check workspace scoping |
| 429 | `Account rate limited, please throttle your requests.` | Too many requests | Backoff, batch, or upgrade plan |
| 500 | `An unexpected server error occurred.` | Retell-side fault | Retry with backoff; check `status.retellai.com` |

### 13.3 Agent error-handling pattern

When a tool returns `isError: true`:

1. Parse the `message` from the JSON `text` content block.
2. For `429` / `500` / `503` — exponential backoff (1s, 2s, 4s) up to 3 retries.
3. For `400` / `404` — do not retry; surface the message to the user and ask for clarification.
4. For `401` / `402` — halt and instruct the user to fix billing/keys.

---

## 14. Security Best Practices

> Connecting an LLM to operational tools is **giving a programmatic operator access to your voice platform**. Treat it accordingly.

### 14.1 Prompt injection risk

Untrusted content — call transcripts, user chat messages, KB documents — may contain instructions designed to make the model run destructive tools (delete agents, publish broken drafts, etc.).

**Mitigations:**
- Enable **confirm-before-running-tools** in your MCP client. Keep it on.
- Especially gate: `retell_delete_*`, `retell_publish_*`, `retell_create_phone_call`, `retell_create_batch_call`.
- Strip transcripts from model context before issuing tool calls when possible.

### 14.2 Least-privilege keys

- Use **scoped API keys** when the dashboard supports them.
- Different keys for dev / staging / prod.
- Rotate keys on a schedule (every 90 days).

### 14.3 Key handling

- **Never** paste API keys into chat prompts.
- Store in client secrets / env vars / OS keychain.
- For server-side agents, load from a secrets manager (Vault, AWS Secrets Manager, etc.).
- The MCP `Authorization` header is set once in client config — the LLM never sees the raw key.

### 14.4 PII handling

- Call transcripts and recordings may contain PHI / PII / PCI.
- Set `data_storage_setting` appropriately per agent.
- Avoid sending full transcripts back to the model; prefer the `call_analysis` summary.
- Honor data subject deletion requests by calling `retell_delete_call` / `retell_delete_chat`.

### 14.5 Read-first workflows

Before any update or delete, call the corresponding `get_*` or `list_*` first. This:
- Prevents destructive operations on the wrong resource.
- Surfaces the current state to the user for confirmation.
- Catches ID typos before damage is done.

### 14.6 Non-production first

Always test agent workflows in a sandbox workspace / dev account before connecting production keys. Use test phone numbers (`+1` test ranges) where possible.

---

## 15. Migration Path: REST ↔ MCP ↔ SDK

All three surfaces are equivalent. Choose based on the *context* of the call:

| If the call originates from… | Use |
|---|---|
| An LLM agent (Cursor, Claude, custom LLM loop) | **MCP** — the LLM natively speaks tool-calling |
| A backend service / cron / webhook handler | **REST** or **SDK** — synchronous, typed, no JSON-RPC overhead |
| A new codebase that wants typed ergonomics | **SDK** — `retell-sdk` (Node) or `retell` (Python) |
| A non-Node/Python codebase (Go, Ruby, Rust, PHP) | **REST** — straightforward HTTP |

### 15.1 Translating between surfaces

| MCP tool | REST endpoint | SDK method |
|---|---|---|
| `retell_create_phone_call` | `POST /v2/create-phone-call` | `client.call.createPhoneCall()` |
| `retell_create_web_call` | `POST /v2/create-web-call` | `client.call.createWebCall()` |
| `retell_list_calls` | `POST /v3/list-calls` | `client.call.list()` |
| `retell_get_call` | `GET /v2/get-call?call_id=...` | `client.call.get()` |
| `retell_create_agent` | `POST /create-agent` | `client.agent.create()` |
| `retell_publish_agent` | `POST /publish-agent-version` | `client.agent.publish()` |
| `retell_create_phone_number` | `POST /create-phone-number` | `client.phoneNumber.create()` |
| `retell_get_concurrency` | `GET /get-concurrency` | `client.concurrency.retrieve()` |
| ... | ... | ... |

The pattern is uniform: every `retell_<verb>_<resource>` maps to one REST endpoint and one SDK method, with identical argument names (camelCase in REST/SDK, snake_case in MCP arguments — both supported by the server).

### 15.2 Mixing surfaces

You can freely mix MCP, REST, and SDK against the same Retell account. Common pattern:

- **Agent (MCP)** — to build and iterate on agents during development.
- **Backend (SDK)** — to programmatically trigger calls in production.
- **Webhooks (REST)** — to receive call events into your backend.

All three share the same data model and IDs.

---

## Appendix A: Full Endpoint Reference

| Domain | Method | Endpoint |
|---|---|---|
| **Call** | POST | `/v2/create-phone-call` |
|  | POST | `/v2/create-web-call` |
|  | GET | `/v2/get-call` |
|  | POST | `/v3/list-calls` |
|  | PATCH | `/v2/update-call` |
|  | PATCH | `/v2/update-live-call` |
|  | PUT | `/v2/rerun-call-analysis` |
|  | POST | `/v2/stop-call` |
|  | DELETE | `/v2/delete-call` |
| **Chat** | POST | `/v2/create-chat` |
|  | POST | `/v2/create-outbound-sms` |
|  | GET | `/v2/get-chat` |
|  | POST | `/v2/create-chat-completion` |
|  | POST | `/v3/list-chats` |
|  | PATCH | `/v2/update-chat` |
|  | PUT | `/v2/rerun-chat-analysis` |
|  | PATCH | `/v2/end-chat` |
|  | DELETE | `/v2/delete-chat` |
| **Playground** | POST | `/v2/agent-playground-completion` |
| **Phone Number** | POST | `/create-phone-number` |
|  | GET | `/get-phone-number` |
|  | GET | `/v2/list-phone-numbers` |
|  | PATCH | `/update-phone-number` |
|  | DELETE | `/delete-phone-number` |
| **Voice Agent** | POST | `/create-agent` |
|  | GET | `/get-agent` |
|  | POST | `/v2/list-agents` |
|  | PATCH | `/update-agent` |
|  | DELETE | `/delete-agent` |
|  | POST | `/publish-agent-version` |
|  | POST | `/create-draft-agent-version` |
|  | DELETE | `/delete-agent-version` |
|  | GET | `/get-agent-versions` |
| **Chat Agent** | POST | `/create-chat-agent` |
|  | GET | `/get-chat-agent` |
|  | POST | `/v2/list-agents` (filter `channel: "chat"`) |
|  | PATCH | `/update-chat-agent` |
|  | DELETE | `/delete-chat-agent` |
|  | POST | `/publish-agent-version` |
|  | POST | `/create-draft-chat-agent-version` |
|  | DELETE | `/delete-chat-agent-version` |
|  | GET | `/get-chat-agent-versions` |
| **Retell LLM** | POST | `/create-retell-llm` |
|  | GET | `/get-retell-llm` |
|  | GET | `/v2/list-retell-llms` |
|  | PATCH | `/update-retell-llm` |
|  | DELETE | `/delete-retell-llm` |
| **Conversation Flow** | POST | `/create-conversation-flow` |
|  | GET | `/get-conversation-flow` |
|  | GET | `/v2/list-conversation-flows` |
|  | PATCH | `/update-conversation-flow` |
|  | DELETE | `/delete-conversation-flow` |
| **Conversation Flow Subflow** | POST | `/create-conversation-flow-subflow` |
|  | GET | `/get-conversation-flow-subflow` |
|  | GET | `/v2/list-conversation-flow-subflows` |
|  | PATCH | `/update-conversation-flow-subflow` |
|  | DELETE | `/delete-conversation-flow-subflow` |
| **MCP Tool** | GET | `/get-mcp-tools` |
| **Knowledge Base** | POST | `/create-knowledge-base` |
|  | GET | `/get-knowledge-base` |
|  | GET | `/list-knowledge-bases` |
|  | DELETE | `/delete-knowledge-base` |
|  | POST | `/add-knowledge-base-sources` |
|  | DELETE | `/delete-knowledge-base-source` |
| **Voice** | POST | `/add-voice` |
|  | POST | `/clone-voice` |
|  | POST | `/search-voice` |
|  | GET | `/get-voice` |
|  | GET | `/list-voices` |
| **Batch Call** | POST | `/create-batch-call` |
| **Test Case Definition** | POST | `/create-test-case-definition` |
|  | GET | `/get-test-case-definition` |
|  | GET | `/v2/list-test-case-definitions` |
|  | PUT | `/update-test-case-definition` |
|  | DELETE | `/delete-test-case-definition` |
| **Batch Test** | POST | `/create-batch-test` |
|  | GET | `/get-batch-test` |
|  | GET | `/v2/list-batch-tests` |
| **Test Run** | GET | `/get-test-run` |
|  | GET | `/v2/list-test-runs` |
| **Account** | GET | `/get-concurrency` |
|  | GET | `/list-export-requests` |
| **Custom Telephony** | POST | `/import-phone-number` |
|  | POST | `/v2/register-phone-call` |
| **Custom LLM** | WS | `/llm-websocket` (Retell → your LLM server, OpenAI-compatible) |

All endpoints require `Authorization: Bearer <RETELL_API_KEY>` and return JSON. Most list/search endpoints are `POST` to accept a filter body.

---

## Appendix B: Troubleshooting Matrix

| Symptom | Likely cause | Fix |
|---|---|---|
| Tools not showing up in client | Wrong URL, wrong header, or client can't reach endpoint | Verify URL is `https://mcp.retellai.com`; verify `Authorization: Bearer <KEY>` header; check corporate proxy / firewall |
| `401 Unauthorized` on every call | Missing/invalid API key | Re-issue key in dashboard; ensure no leading/trailing whitespace |
| `404 Cannot find requested asset` | Wrong ID, or asset in a different workspace | Re-list resources; check workspace selector in dashboard |
| `429 Account rate limited` | Too many requests | Backoff, batch, or upgrade plan |
| Tool call returns `isError: true` with `400 Invalid request format` | Argument schema mismatch | Re-fetch `tools/list`; the input schema is authoritative |
| `create_phone_call` returns 429 even though concurrency is fine | Per-second rate limit hit | Add 100–500ms jitter between sequential calls |
| Web call audio is one-way | Browser mic permission or firewall | Use the Retell web SDK; allow `wss://api.retellai.com` |
| Webhook not firing | Wrong URL, TLS issue, or event filter | Use `retell_test_webhook` to verify; check Retell dashboard → Webhooks log |
| Agent "stuck" in draft | Forgot to call `retell_publish_agent` | Publish — production calls only use published versions |
| Post-call analysis missing | `post_call_analysis_data` not set, or call too short | Configure analysis fields on the agent; ensure call > 5 seconds |
| Latency p95 > 3000ms | Network / model / TTS combination | Switch to `stt_mode: "fast"`, lower-priority model, or closer TTS region |

---

## Document Provenance

- **Compiled:** 2026-07-21
- **Sources:**
  - Official Retell MCP server documentation: `https://docs.retellai.com/get-started/mcp-server`
  - Retell blog — "Meet Retell MCP Server": `https://www.retellai.com/blog/retell-mcp-server`
  - Retell API reference: `https://docs.retellai.com/api-references/overview`
  - Retell API — Create Phone Call: `https://docs.retellai.com/api-references/create-phone-call`
  - Retell API — Create Web Call: `https://docs.retellai.com/api-references/create-web-call`
  - Retell API — Create Voice Agent: `https://docs.retellai.com/api-references/create-agent`
  - Retell API — Get Concurrency: `https://docs.retellai.com/api-references/get-concurrency`
  - Community MCP server (100% SDK parity, 60 tools): `https://github.com/sunnysingh100/retell-mcp-server`
- **Support:** support@retellai.com
- **Status page:** status.retellai.com

For the brief capability summary (what can / cannot be done via MCP), see the companion `README.md`.
