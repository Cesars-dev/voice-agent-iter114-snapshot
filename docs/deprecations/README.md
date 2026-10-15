# Retell API Deprecation Notices

Source: https://docs.retellai.com/deprecation-notice/overview
Scraped: 2026-07-27

This folder documents all upcoming and past deprecations across the Retell API.
Each file covers one deprecation notice with the affected endpoints, migration paths, and examples.

---

## Deprecations by Date

| Date | Type | Summary | File |
|------|------|---------|------|
| **08/31/2026** | API | Update Call (`PATCH /v2/update-call/{id}`) becomes ended-calls-only. `override_dynamic_variables` moves to Update Live Call | [08-31_update_call.md](../Updated_endpoints/deprecation_notice/08-31_update_call.md) |
| **07/31/2026** | API | Legacy `GET /list-agents` and `GET /list-chat-agents` removed. Use `POST /v2/list-agents` with `channel` filter | [07-31_agent_list_endpoints.md](../Updated_endpoints/deprecation_notice/07-31_agent_list_endpoints.md) |
| **07/20/2026** | MCP | Legacy MCP server `retell.stlmcp.com` removed. Use `mcp.retellai.com` | [07-20_legacy_mcp_server.md](../Updated_endpoints/deprecation_notice/07-20_legacy_mcp_server.md) |
| **07/20/2026** | API | `POST /publish-agent/{id}` and `POST /publish-chat-agent/{id}` deprecated. Use `POST /publish-agent-version/{id}` | [07-20_agent_version_endpoints.md](../Updated_endpoints/deprecation_notice/07-20_agent_version_endpoints.md) |
| **07/12/2026** | Models | ElevenLabs Turbo voice models replaced with Flash (same quality, lower latency) | [07-12_elevenlabs_turbo.md](../Updated_endpoints/deprecation_notice/07-12_elevenlabs_turbo.md) |
| **06/15/2026** | API | Legacy list endpoints (GET) replaced by versioned v2/v3 equivalents. Analysis prompt fields moved to `post_call_analysis_data` system presets | [06-15_legacy_list_endpoints.md](../Updated_endpoints/deprecation_notice/06-15_legacy_list_endpoints.md) |
| **05/25/2026** | Models | Claude and Gemini model migrations (automatic) | [05-25_model_replacements.md](../Updated_endpoints/deprecation_notice/05-25_model_replacements.md) |
| **04/18/2026** | API | `tools`/`tool_ids` on `type: "conversation"` nodes deprecated in favor of `subagent` node type | [04-18_conversation_node_tools.md](../Updated_endpoints/deprecation_notice/04-18_conversation_node_tools.md) |
| **04/03/2026** | Models | OpenAI Realtime and Cartesia Sonic model replacements (automatic) | [04-03_model_replacements.md](../Updated_endpoints/deprecation_notice/04-03_model_replacements.md) |
| **03/31/2026** | API | Single-agent fields on phone numbers (`inbound_agent_id`, etc.) deprecated. Use weighted `*_agents` lists | [03-31_phone_number_agent_fields.md](../Updated_endpoints/deprecation_notice/03-31_phone_number_agent_fields.md) |
| **01/23/2026** | API | `show_transferee_as_caller` replaced by `cold_transfer_mode` parameter | [01-23_cold_transfer_mode.md](../Updated_endpoints/deprecation_notice/01-23_cold_transfer_mode.md) |

---

## Endpoint Migration Quick Reference

| Legacy Endpoint | Replacement | Date |
|---|---|---|
| `GET /list-agents` | `POST /v2/list-agents` (filter by `channel`) | 07/31 |
| `GET /list-chat-agents` | `POST /v2/list-agents` (filter `channel: "chat"`) | 07/31 |
| `POST /publish-agent/{id}` | `POST /publish-agent-version/{id}` | 07/20 |
| `POST /publish-chat-agent/{id}` | `POST /publish-agent-version/{id}` | 07/20 |
| `PATCH /v2/update-call/{id}` (live calls) | `PATCH /v2/update-live-call/{id}` | 08/31 |
| `GET /list-batch-tests` | `GET /v2/list-batch-tests` | 06/15 |
| `GET /list-conversation-flow-components` | `GET /v2/list-conversation-flow-components` | 06/15 |
| `GET /list-conversation-flows` | `GET /v2/list-conversation-flows` | 06/15 |
| `GET /list-phone-numbers` | `GET /v2/list-phone-numbers` | 06/15 |
| `GET /list-retell-llms` | `GET /v2/list-retell-llms` | 06/15 |
| `GET /list-test-case-definitions` | `GET /v2/list-test-case-definitions` | 06/15 |
| `GET /list-test-runs/{job_id}` | `GET /v2/list-test-runs/{job_id}` | 06/15 |
| `POST /v2/list-calls` | `POST /v3/list-calls` | 06/15 |
| `GET /list-chat` | `POST /v3/list-chats` | 06/15 |

## Deprecated Fields Migration

| Deprecated Field | Replacement | Affected Endpoints |
|---|---|---|
| `override_dynamic_variables` (on Update Call) | `fields_to_override.override_dynamic_variables` (on Update Live Call) | Update Call, Update Live Call |
| `analysis_summary_prompt` | `post_call_analysis_data` with `type: "system-presets"`, `name: "call_summary"` | Create/Update Agent, Create/Update Chat Agent |
| `analysis_successful_prompt` | `post_call_analysis_data` with `name: "call_successful"` | same |
| `analysis_user_sentiment_prompt` | `post_call_analysis_data` with `name: "user_sentiment"` | same |
| `inbound_agent_id` / `inbound_agent_version` | `inbound_agents: [{ agent_id, agent_version, weight }]` | Create/Update/Get/List Phone Number |
| `outbound_agent_id` / `outbound_agent_version` | `outbound_agents: [{ agent_id, agent_version, weight }]` | same |
| `inbound_sms_agent_id` / `inbound_sms_agent_version` | `inbound_sms_agents: [{ agent_id, agent_version, weight }]` | same |
| `outbound_sms_agent_id` / `outbound_sms_agent_version` | `outbound_sms_agents: [{ agent_id, agent_version, weight }]` | same |
| `tools` / `tool_ids` on `type: "conversation"` nodes | `subagent` node type | Conversation Flow CRUD |
| `show_transferee_as_caller` | `cold_transfer_mode` | Create/Update Retell LLM, Conversation Flow |
