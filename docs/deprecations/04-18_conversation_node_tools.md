# Conversation node tools → subagent node type (04/18/2026)

**Source:** https://docs.retellai.com/deprecation-notice/2026/04-18_conversation_node_tools

## What's changing

The `tools` and `tool_ids` fields on `type: "conversation"` nodes in Conversation Flows are deprecated in favor of the new `subagent` node type.

## Deprecated fields

- `tools` (on `type: "conversation"` nodes)
- `tool_ids` (on `type: "conversation"` nodes)

## Replacement

Use the new `subagent` node type instead of `type: "conversation"` with `tools`/`tool_ids`.

## Affected endpoints

- Create/Update/Get/List Conversation Flow
- Create/Update/Get/List Conversation Flow Component

## Effective date

**04/18/2026**
