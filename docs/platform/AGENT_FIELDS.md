# Agent Fields Reference

The Agent controls voice and telephony only. Prompts live in the LLM object.

## Retrieve Agent

```python
agent = client.agent.retrieve("agent_id")
llm_id = agent.response_engine.llm_id  # always grab this before editing prompt
```

## Update Agent

```python
client.agent.update("agent_id", {
  "agent_name": "My Agent",
  "voice_id": "retell-Cimo",              # validate with client.voice.list()
  "voice_model": "eleven_turbo_v2",
  "voice_temperature": 1.0,               # 0-2, expressiveness
  "voice_speed": 1.0,                     # 0.5-2.0
  "volume": 1.0,
  "language": "en-US",
  "responsiveness": 1.0,                  # 0-1
  "interruption_sensitivity": 1.0,        # 0-1
  "normalize_for_speech": True,           # converts $1,000 → "one thousand dollars"
  "enable_backchannel": True,
  "backchannel_frequency": 0.8,
  "backchannel_words": ["yeah", "got it", "I see"],
  "begin_message": "Hey, thanks for calling!",  # None = LLM decides
  "end_call_after_silence_ms": 30000,
  "max_call_duration_ms": 3600000,
  "reminder_trigger_ms": 10000,
  "reminder_max_count": 1,
  "enable_voicemail_detection": False,
  "voicemail_message": "Sorry I missed you...",
  "ambient_sound": None,                  # None | "coffee-shop" | "convention-hall"
  "ambient_sound_volume": 1.0,
  "webhook_url": "https://yoursite.com/webhook",
  "response_engine": {
    "type": "retell-llm",
    "llm_id": "llm_xxxx"
  },
  "post_call_analysis_data": []           # see WEBHOOKS_AND_ANALYSIS.md
})
```

## Publish & Test

```python
client.agent.publish("agent_id")                            # make changes live
web_call = client.call.create_web_call(agent_id="agent_id") # test without phone minutes
print(web_call.access_token)
```
