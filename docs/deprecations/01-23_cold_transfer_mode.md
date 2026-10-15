# Explicit cold transfer mode selection (01/23/2026)

**Source:** https://docs.retellai.com/deprecation-notice/2026/01-23_cold_transfer_mode_selection

## What's changing

`show_transferee_as_caller` no longer toggles SIP REFER vs SIP INVITE in Cold Transfer options. Use the new `cold_transfer_mode` parameter instead; `show_transferee_as_caller` now only controls caller ID display under `sip_invite`.

## Deprecated field

- `show_transferee_as_caller` (on transfer tool definitions)

## Replacement

- `cold_transfer_mode` parameter

## Affected endpoints

- Create/Update Retell LLM
- Create/Update Conversation Flow
- Create/Update Conversation Flow Component

## Effective date

**01/23/2026**
