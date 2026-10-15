# Webhooks & Post-Call Analysis Reference

## Webhook Events

Set `webhook_url` on the agent. All events POST to that URL.

| Event | When | Key Fields |
|---|---|---|
| `call_started` | Call connects | `call_id`, `agent_id`, `from_number`, `to_number` |
| `call_ended` | Call disconnects | Above + `transcript`, `duration_ms`, `end_reason` |
| `call_analyzed` | ~30s after end | Above + full `call_analysis` object |

## Inbound Dynamic Variables

Retell POSTs to your webhook before the call connects. Return variables to inject into the prompt:

```python
# Retell sends:
{ "call": { "agent_id": "agent_xxx", "from_number": "+1..." } }

# You return:
{
  "call_inbound": {
    "dynamic_variables": {
      "customer_name": "John",
      "account_balance": "2400"
    },
    "override_agent_id": None,
    "begin_message": "Hey John, thanks for calling!"
  }
}
```

## Post-Call Extraction Schema

Define on the agent under `post_call_analysis_data`:

```python
[
  { "type": "boolean", "name": "user_reached",
    "description": "Was a live human reached? False if voicemail." },

  { "type": "string", "name": "call_summary",
    "description": "Summary of the call for human handoff." },

  { "type": "number", "name": "purchase_intent_amount",
    "description": "Dollar amount customer mentioned, 0 if none." },

  { "type": "enum", "name": "call_outcome",
    "description": "Final outcome of the call.",
    "choices": ["Appointment Booked", "Not Interested", "Callback Requested", "Voicemail", "Wrong Number"] }
]
```

## Retrieve Analysis After the Fact

```python
call = client.call.get("call_id")
print(call.call_analysis.custom_analysis_data)
print(call.transcript)
```
