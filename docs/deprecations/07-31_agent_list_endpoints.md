# Legacy agent list endpoints removed (07/31/2026)

**Source:** https://docs.retellai.com/deprecation-notice/2026/07-31_agent_list_endpoints

## What's changing

The legacy list endpoints for agents are removed. Both voice and chat agents are now listed through a single unified endpoint.

## Endpoint migrations

| Legacy | Replacement |
|--------|-------------|
| `GET /list-agents` | `POST /v2/list-agents` |
| `GET /list-chat-agents` | `POST /v2/list-agents` |

## Migration details

1. Change method from GET to POST
2. To list only voice agents, set `filter_criteria.channel` to `{ "type": "string", "op": "eq", "value": "voice" }`
3. To list only chat agents, set `filter_criteria.channel` to `{ "type": "string", "op": "eq", "value": "chat" }`
4. Read results from `items` instead of expecting a top-level array
5. Continue pagination with the returned `pagination_key` while `has_more` is `true`
6. Stop sending `pagination_key_version` — the new endpoint does not use it

### Example

```bash
curl -X POST https://api.retellai.com/v2/list-agents \
  -H "Authorization: Bearer $RETELL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "filter_criteria": {
      "channel": { "type": "string", "op": "eq", "value": "voice" }
    }
  }'
```

### Response format

```json
{
  "items": [ /* agent objects */ ],
  "pagination_key": "next-cursor",
  "has_more": true
}
```

## Deprecation date

**07/31/2026**
