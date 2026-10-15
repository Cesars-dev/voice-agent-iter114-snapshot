# LLM, Tools & State Machine Reference

This is where the prompt, model, tools, and call flow logic live.

## Update LLM

```python
client.llm.update("llm_id", {
  "model": "gpt-4o",              # gpt-4o | gpt-4o-mini | claude-3.5-sonnet | gemini-pro
  "model_temperature": 0.7,       # 0-2
  "general_prompt": "You are...", # main system prompt, active in all states
  "general_tools": [],            # tools available in all states
  "states": [],                   # state machine — see below
  "starting_state": "greeting",   # must match a state name
  "knowledge_base_ids": ["kb_xxx"],
  "inbound_dynamic_variables_webhook_url": "https://..."
})
```

## Tool Types

### End Call

```python
{ "type": "end_call", "name": "end_call", "description": "End the call politely." }
```

### Transfer Call

```python
{ "type": "transfer_call", "name": "transfer_to_sales",
  "description": "Transfer to sales.", "number": "+12125551234" }
```

### Custom Webhook Tool

```python
{
  "type": "custom",
  "name": "check_order_status",
  "description": "Look up order by ID",
  "url": "https://yourapi.com/order",
  "speak_during_execution": True,
  "execution_message_description": "Let me check that for you...",
  "parameters": {
    "type": "object",
    "properties": {
      "order_id": { "type": "string", "description": "The order ID" }
    },
    "required": ["order_id"]
  }
}
```

### Other Built-ins

```python
{ "type": "check_availability_cal" }   # Cal.com availability check
{ "type": "book_appointment_cal" }     # Cal.com booking
{ "type": "press_digit" }              # DTMF tones
{ "type": "send_text" }                # SMS to caller
```

## State Machine

Use states to narrow prompt + tools per stage — reduces hallucination significantly.

```python
"states": [
  {
    "name": "greeting",
    "state_prompt": "Greet the user and ask how you can help.",
    "tools": [],
    "edges": [
      { "description": "User wants to book", "destination_state_name": "booking" }
    ]
  },
  {
    "name": "booking",
    "state_prompt": "Help the user book an appointment.",
    "tools": [{ "type": "book_appointment_cal" }],
    "edges": [
      { "description": "Done or wants to end", "destination_state_name": "closing" }
    ]
  },
  {
    "name": "closing",
    "state_prompt": "Thank the user and end the call.",
    "tools": [{ "type": "end_call", "name": "end_call", "description": "End the call." }],
    "edges": []
  }
]
```

## Dynamic Variables in Prompts

Reference injected variables anywhere in prompts using `{{variable_name}}`:

```
"You are speaking with {{customer_name}}. Their balance is {{account_balance}}."
```
