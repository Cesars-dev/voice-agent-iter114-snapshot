# Legacy MCP server (retell.stlmcp.com) removed (07/20/2026)

**Source:** https://docs.retellai.com/deprecation-notice/2026/07-20_legacy_mcp_server

## What's changing

The hosted MCP server at `retell.stlmcp.com` was shut down on 07/20/2026.

## Migration

1. Change the MCP server URL from `https://retell.stlmcp.com` to `https://mcp.retellai.com`
2. Auth is unchanged — send your Retell API key as a `Bearer` token
3. LLM clients (Claude, Cursor, etc.) discover available tools automatically — no code changes needed

### .mcp.json

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

The local `npx @retell-ai/mcp-server` package is not affected.

## Removal date

**07/20/2026**
