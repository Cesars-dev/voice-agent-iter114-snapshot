# Phone number single-agent fields removed (03/31/2026)

**Source:** https://docs.retellai.com/deprecation-notice/2026/03-31_phone_number_agent_fields

## What's changing

The single-agent fields on phone number configuration are deprecated in favor of weighted agent lists for inbound/outbound calls and SMS.

## Deprecated fields

- `inbound_agent_id`, `inbound_agent_version`
- `outbound_agent_id`, `outbound_agent_version`
- `inbound_sms_agent_id`, `inbound_sms_agent_version`
- `outbound_sms_agent_id`, `outbound_sms_agent_version`

## Replacement fields

- `inbound_agents`
- `outbound_agents`
- `inbound_sms_agents`
- `outbound_sms_agents`

## Migration

For a single agent, set the corresponding `*_agents` list to a single entry with `weight: 1`. For multiple agents, split weights so they sum to 1.

### Example

**Before:**
```json
{
  "inbound_agent_id": "oBeDLoLOeuAbiuaMFXRtDOLriTJ5tSxD",
  "inbound_agent_version": 3
}
```

**After:**
```json
{
  "inbound_agents": [
    { "agent_id": "oBeDLoLOeuAbiuaMFXRtDOLriTJ5tSxD", "agent_version": 3, "weight": 1 }
  ]
}
```

**Note:** Existing data is converted automatically. Until the deprecation date, the APIs remain backwards-compatible as long as only a single agent is used for each of the deprecated fields.

## Affected endpoints

- Create Phone Number, Import Phone Number, Update Phone Number, Get Phone Number, List Phone Numbers

## Effective date

**03/31/2026**
