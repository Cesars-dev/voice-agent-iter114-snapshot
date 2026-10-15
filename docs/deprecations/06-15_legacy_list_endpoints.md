# Legacy list endpoints + Analysis prompt fields (06/15/2026)

**Source:** https://docs.retellai.com/deprecation-notice/2026/06-15_legacy_list_endpoints

## Part 1: Legacy List Endpoints

The following legacy GET list endpoints are replaced by versioned equivalents with unified pagination.

### Endpoint migrations

| Legacy | Replacement |
|--------|-------------|
| `GET /list-batch-tests` | `GET /v2/list-batch-tests` |
| `GET /list-conversation-flow-components` | `GET /v2/list-conversation-flow-components` |
| `GET /list-conversation-flows` | `GET /v2/list-conversation-flows` |
| `GET /list-phone-numbers` | `GET /v2/list-phone-numbers` |
| `GET /list-retell-llms` | `GET /v2/list-retell-llms` |
| `GET /list-test-case-definitions` | `GET /v2/list-test-case-definitions` |
| `GET /list-test-runs/{test_case_batch_job_id}` | `GET /v2/list-test-runs/{test_case_batch_job_id}` |
| `POST /v2/list-calls` | `POST /v3/list-calls` |
| `GET /list-chat` | `POST /v3/list-chats` |

### Migration details

1. Update each client call to the new method/path
2. `GET /list-chat` changes both method and path to `POST /v3/list-chats`
3. Versioned list endpoints return unified pagination fields: `items`, `pagination_key`, `has_more`
4. Read `items` from the paginated response object (instead of a top-level array)

---

## Part 2: Analysis Prompt Fields

The following top-level analysis prompt fields on voice and chat agent configurations are deprecated.

### Deprecated fields

- `analysis_summary_prompt`
- `analysis_successful_prompt`
- `analysis_user_sentiment_prompt`

These fields exist on: Create Agent, Update Agent, Create Chat Agent, Update Chat Agent.

### Replacement

Use system preset items inside `post_call_analysis_data` (voice) or `post_chat_analysis_data` (chat).

| Deprecated field | Preset `name` (voice) | Preset `name` (chat) |
|---|---|---|
| `analysis_summary_prompt` | `call_summary` | `chat_summary` |
| `analysis_successful_prompt` | `call_successful` | `chat_successful` |
| `analysis_user_sentiment_prompt` | `user_sentiment` | `user_sentiment` |

### Example migration

**Before:**
```json
{
  "analysis_summary_prompt": "Summarize the outcome of the conversation in two sentences."
}
```

**After:**
```json
{
  "post_call_analysis_data": [
    {
      "type": "system-presets",
      "name": "call_summary",
      "description": "Summarize the outcome of the conversation in two sentences."
    }
  ]
}
```

## Effective date

**06/15/2026**
