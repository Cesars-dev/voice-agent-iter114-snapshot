"""TTS provider factory — Cartesia (default) or ElevenLabs, env-switched.

Both adapters expose the same four-method contract:
    connect() / speak(ctx, text, continue_) / cancel(ctx) / close()
plus `new_context_id()` and `first_byte_ts`. CallSession, the sentence gate
and barge-in logic never know which engine is live.

  TTS_PROVIDER=cartesia   (default — you have credits; sonic-3.6; ~1 credit/char;
                            contexts keep prosody; native mulaw@8000; lowest TTFB)
  TTS_PROVIDER=elevenlabs (your Chloe settings; ulaw_8000 passthrough; PAYG
                            $0.05/1k chars Flash, $0.10/1k v2/v3)
"""
from __future__ import annotations


def create_tts(settings, on_audio, transport: str | None = None):
    if settings.tts_provider == "elevenlabs":
        from .elevenlabs_tts import ElevenLabsTTS
        return ElevenLabsTTS(settings, on_audio, transport=transport)
    from .cartesia_tts import CartesiaTTS
    return CartesiaTTS(settings, on_audio, transport=transport)


def tts_label(settings) -> str:
    if settings.tts_provider == "elevenlabs":
        return f"elevenlabs/{settings.elevenlabs_model_id}"
    return f"cartesia/{settings.cartesia_model_id}"
