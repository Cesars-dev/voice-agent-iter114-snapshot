# TELEPHONY.md — connecting a real number (research answers)

Your questions, answered directly.

## Q1: "How do we connect a number? Can we receive calls / purchase numbers?"

**Yes — buy a Twilio number and receive calls on it.** Two steps, both automated:

1. **Buy**: `python scripts/provision_twilio_number.py --area-or-code 312 --host calls.your-domain.com`
   - searches available US local numbers, buys one (**$1.15/month**), sets the
     inbound voice webhook to `https://your-host/twiml`.
2. **Receive**: someone dials it → Twilio POSTs `/twiml` → we return:

```xml
<Response>
  <Connect>
    <Stream url="wss://calls.your-domain.com/media" track="inbound_track">
      <Parameter name="callback_number" value="+1312..."/>   <!-- seeds the dv, Retell-style -->
    </Stream>
  </Connect>
  <Hangup/>   <!-- fires when WE close the stream (end_call) -->
</Response>
```

From that point audio is a plain WebSocket between Twilio and your server:
inbound mulaw/8000 frames in, outbound mulaw/8000 frames back, `clear` for
barge-in, `mark` for playback tracking. Outbound calls are possible the same
way (REST `Calls.create` with the same Twiml URL) if you later want Linda to
dial out.

## Q2: "Do Deepgram / Cartesia offer native number integrations?"

| Vendor | Numbers? | What they actually offer |
|---|---|---|
| **Deepgram** | **No numbers.** | Voice Agent API with **Twilio / Telnyx(SIP) / Genesys / Amazon Connect / AudioCodes** integrations — you still buy the number from Twilio/Telnyx and bridge audio to Deepgram. Also **self-hostable** (their STT container) if you ever want to drop the API. |
| **Cartesia** | **Yes, for their hosted "Line" agents only.** | Agent calling **$0.06/min** + **+$0.014/min** when using a Cartesia-provided number. But "Line" = their hosted agent runtime — using it means handing back the orchestration (the Retell pattern). Not used here. |
| **Twilio** | Yes — this build's carrier. | Numbers from $1.15/mo (US local), Media Streams included in voice minutes. |
| **Telnyx** | Yes — the cheaper alternative. | Numbers from ~$1/mo, voice platform fees ~$0.002–0.003/min, **Media Streaming over WebSockets $0.0035/min**, decrypted forking $0.0025/min. |

## Q3: "How much do they charge per minute / per call?"

| Item | Price |
|---|---|
| Twilio US local number | **$1.15 / month** |
| Twilio inbound call (US local) | **$0.0085 / min** (toll-free ≈ $0.022/min — avoid) |
| Twilio outbound (US local) | $0.014 / min |
| Twilio Media Streams | $0 extra (rides the voice minutes) |
| Deepgram Flux STT (streaming) | **$0.0065 / min** (PAYG promo; $200 signup credit ≈ 30h) |
| Deepgram Nova-3 STT | $0.0043–0.0048 / min |
| Cartesia TTS | **~1 credit per character** (Sonic; Free 20K / Pro ~100K / Growth ~1.25M / Scale 8M credits per month; overage optional) |
| Deepgram TTS (Aura-2) — fallback | $0.030 / 1k chars |
| Deepgram Voice Agent API (all-in, Option B) | $0.056/min now → $0.075/min (BYO-TTS $0.065) per websocket-minute |
| Langfuse OSS | $0 (self-hosted) |

**Marginal cost of this stack ≈ $0.015–0.02/min** (Twilio+Deepgram+Cartesia at
plan rates) + your LLM tokens — roughly **$0.20–0.60 per 10-minute call**.

## Option B — the managed-media alternative (documented, not wired)

If you ever stop wanting to own the media loop: **Deepgram Voice Agent API**
supports **Cartesia as a native TTS provider**:

```jsonc
// POST /v1/agents — agent configuration
{
  "listen": {"provider": {"type": "deepgram", "model": "flux-general-en"}},
  "think":  {"provider": {"type": "custom_messaging", "url": "wss://your-server/agent"}},  // <- your LangGraph
  "speak":  {"provider": {"type": "cartesia", "model": "sonic-2", "voice": {"mode": "id", "id": "..."}}}
}
```

Deepgram then owns STT + turn-taking + barge-in + TTS + Twilio bridging and
calls your server with transcripts (your LangGraph brain stays the same — it
just answers `UserMessage` frames with text). Costs $0.056–0.075/min on top of
the BYO pieces and moves the latency-critical loop off your box. We default to
the self-hosted loop (your requirement); this is the escape hatch.

## Why Twilio first (not Telnyx)

Same decision tree you'd run: Twilio's Media Streams is the most documented,
most replicated bidirectional-audio interface in the voice-agent world (it's
what the 800ms blog posts are built on); Telnyx saves ~$0.005/min at the cost
of rougher edges. The telephony surface in this codebase is deliberately ONE
module (`media/session.py` + the `/twiml` endpoint), so a Telnyx port is a
contained change when call volume justifies it.

## WSS/TLS requirement (important)

Twilio Media Streams only connects to **wss:// with a valid public cert**.
That's why the deploy ships Caddy (automatic Let's Encrypt + renewal) —
`deploy/Caddyfile`. Never expose uvicorn directly on the public internet.
