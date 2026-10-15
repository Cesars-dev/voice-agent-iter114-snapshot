# Unified publish-agent-version endpoint (07/20/2026)

**Source:** https://docs.retellai.com/deprecation-notice/2026/07-20_agent_version_endpoints

## What's changing

The legacy publish endpoints are deprecated in favor of a unified `publish-agent-version` endpoint that works for both voice and chat agents.

## Endpoint migrations

| Legacy | Replacement |
|--------|-------------|
| `POST /publish-agent/{agent_id}` | `POST /publish-agent-version/{agent_id}` |
| `POST /publish-chat-agent/{agent_id}` | `POST /publish-agent-version/{agent_id}` |

## Migration

Simply update client calls to use the new path. The request body and response format are the same.

## Deprecation date

**07/20/2026**
